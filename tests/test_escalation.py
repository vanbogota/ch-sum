from ghostwriter.llm.escalation import EscalationChecker, keyword_hits
from ghostwriter.storage import Message

from .conftest import FakeLLM, ts


def names(persona, text):
    return [c.name for c in keyword_hits(persona.categories, text)]


def test_categories_loaded(persona):
    assert {c.name for c in persona.categories} == {"money", "commitments", "emotional", "ai_suspicion"}


def test_keyword_hits(persona):
    assert names(persona, "Ты что, бот?") == ["ai_suspicion"]
    assert names(persona, "Купил новые ботинки") == []
    assert names(persona, "Займёшь денег до пятницы?") == ["money"]
    assert names(persona, "давай созвонимся") == ["commitments"]
    assert names(persona, "Are you using ChatGPT?") == ["ai_suspicion"]
    assert names(persona, "как работа?") == []


def _msg(text):
    return Message(id=1, channel="telegram", direction="in", chat_id="1", text=text, author_name="V", meta={}, timestamp=ts(1))


async def test_forced_category_skips_llm(persona, settings):
    llm = FakeLLM()
    checker = EscalationChecker(llm, persona.categories, settings.tz, "Ivan", "Vladimir")
    d = await checker.check([_msg("это бот отвечает?")], [])
    assert d.escalate and d.category == "ai_suspicion"
    assert llm.calls == []


async def test_llm_decides_with_hint(persona, settings):
    llm = FakeLLM()
    llm.json_responses = [{"escalate": False, "category": "none", "reason": "casual mention"}]
    checker = EscalationChecker(llm, persona.categories, settings.tz, "Ivan", "Vladimir")
    d = await checker.check([_msg("вчера заплатил 10 евро за кофе, жесть")], [])
    assert not d.escalate and d.category is None
    assert "money" in llm.calls[0][2]  # keyword hint passed to the classifier
