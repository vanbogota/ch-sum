from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ghostwriter.config import Settings
from ghostwriter.persona import Persona
from ghostwriter.storage import Channel, Direction, Draft, Message, Store

ROOT = Path(__file__).resolve().parent.parent


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        tg_api_id=1,
        tg_api_hash="hash",
        control_bot_token="123:abc",
        owner_tg_id=100,
        vladimir_tg_id=200,
        persona_dir=ROOT / "persona",
        database_url="sqlite+aiosqlite:///:memory:",
        debounce_seconds=0,
        active_hours="00:00-00:00",  # always active
        reply_delay_seconds="0",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class FakeLLM:
    """Returns scripted JSON / text responses and records prompts."""

    def __init__(self) -> None:
        self.json_responses: list[dict[str, Any]] = []
        self.text_responses: list[str] = []
        self.calls: list[tuple[str, str, str]] = []
        self.purposes: list[str] = []

    async def json(self, system: str, user: str, schema: dict[str, Any], max_tokens: int = 0, purpose: str = "") -> dict[str, Any]:
        self.calls.append(("json", system, user))
        self.purposes.append(purpose)
        return self.json_responses.pop(0)

    async def text(self, system: str, user: str, max_tokens: int = 0, purpose: str = "") -> str:
        self.calls.append(("text", system, user))
        self.purposes.append(purpose)
        return self.text_responses.pop(0)


class FakeSender:
    def __init__(self, store: Store, channel: str = Channel.TELEGRAM) -> None:
        self.store = store
        self.channel = channel
        self.sent: list[str] = []
        self.sent_to: list[str | None] = []
        self.fail = False

    async def send(self, text: str, reply_to: Message | None) -> Message:
        if self.fail:
            raise RuntimeError("network down")
        self.sent.append(text)
        self.sent_to.append(reply_to.chat_id if reply_to else None)
        msg, _ = await self.store.add_message(
            channel=self.channel, direction=Direction.OUT, chat_id=reply_to.chat_id if reply_to else "200", text=text,
            author_name="Ivan", external_id=f"sent-{len(self.sent)}", status="sent",
        )
        return msg


class FakeChats:
    """Stands in for the Telegram gateway's chat functions."""

    def __init__(self, refs, history=None) -> None:
        self.refs = {r.chat_id: r for r in refs}
        self.history = history or {}  # chat_id -> list of add_message kwargs, "imported" on backfill
        self.store: Store | None = None
        self.watched: set[int] = set()
        self.backfilled: list[int] = []

    async def backfill(self, chat_id, limit, since=None):
        self.backfilled.append(chat_id)
        out = []
        for kw in self.history.get(chat_id, []):
            m, created = await self.store.add_message(**kw)
            if created:
                out.append(m)
        return out

    async def search_chats(self, query, kinds=None, limit=10):
        q = (query or "").casefold()
        return [r for r in self.refs.values() if (not kinds or r.kind in kinds) and q in r.title.casefold()][:limit]

    async def chat_ref(self, chat):
        return self.refs[int(chat)]


class FakeNotifier:
    def __init__(self) -> None:
        self.cards: list[Draft] = []
        self.refreshed: list[Draft] = []
        self.escalations: list[tuple[Draft, Sequence[Message]]] = []
        self.infos: list[str] = []

    async def draft_card(self, draft: Draft) -> None:
        self.cards.append(draft)

    async def refresh_card(self, draft: Draft) -> None:
        self.refreshed.append(draft)

    async def escalation(self, draft: Draft, messages: Sequence[Message]) -> None:
        self.escalations.append((draft, messages))

    async def info(self, text: str) -> None:
        self.infos.append(text)


@pytest.fixture
async def store() -> Store:
    s = Store("sqlite+aiosqlite:///:memory:")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def persona() -> Persona:
    return Persona.load(ROOT / "persona")


def ts(minutes_ago: float) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_ago)
