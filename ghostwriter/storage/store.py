"""Async storage layer. Uses SQLAlchemy so SQLite can be swapped for Postgres/Supabase via DATABASE_URL."""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Select, and_, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .models import Base, Direction, Draft, DraftStatus, KeyValue, Message, utcnow


# A conversation scope: the (channel, chat_id) pairs that make up one conversation,
# e.g. [("telegram", "123"), ("email", "vlad@x.org")]. None means "everything".
Scope = Sequence[tuple[str, str]] | None


def _in_scope(q: Select[Any], scope: Scope) -> Select[Any]:
    if scope is None:
        return q
    if not scope:
        return q.where(false())
    return q.where(or_(*(and_(Message.channel == ch, Message.chat_id == str(cid)) for ch, cid in scope)))


class Store:
    def __init__(self, database_url: str) -> None:
        if database_url.startswith("sqlite") and ":///" in database_url:
            path = database_url.split(":///", 1)[1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(database_url)
        self._session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # ---------------------------------------------------------------- messages

    async def add_message(
        self,
        *,
        channel: str,
        direction: str,
        chat_id: str | int,
        text: str,
        author_name: str,
        author_id: str | int | None = None,
        external_id: str | int | None = None,
        timestamp: datetime | None = None,
        meta: dict[str, Any] | None = None,
        status: str = "new",
    ) -> tuple[Message, bool]:
        """Insert a message. Returns (message, created); duplicates (same external id) return the existing row."""
        chat_id = str(chat_id)
        ext = None if external_id is None else str(external_id)
        async with self._session() as s:
            if ext is not None:
                existing = await s.scalar(
                    select(Message).where(
                        Message.channel == channel, Message.chat_id == chat_id, Message.external_id == ext
                    )
                )
                if existing is not None:
                    return existing, False
            msg = Message(
                channel=channel,
                direction=direction,
                chat_id=chat_id,
                external_id=ext,
                author_id=None if author_id is None else str(author_id),
                author_name=author_name,
                text=text,
                timestamp=timestamp or utcnow(),
                meta=meta or {},
                status=status,
            )
            s.add(msg)
            await s.commit()
            return msg, True

    async def get_message(self, message_id: int) -> Message | None:
        async with self._session() as s:
            return await s.get(Message, message_id)

    async def set_message_status(self, message_id: int, status: str) -> None:
        async with self._session() as s:
            await s.execute(update(Message).where(Message.id == message_id).values(status=status))
            await s.commit()

    async def set_message_statuses(self, message_ids: Iterable[int], status: str) -> None:
        ids = list(message_ids)
        if not ids:
            return
        async with self._session() as s:
            await s.execute(update(Message).where(Message.id.in_(ids)).values(status=status))
            await s.commit()

    async def recent_history(self, limit: int, before: datetime | None = None, scope: Scope = None) -> list[Message]:
        """Last `limit` messages of the scope, oldest first."""
        async with self._session() as s:
            q = _in_scope(select(Message), scope)
            if before is not None:
                q = q.where(Message.timestamp <= before)
            rows = (await s.scalars(q.order_by(Message.timestamp.desc(), Message.id.desc()).limit(limit))).all()
        return list(reversed(rows))

    async def find_messages(
        self,
        *,
        author: str | None = None,
        direction: str | None = None,
        channel: str | None = None,
        since: datetime | None = None,
        limit: int | None = 100,
        scope: Scope = None,
    ) -> list[Message]:
        """Messages filtered by author (case-insensitive substring of name, or exact id), newest `limit`, oldest first."""
        async with self._session() as s:
            q = _in_scope(select(Message), scope)
            if author:
                needle = author.strip().lstrip("@").lower()
                q = q.where(or_(func.lower(Message.author_name).contains(needle), Message.author_id == needle))
            if direction:
                q = q.where(Message.direction == direction)
            if channel:
                q = q.where(Message.channel == channel)
            if since is not None:
                q = q.where(Message.timestamp >= since)
            q = q.order_by(Message.timestamp.desc(), Message.id.desc())
            if limit:
                q = q.limit(limit)
            rows = (await s.scalars(q)).all()
        return list(reversed(rows))

    async def owner_style_samples(self, limit: int, scope: Scope = None) -> list[Message]:
        """The owner's most recent messages typed by hand (not bot-sent), oldest first."""
        async with self._session() as s:
            q = (
                _in_scope(select(Message), scope)
                .where(Message.direction == Direction.OUT, Message.status != "sent")
                .where(func.length(Message.text) > 0)
                .order_by(Message.timestamp.desc())
                .limit(limit)
            )
            rows = (await s.scalars(q)).all()
        return list(reversed(rows))

    async def latest_incoming(
        self, author_id: str | int | None = None, channel: str | None = None, scope: Scope = None
    ) -> Message | None:
        async with self._session() as s:
            q = _in_scope(select(Message), scope).where(Message.direction == Direction.IN)
            if author_id is not None:
                q = q.where(Message.author_id == str(author_id))
            if channel is not None:
                q = q.where(Message.channel == channel)
            return await s.scalar(q.order_by(Message.timestamp.desc(), Message.id.desc()).limit(1))

    async def unhandled_incoming(
        self, channel: str, author_id: str | int | None = None, chat_id: str | int | None = None
    ) -> list[Message]:
        async with self._session() as s:
            q = select(Message).where(
                Message.direction == Direction.IN, Message.channel == channel, Message.status == "new"
            )
            if chat_id is not None:
                q = q.where(Message.chat_id == str(chat_id))
            if author_id is not None:
                q = q.where(Message.author_id == str(author_id))
            return list((await s.scalars(q.order_by(Message.timestamp, Message.id))).all())

    async def last_message(self, channel: str) -> Message | None:
        async with self._session() as s:
            return await s.scalar(
                select(Message).where(Message.channel == channel).order_by(Message.timestamp.desc()).limit(1)
            )

    async def last_outgoing_before(self, channel: str, chat_id: str | int, moment: datetime) -> Message | None:
        async with self._session() as s:
            return await s.scalar(
                select(Message)
                .where(Message.channel == channel, Message.chat_id == str(chat_id))
                .where(Message.direction == Direction.OUT, Message.timestamp <= moment)
                .order_by(Message.timestamp.desc())
                .limit(1)
            )

    async def participants(self, scope: Scope = None) -> list[str]:
        async with self._session() as s:
            rows = (await s.scalars(_in_scope(select(Message.author_name), scope).distinct())).all()
        return sorted({r for r in rows if r and r != "?"})

    async def count_messages(self) -> dict[str, int]:
        async with self._session() as s:
            rows = (await s.execute(select(Message.channel, func.count()).group_by(Message.channel))).all()
        return {ch: n for ch, n in rows}

    # ------------------------------------------------------------------ drafts

    async def create_draft(self, **fields: Any) -> Draft:
        async with self._session() as s:
            draft = Draft(**fields)
            s.add(draft)
            await s.commit()
            draft_id = draft.id
        loaded = await self.get_draft(draft_id)
        assert loaded is not None
        return loaded

    async def get_draft(self, draft_id: int) -> Draft | None:
        async with self._session() as s:
            return await s.get(Draft, draft_id)

    async def update_draft(self, draft_id: int, **fields: Any) -> Draft | None:
        fields["updated_at"] = utcnow()
        async with self._session() as s:
            await s.execute(update(Draft).where(Draft.id == draft_id).values(**fields))
            await s.commit()
        return await self.get_draft(draft_id)

    async def drafts_by_status(self, *statuses: str, channel: str | None = None) -> list[Draft]:
        async with self._session() as s:
            q = select(Draft).where(Draft.status.in_(statuses))
            if channel is not None:
                q = q.where(Draft.channel == channel)
            return list((await s.scalars(q.order_by(Draft.created_at))).all())

    async def draft_counts(self) -> dict[str, int]:
        async with self._session() as s:
            rows = (await s.execute(select(Draft.status, func.count()).group_by(Draft.status))).all()
        return {st: n for st, n in rows}

    # --------------------------------------------------------------------- kv

    async def kv_get(self, key: str) -> str | None:
        async with self._session() as s:
            row = await s.get(KeyValue, key)
            return row.value if row else None

    async def kv_set(self, key: str, value: str) -> None:
        async with self._session() as s:
            row = await s.get(KeyValue, key)
            if row is None:
                s.add(KeyValue(key=key, value=value))
            else:
                row.value = value
            await s.commit()


__all__ = ["DraftStatus", "Scope", "Store"]
