"""Drive the OAuth 2.1 browser flow against the live stack (both mode)."""

import base64
import hashlib
import secrets

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from tests.live._client import MCP_URL, PASSWORD, USERNAME, call, run_async

BASE = str(httpx.URL(MCP_URL).copy_with(path="/", query=None)).rstrip("/")
REDIRECT = "http://localhost:9/cb"


def _login(http: httpx.Client) -> dict:
    """register -> authorize -> /login -> token; returns the token JSON + client_id."""
    client_id = http.post("/register", json={
        "client_name": "live-oauth", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }).json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    r = http.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
    })
    pending = httpx.URL(r.headers["location"]).params["id"]
    r = http.post("/login", data={"id": pending, "username": USERNAME, "password": PASSWORD})
    back = httpx.URL(r.headers["location"])
    # RFC 9207: the redirect names the issuer, so a client can't be mixed up.
    assert back.params["iss"].rstrip("/") == BASE
    code = back.params["code"]
    tokens = http.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier,
    }).json()
    return {**tokens, "client_id": client_id}


def test_oauth_access_token_calls_a_tool():
    with httpx.Client(base_url=BASE) as http:
        tokens = _login(http)

    async def go():
        transport = StreamableHttpTransport(
            MCP_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        async with Client(transport) as c:
            return await c.call_tool("statistics_retrieve", {})

    assert run_async(go()).data["documents_total"] >= 6


def test_refresh_rotates_and_old_access_token_is_rejected():
    with httpx.Client(base_url=BASE) as http:
        tokens = _login(http)
        r = http.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
            "client_id": tokens["client_id"],
        })
        assert r.status_code == 200
        assert r.json()["access_token"] != tokens["access_token"]
        stale = http.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert stale.status_code == 401


def test_wrong_password_shows_paperless_reason_and_keeps_the_login_open():
    with httpx.Client(base_url=BASE) as http:
        client_id = http.post("/register", json={
            "client_name": "live-oauth", "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }).json()["client_id"]
        r = http.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
            "code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "s",
        })
        pending = httpx.URL(r.headers["location"]).params["id"]
        form = {"id": pending, "username": USERNAME}
        bad = http.post("/login", data={**form, "password": "wrong"})
        assert bad.status_code == 401 and "Paperless rejected the login" in bad.text
        assert http.post("/login", data={**form, "password": PASSWORD}).status_code == 302


def test_revoke_kills_our_tokens_and_the_paperless_token_lives_on():
    """Revoking one client drops only our pair: Paperless's one API token per
    user is shared with the web UI and every other client."""
    with httpx.Client(base_url=BASE) as http:
        tokens = _login(http)
        r = http.post("/revoke", data={
            "token": tokens["refresh_token"], "client_id": tokens["client_id"],
        })
        assert r.status_code == 200
        dead = http.post(
            "/mcp", json={}, headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert dead.status_code == 401
    # The static token (the same Paperless API token) still works.
    assert run_async(call("statistics_retrieve")).data["documents_total"] >= 6
