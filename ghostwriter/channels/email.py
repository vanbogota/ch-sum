"""Email channel: IMAP polling for incoming/sent mail, SMTP for replies. Standard library only."""
from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import logging
import re
import smtplib
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime

from ..config import Settings
from ..storage import Channel, Direction, Message, Store
from ..storage.models import MessageStatus

log = logging.getLogger(__name__)

OnMessage = Callable[[Message, bool], Awaitable[None]]

_QUOTE_HEADER = re.compile(
    r"^\s*(On .+ wrote:|.+ (написал|написала|пишет)\S*:|-{2,}\s*Original Message\s*-{2,}|-{2,}\s*Исходное сообщение\s*-{2,}|From: .+)\s*$",
    re.IGNORECASE,
)


@dataclass
class ParsedEmail:
    message_id: str
    subject: str
    from_addr: str
    from_name: str
    to_addrs: list[str]
    date: datetime
    body: str
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)


def strip_quoted(text: str) -> str:
    """Drop the quoted previous message and trailing signature separator from a reply body."""
    out: list[str] = []
    for line in text.splitlines():
        if _QUOTE_HEADER.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        if line.rstrip() == "--":  # signature delimiter
            break
        out.append(line)
    return "\n".join(out).strip()


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_email(raw: bytes) -> ParsedEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        body = body_part.get_content()
        if body_part.get_content_type() == "text/html":
            body = _html_to_text(body)
    from_list = getaddresses([str(msg.get("From", ""))])
    from_name, from_addr = from_list[0] if from_list else ("", "")
    to_addrs = [a.lower() for _, a in getaddresses([str(v) for v in msg.get_all("To", []) + msg.get_all("Cc", [])]) if a]
    try:
        date = parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else datetime.now(UTC)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        date = datetime.now(UTC)
    message_id = str(msg.get("Message-ID", "")).strip() or make_msgid()
    refs = str(msg.get("References", "")).split()
    in_reply_to = str(msg.get("In-Reply-To", "")).strip() or None
    return ParsedEmail(
        message_id=message_id,
        subject=str(msg.get("Subject", "")),
        from_addr=from_addr.lower(),
        from_name=from_name or from_addr,
        to_addrs=to_addrs,
        date=date,
        body=strip_quoted(body),
        in_reply_to=in_reply_to,
        references=refs,
    )


def reply_subject(subject: str) -> str:
    subject = subject.strip()
    return subject if re.match(r"^(re|ответ)\s*:", subject, re.IGNORECASE) else f"Re: {subject}".strip()


def build_reply(
    *,
    from_addr: str,
    to_addr: str,
    body: str,
    subject: str,
    in_reply_to: str | None,
    references: list[str],
) -> EmailMessage:
    """A reply with proper threading headers so it lands in the same thread."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = reply_subject(subject) if in_reply_to else (subject or "(no subject)")
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg["Date"] = email.utils.format_datetime(datetime.now(UTC))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = [r for r in references if r != in_reply_to] + [in_reply_to]
        msg["References"] = " ".join(refs[-20:])
    msg.set_content(body)
    return msg


def _quote_folder(name: str) -> str:
    return f'"{name}"' if " " in name and not name.startswith('"') else name


class EmailGateway:
    def __init__(self, settings: Settings, store: Store) -> None:
        if not settings.email_enabled:
            raise ValueError("email is not configured")
        self.settings = settings
        self.store = store
        self.contact = (settings.vladimir_email or "").lower()
        self.on_message: OnMessage | None = None

    # ------------------------------------------------------------------ IMAP

    def _imap(self) -> imaplib.IMAP4:
        s = self.settings
        conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port) if s.imap_port == 993 else imaplib.IMAP4(s.imap_host, s.imap_port)
        if s.imap_port != 993:
            conn.starttls(ssl.create_default_context())
        conn.login(s.imap_user or "", s.imap_password.get_secret_value() if s.imap_password else "")
        return conn

    def _fetch_folder(self, conn: imaplib.IMAP4, folder: str, criterion: str, last_uid: int | None) -> tuple[list[tuple[int, bytes]], int]:
        """Fetch messages in `folder` matching `criterion` with UID > last_uid. Returns (messages, new_last_uid)."""
        typ, _ = conn.select(_quote_folder(folder), readonly=True)
        if typ != "OK":
            log.warning("email: cannot open folder %s", folder)
            return [], last_uid or 0
        if last_uid is None:
            # First run: take the last month as history and start tracking from the current UIDNEXT.
            since = (datetime.now(UTC) - timedelta(days=30)).strftime("%d-%b-%Y")
            typ, data = conn.uid("SEARCH", None, criterion, "SINCE", since)
        else:
            typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*", criterion)
        uids = [int(u) for u in (data[0] or b"").split()] if typ == "OK" else []
        if last_uid is not None:
            uids = [u for u in uids if u > last_uid]  # "n:*" always matches the last message
        out: list[tuple[int, bytes]] = []
        for uid in uids:
            typ, parts = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")  # PEEK: don't mark as read
            if typ == "OK" and parts and isinstance(parts[0], tuple):
                out.append((uid, parts[0][1]))
        new_last = max([last_uid or 0, *uids])
        if last_uid is None:
            typ, status = conn.status(_quote_folder(folder), "(UIDNEXT)")
            m = re.search(rb"UIDNEXT (\d+)", status[0] or b"") if typ == "OK" else None
            if m:
                new_last = max(new_last, int(m.group(1)) - 1)
        return out, new_last

    def _poll_sync(self, markers: dict[str, int | None]) -> tuple[list[tuple[str, bytes]], dict[str, int]]:
        folders = [(self.settings.imap_inbox_folder, f'FROM "{self.contact}"')]
        if self.settings.imap_sent_folder:
            folders.append((self.settings.imap_sent_folder, f'TO "{self.contact}"'))
        conn = self._imap()
        fetched: list[tuple[str, bytes]] = []
        new_markers: dict[str, int] = {}
        try:
            for folder, criterion in folders:
                msgs, last = self._fetch_folder(conn, folder, criterion, markers.get(folder))
                fetched.extend((folder, raw) for _, raw in msgs)
                new_markers[folder] = last
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
        return fetched, new_markers

    async def poll_once(self) -> list[Message]:
        folders = [self.settings.imap_inbox_folder] + ([self.settings.imap_sent_folder] if self.settings.imap_sent_folder else [])
        markers: dict[str, int | None] = {}
        for f in folders:
            v = await self.store.kv_get(f"email:uid:{f}")
            markers[f] = int(v) if v is not None else None
        fetched, new_markers = await asyncio.to_thread(self._poll_sync, markers)

        stored_new: list[Message] = []
        for folder, raw in fetched:
            first_sync = markers.get(folder) is None
            msg, created = await self._store(parse_email(raw), history=first_sync)
            if created:
                stored_new.append(msg)
                if not first_sync and self.on_message:
                    await self.on_message(msg, True)
        for f, uid in new_markers.items():
            await self.store.kv_set(f"email:uid:{f}", str(uid))
        if stored_new:
            log.info("email: stored %d new messages", len(stored_new))
        return stored_new

    async def _store(self, p: ParsedEmail, history: bool) -> tuple[Message, bool]:
        incoming = p.from_addr == self.contact
        if incoming:
            status = MessageStatus.IGNORED if history else MessageStatus.NEW
        else:
            status = MessageStatus.MANUAL
        return await self.store.add_message(
            channel=Channel.EMAIL,
            direction=Direction.IN if incoming else Direction.OUT,
            chat_id=self.contact,
            external_id=p.message_id,
            author_id=p.from_addr,
            author_name=p.from_name if incoming else self.settings.owner_name,
            text=p.body,
            timestamp=p.date,
            meta={
                "subject": p.subject,
                "message_id": p.message_id,
                "in_reply_to": p.in_reply_to,
                "references": p.references,
            },
            status=status,
        )

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # keep polling on transient IMAP errors
                log.exception("email: poll failed")
            await asyncio.sleep(self.settings.email_poll_seconds)

    # ------------------------------------------------------------------ SMTP

    def _send_sync(self, msg: EmailMessage) -> None:
        s = self.settings
        password = s.smtp_password.get_secret_value() if s.smtp_password else ""
        if s.smtp_port == 465:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ssl.create_default_context()) as smtp:
                smtp.login(s.smtp_user or "", password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(s.smtp_user or "", password)
                smtp.send_message(msg)

    async def send(self, text: str, reply_to: Message | None) -> Message:
        meta = reply_to.meta if reply_to is not None and reply_to.channel == Channel.EMAIL else {}
        msg = build_reply(
            from_addr=self.settings.email_sender or "",
            to_addr=self.contact,
            body=text,
            subject=meta.get("subject") or "",
            in_reply_to=meta.get("message_id"),
            references=list(meta.get("references") or []),
        )
        await asyncio.to_thread(self._send_sync, msg)
        stored, _ = await self.store.add_message(
            channel=Channel.EMAIL,
            direction=Direction.OUT,
            chat_id=self.contact,
            external_id=msg["Message-ID"],
            author_id=(self.settings.email_sender or "").lower(),
            author_name=self.settings.owner_name,
            text=text,
            meta={
                "subject": str(msg["Subject"]),
                "message_id": msg["Message-ID"],
                "in_reply_to": msg.get("In-Reply-To"),
                "references": str(msg.get("References", "")).split(),
                "via_bot": True,
            },
            status=MessageStatus.SENT,
        )
        return stored
