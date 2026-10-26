# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone MCP server that turns the [Paperless-ngx](https://docs.paperless-ngx.com) REST API into MCP tools. It runs as a **container sidecar** next to a Paperless instance, downloads that instance's OpenAPI spec at startup, and serves the generated tools over streamable **HTTP**. Clients either present their own Paperless API token in the `Authorization` header (forwarded per-request) or log in through OAuth 2.1, where the server trades the Paperless username and password for the user's API token and stores it per client.

It is the Paperless counterpart of [trilium-mcp](https://github.com/Marceltov/trilium-mcp) and deliberately mirrors its structure. The entire server is one module: `app/server.py`.

## Commands

Development uses `uv` (in `app/`) and Docker Compose (at the repo root).

```bash
# Full suite against a fresh, disposable stack (recommended): down -v, build, seed,
# wait for health, pytest, down -v. Falls back to uv's container image without uv.
./run-tests.sh
./run-tests.sh tests/live -q     # extra args pass through to pytest
./run-tests.sh -k upload         # a single test by keyword

# Manual test run (from app/). Live tests auto-skip when the stack is down.
cd app && uv run pytest
cd app && uv run pytest -k test_strip_scheme

# Seeded dev stack: Paperless on :9190 (admin / paperless-mcp), MCP on :9191.
docker compose up -d --build     # rebuild after any change under app/
docker compose down -v           # reset: the instance lives in volumes

# Live stack: only the MCP, against a REAL Paperless (see .env.live.example), on :9192.
docker compose --env-file .env.live -f docker-compose.live.yaml up -d --build
cd app && uv run --env-file ../.env.live python ../dev/live-smoke.py   # read-only
```

Docs: `uvx --with mkdocs-material --with 'mkdocs<2' mkdocs serve` (CI builds with `--strict`). User-facing docs live in `docs/`; the README only links there, so document new config and features in `docs/`. `examples/` holds the compose examples embedded in `docs/compose-examples.md`.

Workflows: `publish.yml` pushes `:<branch>` on every branch push and the versioned tags plus a GitHub release on `v*`; `pages.yml` publishes the docs on pushes to `main`; `cleanup-packages.yml` prunes untagged manifests weekly.

## Rules that are easy to break

- **Live tests are pinned to the dev stack** (`localhost:9190` / `:9191`, in `app/tests/live/_client.py`) and must never read their target from the environment: `.env.live` puts a real instance's `PAPERLESS_URL` and `PAPERLESS_TOKEN` in a developer's shell, and the tests create and delete documents. `test_live_target.py` guards this.
- **Against the live stack, only read.** `dev/live-smoke.py` calls listing and reading tools only; keep it that way.
- **Revoke must not touch Paperless.** Paperless has ONE API token per user, shared by all that user's clients and the web UI. `/api/token/` returns the existing one. Never add a "delete the token on revoke" step, and never expose `/api/token/` or `/api/profile/generate_auth_token/` as tools (`NEVER_EXPOSED`).
- **Every default tool needs a live test**, or an entry with a reason in `_UNTESTED` in `test_coverage.py`.
- **Use `httpx2`, not `httpx`, in the server and the seed.** FastMCP 4 deprecates `httpx` clients in `from_openapi`, and the image ships only `httpx2`; plain `httpx` is a dev dependency for the live tests.
- **`PAPERLESS_TOKEN_THROTTLE_RATE` is raised in the dev stack** because the suite logs in more than Paperless's default five times a minute. A `429` in a live test usually means that setting got lost.

## Architecture

`build_server()` is the core. `resolve_spec()` downloads `PAPERLESS_URL/api/schema/?format=json` (retrying while Paperless boots, `PAPERLESS_SPEC_WAIT`), falls back to the bundled `app/paperless-api.openapi.json`, or uses `PAPERLESS_API_SPEC` (file or URL, no fallback). `api_version()` reads the API version from the spec's `info.version` (`"6.0.0 (10)"` → `10`); it is sent as `Accept: application/json; version=N`, without which Paperless answers in version-1 shapes.

`select_operations()` then trims the spec before `FastMCP.from_openapi` sees it:

- `NEVER_EXPOSED` path prefixes are dropped in every configuration.
- `REPLACED_PATHS` are dropped and replaced by hand-written tools of the same name.
- Toolsets (`toolset_of`: first path segment after `/api/`) not in `MCP_TOOLSETS` / `DEFAULT_TOOLSETS` are dropped; `all` keeps everything, `default` expands to `DEFAULT_TOOLSETS`.
- `PUT` is dropped wherever the path also has `PATCH`.
- `normalize_operation()` repairs what the spec gets wrong for FastMCP: request bodies reduced to JSON (FastMCP takes the first content type listed, sometimes multipart), `fields` made non-exploded (Paperless wants `fields=a,b`), and the notes endpoints' stray query `id` removed except on DELETE (where the tool takes `id__path` and `id__query`). `BODYLESS_OPERATIONS` (`mail_account_process`) lose the body the spec declares but Paperless never reads, so the tool takes only `id`.

Endpoints that don't fit JSON-in/JSON-out, and how each is handled:

- **Non-object responses** (bare arrays, scalars, the bodiless trash endpoints): a *metadata* problem. `describe_tool` (the `mcp_component_fn`) clears every generated output schema.
- **Descriptions and hints**: `describe_tool` also sets each description (`tool_description`: `METHOD /path`, then the hand-written `TOOL_GUIDES` entry, else the spec text with boilerplate replaced and a paging/delete note) and the MCP annotations (`tool_annotations`: from the HTTP method, with `READ_ONLY_POSTS`, `DESTRUCTIVE_POSTS`, `OPEN_WORLD` as exceptions by operation ID). A new POST that deletes must be added to `DESTRUCTIVE_POSTS`. `INSTRUCTIONS` is the server-level guidance.
- **File responses** (`download`, `preview`, `thumb`; the spec wrongly types them `application/json`) and the **multipart upload** (`post_document`): *behavior* problems. Replaced by `register_file_tools` (returns FastMCP `File`/`Image`, capped at `MAX_FILE_BYTES`) and `register_upload_tool`.

`validate_output=False` because live responses don't always match the spec.

### Auth model

In `token` mode the server holds no secret:

1. `TokenCaptureMiddleware` (pure ASGI, so it doesn't buffer the streamable-HTTP response) requires an `Authorization` header, else `401`, and stashes it in the `_incoming_auth` contextvar. `/health` is always open.
2. `PaperlessTokenAuth` (an `httpx.Auth`) reads it on the outgoing call, strips a `Bearer `/`Token ` prefix (`strip_scheme`) and sends `Authorization: Token <token>`.

`MCP_AUTH_MODE` (`token|oauth|both`) picks the gate; see `resolve_auth_mode` (unset → `both` if `MCP_BASE_URL` + `MCP_OAUTH_SECRET` are set, else `token`) and `wrap_app`. In `oauth`/`both`, OAuth comes from [mcp-login-oauth](https://github.com/Marceltov/mcp-login-oauth) (pinned by git tag; ADR 0004): `build_oauth_provider` supplies `LOGIN_FIELDS` (username, password, two-factor code, and an API token in a collapsed single-sign-on group) and `check_login`, which trades them at Paperless's `/api/token/` or checks a pasted token against `/api/profile/`. The grant `{"paperless_token": …}` travels in `get_access_token().claims["grant"]`. There is deliberately no `on_revoke`. In `both`, the library's `BearerPrefixMiddleware(schemes=("Token",))` rewrites raw and `Token …` headers to `Bearer …`, and `verify_static` forwards a non-`pmcp_` bearer as a raw API token. The store is Fernet-encrypted under `/data/oauth` with salt `OAUTH_STORE_SALT`. `MCP_BASE_URL` must be HTTPS or localhost (`validate_base_url`).

### Startup resilience

If the auth configuration is invalid or a pinned spec can't be loaded, `main()` falls back to `build_error_server()`, which completes the MCP handshake but exposes only a `startup_error` tool.

### Configuration

All environment variables, no CLI args: `PAPERLESS_URL`, `PAPERLESS_API_SPEC`, `PAPERLESS_SPEC_WAIT`, `PAPERLESS_API_VERSION`, `MCP_TOOLSETS`, `MCP_HOST`, `MCP_PORT`, `MCP_PATH`, `MCP_ALLOWED_HOSTS`, `MCP_AUTH_MODE`, `MCP_BASE_URL`, `MCP_OAUTH_SECRET`. Host protection is off by default and only restricts when `MCP_ALLOWED_HOSTS` is set.

## Test layout

- `app/tests/_fake.py`: the bundled spec, a mock-Paperless client, and an in-process MCP session helper for the unit tests.
- `test_auth.py`, `test_spec.py`, `test_tools.py`, `test_oauth.py`: unit tests, no network.
- `live/`: against the dev stack; `_client.py` has the pinned target and helpers (`payload`, `rows`, `find_document`, `upload_text`).
- `test_coverage.py`, `test_live_target.py`: guards.

## Dev fixture

The dev Paperless (`docker-compose.yaml`) is created empty and filled by the one-shot `seed` service running `dev/seed/seed.py` through the REST API: 4 correspondents, 4 document types, 5 tags, 1 custom field, and the 6 text files in `dev/seed/documents/`. The MCP container depends on the seed completing. Credentials `admin` / `paperless-ngx-mcp` are committed dev fixtures; never reuse them.

## Diagrams

Mermaid, inline in the docs pages; the architecture flowchart is `docs/diagrams/architecture.mmd`, embedded in two pages via snippets. Check a change with a local `mkdocs serve` in both themes.
