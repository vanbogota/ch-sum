"""Telethon userbot: reads the watched chat from the owner's account and sends replies as the owner."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from telethon import TelegramClient, events, utils
from telethon.tl.types import User
from telethon.tl.custom.message import Message as TgMessage

from ..chats import ChatRef
from ..config import Settings
from ..scheduling import typing_seconds
from ..storage import Channel, Direction, Message, Store
from ..storage.models import MessageStatus

log = logging.getLogger(__name__)

OnMessage = Callable[[Message, bool], Awaitable[None]]


def message_text(m: TgMessage) -> str:
    """Text of a Telegram message, with a placeholder for media."""
    text = m.message or ""
    placeholder = None
    if m.voice:
        placeholder = "[voice message]"
    elif m.video_note:
        placeholder = "[video message]"
    elif m.sticker:
        placeholder = "[sticker]"
    elif m.photo:
        placeholder = "[photo]"
    elif m.video:
        placeholder = "[video]"
    elif m.gif:
        placeholder = "[gif]"
    elif m.document:
        placeholder = "[file]"
    elif m.geo:
        placeholder = "[location]"
    elif m.poll:
        placeholder = "[poll]"
    if placeholder:
        return f"{placeholder} {text}".strip()
    return text


class TelegramGateway:
    def __init__(self, settings: Settings, store: Store, client: TelegramClient | None = None) -> None:
        self.settings = settings
        self.store = store
        settings.tg_session_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client or TelegramClient(
            str(settings.tg_session_path), settings.tg_api_id, settings.tg_api_hash.get_secret_value()
        )
        self.on_message: OnMessage | None = None
        # Chats whose new messages are stored live (the contact's chat and the current chat).
        self.watched: set[int] = set()
        self._me_name = settings.owner_name
        # Held while the bot sends, so the echo of our own message is recognised as bot-sent.
        self._send_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorised. Run `python -m ghostwriter login` first.")
        me = await self.client.get_me()
        self._me_name = utils.get_display_name(me) or self.settings.owner_name
        await self.client.get_dialogs()  # fills the entity cache so numeric ids resolve
        self.client.add_event_handler(self._on_new_message, events.NewMessage())

    async def stop(self) -> None:
        await self.client.disconnect()

    async def run(self) -> None:
        await self.client.run_until_disconnected()

    async def _store(self, m: TgMessage, status: str | None = None) -> tuple[Message, bool]:
        if m.out:
            author_name, author_id = self._me_name, m.sender_id
        else:
            sender = await m.get_sender()
            author_name = utils.get_display_name(sender) if sender else "?"
            author_id = m.sender_id
        meta: dict[str, object] = {}
        if m.reply_to_msg_id:
            meta["reply_to"] = m.reply_to_msg_id
        if status is None:
            status = MessageStatus.MANUAL if m.out else MessageStatus.NEW
        return await self.store.add_message(
            channel=Channel.TELEGRAM,
            direction=Direction.OUT if m.out else Direction.IN,
            chat_id=m.chat_id,
            external_id=m.id,
            author_id=author_id,
            author_name=author_name or "?",
            text=message_text(m),
            timestamp=m.date,
            meta=meta,
            status=status,
        )

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        if event.chat_id not in self.watched:
            return
        m: TgMessage = event.message
        if m.out:
            async with self._send_lock:  # wait until a bot send (if any) has been recorded
                stored, created = await self._store(m)
        else:
            stored, created = await self._store(m)
        log.info("telegram: %s message #%s (%s)", stored.direction, stored.id, "new" if created else "dup")
        log.debug("telegram text: %r", stored.text)
        if self.on_message:
            await self.on_message(stored, created)

    async def backfill(self, chat_id: int, limit: int, since: datetime | None = None) -> list[Message]:
        """Import the last `limit` messages of a chat (stopping at `since`). Returns new messages, oldest first.

        Backfilled messages are stored as already handled so they don't trigger drafts.
        """
        created_msgs: list[Message] = []
        async for m in self.client.iter_messages(chat_id, limit=limit):
            if since is not None and m.date < since:
                break
            if m.action is not None:  # service messages (joins, pins, ...)
                continue
            status = MessageStatus.MANUAL if m.out else MessageStatus.IGNORED
            stored, created = await self._store(m, status=status)
            if created:
                created_msgs.append(stored)
        created_msgs.sort(key=lambda x: (x.timestamp, x.id))
        log.info("telegram: backfilled %d new messages from %s", len(created_msgs), chat_id)
        return created_msgs

    async def send(self, text: str, reply_to: Message | None = None) -> Message:
        """Send as the owner into the chat of `reply_to`, with a realistic "typing…" pause, and record it.

        The message is sent as a plain message, not a quote: that reads more natural.
        """
        if reply_to is None:
            raise ValueError("telegram send needs the message being answered")
        chat_id = int(reply_to.chat_id)
        async with self._send_lock:
            async with self.client.action(chat_id, "typing"):
                await asyncio.sleep(typing_seconds(text))
            sent = await self.client.send_message(chat_id, text)
            stored, created = await self.store.add_message(
                channel=Channel.TELEGRAM,
                direction=Direction.OUT,
                chat_id=chat_id,
                external_id=sent.id,
                author_id=sent.sender_id,
                author_name=self._me_name,
                text=text,
                timestamp=sent.date or datetime.now().astimezone(),
                meta={"via_bot": True},
                status=MessageStatus.SENT,
            )
            if not created:
                await self.store.set_message_status(stored.id, MessageStatus.SENT)
        return stored

    async def list_dialogs(self, limit: int = 50) -> list[tuple[int, str, str]]:
        return [(c.chat_id, c.kind, c.title) for c in await self.search_chats(None, limit=limit)]

    async def search_chats(self, query: str | None, kinds: set[str] | None = None, limit: int = 10) -> list[ChatRef]:
        """Recent dialogs, optionally filtered by name / @username / id. Exact matches come first."""
        needle = (query or "").strip().lstrip("@").casefold()
        exact: list[ChatRef] = []
        partial: list[ChatRef] = []
        async for d in self.client.iter_dialogs(limit=300 if needle else limit * 3):
            if getattr(d.entity, "bot", False) or d.id == 777000:  # skip bots and the Telegram service chat
                continue
            ref = _dialog_ref(d)
            if kinds and ref.kind not in kinds:
                continue
            if not needle:
                partial.append(ref)
            else:
                username = (getattr(d.entity, "username", None) or "").casefold()
                names = {ref.title.casefold(), username, str(ref.chat_id)}
                if needle in names:
                    exact.append(ref)
                elif any(needle in n for n in names if n):
                    partial.append(ref)
            if not needle and len(partial) >= limit:
                break
        return (exact + partial)[:limit]

    async def chat_ref(self, chat: int | str) -> ChatRef:
        entity = await self.client.get_entity(chat)
        kind = "user" if isinstance(entity, User) else "channel" if getattr(entity, "broadcast", False) else "group"
        peer_id = utils.get_peer_id(entity)
        return ChatRef(peer_id, utils.get_display_name(entity) or str(peer_id), kind, peer_id if kind == "user" else None)


def _dialog_ref(d: object) -> ChatRef:
    kind = "user" if d.is_user else "group" if d.is_group else "channel"  # type: ignore[attr-defined]
    chat_id = d.id  # type: ignore[attr-defined]
    return ChatRef(chat_id, d.name or str(chat_id), kind, chat_id if kind == "user" else None)  # type: ignore[attr-defined]
