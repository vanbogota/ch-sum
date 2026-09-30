import random
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from ghostwriter.config import parse_hours, parse_range
from ghostwriter.scheduling import ActiveHours, compute_send_time, typing_seconds

from .conftest import make_settings

HEL = ZoneInfo("Europe/Helsinki")


def test_parse_range():
    assert parse_range("60-600") == (60, 600)
    assert parse_range("5") == (5, 5)
    with pytest.raises(ValueError):
        parse_range("10-5")


def test_parse_hours():
    assert parse_hours("09:00-23:00") == (time(9), time(23))


def test_settings_watched_chat():
    assert make_settings().watched_chat == 200
    assert make_settings(tg_chat="-100123").watched_chat == -100123
    assert make_settings(tg_chat="@somegroup").watched_chat == "@somegroup"
    assert make_settings(tg_chat="").watched_chat == 200


def test_settings_validation():
    with pytest.raises(ValueError):
        make_settings(active_hours="9-23")
    with pytest.raises(ValueError):
        make_settings(timezone="Mars/Olympus")


def test_email_enabled_requires_all():
    assert not make_settings(imap_host="imap.x").email_enabled
    assert make_settings(imap_host="imap.x", smtp_host="smtp.x", vladimir_email="v@x.org").email_enabled


def test_active_hours_simple():
    h = ActiveHours(time(9), time(23), HEL)
    assert h.is_active(datetime(2026, 9, 30, 12, 0, tzinfo=HEL))
    assert not h.is_active(datetime(2026, 9, 30, 23, 30, tzinfo=HEL))
    assert not h.is_active(datetime(2026, 9, 30, 3, 0, tzinfo=HEL))
    assert h.next_start(datetime(2026, 9, 30, 23, 30, tzinfo=HEL)) == datetime(2026, 10, 1, 9, 0, tzinfo=HEL)
    assert h.next_start(datetime(2026, 9, 30, 3, 0, tzinfo=HEL)) == datetime(2026, 9, 30, 9, 0, tzinfo=HEL)


def test_active_hours_cross_midnight():
    h = ActiveHours(time(22), time(2), HEL)
    assert h.is_active(datetime(2026, 9, 30, 23, 0, tzinfo=HEL))
    assert h.is_active(datetime(2026, 9, 30, 1, 0, tzinfo=HEL))
    assert not h.is_active(datetime(2026, 9, 30, 12, 0, tzinfo=HEL))


def test_compute_send_time_within_hours():
    h = ActiveHours(time(9), time(23), HEL)
    now = datetime(2026, 9, 30, 12, 0, tzinfo=HEL)
    at = compute_send_time(now, (60, 600), h, random.Random(1))
    assert 60 <= (at - now).total_seconds() <= 600


def test_compute_send_time_quiet_hours_queues_until_morning():
    h = ActiveHours(time(9), time(23), HEL)
    now = datetime(2026, 9, 30, 22, 58, tzinfo=HEL)
    at = compute_send_time(now, (300, 300), h, random.Random(1)).astimezone(HEL)
    assert at.date().day == 1 and at.hour == 9 and at.minute <= 20


def test_typing_seconds_bounds():
    assert typing_seconds("hi") == 2.0
    assert typing_seconds("x" * 10_000) == 25.0
