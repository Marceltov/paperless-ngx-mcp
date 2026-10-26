---
status: accepted
date: 2026-10-26
decision-makers: Marcel Bruckner
---

# OAuth through the shared mcp-login-oauth library, on FastMCP 4

## Context and Problem Statement

ADR 0001 made this server its own OAuth authorization server, with the Paperless login as the login:
username, password and optional two-factor code traded for the user's API token, or a pasted API
token for single-sign-on accounts. The code (provider, login and dead-end pages, encrypted store,
`BearerPrefixMiddleware`) was one of four drifting copies across Marcel's MCP servers. This copy had
no delay after a wrong password, its `/revoke` was the MCP SDK's, which demands a client secret from
public clients, and it neither bound tokens to this server (RFC 8707) nor named the issuer on the
redirect (RFC 9207). The library also needs FastMCP 4, and this server was pinned to FastMCP 3.

## Considered Options

* Keep the in-repo copy and port fixes by hand.
* Use [mcp-login-oauth](https://github.com/Marceltov/mcp-login-oauth), supplying only the login
  fields, the login check and the raw-token passthrough.

## Decision Outcome

Chosen: **mcp-login-oauth** (v0.2.0, pinned by git tag). ADR 0001 still holds: Paperless checks the
login, and revoking drops only this server's tokens, never the Paperless API token, which is one per
user and shared with the web UI. v0.2.0 of the library was cut partly for this server: `LoginField`
gained hints and collapsible groups, so the single-sign-on token field stays in its "Signed in
through single sign-on?" section, and `BearerPrefixMiddleware` learned to rewrite Paperless's
`Token <x>` header.

FastMCP 4 comes with it. Its OpenAPI provider deprecates plain `httpx` clients for `httpx2` (the same
API), so the server and the dev seed use `httpx2`; the generated tools are unchanged.

### Consequences

* Good, because the server gets public-client revoke, resource binding, `iss`, the failed-login delay
  and login links that end after 5 failures, and loses ~350 lines.
* Bad, because stored OAuth logins don't carry over: the store has a new salt, so old entries (another
  record shape) read as misses and OAuth clients log in once more.
* Bad, because the image build needs git in its builder stage until the library is on PyPI.
