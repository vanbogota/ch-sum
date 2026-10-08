from ghostwriter.channels.email import build_reply, parse_email, reply_subject, strip_quoted

RAW = b"""From: Vladimir Petrov <Vlad@Example.org>
To: ivan@example.com
Subject: Re: football
Date: Tue, 29 Sep 2026 18:30:00 +0300
Message-ID: <abc@example.org>
In-Reply-To: <prev@example.com>
References: <root@example.com> <prev@example.com>
Content-Type: text/plain; charset=utf-8
Content-Transfer-Encoding: 8bit

\xd0\x9f\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82! Did you watch the match?

On Mon, 28 Sep 2026 Ivan <ivan@example.com> wrote:
> old stuff
"""


def test_parse_email():
    p = parse_email(RAW)
    assert p.from_addr == "vlad@example.org"
    assert p.from_name == "Vladimir Petrov"
    assert p.message_id == "<abc@example.org>"
    assert p.references == ["<root@example.com>", "<prev@example.com>"]
    assert p.body == "Привет! Did you watch the match?"
    assert p.date.tzinfo is not None


def test_strip_quoted_russian_header():
    text = "Ок, договорились\n\n29 сент. 2026 г., в 18:30, Иван <i@x> написал:\n> ..."
    assert strip_quoted(text) == "Ок, договорились"


def test_reply_subject():
    assert reply_subject("football") == "Re: football"
    assert reply_subject("RE: football") == "RE: football"


def test_build_reply_threading():
    msg = build_reply(
        from_addr="ivan@example.com", to_addr="vlad@example.org", body="Yes!",
        subject="Re: football", in_reply_to="<abc@example.org>",
        references=["<root@example.com>", "<prev@example.com>"],
    )
    assert msg["In-Reply-To"] == "<abc@example.org>"
    assert msg["References"] == "<root@example.com> <prev@example.com> <abc@example.org>"
    assert msg["Subject"] == "Re: football"
    assert msg["Message-ID"].endswith("@example.com>")
