"""The MCP connector: tools work in-process, and the HTTP endpoint needs the token."""
import asyncio
import socket

import pytest
from mcp import Client

from ghostwriter.chats import ChatRef
from ghostwriter.core import Ghostwriter
from ghostwriter.llm import DisabledLLM, LLMError
from ghostwriter.mcp_server import build_http_app, build_server
from ghostwriter.storage import Direction

from .conftest import FakeChats, make_settings, ts

VLAD = ChatRef(200, "Vladimir", "user", 200)
WORK = ChatRef(-100500, "Работа", "group", None)
WORK2 = ChatRef(-100600, "Работа старая", "group", None)
TOKEN = "t" * 32


def msg(chat_id, text, author_id, name, minutes_ago, ext, direction="in"):
    return dict(channel="telegram", direction=direction, chat_id=chat_id, text=text, author_id=author_id,
                author_name=name, external_id=ext, timestamp=ts(minutes_ago), status="ignored")


class FakeTg(FakeChats):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sent: list[tuple[int, str]] = []

    async def send_to(self, chat_id, text):
        self.sent.append((chat_id, text))
        m, _ = await self.store.add_message(channel="telegram", direction=Direction.OUT, chat_id=chat_id,
                                            text=text, author_name="Ivan", external_id=f"s{len(self.sent)}", status="sent")
        return m

    async def search(self, chat_id, text, limit=30):
        await self.backfill(chat_id, 100)  # the real one searches Telegram itself
        return [m for m in await self.store.find_messages(scope=[("telegram", str(chat_id))], limit=None)
                if text.lower() in m.text.lower()][:limit]


@pytest.fixture
async def setup(store, persona):
    history = {
        VLAD.chat_id: [msg(200, "старое сообщение", 200, "Vladimir", 60 * 72, "v0"),
                       msg(200, "посмотри документ про подписку", 200, "Vladimir", 30, "v1"),
                       msg(200, "ок, гляну вечером", 100, "Ivan", 20, "o1", direction="out")],
        WORK.chat_id: [msg(WORK.chat_id, "релиз в пятницу", 11, "Анна", 10, "w1")],
    }
    tg = FakeTg([VLAD, WORK, WORK2], history)
    tg.store = store

    def build(**overrides):
        base = dict(control_bot_token="", anthropic_api_key="", mcp_enabled=True, mcp_token=TOKEN)
        settings = make_settings(**{**base, **overrides})
        core = Ghostwriter(settings, store, persona, DisabledLLM(), {"telegram": tg}, chats=tg)
        return core, settings

    return build, tg


def text_of(result):
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def test_read_tools(setup):
    build, tg = setup
    core, settings = build()
    await core.load_state()
    assert not core.auto_drafts
    async with Client(build_server(core, tg, settings)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names == {"list_chats", "get_messages", "search_messages", "get_persona", "send_message"}

        out = text_of(await client.call_tool("list_chats", {"query": "работа"}))
        assert "chat_id=-100500" in out and "chat_id=-100600" in out

        day = text_of(await client.call_tool("get_messages", {"chat": "Vladimir", "since_hours": 24}))
        assert "посмотри документ" in day and "гляну вечером" in day and "старое" not in day
        assert "2 messages" in day

        mine = text_of(await client.call_tool("get_messages", {"chat": "200", "author": "me"}))
        assert "гляну вечером" in mine and "посмотри документ" not in mine

        found = text_of(await client.call_tool("search_messages", {"chat": "-100500", "text": "релиз"}))
        assert "релиз в пятницу" in found

        persona = text_of(await client.call_tool("get_persona", {"chat": "Vladimir"}))
        assert "About the owner" in persona and "гляну вечером" in persona


async def test_ambiguous_chat_name_is_an_error(setup):
    build, tg = setup
    core, settings = build(mcp_allow_send=True)
    await core.load_state()
    async with Client(build_server(core, tg, settings)) as client:
        r = await client.call_tool("send_message", {"chat": "Рабо", "text": "привет"})
        assert r.is_error and "several chats" in text_of(r)
        assert tg.sent == []
        r = await client.call_tool("send_message", {"chat": "Работа", "text": "привет"})  # exact title
        assert not r.is_error and tg.sent == [(-100500, "привет")]


async def test_send_disabled_by_default(setup):
    build, tg = setup
    core, settings = build()
    await core.load_state()
    async with Client(build_server(core, tg, settings)) as client:
        r = await client.call_tool("send_message", {"chat": "200", "text": "привет"})
        assert r.is_error and "disabled" in text_of(r) and tg.sent == []


async def test_disabled_llm_explains():
    with pytest.raises(LLMError, match="MCP"):
        await DisabledLLM().text("s", "u")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_http_requires_token(setup):
    import httpx2
    import uvicorn

    build, tg = setup
    core, settings = build()
    await core.load_state()
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(build_http_app(build_server(core, tg, settings), settings),
                                           host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.05)
        base = f"http://127.0.0.1:{port}"
        async with httpx2.AsyncClient() as http:
            assert (await http.get(f"{base}/health")).status_code == 200
            assert (await http.post(f"{base}/mcp", json={})).status_code == 404
            assert (await http.post(f"{base}/wrongtoken/mcp", json={})).status_code == 404
        async with Client(f"{base}/{TOKEN}/mcp") as client:  # the URL form used by claude.ai connectors
            out = text_of(await client.call_tool("list_chats", {}))
            assert "Vladimir" in out
    finally:
        server.should_exit = True
        await task


def test_short_token_rejected(setup):
    build, tg = setup
    core, settings = build(mcp_token="short")
    with pytest.raises(ValueError, match="MCP_TOKEN"):
        build_http_app(build_server(core, tg, settings), settings)
