"""CLI: python -m ghostwriter {run,login,chats,backfill,export-examples}"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import get_settings


async def _telegram(settings):  # type: ignore[no-untyped-def]
    from .channels.telegram import TelegramGateway
    from .storage import Store

    store = Store(settings.database_url)
    await store.init()
    return store, TelegramGateway(settings, store)


async def cmd_login() -> None:
    from telethon import TelegramClient

    s = get_settings()
    s.tg_session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(s.tg_session_path), s.tg_api_id, s.tg_api_hash.get_secret_value())
    def ask_phone() -> str:
        while True:
            value = input("Your phone number in international format (e.g. +358401234567): ").strip()
            if ":" in value:
                print("That looks like a bot token. Log in with YOUR phone number: the assistant works as you.")
                continue
            return value

    await client.start(phone=ask_phone)  # then asks for the login code (sent in Telegram) and 2FA password
    me = await client.get_me()
    if me.bot:
        await client.log_out()
        sys.exit("Logged in as a bot: run `login` again with your phone number.")
    print(f"Logged in as {me.first_name} (id {me.id}). Session saved to {s.tg_session_path}")
    await client.disconnect()


async def cmd_chats(limit: int) -> None:
    s = get_settings()
    store, tg = await _telegram(s)
    await tg.client.connect()
    if not await tg.client.is_user_authorized():
        sys.exit("Not logged in: run `python -m ghostwriter login`")
    for chat_id, kind, name in await tg.list_dialogs(limit):
        print(f"{chat_id:>16}  {kind:<8} {name}")
    await tg.stop()
    await store.close()


async def cmd_backfill(limit: int, chat: str | None) -> None:
    s = get_settings()
    store, tg = await _telegram(s)
    await tg.start()
    target: int | str
    if chat:
        target = int(chat) if chat.lstrip("-").isdigit() else chat
    elif s.vladimir_tg_id:
        target = s.watched_chat
    else:
        sys.exit("Pass --chat <id or @username> (see `chats`) or set VLADIMIR_TG_ID.")
    ref = await tg.chat_ref(target)
    created = await tg.backfill(ref.chat_id, limit)
    print(f"{ref.title}: imported {len(created)} new messages.")
    await tg.stop()
    await store.close()


async def cmd_export_examples(limit: int, out: str | None) -> None:
    """Write the owner's real messages to persona/examples.md as a style reference."""
    from .storage import Store

    s = get_settings()
    store = Store(s.database_url)
    await store.init()
    msgs = await store.owner_style_samples(limit)
    await store.close()
    if not msgs:
        sys.exit("No messages from you stored yet: run `python -m ghostwriter backfill` first.")
    path = out or str(s.persona_dir / "examples.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Real messages {s.owner_name} sent to {s.contact_name} (auto-exported, edit freely)\n\n")
        for m in msgs:
            text = m.text.strip().replace("\n", "\n  ")
            f.write(f"- ({m.channel}) {text}\n")
    print(f"Wrote {len(msgs)} examples to {path}")


def cmd_hash_password() -> None:
    import getpass

    from .oauth import hash_password

    pw = getpass.getpass("Пароль для входа в коннектор (12+ символов): ")
    if len(pw) < 12:
        sys.exit("Слишком короткий пароль.")
    if getpass.getpass("Ещё раз: ") != pw:
        sys.exit("Пароли не совпали.")
    print("\nДобавь в .env:\nMCP_PASSWORD_HASH=" + hash_password(pw))


async def cmd_mcp_sessions(revoke: bool) -> None:
    from datetime import datetime

    from .oauth import OwnerOAuthProvider
    from .storage import Store

    s = get_settings()
    store = Store(s.database_url)
    await store.init()
    provider = OwnerOAuthProvider(store=store, password_hash="", issuer_url="http://localhost")
    if revoke:
        n = await provider.revoke_all()
        print(f"Отозвано сеансов: {n}. Подключённым клиентам придётся войти заново.")
    else:
        rows = await provider.sessions()
        for r in rows:
            print(f"- {r['client']}: до {datetime.fromtimestamp(float(r['expires_at'])):%Y-%m-%d %H:%M}")  # type: ignore[arg-type]
        print(f"Всего: {len(rows)}")
    await store.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="ghostwriter", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the assistant")
    sub.add_parser("login", help="log in to your Telegram account (creates the session file)")
    c = sub.add_parser("chats", help="list your Telegram chats with their ids")
    c.add_argument("--limit", type=int, default=50)
    b = sub.add_parser("backfill", help="import history of the watched chat")
    b.add_argument("--limit", type=int, default=1000)
    b.add_argument("--chat", help="chat id or @username (default: the .env contact)")
    e = sub.add_parser("export-examples", help="write your real messages to persona/examples.md")
    e.add_argument("--limit", type=int, default=40)
    e.add_argument("--out")
    sub.add_parser("hash-password", help="make MCP_PASSWORD_HASH for the connector login")
    ms = sub.add_parser("mcp-sessions", help="list connected MCP clients (OAuth)")
    ms.add_argument("--revoke-all", action="store_true", help="log all of them out")
    args = p.parse_args()

    if args.cmd == "run":
        from .app import run

        if missing := get_settings().missing_for_run():
            sys.exit(f"Fill in .env first: {', '.join(missing)} (chat ids: `python -m ghostwriter chats`)")
        try:
            asyncio.run(run(get_settings()))
        except KeyboardInterrupt:
            pass
    elif args.cmd == "login":
        asyncio.run(cmd_login())
    elif args.cmd == "chats":
        asyncio.run(cmd_chats(args.limit))
    elif args.cmd == "backfill":
        asyncio.run(cmd_backfill(args.limit, args.chat))
    elif args.cmd == "hash-password":
        cmd_hash_password()
    elif args.cmd == "mcp-sessions":
        asyncio.run(cmd_mcp_sessions(args.revoke_all))
    elif args.cmd == "export-examples":
        asyncio.run(cmd_export_examples(args.limit, args.out))


if __name__ == "__main__":
    main()
