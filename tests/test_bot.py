from ghostwriter.control.bot import ControlBot, DraftCb, keyboard
from ghostwriter.control.render import render_card
from ghostwriter.core import Ghostwriter
from ghostwriter.storage import DraftStatus

from .conftest import FakeLLM, make_settings, ts


async def _draft(store, **kw):
    m, _ = await store.add_message(channel="telegram", direction="in", chat_id=200, text="<b>hi</b> & bye", author_name="V", author_id=200, timestamp=ts(1))
    return await store.create_draft(source_message_id=m.id, channel="telegram", text="ok <3", **kw)


async def test_card_escapes_html_and_buttons(store, settings):
    d = await _draft(store)
    card = render_card(d, settings.tz, "Vladimir")
    assert "&lt;b&gt;hi&lt;/b&gt; &amp; bye" in card and "ok &lt;3" in card
    kb = keyboard(d)
    actions = [DraftCb.unpack(b.callback_data).action for row in kb.inline_keyboard for b in row]
    assert actions == ["send", "edit", "skip", "regen"]


async def test_keyboard_by_status(store):
    assert keyboard(await _draft(store, status=DraftStatus.SENT)) is None
    queued = keyboard(await _draft(store, status=DraftStatus.QUEUED))
    assert [DraftCb.unpack(b.callback_data).action for row in queued.inline_keyboard for b in row] == ["now", "skip"]


async def test_control_bot_builds(store, persona):
    settings = make_settings()
    core = Ghostwriter(settings, store, persona, FakeLLM(), {})
    bot = ControlBot(settings, core)
    assert bot.owner == 100
    await bot.bot.session.close()


async def test_route_extracts_chat(settings):
    from ghostwriter.llm.analyst import ROUTE_SCHEMA, Analyst

    assert "chat" in ROUTE_SCHEMA["required"]
    llm = FakeLLM()
    llm.json_responses = [{"action": "summary", "chat": "Работа", "author": None, "limit": None, "since_hours": 24, "instructions": None}]
    intent = await Analyst(llm, settings.tz, "Ivan", "Vladimir").route("саммари группы Работа за день", ["Анна"], "Vladimir")
    assert (intent.action, intent.chat, intent.since_hours) == ("summary", "Работа", 24)
    assert "current chat: Vladimir" in llm.calls[0][1]


def test_chooser_buttons():
    from ghostwriter.chats import ChatRef
    from ghostwriter.control.bot import SelectCb, chooser

    kb = chooser([ChatRef(-1, "Работа", "group"), ChatRef(5, "Petya", "user", 5)], "chat")
    data = [SelectCb.unpack(row[0].callback_data) for row in kb.inline_keyboard]
    assert [(d.target, d.chat_id) for d in data] == [("chat", -1), ("chat", 5)]
    assert kb.inline_keyboard[0][0].text == "👥 Работа"
