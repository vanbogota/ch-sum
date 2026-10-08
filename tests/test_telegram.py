from datetime import UTC, datetime

from telethon.tl import types

from ghostwriter.channels.telegram import message_text


def tg_message(text, media=None):
    return types.Message(
        id=1, peer_id=types.PeerUser(200), date=datetime.now(UTC), message=text, media=media
    )


def test_plain_text():
    assert message_text(tg_message("привет")) == "привет"


def test_photo_with_caption():
    photo = types.MessageMediaPhoto(
        photo=types.Photo(id=1, access_hash=0, file_reference=b"", date=datetime.now(UTC), sizes=[], dc_id=2)
    )
    assert message_text(tg_message("смотри", photo)) == "[photo] смотри"


def test_geo():
    geo = types.MessageMediaGeo(geo=types.GeoPoint(long=24.9, lat=60.1, access_hash=0))
    assert message_text(tg_message("", geo)) == "[location]"
