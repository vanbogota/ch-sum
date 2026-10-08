"""Drafts replies in the owner's voice."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..persona import Persona
from ..storage.models import Message
from .client import LLM
from .prompts import format_history, format_message, format_style_samples

log = logging.getLogger(__name__)

CHANNEL_GUIDANCE = {
    "telegram": (
        "This is a Telegram chat: keep it short and casual like a messenger reply, usually one to three "
        "sentences, unless the style examples show otherwise. No greeting or sign-off."
    ),
    "email": (
        "This is an email reply: it can be longer and more structured than a chat message, with a short "
        "greeting and sign-off in the owner's usual manner. Write only the body (no subject line, no quoted text)."
    ),
}

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "The message text to send, or empty if escalating."},
        "escalate": {
            "type": "boolean",
            "description": "True if a good reply would require facts, decisions or commitments you do not have.",
        },
        "note": {"type": "string", "description": "Short note for the owner (why escalated, or what you assumed)."},
    },
    "required": ["reply", "escalate", "note"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class DraftResult:
    text: str
    escalate: bool
    note: str


class Drafter:
    def __init__(self, llm: LLM, persona: Persona, tz: ZoneInfo, owner_name: str, contact_name: str) -> None:
        self.llm = llm
        self.persona = persona
        self.tz = tz
        self.owner_name = owner_name
        self.contact_name = contact_name

    def system_prompt(self, contact_name: str | None = None, contact_notes: str = "") -> str:
        o, c = self.owner_name, contact_name or self.contact_name
        return f"""You are ghostwriting messages that {o} sends to {c}. The reply is sent from {o}'s own account and must read as if {o} typed it himself.

<about_owner>
{self.persona.context or "(not provided)"}
</about_owner>

<about_contact>
{contact_notes or f"(no notes about {c}: don't assume anything about the relationship beyond the conversation)"}
</about_contact>

<style_examples>
Real messages {o} wrote. Match their tone, length, punctuation, capitalisation, emoji use, slang and language:
{self.persona.examples or "(not provided)"}
</style_examples>

Rules:
- Never invent facts about {o}'s life, plans, whereabouts, feelings, other people or commitments. Use only what is in about_owner and the conversation. If a reply needs something you don't know, either keep it vague and friendly without making anything up, or set escalate=true.
- Never agree to meetings, calls, dates, payments or any other commitment. Never say or hint that you are an AI or an assistant.
- Reply in the language used in the latest messages you are answering.
- Reply to what is actually new; don't repeat what {o} already said in the history.
- Output JSON only."""

    async def draft(
        self,
        *,
        source: Message,
        new_messages: Sequence[Message],
        history: Sequence[Message],
        style_samples: Sequence[Message],
        instructions: str | None = None,
        contact_name: str | None = None,
        contact_notes: str = "",
    ) -> DraftResult:
        guidance = CHANNEL_GUIDANCE.get(source.channel, "")
        parts = [
            f"More recent examples of how {self.owner_name} writes (typed by hand):\n{format_style_samples(style_samples)}",
            f"Conversation so far (both channels, oldest first):\n{format_history(history, self.tz, self.owner_name)}",
            "Messages to reply to:\n" + "\n".join(format_message(m, self.tz, self.owner_name) for m in new_messages),
            f"Reply channel: {source.channel}. {guidance}",
        ]
        if instructions:
            parts.append(
                f"{self.owner_name}'s instructions for this reply (follow them; they override the defaults): {instructions}"
            )
        result = await self.llm.json(self.system_prompt(contact_name, contact_notes), "\n\n".join(parts), SCHEMA, max_tokens=3000, purpose="draft")
        text = str(result.get("reply", "")).strip()
        escalate = bool(result.get("escalate")) or not text
        return DraftResult(text=text, escalate=escalate, note=str(result.get("note", "")).strip())
