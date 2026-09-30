import asyncio

import pytest

from ghostwriter.core import ActionError, Ghostwriter, SummaryRequest
from ghostwriter.storage import DraftStatus

from .conftest import FakeLLM, FakeNotifier, FakeSender, make_settings, ts

NO_ESCALATION = {"escalate": False, "category": "none", "reason": ""}


@pytest.fixture
def env(store, persona):
    def build(**overrides):
        settings = make_settings(**overrides)
        llm = FakeLLM()
        sender = FakeSender(store)
        core = Ghostwriter(settings, store, persona, llm, {"telegram": sender})
        notifier = FakeNotifier()
        core.notifier = notifier
        return core, llm, sender, notifier

    return build


async def incoming(store, text, minutes_ago=1.0, ext=None, author_id=200, name="Vladimir"):
    msg, created = await store.add_message(
        channel="telegram", direction="in", chat_id=200, text=text, author_name=name,
        author_id=author_id, external_id=ext or text, timestamp=ts(minutes_ago),
    )
    return msg, created


async def wait_tasks():
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_full_flow_draft_approve_send(env, store):
    core, llm, sender, notifier = env()
    await store.add_message(channel="telegram", direction="out", chat_id=200, text="ахах норм", author_name="Ivan", status="manual", timestamp=ts(10))
    llm.json_responses = [NO_ESCALATION, {"reply": "да, видел, жесть", "escalate": False, "note": ""}]

    m1, c1 = await incoming(store, "видел матч?", 2)
    m2, c2 = await incoming(store, "вот это был гол", 1)
    await core.on_message(m1, c1)
    await core.on_message(m2, c2)  # debounced: one draft for both
    await wait_tasks()

    assert len(notifier.cards) == 1
    draft = notifier.cards[0]
    assert draft.status == DraftStatus.PENDING and draft.source_message_id == m2.id
    drafter_prompt = llm.calls[1][2]
    assert "видел матч?" in drafter_prompt and "вот это был гол" in drafter_prompt
    assert "ахах норм" in drafter_prompt  # owner's own messages used as style reference
    assert "Ivan" in llm.calls[1][1]

    d = await core.approve(draft.id)
    assert d.status == DraftStatus.QUEUED
    await wait_tasks()
    assert sender.sent == ["да, видел, жесть"]
    final = await store.get_draft(draft.id)
    assert final.status == DraftStatus.SENT and final.sent_message_id is not None
    with pytest.raises(ActionError):
        await core.approve(draft.id)


async def test_edit_sends_owner_text(env, store):
    core, llm, sender, notifier = env()
    llm.json_responses = [NO_ESCALATION, {"reply": "draft", "escalate": False, "note": ""}]
    m, c = await incoming(store, "как дела?")
    await core.on_message(m, c)
    await wait_tasks()
    d = await core.approve(notifier.cards[0].id, "мой текст")
    assert d.edited and d.final_text == "мой текст"
    await wait_tasks()
    assert sender.sent == ["мой текст"]


async def test_sensitive_message_is_escalated(env, store):
    core, llm, sender, notifier = env()
    llm.json_responses = [{"escalate": True, "category": "money", "reason": "asks for a loan"}]
    m, c = await incoming(store, "займёшь 500 евро?")
    await core.on_message(m, c)
    await wait_tasks()
    assert not notifier.cards
    draft, msgs = notifier.escalations[0]
    assert draft.status == DraftStatus.ESCALATED and "money" in draft.reason
    assert (await store.get_message(m.id)).status == "escalated"
    # owner writes the answer himself via "Edit"
    await core.approve(draft.id, "не, сейчас не могу")
    await wait_tasks()
    assert sender.sent == ["не, сейчас не могу"]


async def test_bot_question_forced_escalation_without_llm(env, store):
    core, llm, sender, notifier = env()
    m, c = await incoming(store, "слушай, это ты или бот отвечает?")
    await core.on_message(m, c)
    await wait_tasks()
    assert notifier.escalations and llm.calls == []


async def test_drafter_can_escalate(env, store):
    core, llm, sender, notifier = env()
    llm.json_responses = [NO_ESCALATION, {"reply": "", "escalate": True, "note": "need to know his plans"}]
    m, c = await incoming(store, "что у тебя нового на работе?")
    await core.on_message(m, c)
    await wait_tasks()
    assert notifier.escalations and "drafter" in notifier.escalations[0][0].reason


async def test_other_chat_members_do_not_trigger(env, store):
    core, llm, sender, notifier = env()
    m, c = await incoming(store, "hi all", author_id=999, name="Petya")
    await core.on_message(m, c)
    await wait_tasks()
    assert llm.calls == [] and (await store.get_message(m.id)).status == "ignored"


async def test_manual_reply_supersedes_pending(env, store):
    core, llm, sender, notifier = env()
    llm.json_responses = [NO_ESCALATION, {"reply": "draft", "escalate": False, "note": ""}]
    m, c = await incoming(store, "ну что?")
    await core.on_message(m, c)
    await wait_tasks()
    out, created = await store.add_message(channel="telegram", direction="out", chat_id=200, text="сам ответил", author_name="Ivan", status="manual", external_id="o1")
    await core.on_message(out, created)
    assert (await store.get_draft(notifier.cards[0].id)).status == DraftStatus.SUPERSEDED


async def test_skip_cancels_queued(env, store):
    core, llm, sender, notifier = env(reply_delay_seconds="3600")
    llm.json_responses = [NO_ESCALATION, {"reply": "draft", "escalate": False, "note": ""}]
    m, c = await incoming(store, "привет")
    await core.on_message(m, c)
    await wait_tasks()
    d = await core.approve(notifier.cards[0].id)
    assert d.status == DraftStatus.QUEUED
    await core.skip(d.id)
    await wait_tasks()
    assert sender.sent == [] and (await store.get_draft(d.id)).status == DraftStatus.SKIPPED


async def test_send_failure_marks_failed(env, store):
    core, llm, sender, notifier = env()
    sender.fail = True
    llm.json_responses = [NO_ESCALATION, {"reply": "draft", "escalate": False, "note": ""}]
    m, c = await incoming(store, "привет")
    await core.on_message(m, c)
    await wait_tasks()
    await core.approve(notifier.cards[0].id)
    await wait_tasks()
    assert (await store.get_draft(notifier.cards[0].id)).status == DraftStatus.FAILED


async def test_auto_mode_queues_without_approval(env, store):
    core, llm, sender, notifier = env(auto_mode=True)
    llm.json_responses = [NO_ESCALATION, {"reply": "ок", "escalate": False, "note": ""}]
    m, c = await incoming(store, "норм?")
    await core.on_message(m, c)
    await wait_tasks()
    assert sender.sent == ["ок"]


async def test_request_reply_in_my_style_with_instructions(env, store):
    core, llm, sender, notifier = env()
    await store.add_message(channel="telegram", direction="out", chat_id=200, text="давно", author_name="Ivan", status="manual", timestamp=ts(60), external_id="o")
    await incoming(store, "приедешь в субботу?", 5)
    llm.json_responses = [{"reply": "в эту субботу никак, давай позже", "escalate": False, "note": ""}]
    d = await core.request_reply("вежливо откажись")
    assert d.requested_by == "owner" and d.status == DraftStatus.PENDING
    assert "вежливо откажись" in llm.calls[0][2]
    assert "commitments" in (d.reason or "")  # sensitive-topic warning on the card
    assert notifier.cards[-1].id == d.id


async def test_request_reply_without_messages(env):
    core, *_ = env()
    with pytest.raises(ActionError):
        await core.request_reply()


async def test_regenerate_supersedes_old(env, store):
    core, llm, sender, notifier = env()
    llm.json_responses = [NO_ESCALATION, {"reply": "v1", "escalate": False, "note": ""}, {"reply": "v2", "escalate": False, "note": ""}]
    m, c = await incoming(store, "как ты?")
    await core.on_message(m, c)
    await wait_tasks()
    first = notifier.cards[0]
    second = await core.regenerate(first.id)
    assert second.text == "v2"
    assert (await store.get_draft(first.id)).status == DraftStatus.SUPERSEDED


async def test_summary_filters_by_author(env, store):
    core, llm, sender, notifier = env()
    await incoming(store, "от Вовы", 3)
    await store.add_message(channel="telegram", direction="out", chat_id=200, text="от меня", author_name="Ivan Petrov", status="manual", timestamp=ts(2), external_id="o")
    llm.text_responses = ["• summary"]
    out = await core.summarize(SummaryRequest(author="Vladimir"))
    assert out.endswith("• summary") and "1 сообщ." in out
    assert "от Вовы" in llm.calls[0][2] and "от меня" not in llm.calls[0][2]

    llm.text_responses = ["• mine"]
    await core.summarize(SummaryRequest(author="я"))
    assert "от меня" in llm.calls[1][2] and "от Вовы" not in llm.calls[1][2]

    missing = await core.summarize(SummaryRequest(author="Petya"))
    assert "не найдено" in missing and llm.calls[2:] == []


async def test_resume_queued_on_start(env, store):
    core, llm, sender, notifier = env()
    m, _ = await incoming(store, "привет")
    d = await store.create_draft(source_message_id=m.id, channel="telegram", text="hi", final_text="hi", status=DraftStatus.QUEUED, scheduled_at=ts(5))
    core2, *_ = env()
    core2.senders["telegram"] = sender
    await core2.start()
    core2.queue.schedule(d.id, ts(0))  # skip the resume jitter in the test
    await wait_tasks()
    assert sender.sent == ["hi"]


async def test_catch_up_drafts_for_unanswered_tail(env, store):
    from ghostwriter.app import catch_up

    core, llm, sender, notifier = env()

    class FakeTg:
        async def backfill(self, limit):
            out = []
            for i, (direction, text, author) in enumerate([("out", "last reply", 100), ("in", "missed 1", 200), ("in", "missed 2", 200)]):
                m, _ = await store.add_message(
                    channel="telegram", direction=direction, chat_id=200, text=text, author_name="x",
                    author_id=author, external_id=f"b{i}", timestamp=ts(10 - i),
                    status="manual" if direction == "out" else "ignored",
                )
                out.append(m)
            return out

    llm.json_responses = [NO_ESCALATION, {"reply": "sorry, was busy", "escalate": False, "note": ""}]
    assert await catch_up(core, FakeTg(), 10) == 3
    await wait_tasks()
    assert notifier.cards and "missed 1" in llm.calls[1][2] and "missed 2" in llm.calls[1][2]
