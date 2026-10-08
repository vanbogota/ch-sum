"""Switching the contact and the current chat from the bot."""
import asyncio

import pytest

from ghostwriter.chats import ChatRef
from ghostwriter.core import ActionError, Ghostwriter, SummaryRequest
from ghostwriter.storage import DraftStatus

from .conftest import FakeChats, FakeLLM, FakeNotifier, FakeSender, make_settings, ts

VLAD = ChatRef(200, "Vladimir", "user", 200)
PETYA = ChatRef(300, "Petya", "user", 300)
WORK = ChatRef(-100500, "Работа", "group", None)
NO_ESCALATION = {"escalate": False, "category": "none", "reason": ""}


def msg(chat_id, text, author_id, name, minutes_ago, ext, direction="in", status="ignored"):
    return dict(channel="telegram", direction=direction, chat_id=chat_id, text=text, author_id=author_id,
                author_name=name, external_id=ext, timestamp=ts(minutes_ago), status=status)


@pytest.fixture
async def env(store, persona):
    async def build(history=None, **overrides):
        chats = FakeChats([VLAD, PETYA, WORK], history)
        chats.store = store
        llm, sender, notifier = FakeLLM(), FakeSender(store), FakeNotifier()
        core = Ghostwriter(make_settings(**overrides), store, persona, llm, {"telegram": sender}, chats=chats)
        await core.load_state()
        core.notifier = notifier
        return core, llm, sender, notifier, chats

    return build


async def wait_tasks():
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_default_contact_from_env(env):
    core, *_, chats = await env()
    assert core.contact == VLAD and core.current == VLAD
    assert chats.watched == {200}


async def test_switch_contact_moves_auto_drafts(env, store):
    core, llm, sender, notifier, chats = await env()
    await core.set_contact(PETYA)
    assert core.contact.chat_id == 300 and core.current.chat_id == 300 and chats.watched == {300}
    assert chats.backfilled == [300]

    vlad, c1 = await store.add_message(**msg(200, "привет от Вовы", 200, "Vladimir", 2, "v1", status="new"))
    await core.on_message(vlad, c1)
    llm.json_responses = [NO_ESCALATION, {"reply": "здорово, Петь", "escalate": False, "note": ""}]
    petya, c2 = await store.add_message(**msg(300, "здорово", 300, "Petya", 1, "p1", status="new"))
    await core.on_message(petya, c2)
    await wait_tasks()

    assert (await store.get_message(vlad.id)).status == "ignored"
    assert len(notifier.cards) == 1 and notifier.cards[0].source.chat_id == "300"
    assert "Petya" in llm.calls[1][1]  # drafter writes to Petya now
    await core.approve(notifier.cards[0].id)
    await wait_tasks()
    assert sender.sent_to == ["300"]


async def test_selection_survives_restart(env):
    core, *_ = await env()
    await core.set_contact(PETYA)
    await core.set_current(WORK)
    core2, *_ = await env()
    assert core2.contact.chat_id == 300 and core2.current.chat_id == WORK.chat_id


async def test_group_cannot_be_contact(env):
    core, *_ = await env()
    with pytest.raises(ActionError):
        await core.set_contact(WORK)


async def test_group_summary_keeps_contact_drafts(env, store):
    history = {WORK.chat_id: [
        msg(WORK.chat_id, "релиз в пятницу", 11, "Анна", 30, "w1"),
        msg(WORK.chat_id, "ок, тестирую", 12, "Олег", 20, "w2"),
    ]}
    core, llm, sender, notifier, chats = await env(history)
    await store.add_message(**msg(200, "личное от Вовы", 200, "Vladimir", 10, "v1"))
    await core.set_current(WORK)
    assert chats.watched == {200, WORK.chat_id}

    llm.text_responses = ["• релиз в пятницу"]
    out = await core.summarize(SummaryRequest())
    assert "Работа" in out and "2 сообщ." in out
    prompt = llm.calls[0][2]
    assert "релиз в пятницу" in prompt and "личное от Вовы" not in prompt
    assert core.contact == VLAD  # auto-drafts still for Vladimir


async def test_one_off_summary_of_other_chat(env, store):
    history = {WORK.chat_id: [msg(WORK.chat_id, "созвон в 10", 11, "Анна", 30, "w1")]}
    core, llm, *_ = await env(history)
    llm.text_responses = ["• созвон"]
    await core.summarize(SummaryRequest(), chat=WORK)
    assert "созвон в 10" in llm.calls[0][2]
    assert core.current == VLAD  # the current chat did not change


async def test_reply_in_group_goes_to_group(env, store):
    history = {WORK.chat_id: [
        msg(WORK.chat_id, "кто возьмёт задачу?", 11, "Анна", 5, "w1"),
    ]}
    core, llm, sender, notifier, chats = await env(history)
    await core.set_current(WORK)
    llm.json_responses = [{"reply": "могу взять", "escalate": False, "note": ""}]
    d = await core.request_reply()
    assert d.source.chat_id == str(WORK.chat_id)
    assert "Работа" in llm.calls[0][1]
    await core.approve(d.id)
    await wait_tasks()
    assert sender.sent_to == [str(WORK.chat_id)]
    assert (await store.get_draft(d.id)).status == DraftStatus.SENT


async def test_manual_reply_in_group_does_not_touch_contact_drafts(env, store):
    core, llm, sender, notifier, chats = await env()
    llm.json_responses = [NO_ESCALATION, {"reply": "draft", "escalate": False, "note": ""}]
    m, c = await store.add_message(**msg(200, "ну что?", 200, "Vladimir", 1, "v1", status="new"))
    await core.on_message(m, c)
    await wait_tasks()
    out, created = await store.add_message(**msg(WORK.chat_id, "в группе", 100, "Ivan", 0, "o1", direction="out", status="manual"))
    await core.on_message(out, created)
    assert (await store.get_draft(notifier.cards[0].id)).status == DraftStatus.PENDING


async def test_no_contact_configured(store, persona):
    core = Ghostwriter(make_settings(vladimir_tg_id=""), store, persona, FakeLLM(), {})
    await core.load_state()
    assert core.contact is None and core.current is None
    with pytest.raises(ActionError):
        await core.summarize(SummaryRequest())
