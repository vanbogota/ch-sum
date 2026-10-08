"""Orchestration: incoming message -> escalation check -> draft -> owner approval -> timed send."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .config import Settings
from .llm import LLM, LLMError
from .llm.analyst import Analyst, Intent
from .llm.drafter import Drafter
from .llm.escalation import EscalationChecker, keyword_hits
from .persona import Persona
from .scheduling import ActiveHours, compute_send_time
from .storage import Channel, Direction, Draft, DraftStatus, Message, Store
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


class Ghostwriter:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        persona: Persona,
        llm: LLM,
        senders: dict[str, Sender],
    ) -> None:
        self.settings = settings
        self.store = store
        self.persona = persona
        self.senders = senders
        tz = settings.tz
        o, c = settings.owner_name, settings.contact_name
        self.escalation = EscalationChecker(llm, persona.categories, tz, o, c)
        self.drafter = Drafter(llm, persona, tz, o, c)
        self.analyst = Analyst(llm, tz, o, c)
        self.hours = ActiveHours(*settings.active_window, tz)
        self.queue = SendQueue(self.deliver)
        self.notifier: Notifier | None = None
        self._debounce: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------- lifecycle

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

    def _lock(self, channel: str) -> asyncio.Lock:
        return self._locks.setdefault(channel, asyncio.Lock())

    def contact_id(self, channel: str) -> str | None:
        if channel == Channel.TELEGRAM:
            return str(self.settings.vladimir_tg_id)
        if channel == Channel.EMAIL and self.settings.vladimir_email:
            return self.settings.vladimir_email.lower()
        return None

    def is_contact(self, msg: Message) -> bool:
        return msg.direction == Direction.IN and msg.author_id == self.contact_id(msg.channel)

    async def _notify(self, text: str) -> None:
        if self.notifier:
            await self.notifier.info(text)

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
        """The owner answered by hand: pending drafts on that channel are no longer needed."""
        pending = await self.store.unhandled_incoming(msg.channel)
        await self.store.set_message_statuses((m.id for m in pending), MessageStatus.HANDLED)
        task = self._debounce.pop(msg.channel, None)
        if task:
            task.cancel()
        for d in await self.store.drafts_by_status(DraftStatus.PENDING, channel=msg.channel):
            d = await self.store.update_draft(d.id, status=DraftStatus.SUPERSEDED, reason="ты ответил сам")
            if d and self.notifier:
                await self.notifier.refresh_card(d)

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

    async def _context(self, new_messages: Sequence[Message]) -> tuple[list[Message], list[Message]]:
        new_ids = {m.id for m in new_messages}
        limit = self.settings.history_limit
        history = await self.store.recent_history(limit + len(new_ids), before=new_messages[-1].timestamp)
        history = [m for m in history if m.id not in new_ids][-limit:]
        style = await self.store.owner_style_samples(self.settings.style_sample_size)
        return history, style

    async def process_channel(self, channel: str) -> Draft | None:
        async with self._lock(channel):
            new = await self.store.unhandled_incoming(channel, author_id=self.contact_id(channel))
            if not new:
                return None
            source = new[-1]
            history, style = await self._context(new)

            decision = await self.escalation.check(new, history)
            if decision.escalate:
                return await self._escalate(source, new, f"{decision.category or 'sensitive'}: {decision.reason}")

            result = await self.drafter.draft(source=source, new_messages=new, history=history, style_samples=style)
            if result.escalate:
                return await self._escalate(source, new, f"drafter: {result.note or 'needs your input'}")

            await self._supersede_pending(channel)
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

    async def _supersede_pending(self, channel: str, reason: str = "replaced by a newer draft") -> None:
        for d in await self.store.drafts_by_status(DraftStatus.PENDING, channel=channel):
            d = await self.store.update_draft(d.id, status=DraftStatus.SUPERSEDED, reason=reason)
            if d and self.notifier:
                await self.notifier.refresh_card(d)

    # ------------------------------------------------------- owner commands

    async def _batch_for(self, source: Message) -> list[Message]:
        """Contact messages since the owner's last reply, up to and including `source`."""
        last_out = await self.store.last_outgoing_before(source.channel, source.timestamp)
        since = last_out.timestamp if last_out else source.timestamp - timedelta(days=1)
        msgs = await self.store.find_messages(channel=source.channel, direction=Direction.IN, since=since, limit=20)
        batch = [m for m in msgs if m.timestamp <= source.timestamp and self.is_contact(m) and m.timestamp > since]
        return batch or [source]

    async def request_reply(self, instructions: str | None = None, channel: str | None = None) -> Draft:
        """Owner asked for a reply ("ответь в моем стиле"). Skips the escalation gate: the owner is in the loop."""
        source: Message | None = None
        for ch in [channel] if channel else list(self.senders):
            cand = await self.store.latest_incoming(author_id=self.contact_id(ch), channel=ch)
            if cand and (source is None or cand.timestamp > source.timestamp):
                source = cand
        if source is None:
            raise ActionError(f"Пока нет сообщений от {self.settings.contact_name}. Попробуй /sync.")
        return await self._draft_for(source, instructions)

    async def _draft_for(self, source: Message, instructions: str | None) -> Draft:
        async with self._lock(source.channel):
            batch = await self._batch_for(source)
            history, style = await self._context(batch)
            result = await self.drafter.draft(
                source=source, new_messages=batch, history=history, style_samples=style, instructions=instructions
            )
            if not result.text:
                raise ActionError(f"Не получилось написать ответ: {result.note or 'без объяснения'}")
            notes = [result.note] if result.note else []
            hits = keyword_hits(self.persona.categories, "\n".join(m.text for m in batch))
            if hits:
                notes.append("⚠️ чувствительная тема: " + ", ".join(c.name for c in hits))
            await self._supersede_pending(source.channel)
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
        return await self._draft_for(old.source, instructions or old.instructions)

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

    async def select_messages(self, author: str | None, limit: int | None, since_hours: float | None) -> list[Message]:
        since = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours else None
        limit = limit or (1000 if since else 100)
        if self._is_owner(author):
            return await self.store.find_messages(direction=Direction.OUT, since=since, limit=limit)
        return await self.store.find_messages(author=author, since=since, limit=limit)

    async def summarize(self, req: SummaryRequest) -> str:
        msgs = await self.select_messages(req.author, req.limit, req.since_hours)
        if not msgs:
            known = ", ".join(await self.store.participants()) or "none"
            who = f" от «{req.author}»" if req.author else ""
            return f"Сообщений{who} не найдено. Известные участники: {known}."
        header = f"📋 {len(msgs)} сообщ., {msgs[0].timestamp.astimezone(self.settings.tz):%d.%m %H:%M} – {msgs[-1].timestamp.astimezone(self.settings.tz):%d.%m %H:%M}\n\n"
        return header + await self.analyst.summarize(msgs, focus=req.focus, author=req.author)

    async def ask(self, question: str, limit: int | None = None, author: str | None = None, since_hours: float | None = None) -> str:
        msgs = await self.select_messages(author, limit or 300, since_hours)
        if not msgs:
            return "Сообщений пока нет. Попробуй /sync."
        return await self.analyst.answer(question, msgs)

    async def route(self, text: str) -> Intent:
        return await self.analyst.route(text, await self.store.participants())


__all__ = ["ActionError", "Ghostwriter", "LLMError", "Notifier", "SendQueue", "SummaryRequest"]
