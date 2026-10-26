"""OAuth through mcp-login-oauth against a fake Paperless. The library's own
tests cover the OAuth machinery; these cover how this server plugs into it: the
username/password/two-factor login, the pasted API token for single-sign-on
accounts, a revoke that never touches Paperless, and both mode."""

import asyncio
import base64
import hashlib
import json
import secrets

import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from key_value.aio.stores.memory import MemoryStore

import server
from tests._fake import BASE, STATISTICS, asgi_factory, build, mcp_session

OAUTH_VARS = {"MCP_BASE_URL": "https://mcp.example", "MCP_OAUTH_SECRET": "s3cret-s3cret"}
REDIRECT = "http://localhost:9/cb"


@pytest.mark.parametrize(
    "mode, env, expected",
    [
        (None, {}, "token"),              # unset + no OAuth vars: tokens only
        (None, OAUTH_VARS, "both"),       # unset + vars: default is both
        ("token", {}, "token"),
        ("oauth", OAUTH_VARS, "oauth"),
        ("BOTH", OAUTH_VARS, "both"),
    ],
)
def test_resolve_auth_mode(monkeypatch, mode, env, expected):
    for var in ("MCP_AUTH_MODE", *OAUTH_VARS):
        monkeypatch.delenv(var, raising=False)
    if mode:
        monkeypatch.setenv("MCP_AUTH_MODE", mode)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert server.resolve_auth_mode() == expected


@pytest.mark.parametrize("mode", ["oauth", "both", "bogus"])
def test_resolve_auth_mode_fails_loudly_when_explicit(monkeypatch, mode):
    for var in OAUTH_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MCP_AUTH_MODE", mode)
    with pytest.raises(RuntimeError, match="MCP_"):
        server.resolve_auth_mode()


def test_plain_http_base_url_fails_at_startup(monkeypatch):
    # OAuth issuers must be HTTPS (localhost excepted); catch it while building,
    # so main() shows startup_error instead of serve() crashing later.
    monkeypatch.setenv("MCP_BASE_URL", "http://192.168.1.50:8081")
    with pytest.raises(RuntimeError, match="MCP_BASE_URL.*HTTPS"):
        server.validate_base_url()


def test_localhost_http_base_url_is_allowed(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "http://localhost:8081/")
    assert server.validate_base_url() == "http://localhost:8081"


def test_encrypted_store_round_trips(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "OAUTH_STORE_DIR", tmp_path)
    monkeypatch.setenv("MCP_OAUTH_SECRET", "s3cret-s3cret")
    store = server.build_oauth_store()

    async def go():
        await store.put("k", {"secret": "needle-value"}, collection="c")
        return await store.get("k", collection="c")

    assert asyncio.run(go()) == {"secret": "needle-value"}
    stored = b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file())
    assert stored and b"needle-value" not in stored  # encrypted at rest


def test_build_oauth_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "OAUTH_STORE_DIR", tmp_path)
    for k, v in OAUTH_VARS.items():
        monkeypatch.setenv(k, v)
    both, oauth = server.build_oauth_provider("both"), server.build_oauth_provider("oauth")
    assert both.verify_static is not None and oauth.verify_static is None
    assert str(both.base_url).rstrip("/") == "https://mcp.example"
    assert both.token_prefix == server.OAUTH_TOKEN_PREFIX
    # Paperless has ONE API token per user, shared by all of that user's
    # clients and the web UI: revoking one client must never touch it.
    assert both.on_revoke is None


@pytest.fixture(autouse=True)
def local_base_url(monkeypatch):
    monkeypatch.setenv("MCP_BASE_URL", "http://localhost")


def fake_paperless(calls: list, seen: dict):
    """A Paperless that knows one user (alice / pw, API token `alice-token`),
    optionally with two-factor on (`seen["mfa"]`, code 123456)."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        auth = request.headers.get("Authorization")
        if request.url.path == "/api/token/":
            body = json.loads(request.content)
            if (body["username"], body["password"]) != ("alice", "pw"):
                return httpx2.Response(400, json={
                    "non_field_errors": ["Unable to log in with provided credentials."]})
            if seen.get("mfa") and body.get("code") != "123456":
                return httpx2.Response(400, json={"non_field_errors": ["MFA code is required"]})
            return httpx2.Response(200, json={"token": "alice-token"})
        if request.url.path == "/api/profile/":
            return httpx2.Response(200 if auth == "Token alice-token" else 401, json={})
        if request.url.path == "/api/statistics/":
            seen["auth"] = auth
            return httpx2.Response(200, json=STATISTICS)
        return httpx2.Response(404)
    return handler


def flow_app(mode="oauth", seen=None):
    """FastMCP app on a fake Paperless -> (inner, app wrapped for `mode`, calls, seen)."""
    calls, seen = [], seen if seen is not None else {}
    handler = fake_paperless(calls, seen)
    provider = server.build_oauth_provider(
        mode,
        paperless=httpx2.AsyncClient(base_url=BASE, transport=httpx2.MockTransport(handler)),
        store=MemoryStore(),
    )
    provider.failed_login_delay = 0
    inner = build(handler, auth=provider).http_app(path=server.DEFAULT_PATH)
    return inner, server.wrap_app(inner, mode), calls, seen


def run(inner, steps, app=None):
    """Run `steps(http)` against `app` (default `inner`) with `inner`'s lifespan up."""
    async def go():
        async with inner.router.lifespan_context(inner):
            async with asgi_factory(app or inner)() as http:
                return await steps(http)
    return asyncio.run(go())


async def begin_login(http) -> dict:
    """register -> authorize; returns what the rest of the flow needs."""
    r = await http.post("/register", json={
        "client_name": "itest", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })
    client_id = r.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    r = await http.get("/authorize", params={
        "response_type": "code", "client_id": client_id,
        "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "s1",
    })
    pending = httpx2.URL(r.headers["location"]).params["id"]
    return {"client_id": client_id, "verifier": verifier, "pending": pending}


async def finish_login(http, flow: dict, form: dict) -> dict:
    """POST /login with `form` -> /token; returns the token response."""
    r = await http.post("/login", data={"id": flow["pending"], **form})
    assert r.status_code == 302, r.text
    back = httpx2.URL(r.headers["location"])
    assert back.params["state"] == "s1"
    r = await http.post("/token", data={
        "grant_type": "authorization_code", "code": back.params["code"],
        "redirect_uri": REDIRECT, "client_id": flow["client_id"],
        "code_verifier": flow["verifier"],
    })
    return r.json()


async def statistics(app, authorization: str):
    transport = StreamableHttpTransport(
        url="http://localhost/mcp",
        headers={"Authorization": authorization},
        httpx_client_factory=asgi_factory(app),
    )
    async with Client(transport) as c:
        return await c.call_tool("statistics_retrieve", {})


def test_full_oauth_flow_forwards_the_users_paperless_token():
    """register -> authorize -> /login -> token -> MCP tool call, all in-process;
    the tool's Paperless call must carry the token /api/token/ returned."""
    inner, app, _, seen = flow_app()

    async def steps(http):
        # Unauthenticated MCP call -> 401 that points at OAuth discovery.
        r = await http.post(server.DEFAULT_PATH, json={})
        assert r.status_code == 401
        assert "resource_metadata" in r.headers["www-authenticate"]

        flow = await begin_login(http)
        page = await http.get("/login", params={"id": flow["pending"]})
        assert "itest" in page.text and "localhost:9" in page.text
        bad = await http.post("/login", data={
            "id": flow["pending"], "username": "alice", "password": "nope"})
        assert bad.status_code == 401  # pending login survives a typo
        assert "Unable to log in with provided credentials" in bad.text
        tokens = await finish_login(http, flow, {"username": "alice", "password": "pw"})
        return await statistics(app, f"Bearer {tokens['access_token']}")

    assert run(inner, steps).data["documents_total"] == 6
    assert seen["auth"] == "Token alice-token"


def test_login_page_offers_the_api_token_for_single_sign_on_accounts():
    inner, _, _, _ = flow_app()

    async def steps(http):
        flow = await begin_login(http)
        return (await http.get("/login", params={"id": flow["pending"]})).text

    page = run(inner, steps)
    assert "Paperless username" in page and "Two-factor code, if enabled" in page
    # The token sits in a collapsed section after the password fields.
    details = page[page.index("<details"):page.index("</details>")]
    assert "Signed in through single sign-on" in details and "My Profile" in details
    assert 'name="token"' in details and 'name="password"' not in details


def test_login_asks_for_the_two_factor_code_when_paperless_does():
    inner, _, _, _ = flow_app(seen={"mfa": True})

    async def steps(http):
        flow = await begin_login(http)
        credentials = {"username": "alice", "password": "pw"}
        r = await http.post("/login", data={"id": flow["pending"], **credentials})
        assert r.status_code == 401 and "MFA code is required" in r.text
        return await finish_login(http, flow, {**credentials, "code": "123456"})

    assert run(inner, steps)["access_token"].startswith(server.OAUTH_TOKEN_PREFIX)


def test_login_with_a_pasted_api_token_for_single_sign_on_accounts():
    """An account that signs in to Paperless through SSO has no password to
    trade; pasting its API token must work, and a wrong one must not."""
    inner, app, calls, seen = flow_app()

    async def steps(http):
        flow = await begin_login(http)
        bad = await http.post("/login", data={"id": flow["pending"], "token": "wrong"})
        assert bad.status_code == 401 and "rejected the API token" in bad.text
        tokens = await finish_login(http, flow, {"token": "Token alice-token"})
        return await statistics(app, f"Bearer {tokens['access_token']}")

    run(inner, steps)
    assert seen["auth"] == "Token alice-token"
    assert not [c for c in calls if c.url.path == "/api/token/"]


def test_empty_login_form_is_not_sent_to_paperless():
    inner, _, calls, _ = flow_app()

    async def steps(http):
        flow = await begin_login(http)
        return await http.post("/login", data={"id": flow["pending"]})

    r = run(inner, steps)
    assert r.status_code == 401 and "username and password" in r.text
    assert calls == []


def test_login_throttle_is_explained():
    # Paperless throttles its login endpoint and says when to retry; pass that on.
    response = httpx2.Response(
        429, json={"detail": "Request was throttled. Expected available in 14 seconds."}
    )
    assert "Expected available in 14 seconds" in server._rejection(response)
    assert server._rejection(httpx2.Response(502, text="<html>")).endswith("HTTP 502")


def test_refresh_rotates_and_keeps_the_paperless_token():
    inner, app, _, seen = flow_app()

    async def steps(http):
        flow = await begin_login(http)
        first = await finish_login(http, flow, {"username": "alice", "password": "pw"})
        r = await http.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"],
            "client_id": flow["client_id"]})
        second = r.json()
        stale = await http.post(server.DEFAULT_PATH, json={},
                                headers={"Authorization": f"Bearer {first['access_token']}"})
        assert stale.status_code == 401
        return await statistics(app, f"Bearer {second['access_token']}")

    run(inner, steps)
    assert seen["auth"] == "Token alice-token"


def test_revoke_kills_our_tokens_and_leaves_the_paperless_token_alone():
    inner, _, calls, _ = flow_app()

    async def steps(http):
        flow = await begin_login(http)
        tokens = await finish_login(http, flow, {"username": "alice", "password": "pw"})
        before = len(calls)
        r = await http.post("/revoke", data={
            "token": tokens["refresh_token"], "client_id": flow["client_id"]})
        assert r.status_code == 200
        dead = await http.post(server.DEFAULT_PATH, json={},
                               headers={"Authorization": f"Bearer {tokens['access_token']}"})
        return before, dead.status_code

    before, status = run(inner, steps)
    assert status == 401
    assert len(calls) == before  # Paperless was not asked anything


@pytest.mark.parametrize("header", ["raw-api-token", "Token raw-api-token",
                                    "Bearer raw-api-token"])
def test_both_mode_accepts_token_mode_headers(header):
    """Token-mode clients send `Authorization: <token>` or Paperless's own
    `Token <token>`; in both mode that must still reach Paperless, as it does
    in token mode."""
    inner, app, _, seen = flow_app("both")

    async def go():
        async with mcp_session(inner, app, header) as c:
            return await c.call_tool("statistics_retrieve", {})

    asyncio.run(go())
    assert seen["auth"] == "Token raw-api-token"


def test_oauth_mode_rejects_a_raw_token():
    inner, app, calls, _ = flow_app("oauth")

    async def steps(http):
        return await http.post(server.DEFAULT_PATH, json={},
                               headers={"Authorization": "Bearer raw-api-token"})

    assert run(inner, steps, app).status_code == 401
    assert calls == []


def test_stale_prefixed_token_is_rejected_even_in_both_mode():
    # An expired/revoked OAuth token must 401 (so the client refreshes), not be
    # forwarded to Paperless as if it were a raw API token.
    inner, app, calls, _ = flow_app("both")

    async def steps(http):
        return await http.post(
            server.DEFAULT_PATH, json={},
            headers={"Authorization": f"Token {server.OAUTH_TOKEN_PREFIX}gone"})

    assert run(inner, steps, app).status_code == 401
    assert calls == []


def assert_explains_reconnect(page: str):
    """Dead-end login pages must say that it isn't the server's fault, and the fix."""
    assert "Nothing is wrong with this server" in page
    assert "remove the connector and add it again" in page
    assert "Reconnect" in page


def test_expired_login_link_is_rejected():
    inner, _, _, _ = flow_app()

    async def steps(http):
        return await http.get("/login", params={"id": "nope"})

    r = run(inner, steps)
    assert r.status_code == 400
    assert_explains_reconnect(r.text)


def test_replayed_login_says_completed_and_asks_paperless_nothing():
    """A browser can re-submit the login form after it succeeded (seen with
    claude.ai's connector flow). The replay must not log in a second time, and
    must not claim the link "expired" -- the login already went through."""
    inner, _, calls, _ = flow_app()

    async def steps(http):
        flow = await begin_login(http)
        form = {"id": flow["pending"], "username": "alice", "password": "pw"}
        first = await http.post("/login", data=form)
        assert first.status_code == 302
        return await http.post("/login", data=form)

    replay = run(inner, steps)
    assert replay.status_code == 200
    assert "already" in replay.text and "expired" not in replay.text
    assert_explains_reconnect(replay.text)
    assert len([c for c in calls if c.url.path == "/api/token/"]) == 1
