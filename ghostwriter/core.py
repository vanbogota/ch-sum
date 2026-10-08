"""Orchestration: incoming message -> escalation check -> draft -> owner approval -> timed send."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .chats import ChatRef
from .config import Settings
from .llm import LLM, LLMError
from .llm.analyst import Analyst, Intent
from .llm.drafter import Drafter
from .llm.escalation import EscalationChecker, keyword_hits
from .persona import Persona
from .scheduling import ActiveHours, compute_send_time
from .storage import Channel, Direction, Draft, DraftStatus, Message, Scope, Store
from .storage.models import MessageStatus

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def draft_card(self, draft: Draft) -> None: ...
    async def refresh_card(self, draft: Draft) -> None: ...
    async def escalation(self, draft: Draft, messages: Sequence[Message]) -> None: ...
    async def info(self, text: str) -> None: ...


class Sender(Protocol):
    async def send(self, text: str, reply_to: Message | None) -> Message: ...


class ActionError(Exception):
    """A user-facing error for control-bot actions."""


class SendQueue:
    """Waits until each approved draft's scheduled time, then delivers it."""

    def __init__(self, deliver: Callable[[int], Awaitable[None]]) -> None:
        self._deliver = deliver
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def schedule(self, draft_id: int, at: datetime) -> None:
        self.cancel(draft_id)
        self._tasks[draft_id] = asyncio.create_task(self._run(draft_id, at), name=f"send-draft-{draft_id}")

    def cancel(self, draft_id: int) -> bool:
        task = self._tasks.pop(draft_id, None)
        if task and not task.done():
            task.cancel()
            return True
        return False

    async def _run(self, draft_id: int, at: datetime) -> None:
        try:
            while (remaining := (at - datetime.now(UTC)).total_seconds()) > 0:
                await asyncio.sleep(min(remaining, 60))
            await self._deliver(draft_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("send queue: delivering draft %s failed", draft_id)
        finally:
            if self._tasks.get(draft_id) is asyncio.current_task():
                self._tasks.pop(draft_id, None)

    def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()


@dataclass
class SummaryRequest:
    author: str | None = None
    limit: int | None = None
    since_hours: float | None = None
    focus: str | None = None


class ChatSource(Protocol):
    """The parts of the Telegram gateway the core needs to switch and sync chats."""

    watched: set[int]

    async def backfill(self, chat_id: int, limit: int, since: datetime | None = None) -> list[Message]: ...
    async def search_chats(self, query: str | None, kinds: set[str] | None = None, limit: int = 10) -> list[ChatRef]: ...
    async def chat_ref(self, chat: int | str) -> ChatRef: ...


KV_CONTACT = "state:contact"
KV_CURRENT = "state:current_chat"
SYNC_ON_SELECT = 300


class Ghostwriter:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        persona: Persona,
        llm: LLM,
        senders: dict[str, Sender],
        chats: ChatSource | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.persona = persona
        self.senders = senders
        self.chats = chats
        tz = settings.tz
        o, c = settings.owner_name, settings.contact_name
        self.escalation = EscalationChecker(llm, persona.categories, tz, o, c)
        self.drafter = Drafter(llm, persona, tz, o, c)
        self.analyst = Analyst(llm, tz, o, c)
        self.hours = ActiveHours(*settings.active_window, tz)
        self.queue = SendQueue(self.deliver)
        self.notifier: Notifier | None = None
        # contact: whose messages get auto-drafts; current: the chat commands work on by default
        self.contact: ChatRef | None = None
        self.current: ChatRef | None = None
        self._debounce: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------- lifecycle

    async def load_state(self) -> None:
        """Restore the selected contact / current chat; fall back to the .env defaults."""
        if raw := await self.store.kv_get(KV_CONTACT):
            self.contact = ChatRef.loads(raw)
        elif self.settings.vladimir_tg_id:
            self.contact = await self._default_contact()
        if raw := await self.store.kv_get(KV_CURRENT):
            self.current = ChatRef.loads(raw)
        else:
            self.current = self.contact
        self._update_watched()
        log.info("contact: %s; current chat: %s", self.contact and self.contact.chat_id, self.current and self.current.chat_id)

    async def _default_contact(self) -> ChatRef:
        uid = self.settings.vladimir_tg_id
        fallback = ChatRef(uid, self.settings.contact_name, "user", uid)
        if self.chats is None:
            return fallback
        try:
            ref = await self.chats.chat_ref(self.settings.watched_chat)
        except Exception:  # entity not in cache yet etc.
            log.warning("cannot resolve %s, using the configured id", self.settings.watched_chat)
            return fallback
        return replace(ref, user_id=uid)

    def _update_watched(self) -> None:
        if self.chats is not None:
            self.chats.watched = {r.chat_id for r in (self.contact, self.current) if r is not None}

    async def start(self) -> None:
        """Resume approved drafts that were waiting when the process stopped."""
        now = datetime.now(UTC)
        for d in await self.store.drafts_by_status(DraftStatus.QUEUED):
            at = d.scheduled_at or now
            if at < now:
                at = compute_send_time(now, (5, 60), self.hours)
            self.queue.schedule(d.id, at)
            log.info("resumed queued draft #%s for %s", d.id, at.isoformat())

    async def stop(self) -> None:
        self.queue.shutdown()
        for t in self._debounce.values():
            t.cancel()

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def _notify(self, text: str) -> None:
        if self.notifier:
            await self.notifier.info(text)

    # --------------------------------------------------------- chats & scope

    def _email_belongs_to(self, ref: ChatRef | None) -> bool:
        """Email is configured for the .env contact only."""
        return bool(
            ref is not None
            and Channel.EMAIL in self.senders
            and self.settings.vladimir_email
            and ref.user_id == self.settings.vladimir_tg_id
        )

    def scope_for(self, ref: ChatRef | None) -> list[tuple[str, str]]:
        if ref is None:
            return []
        scope = [(Channel.TELEGRAM.value, str(ref.chat_id))]
        if self._email_belongs_to(ref) and self.settings.vladimir_email:
            scope.append((Channel.EMAIL.value, self.settings.vladimir_email.lower()))
        return scope

    def display_name(self, ref: ChatRef) -> str:
        if ref.is_private:
            return ref.title
        if ref.user_id and ref.user_id == self.settings.vladimir_tg_id:
            return f"{self.settings.contact_name} (group «{ref.title}»)"
        return f"the group chat «{ref.title}»"

    def contact_id(self, channel: str) -> str | None:
        if self.contact is None:
            return None
        if channel == Channel.TELEGRAM and self.contact.user_id:
            return str(self.contact.user_id)
        if channel == Channel.EMAIL and self._email_belongs_to(self.contact) and self.settings.vladimir_email:
            return self.settings.vladimir_email.lower()
        return None

    def contact_chat_id(self, channel: str) -> str | None:
        if self.contact is None:
            return None
        if channel == Channel.TELEGRAM:
            return str(self.contact.chat_id)
        if channel == Channel.EMAIL and self._email_belongs_to(self.contact) and self.settings.vladimir_email:
            return self.settings.vladimir_email.lower()
        return None

    def is_contact(self, msg: Message) -> bool:
        return (
            msg.direction == Direction.IN
            and msg.author_id is not None
            and msg.author_id == self.contact_id(msg.channel)
            and msg.chat_id == self.contact_chat_id(msg.channel)
        )

    def _is_contact_chat(self, ref: ChatRef | None) -> bool:
        return ref is not None and self.contact is not None and ref.chat_id == self.contact.chat_id

    async def sync(self, ref: ChatRef, limit: int, since: datetime | None = None) -> int:
        """Pull fresh Telegram history of a chat into the store."""
        if self.chats is None:
            return 0
        try:
            return len(await self.chats.backfill(ref.chat_id, limit, since))
        except Exception:
            log.exception("sync of chat %s failed; using stored messages", ref.chat_id)
            return 0

    async def find_chats(self, query: str | None, kinds: set[str] | None = None, limit: int = 8) -> list[ChatRef]:
        if self.chats is None:
            raise ActionError("Telegram недоступен.")
        return await self.chats.search_chats(query, kinds, limit)

    async def chat_by_id(self, chat_id: int) -> ChatRef:
        for ref in (self.contact, self.current):
            if ref is not None and ref.chat_id == chat_id:
                return ref
        if self.chats is None:
            raise ActionError("Telegram недоступен.")
        return await self.chats.chat_ref(chat_id)

    async def set_contact(self, ref: ChatRef) -> int:
        """Auto-drafts now go to this person; the current chat switches to them as well."""
        if not ref.is_private:
            raise ActionError("Собеседником может быть только человек. Для группы используй /chat.")
        ref = replace(ref, user_id=ref.user_id or ref.chat_id)
        self.contact = ref
        self.current = ref
        await self.store.kv_set(KV_CONTACT, ref.dumps())
        await self.store.kv_set(KV_CURRENT, ref.dumps())
        self._update_watched()
        log.info("contact switched to %s", ref.chat_id)
        return await self.sync(ref, SYNC_ON_SELECT)

    async def set_current(self, ref: ChatRef) -> int:
        """Summaries, questions and "reply in my style" now work on this chat."""
        if self._is_contact_chat(ref):
            ref = self.contact  # type: ignore[assignment]
        self.current = ref
        await self.store.kv_set(KV_CURRENT, ref.dumps())
        self._update_watched()
        log.info("current chat switched to %s", ref.chat_id)
        return await self.sync(ref, SYNC_ON_SELECT)

    async def _ref_for(self, msg: Message) -> ChatRef:
        if msg.channel == Channel.EMAIL or msg.chat_id == self.contact_chat_id(msg.channel):
            assert self.contact is not None
            return self.contact
        try:
            return await self.chat_by_id(int(msg.chat_id))
        except Exception:
            return ChatRef(int(msg.chat_id), msg.author_name, "user", None)

    # --------------------------------------------------------------- ingest

    async def on_message(self, msg: Message, created: bool) -> None:
        """Called by channel gateways for every stored message."""
        if not created:
            return
        if msg.direction == Direction.IN:
            if self.is_contact(msg):
                if msg.status == MessageStatus.NEW:
                    self.schedule_processing(msg.channel)
            else:
                await self.store.set_message_status(msg.id, MessageStatus.IGNORED)
            return
        if msg.status == MessageStatus.MANUAL:
            await self._owner_replied_manually(msg)

    async def _owner_replied_manually(self, msg: Message) -> None:
        """The owner answered by hand: pending drafts in that chat are no longer needed."""
        pending = await self.store.unhandled_incoming(msg.channel, chat_id=msg.chat_id)
        await self.store.set_message_statuses((m.id for m in pending), MessageStatus.HANDLED)
        if msg.chat_id == self.contact_chat_id(msg.channel):
            task = self._debounce.pop(msg.channel, None)
            if task:
                task.cancel()
        await self._supersede_pending(msg.channel, msg.chat_id, reason="ты ответил сам")

    def schedule_processing(self, channel: str) -> None:
        """Wait for a quiet period so a burst of messages gets one reply."""
        old = self._debounce.get(channel)
        if old and not old.done():
            old.cancel()

        async def later() -> None:
            await asyncio.sleep(self.settings.debounce_seconds)
            self._debounce.pop(channel, None)
            try:
                await self.process_channel(channel)
            except Exception as exc:
                log.exception("processing %s failed", channel)
                await self._notify(f"⚠️ Не удалось обработать новые сообщения ({channel}): {exc}")

        self._debounce[channel] = asyncio.create_task(later(), name=f"debounce-{channel}")

    async def _context(self, new_messages: Sequence[Message], scope: Scope) -> tuple[list[Message], list[Message]]:
        new_ids = {m.id for m in new_messages}
        limit = self.settings.history_limit
        history = await self.store.recent_history(limit + len(new_ids), before=new_messages[-1].timestamp, scope=scope)
        history = [m for m in history if m.id not in new_ids][-limit:]
        n = self.settings.style_sample_size
        style = await self.store.owner_style_samples(n, scope=scope)
        if len(style) < min(10, n):  # little history with this person: use the owner's messages everywhere
            style = await self.store.owner_style_samples(n)
        return history, style

    async def process_channel(self, channel: str) -> Draft | None:
        contact = self.contact
        if contact is None:
            return None
        async with self._lock(f"{channel}:{self.contact_chat_id(channel)}"):
            new = await self.store.unhandled_incoming(
                channel, author_id=self.contact_id(channel), chat_id=self.contact_chat_id(channel)
            )
            if not new:
                return None
            source = new[-1]
            history, style = await self._context(new, self.scope_for(contact))
            name = self.display_name(contact)

            decision = await self.escalation.check(new, history, contact_name=name)
            if decision.escalate:
                return await self._escalate(source, new, f"{decision.category or 'sensitive'}: {decision.reason}")

            result = await self.drafter.draft(
                source=source, new_messages=new, history=history, style_samples=style,
                contact_name=name, contact_notes=self.persona.contact_notes(contact.chat_id),
            )
            if result.escalate:
                return await self._escalate(source, new, f"drafter: {result.note or 'needs your input'}")

            await self._supersede_pending(channel, source.chat_id)
            draft = await self.store.create_draft(
                source_message_id=source.id,
                channel=channel,
                text=result.text,
                reason=result.note or None,
                requested_by="auto",
            )
            await self.store.set_message_statuses((m.id for m in new), MessageStatus.HANDLED)
            if self.notifier:
                await self.notifier.draft_card(draft)
            if self.settings.auto_mode:
                draft = await self.approve(draft.id)
            return draft

    async def _escalate(self, source: Message, new: Sequence[Message], reason: str) -> Draft:
        draft = await self.store.create_draft(
            source_message_id=source.id, channel=source.channel, status=DraftStatus.ESCALATED, reason=reason
        )
        await self.store.set_message_statuses((m.id for m in new), MessageStatus.ESCALATED)
        log.info("escalated message #%s (%s)", source.id, reason.split(":")[0])
        if self.notifier:
            await self.notifier.escalation(draft, new)
        return draft

    async def _supersede_pending(self, channel: str, chat_id: str, reason: str = "replaced by a newer draft") -> None:
        for d in await self.store.drafts_by_status(DraftStatus.PENDING, channel=channel):
            if d.source.chat_id != str(chat_id):
                continue
            d = await self.store.update_draft(d.id, status=DraftStatus.SUPERSEDED, reason=reason)
            if d and self.notifier:
                await self.notifier.refresh_card(d)

    # ------------------------------------------------------- owner commands

    async def _batch_for(self, source: Message, ref: ChatRef) -> list[Message]:
        """Incoming messages since the owner's last reply in that chat, up to and including `source`."""
        last_out = await self.store.last_outgoing_before(source.channel, source.chat_id, source.timestamp)
        since = last_out.timestamp if last_out else source.timestamp - timedelta(days=1)
        msgs = await self.store.find_messages(
            direction=Direction.IN, since=since, limit=20, scope=[(source.channel, source.chat_id)]
        )
        only_contact = self._is_contact_chat(ref) and bool(ref.user_id)
        batch = [
            m for m in msgs
            if since < m.timestamp <= source.timestamp and (not only_contact or self.is_contact(m))
        ]
        return batch or [source]

    async def request_reply(
        self, instructions: str | None = None, channel: str | None = None, chat: ChatRef | None = None
    ) -> Draft:
        """Owner asked for a reply ("ответь в моем стиле"). Skips the escalation gate: the owner is in the loop."""
        ref = chat or self.current
        if ref is None:
            raise ActionError("Сначала выбери чат: /chat или /contact.")
        source: Message | None = None
        if self._is_contact_chat(ref):
            for ch in [channel] if channel else list(self.senders):
                cand = await self.store.latest_incoming(
                    author_id=self.contact_id(ch), channel=ch, scope=self.scope_for(ref)
                )
                if cand and (source is None or cand.timestamp > source.timestamp):
                    source = cand
        else:
            await self.sync(ref, 50)
            source = await self.store.latest_incoming(scope=self.scope_for(ref))
        if source is None:
            raise ActionError(f"В чате «{ref.title}» пока нет входящих сообщений. Попробуй /sync.")
        return await self._draft_for(source, instructions, ref)

    async def _draft_for(self, source: Message, instructions: str | None, ref: ChatRef) -> Draft:
        async with self._lock(f"{source.channel}:{source.chat_id}"):
            batch = await self._batch_for(source, ref)
            history, style = await self._context(batch, self.scope_for(ref))
            result = await self.drafter.draft(
                source=source, new_messages=batch, history=history, style_samples=style, instructions=instructions,
                contact_name=self.display_name(ref), contact_notes=self.persona.contact_notes(ref.chat_id),
            )
            if not result.text:
                raise ActionError(f"Не получилось написать ответ: {result.note or 'без объяснения'}")
            notes = [result.note] if result.note else []
            hits = keyword_hits(self.persona.categories, "\n".join(m.text for m in batch))
            if hits:
                notes.append("⚠️ чувствительная тема: " + ", ".join(c.name for c in hits))
            await self._supersede_pending(source.channel, source.chat_id)
            draft = await self.store.create_draft(
                source_message_id=source.id,
                channel=source.channel,
                text=result.text,
                reason="; ".join(notes) or None,
                instructions=instructions,
                requested_by="owner",
            )
            await self.store.set_message_statuses((m.id for m in batch if m.status == MessageStatus.NEW), MessageStatus.HANDLED)
        if self.notifier:
            await self.notifier.draft_card(draft)
        return draft

    async def regenerate(self, draft_id: int, instructions: str | None = None) -> Draft:
        old = await self._get(draft_id)
        if old.status not in (DraftStatus.PENDING, DraftStatus.ESCALATED, DraftStatus.SUPERSEDED, DraftStatus.SKIPPED):
            raise ActionError(f"Черновик #{draft_id} уже {old.status}, его нельзя переписать.")
        if old.status == DraftStatus.PENDING:
            upd = await self.store.update_draft(draft_id, status=DraftStatus.SUPERSEDED, reason="regenerated")
            if upd and self.notifier:
                await self.notifier.refresh_card(upd)
        return await self._draft_for(old.source, instructions or old.instructions, await self._ref_for(old.source))

    async def approve(self, draft_id: int, text: str | None = None) -> Draft:
        d = await self._get(draft_id)
        if d.status not in (DraftStatus.PENDING, DraftStatus.ESCALATED):
            raise ActionError(f"Черновик #{draft_id} уже {d.status}.")
        if text is None and not d.text:
            raise ActionError("Нечего отправлять — нажми «Изменить» и напиши текст.")
        final = text if text is not None else d.text
        at = compute_send_time(datetime.now(UTC), self.settings.reply_delay_range, self.hours)
        d = await self.store.update_draft(
            draft_id,
            final_text=final,
            edited=text is not None and text != d.text,
            status=DraftStatus.QUEUED,
            scheduled_at=at,
        )
        assert d is not None
        self.queue.schedule(d.id, at)
        log.info("draft #%s queued for %s", d.id, at.isoformat())
        if self.notifier:
            await self.notifier.refresh_card(d)
        return d

    async def send_now(self, draft_id: int) -> Draft:
        d = await self._get(draft_id)
        if d.status != DraftStatus.QUEUED:
            raise ActionError(f"Черновик #{draft_id} не в очереди ({d.status}).")
        now = datetime.now(UTC)
        d = await self.store.update_draft(draft_id, scheduled_at=now)
        self.queue.schedule(draft_id, now)
        assert d is not None
        return d

    async def skip(self, draft_id: int) -> Draft:
        d = await self._get(draft_id)
        if d.status not in (DraftStatus.PENDING, DraftStatus.QUEUED, DraftStatus.ESCALATED):
            raise ActionError(f"Черновик #{draft_id} уже {d.status}.")
        self.queue.cancel(draft_id)
        d = await self.store.update_draft(draft_id, status=DraftStatus.SKIPPED)
        assert d is not None
        if self.notifier:
            await self.notifier.refresh_card(d)
        return d

    async def _get(self, draft_id: int) -> Draft:
        d = await self.store.get_draft(draft_id)
        if d is None:
            raise ActionError(f"Черновик #{draft_id} не найден.")
        return d

    async def deliver(self, draft_id: int) -> None:
        d = await self.store.get_draft(draft_id)
        if d is None or d.status != DraftStatus.QUEUED:
            return
        sender = self.senders.get(d.channel)
        if sender is None:
            await self.store.update_draft(draft_id, status=DraftStatus.FAILED, reason=f"{d.channel} is not configured")
            await self._notify(f"⚠️ Черновик #{draft_id}: канал {d.channel} не настроен.")
            return
        try:
            sent = await sender.send(d.outgoing_text, d.source)
        except Exception as exc:
            log.exception("sending draft #%s failed", draft_id)
            failed = await self.store.update_draft(draft_id, status=DraftStatus.FAILED, reason=str(exc))
            if failed and self.notifier:
                await self.notifier.refresh_card(failed)
            return
        d = await self.store.update_draft(draft_id, status=DraftStatus.SENT, sent_message_id=sent.id)
        log.info("draft #%s sent via %s", draft_id, sent.channel)
        if d and self.notifier:
            await self.notifier.refresh_card(d)

    # ------------------------------------------------------------- analysis

    def _is_owner(self, name: str | None) -> bool:
        if not name:
            return False
        n = name.strip().lower()
        return n in {"me", "я", "мне", "меня", "мои", "мой", "myself", self.settings.owner_name.lower()}

    async def select_messages(
        self, author: str | None, limit: int | None, since_hours: float | None, scope: Scope = None
    ) -> list[Message]:
        since = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours else None
        limit = limit or (1000 if since else 100)
        if self._is_owner(author):
            return await self.store.find_messages(direction=Direction.OUT, since=since, limit=limit, scope=scope)
        return await self.store.find_messages(author=author, since=since, limit=limit, scope=scope)

    def _target(self, chat: ChatRef | None) -> ChatRef:
        ref = chat or self.current
        if ref is None:
            raise ActionError("Сначала выбери чат: /chat.")
        return ref

    async def _sync_for(self, ref: ChatRef, limit: int | None, since_hours: float | None) -> None:
        since = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours else None
        await self.sync(ref, min(limit or (2000 if since else 100), 2000), since)

    async def summarize(self, req: SummaryRequest, chat: ChatRef | None = None) -> str:
        ref = self._target(chat)
        await self._sync_for(ref, req.limit, req.since_hours)
        msgs = await self.select_messages(req.author, req.limit, req.since_hours, self.scope_for(ref))
        if not msgs:
            known = ", ".join(await self.store.participants(self.scope_for(ref))) or "none"
            who = f" от «{req.author}»" if req.author else ""
            return f"{ref.label}: сообщений{who} не найдено. Известные участники: {known}."
        tz = self.settings.tz
        header = (
            f"📋 {ref.label} · {len(msgs)} сообщ., "
            f"{msgs[0].timestamp.astimezone(tz):%d.%m %H:%M} – {msgs[-1].timestamp.astimezone(tz):%d.%m %H:%M}\n\n"
        )
        chat_title = None if ref.is_private else ref.title
        return header + await self.analyst.summarize(msgs, focus=req.focus, author=req.author, chat=chat_title)

    async def ask(
        self, question: str, limit: int | None = None, author: str | None = None,
        since_hours: float | None = None, chat: ChatRef | None = None,
    ) -> str:
        ref = self._target(chat)
        await self._sync_for(ref, limit or 300, since_hours)
        msgs = await self.select_messages(author, limit or 300, since_hours, self.scope_for(ref))
        if not msgs:
            return f"{ref.label}: сообщений пока нет. Попробуй /sync."
        return await self.analyst.answer(question, msgs, chat=None if ref.is_private else ref.title)

    async def route(self, text: str) -> Intent:
        scope = self.scope_for(self.current) if self.current else None
        return await self.analyst.route(text, await self.store.participants(scope), self.current.title if self.current else "")


__all__ = ["ActionError", "ChatRef", "ChatSource", "Ghostwriter", "LLMError", "Notifier", "SendQueue", "SummaryRequest"]
