"""Private aiogram control bot: the owner's approval flow and chat commands. Ignores everyone else."""
from __future__ import annotations

import logging
from datetime import datetime
from collections.abc import Awaitable, Callable, Sequence

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Settings
from ..core import ActionError, Ghostwriter, SummaryRequest
from ..llm import LLMError
from ..storage import Draft, DraftStatus, Message
from .render import HELP, parse_summary_args, render_card, render_escalation, split_text

log = logging.getLogger(__name__)


class DraftCb(CallbackData, prefix="d"):
    action: str  # send | edit | skip | regen | now
    draft_id: int


def keyboard(draft: Draft) -> InlineKeyboardMarkup | None:
    b = InlineKeyboardBuilder()
    st = draft.status
    if st == DraftStatus.PENDING:
        b.button(text="✅ Отправить", callback_data=DraftCb(action="send", draft_id=draft.id))
        b.button(text="✏️ Изменить", callback_data=DraftCb(action="edit", draft_id=draft.id))
        b.button(text="⏭ Пропустить", callback_data=DraftCb(action="skip", draft_id=draft.id))
        b.button(text="🔄 Заново", callback_data=DraftCb(action="regen", draft_id=draft.id))
        b.adjust(2, 2)
    elif st == DraftStatus.QUEUED:
        b.button(text="⚡ Отправить сейчас", callback_data=DraftCb(action="now", draft_id=draft.id))
        b.button(text="✖️ Отменить", callback_data=DraftCb(action="skip", draft_id=draft.id))
    elif st == DraftStatus.ESCALATED:
        b.button(text="✏️ Написать", callback_data=DraftCb(action="edit", draft_id=draft.id))
        b.button(text="📝 Черновик", callback_data=DraftCb(action="regen", draft_id=draft.id))
        b.button(text="👌 Сам отвечу", callback_data=DraftCb(action="skip", draft_id=draft.id))
        b.adjust(2, 1)
    elif st in (DraftStatus.SKIPPED, DraftStatus.FAILED):
        b.button(text="🔄 Новый черновик", callback_data=DraftCb(action="regen", draft_id=draft.id))
    else:
        return None
    return b.as_markup()


class ControlBot:
    def __init__(self, settings: Settings, core: Ghostwriter, sync: Callable[[int], Awaitable[int]] | None = None) -> None:
        self.settings = settings
        self.core = core
        self.owner = settings.owner_tg_id
        self.tz = settings.tz
        self.sync = sync
        self.bot = Bot(settings.control_bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.awaiting_edit: int | None = None
        self.dp.include_router(self._router())

    # ------------------------------------------------------------ Notifier

    async def info(self, text: str) -> None:
        for chunk in split_text(text):
            await self.bot.send_message(self.owner, chunk, parse_mode=None)

    async def draft_card(self, draft: Draft) -> None:
        msg = await self.bot.send_message(
            self.owner, render_card(draft, self.tz, self.settings.contact_name), reply_markup=keyboard(draft)
        )
        await self.core.store.update_draft(draft.id, control_message_id=msg.message_id)

    async def escalation(self, draft: Draft, messages: Sequence[Message]) -> None:
        msg = await self.bot.send_message(
            self.owner,
            render_escalation(draft, messages, self.tz, self.settings.contact_name),
            reply_markup=keyboard(draft),
        )
        await self.core.store.update_draft(draft.id, control_message_id=msg.message_id)

    async def refresh_card(self, draft: Draft) -> None:
        fresh = await self.core.store.get_draft(draft.id) or draft
        if not fresh.control_message_id:
            return
        try:
            await self.bot.edit_message_text(
                render_card(fresh, self.tz, self.settings.contact_name),
                chat_id=self.owner,
                message_id=fresh.control_message_id,
                reply_markup=keyboard(fresh),
            )
        except TelegramBadRequest as exc:  # "message is not modified" or too old to edit
            log.debug("refresh card #%s: %s", draft.id, exc)

    # ------------------------------------------------------------ handlers

    def _router(self) -> Router:
        r = Router(name="owner")
        owner_only = F.from_user.id == self.owner
        r.message.filter(owner_only, F.chat.type == ChatType.PRIVATE)
        r.callback_query.filter(owner_only)

        r.message.register(self.cmd_help, CommandStart())
        r.message.register(self.cmd_help, Command("help"))
        r.message.register(self.cmd_summary, Command("summary", "sum", "саммари"))
        r.message.register(self.cmd_reply, Command("reply", "draft"))
        r.message.register(self.cmd_ask, Command("ask"))
        r.message.register(self.cmd_pending, Command("pending"))
        r.message.register(self.cmd_sync, Command("sync"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_cancel, Command("cancel"))
        r.message.register(self.on_text, F.text)
        r.callback_query.register(self.on_button, DraftCb.filter())
        return r

    async def _reply_long(self, m: TgMessage, text: str) -> None:
        for chunk in split_text(text):
            await m.answer(chunk, parse_mode=None)

    async def _run(self, m: TgMessage, coro: Awaitable[str | None]) -> None:
        """Run an action with a typing indicator and user-friendly errors."""
        try:
            async with ChatActionSender.typing(bot=self.bot, chat_id=m.chat.id):
                result = await coro
        except ActionError as exc:
            await m.answer(f"⚠️ {exc}", parse_mode=None)
            return
        except LLMError as exc:
            await m.answer(f"⚠️ Claude: {exc}", parse_mode=None)
            return
        if result:
            await self._reply_long(m, result)

    async def cmd_help(self, m: TgMessage) -> None:
        await m.answer(HELP)

    async def cmd_summary(self, m: TgMessage, command: CommandObject) -> None:
        await self._run(m, self.core.summarize(parse_summary_args(command.args)))

    async def _reply(self, instructions: str | None) -> None:
        await self.core.request_reply(instructions)  # the card is sent by the notifier

    async def cmd_reply(self, m: TgMessage, command: CommandObject) -> None:
        await self._run(m, self._reply(command.args))

    async def cmd_ask(self, m: TgMessage, command: CommandObject) -> None:
        if not command.args:
            await m.answer("Использование: /ask вопрос")
            return
        await self._run(m, self.core.ask(command.args))

    async def cmd_pending(self, m: TgMessage) -> None:
        drafts = await self.core.store.drafts_by_status(DraftStatus.PENDING, DraftStatus.QUEUED, DraftStatus.ESCALATED)
        if not drafts:
            await m.answer("Нет черновиков, ждущих решения.")
            return
        for d in drafts[-10:]:
            await self.draft_card(d)

    async def cmd_sync(self, m: TgMessage, command: CommandObject) -> None:
        if self.sync is None:
            await m.answer("Синхронизация недоступна.")
            return
        limit = int(command.args) if command.args and command.args.strip().isdigit() else 500

        async def go() -> str:
            n = await self.sync(limit)  # type: ignore[misc]
            return f"Готово: добавлено {n} сообщений."

        await self._run(m, go())

    async def cmd_status(self, m: TgMessage) -> None:
        counts = await self.core.store.count_messages()
        drafts = await self.core.store.draft_counts()
        s = self.settings
        lines = [
            f"Модель: {s.anthropic_model}",
            f"Каналы: {', '.join(self.core.senders)}",
            "Сообщений: " + (", ".join(f"{k}: {v}" for k, v in counts.items()) or "0"),
            "Черновики: " + (", ".join(f"{k}: {v}" for k, v in drafts.items()) or "0"),
            f"Активные часы: {s.active_hours} ({s.timezone}), сейчас {'активно' if self.core.hours.is_active(datetime.now(s.tz)) else 'тихие часы'}",
            f"Задержка: {s.reply_delay_seconds} c, авто-режим: {'вкл' if s.auto_mode else 'выкл'}",
        ]
        await m.answer("\n".join(lines), parse_mode=None)

    async def cmd_cancel(self, m: TgMessage) -> None:
        self.awaiting_edit = None
        await m.answer("Ок, отменено.")

    async def on_text(self, m: TgMessage) -> None:
        text = (m.text or "").strip()
        if not text:
            return
        if self.awaiting_edit is not None:
            draft_id, self.awaiting_edit = self.awaiting_edit, None
            try:
                d = await self.core.approve(draft_id, text)
            except ActionError as exc:
                await m.answer(f"⚠️ {exc}", parse_mode=None)
                return
            when = d.scheduled_at.astimezone(self.tz).strftime("%d.%m %H:%M") if d.scheduled_at else "скоро"
            await m.answer(f"Принято, отправлю твой текст ≈ {when}.", parse_mode=None)
            return
        if text.startswith("/"):
            await m.answer("Не знаю такой команды. /help")
            return
        await self._run(m, self._natural(text))

    async def _natural(self, text: str) -> str | None:
        """Free-form request: let Claude pick the action, then run it."""
        intent = await self.core.route(text)
        log.info("routed request to %s", intent.action)
        if intent.action == "summary":
            return await self.core.summarize(
                SummaryRequest(intent.author, intent.limit, intent.since_hours, intent.instructions)
            )
        if intent.action == "reply":
            await self.core.request_reply(intent.instructions)
            return None
        if intent.action == "question":
            return await self.core.ask(intent.instructions or text, intent.limit, intent.author, intent.since_hours)
        return "Не понял запрос. Попробуй «саммари за сутки», «ответь в моем стиле» или /help."

    async def on_button(self, q: CallbackQuery, callback_data: DraftCb) -> None:
        draft_id, action = callback_data.draft_id, callback_data.action
        try:
            if action == "send":
                d = await self.core.approve(draft_id)
                when = d.scheduled_at.astimezone(self.tz).strftime("%H:%M") if d.scheduled_at else ""
                await q.answer(f"В очереди, отправка ≈ {when}")
            elif action == "now":
                await self.core.send_now(draft_id)
                await q.answer("Отправляю…")
            elif action == "skip":
                await self.core.skip(draft_id)
                await q.answer("Пропущено")
            elif action == "edit":
                self.awaiting_edit = draft_id
                await q.answer()
                await self.bot.send_message(
                    self.owner,
                    f"Пришли текст для черновика #{draft_id} — отправлю как есть. /cancel — отмена.",
                    reply_markup=ForceReply(input_field_placeholder="Текст ответа"),
                    parse_mode=None,
                )
            elif action == "regen":
                await q.answer("Пишу новый вариант…")
                async with ChatActionSender.typing(bot=self.bot, chat_id=self.owner):
                    await self.core.regenerate(draft_id)
            else:
                await q.answer("?")
        except ActionError as exc:
            await q.answer(str(exc), show_alert=True)
        except LLMError as exc:
            await self.info(f"⚠️ Claude: {exc}")

    # ------------------------------------------------------------ run

    async def run(self) -> None:
        await self.bot.delete_webhook(drop_pending_updates=False)
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def close(self) -> None:
        await self.dp.stop_polling()
        await self.bot.session.close()
