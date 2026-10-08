"""MCP server: lets Claude (claude.ai, the mobile app, Claude Desktop, Claude Code) read your Telegram chats
and send messages as you. Claude does the thinking under your subscription; no Anthropic API key needed.

Exposed over Streamable HTTP at /mcp. Access (MCP_AUTH):
- oauth (default): Claude registers itself and the owner logs in with a password (see oauth.py);
- token: a secret token in the URL path (`https://host/<token>/mcp`) or as `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .chats import ChatRef
from .config import Settings
from .core import ActionError, Ghostwriter
from .llm.prompts import clip, format_history, format_style_samples
from .oauth import SCOPE, OwnerOAuthProvider
from .storage import Message

log = logging.getLogger(__name__)

MAX_MESSAGES = 300
MIN_TOKEN_LENGTH = 24

INSTRUCTIONS = """Tools for the owner's personal Telegram account (the user you are talking to is the owner).

- Find chats with list_chats; read with get_messages (prefer a time window or a small limit: long chats are big).
- Before drafting a reply in the owner's voice, call get_persona for that chat and match his style.
- Never invent facts about the owner's life, plans or commitments.
- send_message sends immediately from the owner's own account. Only call it after the owner has explicitly
  approved the exact final text in this conversation. Never send on your own initiative."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
SEND = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)


class TelegramTools(Protocol):
    async def send_to(self, chat_id: int, text: str) -> Message: ...
    async def search(self, chat_id: int, text: str, limit: int = 30) -> list[Message]: ...


def make_oauth(core: Ghostwriter, settings: Settings) -> OwnerOAuthProvider:
    base = settings.mcp_base_url
    if not base or settings.mcp_password_hash is None:
        raise ValueError("OAuth needs MCP_DOMAIN (or MCP_PUBLIC_URL) and MCP_PASSWORD_HASH.")
    return OwnerOAuthProvider(
        store=core.store,
        password_hash=settings.mcp_password_hash.get_secret_value(),
        issuer_url=base,
        allow_send=settings.mcp_allow_send,
    )


def build_server(
    core: Ghostwriter, tg: TelegramTools, settings: Settings, oauth: OwnerOAuthProvider | None = None
) -> MCPServer:
    tz, owner = settings.tz, settings.owner_name
    if oauth is None:
        mcp = MCPServer("ghostwriter-telegram", instructions=INSTRUCTIONS)
    else:
        base = oauth.issuer_url.rstrip("/")
        mcp = MCPServer(
            "ghostwriter-telegram",
            instructions=INSTRUCTIONS,
            auth_server_provider=oauth,
            auth=AuthSettings(
                issuer_url=base,
                resource_server_url=f"{base}/mcp",
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
                ),
                revocation_options=RevocationOptions(enabled=True),
                validate_token_resource=False,  # single owner; tokens are opaque and stored server-side
            ),
        )

        @mcp.custom_route("/login", methods=["GET", "POST"])
        async def login(request):  # type: ignore[no-untyped-def]
            return await oauth.login_page(request)

        @mcp.custom_route("/health", methods=["GET"])
        async def health(request):  # type: ignore[no-untyped-def]
            from starlette.responses import PlainTextResponse

            return PlainTextResponse("ok")

    async def find(query: str | None, kinds: set[str] | None, limit: int) -> list[ChatRef]:
        try:
            return await core.find_chats(query, kinds, limit=limit)
        except ActionError as exc:
            raise ToolError(str(exc)) from exc

    async def resolve(chat: str | None) -> ChatRef:
        q = (chat or "").strip()
        if not q:
            if core.current is None:
                raise ToolError("No current chat selected: pass `chat` (name, @username or chat_id).")
            return core.current
        if q.lstrip("-").isdigit():
            try:
                return await core.chat_by_id(int(q))
            except Exception as exc:
                raise ToolError(f"Unknown chat_id {q}. Use list_chats to find it.") from exc
        refs = await find(q, None, 8)
        if not refs:
            raise ToolError(f"No chat matches {q!r}. Use list_chats to find it.")
        exact = [r for r in refs if r.title.casefold() == q.lstrip("@").casefold()]
        if len(refs) == 1:
            return refs[0]
        if len(exact) == 1:
            return exact[0]
        options = "; ".join(f"{r.title} (chat_id {r.chat_id})" for r in refs)
        raise ToolError(f"{q!r} matches several chats: {options}. Call again with the chat_id.")

    def header(ref: ChatRef, msgs: list[Message]) -> str:
        if not msgs:
            return f"{ref.title} [{ref.kind}, chat_id {ref.chat_id}]: no messages."
        first = msgs[0].timestamp.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        last = msgs[-1].timestamp.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        return f"{ref.title} [{ref.kind}, chat_id {ref.chat_id}] · {len(msgs)} messages, {first} – {last} ({settings.timezone})"

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def list_chats(query: str | None = None, kind: str | None = None, limit: int = 20) -> str:
        """List the owner's Telegram chats, newest first.

        Args:
            query: part of a name, @username or chat_id to search for (all chats, archived included).
            kind: "user", "group" or "channel" to filter.
            limit: max chats to return (1-50).
        """
        kinds = {kind} if kind in ("user", "group", "channel") else None
        refs = await find(query, kinds, max(1, min(limit, 50)))
        if not refs:
            return "No chats found."
        lines = [f"- {r.title} [{r.kind}] chat_id={r.chat_id}" for r in refs]
        if core.current:
            lines.append(f"(current chat in the control bot: {core.current.title}, chat_id={core.current.chat_id})")
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_messages(
        chat: str | None = None,
        since_hours: float | None = None,
        limit: int = 50,
        author: str | None = None,
        max_chars: int = 1500,
    ) -> str:
        """Read messages of a chat, oldest first, fresh from Telegram.

        Args:
            chat: chat name, @username or chat_id (default: the current chat of the control bot).
            since_hours: only messages from the last N hours (e.g. 24 for a day, 168 for a week).
            limit: max number of the newest messages (1-300). Applies together with since_hours.
            author: only messages from this person (part of the name), or "me" for the owner's own.
            max_chars: long messages / forwarded posts are cut to this many characters.
        """
        ref = await resolve(chat)
        limit = max(1, min(limit, MAX_MESSAGES))
        since = datetime.now(UTC) - timedelta(hours=since_hours) if since_hours else None
        await core.sync(ref, limit if since is None else MAX_MESSAGES * 3, since)
        msgs = await core.select_messages(author, limit, since_hours, core.scope_for(ref))
        if not msgs:
            return header(ref, msgs)
        return header(ref, msgs) + "\n" + format_history(msgs, tz, owner, max(200, max_chars))

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def search_messages(text: str, chat: str | None = None, limit: int = 30) -> str:
        """Full-history search for a word or phrase in a chat (Telegram's own search), newest first.

        Args:
            text: what to search for.
            chat: chat name, @username or chat_id (default: the current chat of the control bot).
            limit: max results (1-100).
        """
        ref = await resolve(chat)
        found = await tg.search(ref.chat_id, text, max(1, min(limit, 100)))
        if not found:
            return f"{ref.title}: nothing found for {text!r}."
        return f"{ref.title}: {len(found)} matches for {text!r}\n" + format_history(found, tz, owner, 1500)

    @mcp.tool(annotations=READ_ONLY, structured_output=False)
    async def get_persona(chat: str | None = None) -> str:
        """How the owner writes and who the person/group is to him. Use before drafting a reply for him.

        Args:
            chat: chat name, @username or chat_id the reply is for (default: the current chat).
        """
        ref = await resolve(chat)
        persona = core.persona
        style = await core.store.owner_style_samples(settings.style_sample_size, scope=core.scope_for(ref))
        if len(style) < 5:
            style = await core.store.owner_style_samples(settings.style_sample_size)
        parts = [
            f"Owner: {owner}. Reply chat: {ref.title} [{ref.kind}].",
            "About the owner:\n" + (persona.context or "(not provided)"),
            f"About {ref.title}:\n" + (persona.contact_notes(ref.chat_id) or "(no notes)"),
            "Style examples:\n" + clip(persona.examples or "(none)", 4000),
            "Owner's recent messages (style reference):\n" + format_style_samples(style),
        ]
        return "\n\n".join(parts)

    @mcp.tool(annotations=SEND, structured_output=False)
    async def send_message(chat: str, text: str) -> str:
        """Send a Telegram message as the owner, immediately. Only after the owner approved this exact text.

        Args:
            chat: chat name, @username or chat_id. A name must match exactly one chat; prefer chat_id.
            text: the final message text, exactly as approved.
        """
        if not settings.mcp_allow_send:
            raise ToolError("Sending is disabled on the server (MCP_ALLOW_SEND=false).")
        if not text.strip():
            raise ToolError("Empty message.")
        ref = await resolve(chat)
        sent = await tg.send_to(ref.chat_id, text)
        log.info("mcp: sent message #%s to chat %s", sent.id, ref.chat_id)
        when = sent.timestamp.astimezone(tz).strftime("%H:%M")
        return f"Sent to {ref.title} (chat_id {ref.chat_id}) at {when}."

    return mcp


# --------------------------------------------------------------------------------------- HTTP

ASGIApp = Callable[[MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]]


class TokenGate:
    """Lets requests through only with the secret token: as a path prefix or a Bearer header."""

    def __init__(self, app: ASGIApp, token: str, path: str = "/mcp") -> None:
        self.app = app
        self.token = token
        self.prefix = f"/{token}"
        self.path = path

    def _bearer_ok(self, scope: MutableMapping[str, Any]) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                got = value.decode("latin-1")
                return got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip(), self.token)
        return False

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # lifespan
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        if path == "/health":
            await _plain(send, 200, b"ok")
            return
        head, _, rest = path[1:].partition("/")
        if hmac.compare_digest(head, self.token) and ("/" + rest).startswith(self.path):
            scope = dict(scope, path="/" + rest, raw_path=("/" + rest).encode())
            await self.app(scope, receive, send)
            return
        if path.startswith(self.path) and self._bearer_ok(scope):
            await self.app(scope, receive, send)
            return
        await _plain(send, 404, b"not found")


async def _plain(send: Any, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": body})


def build_http_app(mcp: MCPServer, settings: Settings) -> ASGIApp:
    # Host/Origin checks guard localhost servers against browsers; this one sits behind a reverse proxy
    # under a public name and is protected by OAuth or the token instead.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    if settings.mcp_auth == "oauth":
        return mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, transport_security=security)
    token = settings.mcp_token.get_secret_value() if settings.mcp_token else ""
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"MCP_TOKEN must be at least {MIN_TOKEN_LENGTH} characters (try `openssl rand -hex 24`).")
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, transport_security=security)
    return TokenGate(app, token)


async def serve(core: Ghostwriter, tg: TelegramTools, settings: Settings) -> None:
    import uvicorn

    oauth = make_oauth(core, settings) if settings.mcp_auth == "oauth" else None
    app = build_http_app(build_server(core, tg, settings, oauth), settings)
    config = uvicorn.Config(
        app, host=settings.mcp_host, port=settings.mcp_port, log_level="warning", lifespan="on",
        proxy_headers=True, forwarded_allow_ips="*",
    )
    log.info(
        "MCP server on %s:%s (auth %s, send %s)", settings.mcp_host, settings.mcp_port, settings.mcp_auth,
        "enabled" if settings.mcp_allow_send else "disabled",
    )
    await uvicorn.Server(config).serve()


__all__ = ["TokenGate", "build_http_app", "build_server", "make_oauth", "serve"]
