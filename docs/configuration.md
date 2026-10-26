# Configuration

All configuration is through environment variables, so the server runs cleanly as a sidecar. The [compose examples](compose-examples.md) keep them in an env file next to the compose file.

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `PAPERLESS_URL` | `http://paperless:8000` | Base URL of the Paperless-ngx instance, as this container reaches it. |
| `MCP_TOOLSETS` | *(unset = defaults)* | Comma-separated toolsets to expose, or `all`. `default` stands for the default toolsets, so `default,mail_rules` adds to them. See [Tools](tools.md#toolsets). |
| `PAPERLESS_API_SPEC` | *(unset = download)* | Pin the OpenAPI spec to a file path or URL instead of downloading it from `PAPERLESS_URL`. See [The OpenAPI spec](#the-openapi-spec). |
| `PAPERLESS_SPEC_WAIT` | `60` | Seconds to wait for Paperless to serve the spec at startup. |
| `PAPERLESS_API_VERSION` | *(from the spec)* | API version sent to Paperless in the `Accept` header. |
| `MCP_HOST` | `0.0.0.0` | Interface the MCP server binds to. |
| `MCP_PORT` | `8081` | Port the MCP server listens on. |
| `MCP_PATH` | `/mcp` | HTTP path the MCP endpoint is served at. |
| `MCP_ALLOWED_HOSTS` | *(unset = any)* | Comma-separated `Host` allowlist (DNS-rebinding protection). Unset accepts any Host; set it to restrict. See [Security](security.md#host-allowlist). |
| `MCP_AUTH_MODE` | *(unset = automatic)* | Usually leave unset: setting `MCP_BASE_URL` and `MCP_OAUTH_SECRET` turns on OAuth next to API tokens (`both`), otherwise the server accepts API tokens only (`token`). Set `oauth` to reject raw API tokens. See [Choosing a mode](#choosing-a-mode). |
| `MCP_BASE_URL` | *(unset)* | Public URL clients reach this server at, e.g. `https://paperless-ngx-mcp.example.com`. The OAuth issuer: must be HTTPS (plain `http` only for `localhost`). Required for `oauth` and `both`. |
| `MCP_OAUTH_SECRET` | *(unset)* | Encrypts the OAuth store at `/data/oauth` (mount a volume at `/data`). Required for `oauth` and `both`; changing it logs every OAuth client out. |

## The OpenAPI spec

Paperless-ngx serves its own OpenAPI spec at `/api/schema/` (the page at `/api/schema/view/` is the Swagger UI for it). At startup the Paperless-ngx MCP server downloads it from `PAPERLESS_URL` and generates the tools from it, so they match the Paperless version you run. The container log names the source:

```
OpenAPI spec: http://webserver:8000/api/schema/
```

- **Paperless still booting.** Connection errors and `5xx` answers are retried for `PAPERLESS_SPEC_WAIT` seconds, which covers both containers starting together.
- **Download fails.** The server falls back to the snapshot bundled in the image and says so in the log. The tools then describe the Paperless version the image was built against, which may be newer or older than yours. Fix the URL and restart.
- **After a Paperless update.** Restart the Paperless-ngx MCP server to pick up the new spec; it is read once, at startup.
- **Pinning.** Set `PAPERLESS_API_SPEC` to a file (mounted into the container) or a URL to use exactly that spec. There is no fallback then: a pinned spec that can't be loaded starts the server in the [`startup_error` state](security.md#startup-errors).

The API version is read from the spec too. Paperless writes its version as `2.20.3 (9)`; the number in brackets is sent as `Accept: application/json; version=9`, so responses come in the shapes the spec describes. Without it Paperless answers in its oldest shapes.

## OAuth details

With `MCP_BASE_URL` and `MCP_OAUTH_SECRET` set, clients that support the MCP authorization spec need only the URL. On first connect they open a login page served by this server, you enter your **Paperless username and password**, and the server asks Paperless for your API token (`POST /api/token/`). Paperless creates the token at that moment if you don't have one yet.

- The password goes to Paperless once and is never stored. What is stored, encrypted, is the API token.
- Accounts with two-factor authentication enter their code on the same page.
- Accounts without a password (single sign-on) paste their API token on the login page instead.
- Raw API tokens in the `Authorization` header keep working in the default `both` mode.
- Mount a volume at `/data` (see the [compose examples](compose-examples.md)) so logins survive the container being recreated, for example on an image update.
- Paperless allows five login attempts a minute, counted per client address. Every login through the Paperless-ngx MCP server arrives from the sidecar's address, so that budget is shared by all its users. The login page shows Paperless's "try again in N seconds" when it is used up; raise it with Paperless's `PAPERLESS_TOKEN_THROTTLE_RATE` if you need to.
- A login link stops working after five wrong passwords; the client starts a new login.
- Upgrading from 0.3.x to 0.4.0 logs every OAuth client out once: the login store changed format. Reconnect each connector; raw API tokens are unaffected.

An invalid `MCP_AUTH_MODE`, or an explicit `oauth`/`both` without its variables, starts the server in the [`startup_error` state](security.md#startup-errors).

### Choosing a mode

You rarely need `MCP_AUTH_MODE`. Adding the two OAuth variables is enough to turn OAuth on, and API tokens keep working next to it.

Set it explicitly when:

- **The server is open to the internet and you only want OAuth.** In the default `both` mode, any token the server didn't issue is passed to Paperless as an API token. `oauth` turns that off, so clients can only get in through a login you can revoke on this server.
- **You want a mistake to stop the server.** With `oauth` or `both` set explicitly, a missing `MCP_BASE_URL` or `MCP_OAUTH_SECRET` starts the server in the [`startup_error` state](security.md#startup-errors). When the mode is worked out automatically, the server quietly falls back to token-only and only logs a warning.

`token` turns OAuth off without deleting its variables.
