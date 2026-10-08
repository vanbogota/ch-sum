"""Pure helpers for control-bot texts: argument parsing, cards, splitting."""
from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape
from zoneinfo import ZoneInfo

from ..core import SummaryRequest
from ..storage import Draft, DraftStatus, Message

TG_LIMIT = 4096
SOURCE_PREVIEW = 1200

STATUS_LABELS = {
    DraftStatus.PENDING: "⏳ ждёт решения",
    DraftStatus.QUEUED: "🕒 в очереди",
    DraftStatus.SENT: "✅ отправлено",
    DraftStatus.SKIPPED: "⏭ пропущено",
    DraftStatus.ESCALATED: "🚨 требует тебя",
    DraftStatus.SUPERSEDED: "♻️ заменено",
    DraftStatus.FAILED: "❌ ошибка отправки",
}

_SINCE = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(h|ч|час\w*|d|д|дн\w*|день|m|мин\w*)$", re.IGNORECASE)
_FROM = {"from", "от", "by"}


def parse_summary_args(args: str | None) -> SummaryRequest:
    """`/summary 50 from Vladimir 24h about work` -> SummaryRequest(limit=50, author='Vladimir', since_hours=24, focus='about work')."""
    req = SummaryRequest()
    tokens = (args or "").split()
    focus: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        low = tok.lower()
        if low.isdigit() and req.limit is None:
            req.limit = int(low)
        elif (m := _SINCE.match(low)) and req.since_hours is None:
            value = float(m.group(1).replace(",", "."))
            unit = m.group(2)[0]
            req.since_hours = value * 24 if unit in "dд" else value / 60 if unit in "mм" else value
        elif low in _FROM and i + 1 < len(tokens) and req.author is None:
            i += 1
            req.author = tokens[i].lstrip("@")
        else:
            focus.append(tok)
        i += 1
    req.focus = " ".join(focus) or None
    return req


def split_text(text: str, limit: int = TG_LIMIT - 96) -> list[str]:
    """Split long text on paragraph/line boundaries to fit Telegram's message limit."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks or [""]


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def render_source(messages: Sequence[Message], tz: ZoneInfo, limit: int = SOURCE_PREVIEW) -> str:
    lines = []
    for m in messages:
        when = m.timestamp.astimezone(tz).strftime("%d.%m %H:%M")
        subject = f" «{m.meta.get('subject')}»" if m.channel == "email" and m.meta.get("subject") else ""
        lines.append(f"<i>{when}{escape(subject)}</i> {escape(m.text)}")
    return _clip("\n".join(lines), limit)


def render_card(draft: Draft, tz: ZoneInfo, contact_name: str) -> str:
    src = draft.source
    icon = "✈️" if draft.channel == "telegram" else "📧"
    head = f"{icon} <b>Черновик #{draft.id}</b> · {STATUS_LABELS.get(DraftStatus(draft.status), draft.status)}"
    parts = [head, f"<b>{escape(contact_name)}:</b>\n{render_source([src], tz)}" if src else ""]
    text = draft.outgoing_text
    if text:
        label = "Твой текст" if draft.edited else "Ответ"
        parts.append(f"<b>{label}:</b>\n<code>{escape(_clip(text, 2000))}</code>")
    if draft.instructions:
        parts.append(f"<i>Указания: {escape(_clip(draft.instructions, 300))}</i>")
    if draft.reason:
        parts.append(f"<i>Заметка: {escape(_clip(draft.reason, 400))}</i>")
    if draft.status == DraftStatus.QUEUED and draft.scheduled_at:
        parts.append(f"Отправка ≈ {draft.scheduled_at.astimezone(tz):%d.%m %H:%M}")
    return _clip("\n\n".join(p for p in parts if p), TG_LIMIT)


def render_escalation(draft: Draft, messages: Sequence[Message], tz: ZoneInfo, contact_name: str) -> str:
    icon = "✈️" if draft.channel == "telegram" else "📧"
    return _clip(
        f"🚨 {icon} <b>{escape(contact_name)} пишет — нужен ты</b> (#{draft.id})\n\n"
        f"{render_source(messages, tz, 2500)}\n\n"
        f"<i>Причина: {escape(draft.reason or '—')}</i>\n\n"
        "Ответь сам, нажми «Написать», или «Черновик» — чтобы я всё же предложил вариант.",
        TG_LIMIT,
    )


HELP = """<b>Что я умею</b>

Пиши обычным текстом, например:
• <i>саммари последних сообщений от Владимира</i>
• <i>саммари за 3 дня</i>
• <i>ответь в моем стиле</i> / <i>ответь в моем стиле, вежливо откажись</i>
• <i>о чём мы договорились про поездку?</i>

Команды:
/summary [N] [24h|3d] [from Имя] [тема] — саммари
/reply [указания] — черновик ответа в твоём стиле
/ask вопрос — вопрос по переписке
/pending — черновики, ждущие решения
/sync [N] — подтянуть историю Telegram
/status — состояние
/cancel — отменить редактирование

К каждому черновику: <b>Отправить</b> · <b>Изменить</b> · <b>Пропустить</b> · <b>Заново</b>.
Отправка идёт с задержкой и только в активные часы."""
