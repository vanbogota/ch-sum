"""Owner-facing analysis of the conversation: summaries, questions and natural-language command routing."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from ..storage.models import Message
from .client import LLM
from .prompts import format_history

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["summary", "reply", "question", "help"],
            "description": "summary: summarise messages; reply: draft a reply to the contact; "
            "question: answer a question about the chat; help: unclear request.",
        },
        "author": {
            "type": ["string", "null"],
            "description": "Whose messages to look at (name as the owner wrote it), null for everyone.",
        },
        "limit": {"type": ["integer", "null"], "description": "Number of latest messages, if stated."},
        "since_hours": {"type": ["number", "null"], "description": "Time window in hours, if stated (a day = 24)."},
        "instructions": {
            "type": ["string", "null"],
            "description": "For reply: what the reply should say or how. For question/summary: the question or focus.",
        },
    },
    "required": ["action", "author", "limit", "since_hours", "instructions"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Intent:
    action: str
    author: str | None = None
    limit: int | None = None
    since_hours: float | None = None
    instructions: str | None = None


class Analyst:
    def __init__(self, llm: LLM, tz: ZoneInfo, owner_name: str, contact_name: str) -> None:
        self.llm = llm
        self.tz = tz
        self.owner_name = owner_name
        self.contact_name = contact_name

    async def route(self, command: str, participants: Sequence[str]) -> Intent:
        system = (
            f"You turn {self.owner_name}'s requests to his personal assistant into a structured command. "
            f"The assistant watches {self.owner_name}'s chat with {self.contact_name} and can summarise it, "
            f"answer questions about it, or draft a reply in {self.owner_name}'s style. "
            "Requests may be in Russian or English. "
            f"'me'/'мои'/'я' refers to {self.owner_name}. Known chat participants: {', '.join(participants) or 'unknown'}; "
            "map names and their inflected forms (e.g. 'от Володи' -> Vladimir) to one of them. JSON only."
        )
        r = await self.llm.json(system, command, ROUTE_SCHEMA, max_tokens=800, purpose="route")
        limit = r.get("limit")
        since = r.get("since_hours")
        return Intent(
            action=r.get("action", "help"),
            author=r.get("author") or None,
            limit=int(limit) if isinstance(limit, (int, float)) and limit > 0 else None,
            since_hours=float(since) if isinstance(since, (int, float)) and since > 0 else None,
            instructions=r.get("instructions") or None,
        )

    def _system(self) -> str:
        return (
            f"You are {self.owner_name}'s private assistant. You read his conversation with {self.contact_name} "
            "(Telegram and email) and report to him. Be concise and concrete, cite dates when useful, and never "
            "invent anything that is not in the messages. Answer in the language of his request. "
            "Use plain text with short bullet points (the output is shown in Telegram)."
        )

    async def summarize(self, messages: Sequence[Message], focus: str | None = None, author: str | None = None) -> str:
        what = f"messages from {author}" if author else "the conversation"
        ask = f"Summarise {what} below: main topics, questions or requests waiting for {self.owner_name}'s answer, "
        ask += "any plans, dates or promises mentioned, and the overall mood."
        if focus:
            ask += f"\nFocus / request: {focus}"
        user = f"{ask}\n\nMessages (oldest first):\n{format_history(messages, self.tz, self.owner_name)}"
        return await self.llm.text(self._system(), user, max_tokens=3000, purpose="summary")

    async def answer(self, question: str, messages: Sequence[Message]) -> str:
        user = (
            f"Question: {question}\n\nMessages (oldest first):\n"
            f"{format_history(messages, self.tz, self.owner_name)}"
        )
        return await self.llm.text(self._system(), user, max_tokens=3000, purpose="question")
