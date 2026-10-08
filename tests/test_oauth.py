"""OAuth for the MCP connector, end to end over real HTTP (as claude.ai would do it)."""
import asyncio
import base64
import hashlib
import secrets
import socket
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from ghostwriter.core import Ghostwriter
from ghostwriter.llm import DisabledLLM
from ghostwriter.mcp_server import build_http_app, build_server, make_oauth
from ghostwriter.oauth import hash_password, verify_password

from .conftest import FakeChats, make_settings
from .test_mcp import VLAD, WORK

PASSWORD = "correct horse battery"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


def test_password_hash():
    h = hash_password(PASSWORD)
    assert "$" not in h and h.startswith("scrypt:")
    assert verify_password(PASSWORD, h) and not verify_password("nope", h)
    assert not verify_password(PASSWORD, "garbage")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@pytest.fixture
async def server(store, persona):
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    settings = make_settings(control_bot_token="", anthropic_api_key="", mcp_enabled=True,
                             mcp_password_hash=hash_password(PASSWORD), mcp_public_url=base)
    tg = FakeChats([VLAD, WORK])
    tg.store = store
    core = Ghostwriter(settings, store, persona, DisabledLLM(), {"telegram": tg}, chats=tg)
    await core.load_state()
    oauth = make_oauth(core, settings)
    app = build_http_app(build_server(core, tg, settings, oauth), settings)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        await asyncio.sleep(0.05)
    try:
        yield base, oauth
    finally:
        srv.should_exit = True
        await task


async def login(http, base, redirect=CALLBACK):
    reg = await http.post(f"{base}/register", json={
        "client_name": "Claude", "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = pkce()
    auth = await http.get(f"{base}/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st4te",
    })
    assert auth.status_code == 302, auth.text
    login_url = auth.headers["location"]
    assert login_url.startswith(f"{base}/login?req=")
    page = await http.get(login_url)
    assert page.status_code == 200 and "Claude" in page.text and "password" in page.text
    req = parse_qs(urlparse(login_url).query)["req"][0]
    return client_id, verifier, req


async def test_full_flow(server):
    base, oauth = server
    async with httpx2.AsyncClient() as http:
        # unauthenticated MCP request -> 401 pointing at the metadata
        r = await http.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")
        meta = (await http.get(f"{base}/.well-known/oauth-authorization-server")).json()
        assert meta["registration_endpoint"].endswith("/register") and "S256" in meta["code_challenge_methods_supported"]
        prm = (await http.get(f"{base}/.well-known/oauth-protected-resource/mcp")).json()
        assert prm["authorization_servers"][0].rstrip("/") == base

        client_id, verifier, req = await login(http, base)
        bad = await http.post(f"{base}/login", data={"req": req, "password": "wrong"})
        assert bad.status_code == 401 and "Неверный пароль" in bad.text
        ok = await http.post(f"{base}/login", data={"req": req, "password": PASSWORD})
        assert ok.status_code == 302
        back = urlparse(ok.headers["location"])
        assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
        q = parse_qs(back.query)
        assert q["state"] == ["st4te"]
        # the login link is single-use
        assert (await http.post(f"{base}/login", data={"req": req, "password": PASSWORD})).status_code == 400

        tok = await http.post(f"{base}/token", data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
            "client_id": client_id, "code_verifier": verifier,
        })
        assert tok.status_code == 200, tok.text
        tokens = tok.json()
        assert tokens["token_type"] == "Bearer" and tokens["refresh_token"]
        # the code cannot be reused
        again = await http.post(f"{base}/token", data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
            "client_id": client_id, "code_verifier": verifier,
        })
        assert again.status_code == 400

    # a real MCP session with the access token
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {tokens['access_token']}"}) as authed:
        async with Client(streamable_http_client(f"{base}/mcp", http_client=authed)) as client:
            out = await client.call_tool("list_chats", {})
            assert "Vladimir" in out.content[0].text

    async with httpx2.AsyncClient() as http:
        # refresh rotates both tokens
        ref = await http.post(f"{base}/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id,
        })
        assert ref.status_code == 200, ref.text
        new = ref.json()
        assert new["access_token"] != tokens["access_token"]
        assert await oauth.load_access_token(tokens["access_token"]) is None
        assert await oauth.load_access_token(new["access_token"]) is not None
        assert len(await oauth.sessions()) == 1

        assert await oauth.revoke_all() == 1
        r = await http.post(f"{base}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                            headers={"Authorization": f"Bearer {new['access_token']}"})
        assert r.status_code == 401


async def test_foreign_redirect_is_refused(server):
    base, _ = server
    async with httpx2.AsyncClient() as http:
        reg = await http.post(f"{base}/register", json={
            "client_name": "evil", "redirect_uris": ["https://evil.example/cb"], "token_endpoint_auth_method": "none",
        })
        assert reg.status_code == 400 and "invalid_redirect_uri" in reg.text


async def test_lockout_after_wrong_passwords(server):
    base, _ = server
    async with httpx2.AsyncClient() as http:
        _, _, req = await login(http, base)
        for _ in range(5):
            assert (await http.post(f"{base}/login", data={"req": req, "password": "x"})).status_code == 401
        locked = await http.post(f"{base}/login", data={"req": req, "password": PASSWORD})
        assert locked.status_code == 429


async def test_loopback_redirect_for_claude_code(server):
    base, _ = server
    async with httpx2.AsyncClient() as http:
        await login(http, base, redirect="http://localhost:33418/callback")
