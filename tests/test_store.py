from ghostwriter.storage import Direction

from .conftest import ts


async def test_duplicates_are_ignored(store):
    m1, c1 = await store.add_message(channel="telegram", direction="in", chat_id=200, text="hi", author_name="V", external_id=1)
    m2, c2 = await store.add_message(channel="telegram", direction="in", chat_id=200, text="hi", author_name="V", external_id=1)
    assert c1 and not c2 and m1.id == m2.id


async def test_timestamps_are_utc_aware(store):
    m, _ = await store.add_message(channel="telegram", direction="in", chat_id=1, text="x", author_name="V", external_id=5)
    loaded = await store.get_message(m.id)
    assert loaded.timestamp.tzinfo is not None


async def test_find_messages_by_author_and_order(store):
    await store.add_message(channel="telegram", direction="in", chat_id=1, text="a", author_name="Vladimir Ivanov", author_id=200, timestamp=ts(30))
    await store.add_message(channel="telegram", direction="out", chat_id=1, text="b", author_name="Ivan", author_id=100, timestamp=ts(20))
    await store.add_message(channel="email", direction="in", chat_id="v@x", text="c", author_name="Vladimir", author_id="v@x", timestamp=ts(10))
    msgs = await store.find_messages(author="vladimir")
    assert [m.text for m in msgs] == ["a", "c"]
    assert [m.text for m in await store.find_messages(author="200")] == ["a"]
    assert [m.text for m in await store.find_messages(since=ts(15))] == ["c"]
    assert [m.text for m in await store.recent_history(2)] == ["b", "c"]
    assert await store.participants() == ["Ivan", "Vladimir", "Vladimir Ivanov"]


async def test_style_samples_exclude_bot_sent(store):
    await store.add_message(channel="telegram", direction=Direction.OUT, chat_id=1, text="typed", author_name="Ivan", status="manual", timestamp=ts(5))
    await store.add_message(channel="telegram", direction=Direction.OUT, chat_id=1, text="botsent", author_name="Ivan", status="sent", timestamp=ts(4))
    assert [m.text for m in await store.owner_style_samples(10)] == ["typed"]


async def test_kv(store):
    assert await store.kv_get("k") is None
    await store.kv_set("k", "1")
    await store.kv_set("k", "2")
    assert await store.kv_get("k") == "2"
