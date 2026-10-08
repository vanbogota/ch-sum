"""Decides whether an incoming message is too sensitive to draft automatically."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..persona import EscalationCategory
from ..storage.models import Message
from .client import LLM
from .prompts import format_history, format_message

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EscalationDecision:
    escalate: bool
    category: str | None = None
    reason: str = ""


SCHEMA = {
    "type": "object",
    "properties": {
        "escalate": {"type": "boolean"},
        "category": {"type": "string", "description": "Category name, or 'none'."},
        "reason": {"type": "string", "description": "One short sentence for the owner."},
    },
    "required": ["escalate", "category", "reason"],
    "additionalProperties": False,
}


def keyword_hits(categories: Sequence[EscalationCategory], text: str) -> list[EscalationCategory]:
    return [c for c in categories if c.matches(text)]


class EscalationChecker:
    def __init__(
        self,
        llm: LLM,
        categories: Sequence[EscalationCategory],
        tz: ZoneInfo,
        owner_name: str,
        contact_name: str,
        max_chars: int = 700,
    ) -> None:
        self.max_chars = max_chars
        self.llm = llm
        self.categories = tuple(categories)
        self.tz = tz
        self.owner_name = owner_name
        self.contact_name = contact_name

    def _system(self, contact_name: str) -> str:
        cats = "\n".join(f"- {c.name}: {c.description}" for c in self.categories) or "- (none configured)"
        return (
            f"You screen messages that {contact_name} sends to {self.owner_name}. An assistant drafts "
            f"replies on {self.owner_name}'s behalf, but it must NOT reply on its own to anything in these "
            f"categories; those are escalated to {self.owner_name}:\n{cats}\n\n"
            "Judge the NEW messages in the context of the recent conversation. Escalate if any new message "
            "touches a category, even indirectly or as a follow-up (e.g. 'so, are you in?' after an invitation). "
            "Casual mentions that need no decision (e.g. 'I paid for lunch yesterday, it was great') do not need "
            "escalation. When unsure, escalate. Answer with JSON only."
        )

    async def check(
        self, new_messages: Sequence[Message], history: Sequence[Message], contact_name: str | None = None
    ) -> EscalationDecision:
        text = "\n".join(m.text for m in new_messages)
        hits = keyword_hits(self.categories, text)
        forced = [c for c in hits if c.force]
        if forced:
            return EscalationDecision(True, forced[0].name, f"keyword match ({forced[0].name})")

        hint = f"\nKeyword pre-filter matched: {', '.join(c.name for c in hits)} (may be false positives)." if hits else ""
        user = (
            f"Recent conversation:\n{format_history(history, self.tz, self.owner_name, self.max_chars)}\n\n"
            f"NEW messages to screen:\n"
            + "\n".join(format_message(m, self.tz, self.owner_name, self.max_chars * 2) for m in new_messages)
            + hint
        )
        result = await self.llm.json(self._system(contact_name or self.contact_name), user, SCHEMA, max_tokens=1000, purpose="escalation")
        escalate = bool(result.get("escalate"))
        category = result.get("category") or None
        if category == "none":
            category = None
        log.info("escalation check: escalate=%s category=%s", escalate, category)
        return EscalationDecision(escalate, category, str(result.get("reason", "")))
