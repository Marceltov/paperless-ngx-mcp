import asyncio

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import server
from tests._fake import STATISTICS, build, mcp_session


@pytest.mark.parametrize(
    "header, token",
    [
        ("abc123", "abc123"),
        ("Bearer abc123", "abc123"),
        ("bearer  abc123 ", "abc123"),
        ("Token abc123", "abc123"),
        ("  abc123  ", "abc123"),
    ],
)
def test_strip_scheme(header, token):
    assert server.strip_scheme(header) == token


def _send(header: str | None) -> httpx2.Request:
    """Run PaperlessTokenAuth over a request with `header` as the captured
    client Authorization, and return the outgoing request."""
    reset = server._incoming_auth.set(header)
    try:
        flow = server.PaperlessTokenAuth().auth_flow(httpx2.Request("GET", "http://p/api/"))
        return next(flow)
    finally:
        server._incoming_auth.reset(reset)


@pytest.mark.parametrize("header", ["abc123", "Bearer abc123", "Token abc123"])
def test_token_is_forwarded_with_the_token_scheme(header):
    # Paperless only accepts `Authorization: Token <token>`, whatever the
    # client's own prefix was.
    assert _send(header).headers["Authorization"] == "Token abc123"


@pytest.mark.parametrize("header", [None, "", "Bearer "])
def test_missing_token_raises(header):
    with pytest.raises(RuntimeError, match="No client Authorization"):
        _send(header)


def _wrapped_app():
    async def echo(request):
        # Echo back whatever token the middleware captured for this request.
        return PlainTextResponse(server._incoming_auth.get() or "")

    async def health(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route(server.DEFAULT_PATH, echo, methods=["GET", "POST"]),
        Route(server.HEALTH_PATH, health, methods=["GET"]),
    ])
    return server.TokenCaptureMiddleware(app)


def test_missing_auth_on_mcp_returns_401():
    r = TestClient(_wrapped_app()).post(server.DEFAULT_PATH)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_health_open_without_auth():
    r = TestClient(_wrapped_app()).get(server.HEALTH_PATH)
    assert r.status_code == 200
    assert r.text == "ok"


def test_auth_header_captured_into_contextvar():
    r = TestClient(_wrapped_app()).post(
        server.DEFAULT_PATH, headers={"Authorization": "Bearer abc123"}
    )
    assert r.status_code == 200
    assert r.text == "Bearer abc123"


def test_full_chain_forwards_the_token_and_api_version():
    """Client sends `Authorization: Bearer <token>`; TokenCaptureMiddleware
    captures it and PaperlessTokenAuth forwards it as `Token <token>`, driven
    through the real ASGI app end-to-end."""
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["accept"] = request.headers.get("Accept")
        if request.url.path == "/api/statistics/":
            return httpx2.Response(200, json=STATISTICS)
        return httpx2.Response(404, json={"detail": "Not found."})

    inner = build(handler).http_app(path=server.DEFAULT_PATH)

    async def run():
        async with mcp_session(inner, server.wrap_app(inner, "token"), "Bearer secret") as c:
            return await c.call_tool("statistics_retrieve", {})

    result = asyncio.run(run())
    assert result.data["documents_total"] == 6
    assert captured["auth"] == "Token secret"
    # Without the version Paperless answers in its version-1 response shapes.
    assert captured["accept"] == "application/json; version=10"
