"""Single-user OAuth for the MCP connector: Claude registers itself, the owner logs in with a password.

Flow: Claude -> /authorize (SDK) -> our /login page -> password -> redirect back with a code ->
Claude -> /token (SDK) -> access token (1 h) + refresh token (30 days, rotated on use).
Clients and tokens are stored in the kv table; tokens only as SHA-256 hashes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from html import escape
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from .storage import Store

log = logging.getLogger(__name__)

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
LOGIN_TTL = 600
MAX_FAILURES = 5
LOCKOUT = 15 * 60
SCOPE = "telegram"

K_CLIENT = "oauth:client:"
K_ACCESS = "oauth:access:"
K_REFRESH = "oauth:refresh:"

# Where Claude apps send the browser back after login. Loopback is used by Claude Code / Desktop.
DEFAULT_REDIRECT_HOSTS = ("claude.ai", "claude.com", "localhost", "127.0.0.1")


# ------------------------------------------------------------------------------------- password


def hash_password(password: str, *, n: int = 2**14, r: int = 8, p: int = 1) -> str:
    """scrypt hash as "scrypt:n:r:p:salt:hash" (no "$", so it is safe in .env and docker compose)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt:{n}:{r}:{p}:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split(":")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ------------------------------------------------------------------------------------- provider


@dataclass
class _Pending:
    client_id: str
    client_name: str
    params: AuthorizationParams
    expires_at: float


@dataclass
class _Failures:
    count: int = 0
    first_at: float = 0.0
    locked_until: float = 0.0


@dataclass
class OwnerOAuthProvider:
    """OAuthAuthorizationServerProvider for one owner with one password."""

    store: Store
    password_hash: str
    issuer_url: str
    redirect_hosts: tuple[str, ...] = DEFAULT_REDIRECT_HOSTS
    allow_send: bool = False
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _codes: dict[str, AuthorizationCode] = field(default_factory=dict)
    _failures: _Failures = field(default_factory=_Failures)

    # --- clients (dynamic registration)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = await self.store.kv_get(K_CLIENT + client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            host = urlparse(str(uri)).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in self.redirect_hosts):
                raise RegistrationError("invalid_redirect_uri", f"redirect host {host!r} is not allowed")
        assert client_info.client_id
        await self.store.kv_set(K_CLIENT + client_info.client_id, client_info.model_dump_json())
        log.info("oauth: registered client %s (%s)", client_info.client_name or "?", client_info.client_id)

    # --- authorization: send the browser to our login page

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        self._gc()
        req = secrets.token_urlsafe(24)
        assert client.client_id
        self._pending[req] = _Pending(
            client.client_id, client.client_name or "MCP client", params, time.time() + LOGIN_TTL
        )
        return f"{self.issuer_url.rstrip('/')}/login?req={req}"

    def _gc(self) -> None:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v.expires_at > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}

    def _locked(self) -> bool:
        f, now = self._failures, time.time()
        if f.locked_until > now:
            return True
        if now - f.first_at > LOCKOUT:
            self._failures = _Failures()
        return False

    def _fail(self) -> None:
        f, now = self._failures, time.time()
        if f.count == 0:
            f.first_at = now
        f.count += 1
        if f.count >= MAX_FAILURES:
            f.locked_until = now + LOCKOUT
            log.warning("oauth: too many wrong passwords, login locked for %d min", LOCKOUT // 60)

    async def login_page(self, request: Request) -> Response:
        """GET shows the password form, POST checks it and redirects back to the client with a code."""
        self._gc()
        if request.method == "GET":
            req = request.query_params.get("req", "")
            pending = self._pending.get(req)
            if pending is None:
                return _page("Ссылка устарела", "<p>Начни подключение в Claude заново.</p>", 400)
            return _page("Вход", _form(req, pending, self.allow_send), 200)

        body = parse_qs((await request.body()).decode(errors="replace"))
        req = (body.get("req") or [""])[0]
        password = (body.get("password") or [""])[0]
        pending = self._pending.get(req)
        if pending is None:
            return _page("Ссылка устарела", "<p>Начни подключение в Claude заново.</p>", 400)
        if self._locked():
            return _page("Вход временно заблокирован", "<p>Слишком много неверных попыток. Попробуй через 15 минут.</p>", 429)
        if not verify_password(password, self.password_hash):
            self._fail()
            log.warning("oauth: wrong password")
            return _page("Вход", '<p class="err">Неверный пароль.</p>' + _form(req, pending, self.allow_send), 401)

        self._failures = _Failures()
        del self._pending[req]
        p = pending.params
        code = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            scopes=p.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=pending.client_id,
            code_challenge=p.code_challenge,
            redirect_uri=p.redirect_uri,
            redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
            resource=p.resource,
            subject="owner",
        )
        self._codes[code.code] = code
        log.info("oauth: owner approved client %s", pending.client_name)
        return RedirectResponse(construct_redirect_uri(str(p.redirect_uri), code=code.code, state=p.state), 302)

    # --- tokens

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        self._gc()
        code = self._codes.get(authorization_code)
        return code if code is not None and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return await self._issue(authorization_code.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        raw = await self.store.kv_get(K_REFRESH + _h(refresh_token))
        if raw is None:
            return None
        data = json.loads(raw)
        if data["client_id"] != client.client_id or data["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=data["client_id"], scopes=data["scopes"],
                            expires_at=data["expires_at"], resource=data.get("resource"), subject="owner")

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        raw = await self.store.kv_get(K_REFRESH + _h(refresh_token.token))
        if raw is None:
            raise TokenError("invalid_grant", "refresh token revoked")
        await self._drop_pair(json.loads(raw)["pair"])  # rotate: the old refresh + access tokens die
        return await self._issue(refresh_token.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        raw = await self.store.kv_get(K_ACCESS + _h(token))
        if raw is None:
            return None
        data = json.loads(raw)
        if data["expires_at"] < time.time():
            await self.store.kv_delete(K_ACCESS + _h(token))
            return None
        return AccessToken(token=token, client_id=data["client_id"], scopes=data["scopes"],
                           expires_at=data["expires_at"], resource=data.get("resource"), subject="owner")

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for prefix in (K_ACCESS, K_REFRESH):
            raw = await self.store.kv_get(prefix + _h(token.token))
            if raw is not None:
                await self._drop_pair(json.loads(raw)["pair"])

    async def exchange_identity_assertion(self, client, params):  # type: ignore[no-untyped-def]
        raise TokenError("unsupported_grant_type", "not supported")

    async def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh, pair = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_hex(8)
        now = int(time.time())
        common = {"client_id": client_id, "scopes": scopes, "resource": resource, "pair": pair}
        await self.store.kv_set(K_ACCESS + _h(access), json.dumps({**common, "expires_at": now + ACCESS_TTL, "refresh": _h(refresh)}))
        await self.store.kv_set(K_REFRESH + _h(refresh), json.dumps({**common, "expires_at": now + REFRESH_TTL, "access": _h(access)}))
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, refresh_token=refresh, scope=" ".join(scopes))

    async def _drop_pair(self, pair: str) -> None:
        for prefix in (K_ACCESS, K_REFRESH):
            for key, raw in await self.store.kv_items(prefix):
                if json.loads(raw).get("pair") == pair:
                    await self.store.kv_delete(key)

    async def revoke_all(self) -> int:
        """Log every connected client out (they must log in again)."""
        n = 0
        for prefix in (K_ACCESS, K_REFRESH):
            for key, _ in await self.store.kv_items(prefix):
                await self.store.kv_delete(key)
                n += prefix == K_REFRESH
        return n

    async def sessions(self) -> list[dict[str, object]]:
        out = []
        for _, raw in await self.store.kv_items(K_REFRESH):
            data = json.loads(raw)
            client = await self.get_client(data["client_id"])
            out.append({"client": client.client_name if client else data["client_id"], "expires_at": data["expires_at"]})
        return out


# ------------------------------------------------------------------------------------- html


def _form(req: str, pending: _Pending, allow_send: bool) -> str:
    host = urlparse(str(pending.params.redirect_uri)).hostname or "?"
    rights = "чтение чатов и отправка сообщений от твоего имени" if allow_send else "чтение чатов"
    return f"""
<p><b>{escape(pending.client_name)}</b> ({escape(host)}) запрашивает доступ к твоему Telegram: {rights}.</p>
<form method="post" action="login">
  <input type="hidden" name="req" value="{escape(req)}">
  <label>Пароль<br><input type="password" name="password" autofocus required autocomplete="current-password"></label>
  <button type="submit">Разрешить</button>
</form>
<p class="hint">Если ты не начинал подключение в Claude — просто закрой страницу.</p>"""


def _page(title: str, body: str, status: int) -> HTMLResponse:
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ghostwriter · {escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:26rem;margin:3rem auto;padding:0 1rem;color:#222;background:#fafafa}}
h1{{font-size:1.3rem}} input{{width:100%;padding:.6rem;font-size:1rem;margin:.3rem 0 1rem;box-sizing:border-box}}
button{{padding:.6rem 1.2rem;font-size:1rem;cursor:pointer}} .err{{color:#b00020}} .hint{{color:#777;font-size:.85rem}}
@media (prefers-color-scheme: dark){{body{{background:#121212;color:#eee}} .hint{{color:#999}}}}
</style></head><body><h1>Ghostwriter · {escape(title)}</h1>{body}</body></html>"""
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
