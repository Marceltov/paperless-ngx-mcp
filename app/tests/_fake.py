"""Shared pieces for the unit tests: the bundled spec and an in-process MCP
server whose Paperless is an httpx2 mock transport, so nothing touches the
network."""

from contextlib import asynccontextmanager

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

import server

SPEC = server.load_spec(server.BUNDLED_SPEC)
BASE = "http://paperless:8000"
STATISTICS = {"documents_total": 6, "tag_count": 5}


def paperless_client(handler) -> httpx2.AsyncClient:
    """The client build_server would make, with `handler` standing in for Paperless."""
    return httpx2.AsyncClient(
        base_url=BASE,
        auth=server.PaperlessTokenAuth(),
        headers=server.paperless_headers(server.api_version(SPEC)),
        transport=httpx2.MockTransport(handler),
    )


def build(handler, auth=None):
    return server.build_server(client=paperless_client(handler), auth=auth, spec=SPEC)


def asgi_factory(app):
    """An httpx2 client factory that routes requests into an ASGI app."""
    def factory(**kw):
        kw.pop("transport", None)
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://localhost", **kw
        )
    return factory


@asynccontextmanager
async def mcp_session(inner, app, authorization: str):
    """An MCP client talking to `app` (the wrapped form of FastMCP's `inner`)
    with the given Authorization header."""
    # Drive the inner app's lifespan so the streamable-HTTP session manager
    # initializes (the middlewares forward the lifespan scope).
    async with inner.router.lifespan_context(inner):
        transport = StreamableHttpTransport(
            url="http://localhost/mcp",
            headers={"Authorization": authorization},
            httpx_client_factory=asgi_factory(app),
        )
        async with Client(transport) as client:
            yield client
