"""Prompt building helpers shared by the drafter, the classifier and the analyst."""
from __future__ import annotations

from collections.abc import Sequence
from zoneinfo import ZoneInfo

from ..storage.models import Message

MAX_MESSAGE_CHARS = 4000  # a single very long email should not crowd out the rest of the history


def format_message(msg: Message, tz: ZoneInfo, owner_name: str) -> str:
    when = msg.timestamp.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    author = owner_name if msg.direction == "out" else (msg.author_name or "?")
    text = msg.text if len(msg.text) <= MAX_MESSAGE_CHARS else msg.text[:MAX_MESSAGE_CHARS] + " […]"
    subject = msg.meta.get("subject") if msg.channel == "email" else None
    head = f"[{when} · {msg.channel} · {author}]"
    if subject:
        head += f" (subject: {subject})"
    return f"{head} {text}"


def format_history(messages: Sequence[Message], tz: ZoneInfo, owner_name: str) -> str:
    if not messages:
        return "(no messages)"
    return "\n".join(format_message(m, tz, owner_name) for m in messages)


def format_style_samples(messages: Sequence[Message], limit_chars: int = 6000) -> str:
    lines: list[str] = []
    total = 0
    for m in reversed(messages):  # keep the most recent ones if we must cut
        line = f"- ({m.channel}) {m.text.strip()}"
        total += len(line)
        if total > limit_chars:
            break
        lines.append(line)
    return "\n".join(reversed(lines)) if lines else "(none yet)"
