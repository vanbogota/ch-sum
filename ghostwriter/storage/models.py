from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores datetimes as UTC and always returns timezone-aware values (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Channel(StrEnum):
    TELEGRAM = "telegram"
    EMAIL = "email"


class Direction(StrEnum):
    IN = "in"    # from the contact (or other chat members)
    OUT = "out"  # from the owner, typed manually or sent by the bot


class MessageStatus(StrEnum):
    NEW = "new"              # stored, not processed
    HANDLED = "handled"      # a draft was produced / owner already replied
    ESCALATED = "escalated"  # owner was notified instead of drafting
    IGNORED = "ignored"      # not from the contact, or history backfill
    SENT = "sent"            # outgoing message sent by the bot
    MANUAL = "manual"        # outgoing message typed by the owner


class DraftStatus(StrEnum):
    PENDING = "pending"          # waiting for the owner's decision
    QUEUED = "queued"            # approved, waiting for delay / active hours
    SENT = "sent"
    SKIPPED = "skipped"
    ESCALATED = "escalated"      # not drafted: sensitive topic
    SUPERSEDED = "superseded"    # replaced by a regenerated draft or a manual reply
    FAILED = "failed"


class Base(DeclarativeBase):
    pass


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("channel", "chat_id", "external_id", name="uq_message_external"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8), index=True)
    chat_id: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    author_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    author_name: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=MessageStatus.NEW)

    def __repr__(self) -> str:  # never include the body
        return f"<Message #{self.id} {self.channel}/{self.direction} by {self.author_name!r} at {self.timestamp}>"


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text, default="")          # model output
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)  # what was / will be sent
    edited: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=DraftStatus.PENDING, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # escalation reason / model note
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str] = mapped_column(String(16), default="auto")  # auto | owner
    control_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    sent_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    source: Mapped[Message] = relationship(foreign_keys=[source_message_id], lazy="joined")

    @property
    def outgoing_text(self) -> str:
        return self.final_text if self.final_text is not None else self.text

    def __repr__(self) -> str:
        return f"<Draft #{self.id} {self.status} for message #{self.source_message_id}>"


class KeyValue(Base):
    """Small persistent state (e.g. email sync markers)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
