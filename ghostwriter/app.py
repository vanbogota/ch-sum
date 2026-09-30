"""Wires the components together and runs them."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from .channels.telegram import TelegramGateway
from .config import Settings
from .control.bot import ControlBot
from .core import Ghostwriter, Sender
from .llm import LLM
from .persona import Persona
from .storage import Channel, DraftStatus, Store
from .storage.models import MessageStatus

log = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    # Third-party DEBUG logs can contain message bodies; keep them quiet unless explicitly asked for.
    for noisy in ("telethon", "aiogram", "httpx2", "httpx", "anthropic", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def catch_up(core: Ghostwriter, tg: TelegramGateway, limit: int) -> int:
    """Import recent history. Contact messages after the owner's last reply are treated as new (sent while offline)."""
    created = await tg.backfill(limit)
    tail = []
    for m in reversed(created):
        if m.direction == "out":
            break
        if core.is_contact(m):
            tail.append(m)
    if tail and not await core.store.drafts_by_status(DraftStatus.PENDING, channel=Channel.TELEGRAM):
        await core.store.set_message_statuses((m.id for m in tail), MessageStatus.NEW)
        core.schedule_processing(Channel.TELEGRAM)
    return len(created)


async def run(settings: Settings) -> None:
    setup_logging(settings.log_level)
    store = Store(settings.database_url)
    await store.init()
    persona = Persona.load(settings.persona_dir)
    llm = LLM(settings.anthropic_model, settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None)

    tg = TelegramGateway(settings, store)
    senders: dict[str, Sender] = {"telegram": tg}
    email = None
    if settings.email_enabled:
        from .channels.email import EmailGateway

        email = EmailGateway(settings, store)
        senders["email"] = email

    core = Ghostwriter(settings, store, persona, llm, senders)
    bot = ControlBot(settings, core, sync=lambda n: _count(tg, n))
    core.notifier = bot
    tg.on_message = core.on_message
    if email:
        email.on_message = core.on_message

    await tg.start()
    if settings.backfill_on_start:
        await catch_up(core, tg, settings.backfill_on_start)
    await core.start()

    tasks = [asyncio.create_task(tg.run(), name="telegram"), asyncio.create_task(bot.run(), name="control-bot")]
    if email:
        tasks.append(asyncio.create_task(email.run(), name="email"))
    log.info("ghostwriter running: channels=%s model=%s", ",".join(senders), settings.anthropic_model)
    with suppress(Exception):
        await bot.info(f"🟢 Запущен. Каналы: {', '.join(senders)}. /help")
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t.exception():
                raise t.exception()  # type: ignore[misc]
    finally:
        for t in tasks:
            t.cancel()
        await core.stop()
        with suppress(Exception):
            await bot.close()
        await tg.stop()
        await store.close()


async def _count(tg: TelegramGateway, limit: int) -> int:
    return len(await tg.backfill(limit))
