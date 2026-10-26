# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Paperless-ngx MCP server — Standalone MCP server exposing the Paperless-ngx REST API over HTTP.
# Copyright (C) 2026 Marcel Bruckner
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License along
# with this program. If not, see <https://www.gnu.org/licenses/>.

import base64
import binascii
import json
import os
import re
import sys
import time
import traceback
from contextvars import ContextVar
from pathlib import Path

import httpx2
import uvicorn
import yaml
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.providers.openapi import OpenAPITool
from fastmcp.utilities.types import File, Image
from key_value.aio.protocols import AsyncKeyValue
from mcp.server.auth.routes import validate_issuer_url
from mcp.types import ToolAnnotations
from mcp_login_oauth import (
    BearerPrefixMiddleware,
    LoginField,
    LoginOAuthProvider,
    LoginRejected,
    build_store,
)
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import PlainTextResponse

# A snapshot of the Paperless-ngx OpenAPI spec ships alongside this server
# (baked into the image). It is only the fallback: at startup the spec is
# downloaded from the Paperless instance itself (see resolve_spec), so the tools
# always match the version that is actually running.
BUNDLED_SPEC = Path(__file__).parent / "paperless-api.openapi.json"
SCHEMA_PATH = "/api/schema/"

# All configuration comes from the environment so the server runs cleanly as a
# container sidecar with no command-line arguments.
SERVER_ENV = "PAPERLESS_URL"               # Base URL of the Paperless-ngx instance
SPEC_ENV = "PAPERLESS_API_SPEC"            # Spec override: a file path or a URL
SPEC_WAIT_ENV = "PAPERLESS_SPEC_WAIT"      # Seconds to wait for Paperless at startup
API_VERSION_ENV = "PAPERLESS_API_VERSION"  # Override the API version sent to Paperless
TOOLSETS_ENV = "MCP_TOOLSETS"              # Comma-separated toolsets, "default", or "all"
MCP_HOST_ENV = "MCP_HOST"                  # Interface the MCP server binds to
MCP_PORT_ENV = "MCP_PORT"                  # Port the MCP server listens on
MCP_PATH_ENV = "MCP_PATH"                  # HTTP path the MCP endpoint is served at
MCP_ALLOWED_HOSTS_ENV = "MCP_ALLOWED_HOSTS"  # comma-separated Host allowlist (see serve)
AUTH_MODE_ENV = "MCP_AUTH_MODE"            # token | oauth | both (see resolve_auth_mode)
BASE_URL_ENV = "MCP_BASE_URL"              # public URL clients reach us at (OAuth issuer)
OAUTH_SECRET_ENV = "MCP_OAUTH_SECRET"      # encrypts the OAuth store at rest

DEFAULT_SERVER_URL = "http://paperless:8000"
DEFAULT_SPEC_WAIT = 60
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8081
DEFAULT_PATH = "/mcp"
HEALTH_PATH = "/health"
AUTH_MODES = ("token", "oauth", "both")
OAUTH_STORE_DIR = Path("/data/oauth")      # mount a volume at /data to keep logins

# The Paperless API has ~160 operations, far more than an assistant needs for
# everyday document work, and every tool costs the client context. A toolset is
# the first path segment after /api/ (see toolset_of); these are on by default.
# MCP_TOOLSETS=all exposes everything that isn't in NEVER_EXPOSED.
DEFAULT_TOOLSETS = (
    "documents", "search", "correspondents", "document_types", "tags",
    "storage_paths", "custom_fields", "saved_views", "share_links", "tasks",
    "trash", "statistics", "bulk_edit_objects",
)

# Never tools, in any toolset (matched as path prefixes):
#   * token, generate_auth_token -- they mint or REPLACE the user's single API
#     token. An MCP client already authenticates through the header, and
#     regenerating the token would cut off every client of that user.
#   * totp, disconnect_social_account -- they change how the account logs in.
#   * oauth/callback -- a browser redirect target for mail-account OAuth.
#   * bulk_download -- returns a ZIP of binaries; nothing a model can read.
#   * update_version -- a multipart file upload FastMCP can't express.
NEVER_EXPOSED = (
    "/api/token/",
    "/api/oauth/callback/",
    "/api/profile/generate_auth_token/",
    "/api/profile/totp/",
    "/api/profile/disconnect_social_account/",
    "/api/documents/bulk_download/",
    "/api/documents/{id}/update_version/",
)

# Generated tools that can't work as generated and are replaced by hand-written
# ones of the same name (see register_file_tools / register_upload_tool).
FILE_PATHS = {
    "documents_download_retrieve": "/api/documents/{id}/download/",
    "documents_preview_retrieve": "/api/documents/{id}/preview/",
    "documents_thumb_retrieve": "/api/documents/{id}/thumb/",
}
UPLOAD_PATH = "/api/documents/post_document/"
UPLOAD_TOOL = "documents_post_document_create"
REPLACED_PATHS = (*FILE_PATHS.values(), UPLOAD_PATH)

# A file travels base64-encoded inside the tool result, so cap what we return.
MAX_FILE_BYTES = 10 * 1024 * 1024

# OAuth (see build_oauth_provider). Issued codes/tokens carry this prefix so a
# stale one is recognizably ours and is never mistaken for a Paperless API token.
OAUTH_TOKEN_PREFIX = "pmcp_"
# v2: the mcp-login-oauth record shape. Entries under the old salt no longer
# decrypt, so they read as misses and their clients simply log in again.
OAUTH_STORE_SALT = "paperless-ngx-mcp-oauth-v2"

# Per-request holder for the incoming client Authorization header. Populated by
# TokenCaptureMiddleware and read by PaperlessTokenAuth when calling Paperless.
_incoming_auth: ContextVar[str | None] = ContextVar("incoming_auth", default=None)


def strip_scheme(value: str) -> str:
    """The bare token from an Authorization value: clients may send it raw, or
    with a `Bearer ` or `Token ` prefix."""
    scheme, _, rest = value.strip().partition(" ")
    return rest.strip() if scheme.lower() in ("bearer", "token") else value.strip()


class PaperlessTokenAuth(httpx2.Auth):
    """Forward the client's Paperless API token to Paperless.

    With OAuth (see build_oauth_provider), FastMCP's request-scoped access
    token carries the API token in its grant (`claims["grant"]["paperless_token"]`).
    Otherwise the token is the raw client header in the `_incoming_auth` contextvar (set by
    TokenCaptureMiddleware). Paperless expects `Authorization: Token <token>`,
    so whatever prefix the client used is replaced with that.
    """

    def auth_flow(self, request: httpx2.Request):
        access = get_access_token()
        raw = access.claims["grant"]["paperless_token"] if access else _incoming_auth.get()
        token = strip_scheme(raw) if raw else ""
        if not token:
            raise RuntimeError(
                "No client Authorization header available for the Paperless call."
            )
        request.headers["Authorization"] = f"Token {token}"
        yield request


class TokenCaptureMiddleware:
    """Pure-ASGI middleware that requires a client Authorization header on the
    MCP endpoint and stashes it for the outgoing Paperless call.

    The token IS the auth: a request without one is rejected with 401 before it
    reaches FastMCP; validity is enforced by Paperless on the actual API call.
    The health check is always allowed. Implemented at the ASGI layer (not
    BaseHTTPMiddleware) so it does not buffer the streamable-HTTP response.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # Forward lifespan / websocket scopes untouched.
            await self.app(scope, receive, send)
            return
        if scope.get("path") == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        authorization = headers.get(b"authorization", b"").decode()
        if not authorization:
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"missing Authorization header"}',
            })
            return
        token = _incoming_auth.set(authorization)
        try:
            await self.app(scope, receive, send)
        finally:
            _incoming_auth.reset(token)


def _rejection(response: httpx2.Response) -> str:
    """Paperless's own reason for refusing a login, when it gives one: wrong
    credentials or a missing two-factor code (`non_field_errors`), or its login
    throttle (`detail`, HTTP 429 -- five attempts a minute by default, counted
    per client address, which for Paperless is this server)."""
    try:
        body = response.json()
        reason = body.get("non_field_errors") or body.get("detail")
    except (ValueError, AttributeError):
        reason = None
    if isinstance(reason, list):
        reason = " ".join(str(x) for x in reason)
    return f"Paperless rejected the login: {reason or f'HTTP {response.status_code}'}"


def server_url() -> str:
    return os.environ.get(SERVER_ENV, DEFAULT_SERVER_URL).strip().rstrip("/")


def parse_spec(text: str, origin: str) -> dict:
    """Parse an OpenAPI document. Paperless serves YAML by default and JSON on
    request; YAML is a superset of JSON, so one fallback covers both."""
    if not text.strip():
        raise RuntimeError(f"OpenAPI spec from {origin} is empty.")
    try:
        spec = json.loads(text)
    except ValueError:
        spec = yaml.safe_load(text)
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise RuntimeError(f"OpenAPI spec from {origin} is not a valid OpenAPI document.")
    return spec


def load_spec(spec_path: Path) -> dict:
    """Parse an on-disk OpenAPI spec (the bundled snapshot, or an override)."""
    if not spec_path.exists():
        raise RuntimeError(f"OpenAPI spec not found at {spec_path}.")
    return parse_spec(spec_path.read_text(), str(spec_path))


def download_spec(url: str, wait: float) -> dict:
    """Download the OpenAPI spec from a running Paperless.

    Paperless takes a while to boot, and in a compose file both containers
    start together, so connection errors and 5xx answers are retried for up to
    `wait` seconds. Any other answer (the schema endpoint needing a login,
    a wrong URL) won't fix itself and fails at once.
    """
    # The schema lives at /api/schema/; /api/schema/view/ is its Swagger UI.
    url = re.sub(r"/view/?$", "/", url)
    deadline = time.monotonic() + wait
    while True:
        try:
            response = httpx2.get(
                url, params={"format": "json"}, timeout=30, follow_redirects=True
            )
            if response.status_code < 500:
                response.raise_for_status()
                return parse_spec(response.text, url)
            problem = f"HTTP {response.status_code}"
        except httpx2.HTTPStatusError as e:
            raise RuntimeError(f"{url} answered HTTP {e.response.status_code}.") from e
        except httpx2.HTTPError as e:
            problem = str(e) or e.__class__.__name__
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{url} not reachable after {wait:g}s ({problem}).")
        time.sleep(2)


def resolve_spec() -> tuple[dict, str]:
    """The OpenAPI spec the tools are generated from, and where it came from.

    Default: download it from the Paperless instance, so the tools match the
    version that is running, and fall back to the bundled snapshot if that
    fails. PAPERLESS_API_SPEC pins it to a file or URL instead, with no
    fallback: an explicit choice that doesn't work is an error.
    """
    wait = float(os.environ.get(SPEC_WAIT_ENV, DEFAULT_SPEC_WAIT))
    override = os.environ.get(SPEC_ENV, "").strip()
    if override:
        if override.startswith(("http://", "https://")):
            return download_spec(override, wait), override
        return load_spec(Path(override)), override
    url = server_url() + SCHEMA_PATH
    try:
        return download_spec(url, wait), url
    except RuntimeError as e:
        print(f"Could not download the OpenAPI spec: {e} Falling back to the "
              f"bundled snapshot, which may not match your Paperless version.",
              file=sys.stderr)
        return load_spec(BUNDLED_SPEC), f"{BUNDLED_SPEC.name} (bundled)"


def api_version(spec: dict) -> str | None:
    """The API version to request, from the spec's `info.version`, which
    Paperless writes as "<app version> (<api version>)".

    Without it Paperless answers in its oldest (version 1) response shapes,
    which don't match the spec the tools were generated from.
    """
    override = os.environ.get(API_VERSION_ENV, "").strip()
    if override:
        return override
    match = re.search(r"\((\d+)\)\s*$", str(spec.get("info", {}).get("version", "")))
    return match.group(1) if match else None


def paperless_headers(version: str | None) -> dict[str, str]:
    accept = "application/json"
    return {"Accept": f"{accept}; version={version}" if version else accept}


def toolset_of(path: str) -> str:
    """`/api/documents/{id}/notes/` -> `documents`."""
    parts = path.strip("/").split("/")
    return parts[1] if len(parts) > 1 and parts[0] == "api" else parts[0]


def resolve_toolsets() -> set[str] | None:
    """The enabled toolsets from MCP_TOOLSETS; None means all of them.

    `default` stands for DEFAULT_TOOLSETS, so `default,mail_rules` adds to the
    defaults instead of replacing them.
    """
    raw = os.environ.get(TOOLSETS_ENV, "").strip().lower()
    if not raw:
        return set(DEFAULT_TOOLSETS)
    names = {n.strip() for n in raw.split(",") if n.strip()}
    if "all" in names:
        return None
    if "default" in names:
        names = (names - {"default"}) | set(DEFAULT_TOOLSETS)
    return names


def normalize_operation(method: str, op: dict) -> dict:
    """Fix the spots where the spec, taken literally, yields a tool that can't
    make the request Paperless expects.

    * Request bodies are offered as JSON, form and multipart alike. FastMCP
      takes the first one listed, and where that is multipart (selection_data)
      it sends a body Paperless can't parse. Keep only JSON when it's offered.
    * `fields` is declared as a plain array, which serializes as repeated
      `fields=a&fields=b`; Paperless reads one comma-separated value and would
      silently keep only the last. Declare it non-exploded.
    * The notes endpoints declare a query `id` ("Note ID to delete, used only
      for DELETE") next to the path `id`. FastMCP renames colliding parameters
      to `id__path` / `id__query`, which is needed for DELETE but only obscures
      the document ID on GET and POST. Drop the query twin there.
    * BODYLESS_OPERATIONS declare a body Paperless never reads; drop it, so the
      tool takes only the ID instead of demanding the account's password.
    """
    op = dict(op)
    if op.get("operationId") in BODYLESS_OPERATIONS:
        op.pop("requestBody", None)
    content = (op.get("requestBody") or {}).get("content") or {}
    if "application/json" in content and len(content) > 1:
        op["requestBody"] = {
            **op["requestBody"],
            "content": {"application/json": content["application/json"]},
        }
    parameters = op.get("parameters") or []
    in_path = {p["name"] for p in parameters if p.get("in") == "path"}
    fixed = []
    for param in parameters:
        if param.get("in") == "query":
            if param["name"] in in_path and method != "delete":
                continue
            if param["name"] == "fields" and param.get("schema", {}).get("type") == "array":
                param = {**param, "style": "form", "explode": False}
        fixed.append(param)
    if parameters:
        op["parameters"] = fixed
    return op


def select_operations(spec: dict, toolsets: set[str] | None) -> dict:
    """Trim the spec to the operations that become generated tools.

    Dropped, in order: everything in NEVER_EXPOSED; the operations replaced by
    hand-written tools; toolsets that aren't enabled; and PUT wherever the same
    path has PATCH -- a full replace needs every field re-sent, so the partial
    update does the same job with half the tools and no accidental resets.
    What remains goes through normalize_operation.
    """
    paths = {}
    for path, item in spec["paths"].items():
        if path.startswith(NEVER_EXPOSED) or path in REPLACED_PATHS:
            continue
        if toolsets is not None and toolset_of(path) not in toolsets:
            continue
        kept = {
            method: normalize_operation(method, op) if _is_operation(op) else op
            for method, op in item.items()
            if not (method == "put" and "patch" in item)
        }
        if any(_is_operation(op) for op in kept.values()):
            paths[path] = kept
    return {**spec, "paths": paths}


def _is_operation(value) -> bool:
    return isinstance(value, dict) and "operationId" in value


def register_health(mcp: FastMCP) -> None:
    """Add an unauthenticated health endpoint for container healthchecks."""

    @mcp.custom_route(HEALTH_PATH, methods=["GET"])
    async def health(_request: Request):
        return PlainTextResponse("ok")


# Server-level guidance, sent to every client at initialize.
INSTRUCTIONS = """\
Tools for one Paperless-ngx document archive, acting as the logged-in user.

Working with it:
- Correspondents, document types, tags, storage paths and custom fields are \
referenced by numeric ID. Look the ID up first (tags_list, correspondents_list, \
document_types_list with `name__icontains`) and filter with it.
- An archive holds hundreds to thousands of documents, and every document \
carries its full OCR text in `content`. Never page through documents_list to \
find something: filter or search on the server, pass `fields` to leave out \
`content`, and keep `page_size` small. The `count` in a list result answers \
"how many" without reading the pages.
- Read the full text of a document with documents_retrieve, one document at a \
time, once you know which one you need.
- Changes apply at once and to the real archive. Tools marked destructive \
delete or overwrite: say what will be affected and get the user's \
confirmation first, above all for bulk_edit, bulk_edit_objects, \
documents_delete and trash_create.
"""

# POST operations that only read (the method alone would mark them as writes).
# Operations whose request body the spec declares but Paperless ignores: the
# mail account process action reads only the ID from the path, yet the spec
# gives it the whole account (with the password) as a required body.
BODYLESS_OPERATIONS = frozenset({"mail_account_process"})

READ_ONLY_POSTS = frozenset({
    "documents_selection_data_create", "storage_paths_test",
})
# POST operations that delete, or overwrite a document's file.
DESTRUCTIVE_POSTS = frozenset({
    "bulk_edit", "bulk_edit_objects", "documents_delete", "trash_create",
    "documents_merge", "documents_edit_pdf", "documents_rotate",
    "documents_remove_password", "processed_mail_bulk_delete",
    "users_deactivate_totp_create",
})
# Operations that reach beyond Paperless: they send mail or contact a mailbox.
OPEN_WORLD = frozenset({
    "documents_email_document", "email_documents", "mail_account_test",
    "mail_account_process",
})

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

LIST_NOTE = (
    "Paginated: returns `count` (total matches), `next`, `previous` and "
    "`results`. `page_size` defaults to 25; fetch further pages with `page`."
)
DELETE_NOTE = "Deletes permanently; this cannot be undone."

# Text that says nothing about the endpoint: docstrings of shared Paperless
# mixins that the spec repeats on dozens of unrelated operations, and the
# "Executes GET /api/..." FastMCP fills in where the spec has none. Replaced
# by a sentence built from the tool's name (see summary_from_name).
BOILERPLATE = (
    "Prefetch Django-Guardian permissions", "Mixin to add document count",
    "Pass a user object to serializer", "Executes ",
)
# The plain collection and item paths, the only ones the sentences fit.
CRUD_PATH = re.compile(r"^/api/[a-z_]+/(\{id\}/)?$")
VERBS = {
    "list": "List {noun}.",
    "retrieve": "Get one of the {noun} by ID.",
    "create": "Create a new entry in {noun}.",
    "partial_update": "Change fields of one of the {noun}; fields left out keep their value.",
    "update": "Replace one of the {noun}; every writable field must be sent.",
    "destroy": "Delete one of the {noun}.",
}

# Hand-written descriptions for the tools a model reaches for most, and for
# the ones where a wrong call is expensive. They replace the spec's text.
TOOL_GUIDES = {
    "documents_list": """\
List, filter or search documents. Returns `count` (total matches), `next`, \
`previous` and `results`.

Keep the result small. Each document includes `content`, its full OCR text, \
often several thousand characters. Unless you need the text, pass `fields` \
(for example id,title,created,correspondent,document_type,tags), and keep \
`page_size` low (default 25). To count, ask for `page_size=1` and read `count`.

Narrow on the server rather than paging:
- `query`: full-text search over content, title and metadata; results are \
ranked and each gets a `__search_hit__` with a highlighted snippet.
- `title__icontains`, `content__icontains`, `original_filename__icontains`.
- `correspondent__id`, `document_type__id`, `storage_path__id` (or \
`..__id__in` for several, `..__isnull=true` for none assigned).
- `tags__id__all` (has every tag), `tags__id__in` (has any), \
`tags__id__none`, `is_tagged`, `is_in_inbox`.
- `created__date__gte` / `created__date__lte` (YYYY-MM-DD), `created__year`, \
`created__month`; the same for `added` and `modified`.
- `custom_field_query`, `has_custom_fields`, `archive_serial_number`.
Filters take IDs, not names: look them up with correspondents_list, \
document_types_list, tags_list or storage_paths_list first.

`ordering` sorts by a field, `-` for descending, e.g. `-created`.""",
    "documents_retrieve": """\
Get one document by ID: its metadata and, in `content`, the full extracted \
text. This is the way to read a document; the download and preview tools \
return the binary file instead. Correspondent, document type, storage path \
and tags come back as IDs. Pass `fields` to fetch only some fields, e.g. \
`content` alone or everything but it.""",
    "search_retrieve": """\
Global search across object types: returns matching documents, correspondents, \
document types, storage paths, tags, saved views, custom fields and more, at \
most three of each, for one `query` string. Use it to find out what a name \
refers to. The documents come with their full `content` text and there is no \
`fields` option. To search documents with filters, paging or `fields`, use documents_list with \
`query` instead. `db_only=true` matches titles and names only, without the \
full-text index.""",
    "search_autocomplete_list": """\
Suggest words from the full-text index that start with `term`, most relevant \
first (`limit`, default 10). Useful to find how a word is spelled in the \
archive before searching for it.""",
    "documents_partial_update": """\
Change some fields of one document and leave the rest as they are: title, \
created, correspondent, document_type, storage_path, tags, \
archive_serial_number, custom_fields, owner. `tags` and `custom_fields` \
replace the whole list, so send the full new list (read the document first), \
or use bulk_edit with `add_tag` / `remove_tag` / `modify_tags` to change tags \
without resending them. To change many documents at once use bulk_edit.""",
    "documents_destroy": """\
Delete one document. It moves to the trash, where trash_create can restore it \
until Paperless empties the trash (30 days by default); after that the file \
and its metadata are gone.""",
    "documents_delete": """\
Move documents to the trash: those listed in `documents`, or with `all=true` \
every document matching `filters` (minus `excluded_documents`). `all=true` \
without filters trashes the whole archive. Trashed documents can be restored \
with trash_create until the trash is emptied.""",
    "bulk_edit": """\
Apply one `method` to many documents: the IDs in `documents`, or with \
`all=true` every document matching `filters` (minus `excluded_documents`). \
Check the selection first with documents_list using the same filters.

Methods and their `parameters`:
- `set_correspondent` {correspondent}, `set_document_type` {document_type}, \
`set_storage_path` {storage_path}: an ID, or null to clear.
- `add_tag` {tag}, `remove_tag` {tag}, `modify_tags` {add_tags, remove_tags}.
- `modify_custom_fields` {add_custom_fields, remove_custom_fields}.
- `set_permissions` {owner, set_permissions, merge}.
- `reprocess`: run OCR again.
- `delete`: move to the trash.
- `rotate` {degrees}, `merge` {metadata_document_id, delete_originals}, \
`split` {pages, delete_originals}, `delete_pages` {pages}, `edit_pdf`, \
`remove_password` {password}: these rewrite or replace the stored files.

The file-changing methods and `delete` can't be undone from here. They run \
as background tasks, so the call returns before the work is done.""",
    "bulk_edit_objects": """\
Change or delete many tags, correspondents, document types or storage paths \
at once (`object_type`): the IDs in `objects`, or all of them with \
`all=true`. `operation` is `set_permissions` (with `owner`, `permissions`, \
`merge`) or `delete`. `delete` is permanent: the objects are removed from \
every document that used them, and there is no trash for them.""",
    "trash_list": """\
List the documents in the trash (deleted, not yet purged). Paginated like \
documents_list, and each entry includes the full `content` text, so keep \
`page_size` small.""",
    "trash_create": """\
Act on trashed documents. `action=restore` puts the documents in `documents` \
back into the archive. `action=empty` deletes them for good, file and \
metadata; with `documents` left out it empties the ENTIRE trash. Emptying \
cannot be undone.""",
    "tags_list": """\
List tags, each with its ID, name, `document_count`, `parent` and \
`children`. Find one by name with `name__icontains` or `name__iexact` rather \
than listing all; the ID is what the document filters and updates take.""",
    "correspondents_list": """\
List correspondents (the senders or recipients of documents), each with its \
ID, name, `document_count` and `last_correspondence`. Find one by name with \
`name__icontains` or `name__iexact`; the ID is what the document filters and \
updates take.""",
    "document_types_list": """\
List document types (invoice, contract, ...), each with its ID, name and \
`document_count`. Find one by name with `name__icontains` or `name__iexact`; \
the ID is what the document filters and updates take.""",
    "storage_paths_list": """\
List storage paths (the rules for where files are filed), each with its ID, \
name, `path` template and `document_count`. Filter by `name__icontains`.""",
    "custom_fields_list": """\
List the custom field definitions: ID, name, `data_type` and, for select \
fields, the options. A document's `custom_fields` refer to these by ID.""",
    "statistics_retrieve": """\
Totals for the archive in one small result: number of documents, documents in \
the inbox, total characters, file types, and counts of tags, correspondents, \
document types and storage paths. Use it for "how many" questions instead of \
listing.""",
    "tasks_list": """\
List background tasks (file consumption, OCR, bulk edits). The list grows \
without bound, so filter: `task_id` for the UUID an upload returned, \
`status` (pending, started, success, failure), `is_complete`, and a small \
`page_size`. A finished consumption task lists the new document in \
`related_document_ids`.""",
    "documents_selection_data_create": """\
For a set of documents (`documents`, or `all=true` with `filters`), count how \
many carry each tag, correspondent, document type and storage path. Reads \
only; a cheap way to summarise a selection without fetching the documents.""",
}


def tool_annotations(method: str, name: str) -> ToolAnnotations:
    """Behaviour hints for a generated tool, from its HTTP method.

    GET reads. DELETE and PUT are destructive (PUT replaces the whole object)
    and idempotent. PATCH is an idempotent edit of single fields. POST creates,
    unless it is listed above as a read or as a destructive action.
    """
    method = method.upper()
    read_only = method == "GET" or name in READ_ONLY_POSTS
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=not read_only
        and (method in ("DELETE", "PUT") or name in DESTRUCTIVE_POSTS),
        idempotentHint=read_only or method in ("DELETE", "PUT", "PATCH"),
        openWorldHint=name in OPEN_WORLD,
    )


def summary_from_name(path: str, name: str) -> str:
    """A plain sentence for a CRUD tool, e.g. `saved_views_list` -> "List saved
    views." Empty for action endpoints and names without a known verb.
    """
    if not CRUD_PATH.match(path):
        return ""
    for verb, sentence in VERBS.items():
        if name.endswith(f"_{verb}"):
            return sentence.format(noun=name[: -len(verb) - 1].replace("_", " "))
    return ""


def tool_description(route, name: str, spec_text: str) -> str:
    """The description of a generated tool: the endpoint, then a hand-written
    guide where there is one, else the spec's text plus a note on what the
    method implies (paging, permanent deletion).
    """
    method = route.method.upper()
    parts = [f"{method} {route.path}"]
    if name in TOOL_GUIDES:
        parts.append(TOOL_GUIDES[name].strip())
    else:
        if spec_text.startswith(BOILERPLATE):
            spec_text = summary_from_name(route.path, name)
        if spec_text:
            parts.append(spec_text)
        if method == "GET" and any(p.name == "page_size" for p in route.parameters):
            parts.append(LIST_NOTE)
        elif method == "DELETE":
            parts.append(DELETE_NOTE)
    return "\n\n".join(parts)


def describe_tool(route, component) -> None:
    """Fix up the metadata of a generated tool (mcp_component_fn, see build_server).

    * Lead the description with the method and path, and follow it with a
      hand-written guide or a note on the method (see tool_description). Many
      Paperless operations carry a shared mixin's docstring as their
      description ("Prefetch Django-Guardian permissions ..."), which says
      nothing about the endpoint.
    * Add the read-only / destructive / idempotent hints (tool_annotations).
    * Clear the output schema. With validate_output=False FastMCP reduces it to
      "any object", which is wrong for the many endpoints that answer with a
      bare array, a string or a number (notes, history, next_asn, bulk_edit) or
      with no body at all (trash): clients that check a result against its
      schema then reject a call that succeeded. Without a schema the JSON is
      returned as is.
    """
    if not isinstance(component, OpenAPITool):
        return
    component.description = tool_description(
        route, component.name, (component.description or "").strip()
    )
    component.annotations = tool_annotations(route.method, component.name)
    component.output_schema = None


def register_file_tools(mcp: FastMCP, client: httpx2.AsyncClient) -> None:
    """Register working replacements for the three file-returning tools.

    The spec types download, preview and thumb as `application/json`, but they
    return the file itself. FastMCP's generated tool tries to `response.json()`
    every response and only catches `json.JSONDecodeError`, so a PDF or image
    body raises an uncaught `UnicodeDecodeError`. Here we fetch the bytes and
    return them as a file or image the client can render. The `client` carries
    the same per-request Paperless auth as the generated tools.
    """

    async def fetch(path: str, document_id: int, params: dict | None = None):
        response = await client.get(
            path.replace("{id}", str(document_id)), params=params
        )
        response.raise_for_status()
        if len(response.content) > MAX_FILE_BYTES:
            raise ValueError(
                f"The file is {len(response.content)} bytes, over the "
                f"{MAX_FILE_BYTES}-byte limit for a tool result. Read the "
                f"document's text with documents_retrieve instead."
            )
        mime = response.headers.get("content-type", "").split(";")[0].strip()
        subtype = mime.split("/")[-1] or "octet-stream"
        if mime.startswith("image/"):
            return Image(data=response.content, format=subtype)
        # A text file comes back as readable text, anything else base64-encoded.
        return File(data=response.content, format=subtype, name=f"document-{document_id}")

    @mcp.tool(name="documents_download_retrieve", annotations=READ_ONLY)
    async def download(id: int, original: bool = False):
        """Download a document's file (GET /api/documents/{id}/download/).

        Returns the archived, OCRed PDF by default, or the file as it was
        uploaded with `original=true`. To read a document's text, use
        documents_retrieve instead: its `content` field is the extracted text.
        """
        return await fetch(
            FILE_PATHS["documents_download_retrieve"], id,
            {"original": "true"} if original else None,
        )

    @mcp.tool(name="documents_preview_retrieve", annotations=READ_ONLY)
    async def preview(id: int):
        """Fetch a document's inline preview, usually the archived PDF
        (GET /api/documents/{id}/preview/)."""
        return await fetch(FILE_PATHS["documents_preview_retrieve"], id)

    @mcp.tool(name="documents_thumb_retrieve", annotations=READ_ONLY)
    async def thumb(id: int):
        """Fetch the thumbnail image of a document's first page
        (GET /api/documents/{id}/thumb/)."""
        return await fetch(FILE_PATHS["documents_thumb_retrieve"], id)


def register_upload_tool(mcp: FastMCP, client: httpx2.AsyncClient) -> None:
    """Register a working replacement for the generated upload tool.

    `/api/documents/post_document/` takes a multipart form with the file in it.
    FastMCP's generated tool would send the form as JSON and has no way to carry
    file bytes, so Paperless rejects it. This one builds the multipart request
    itself. The `client` carries the same per-request Paperless auth.
    """

    @mcp.tool(
        name=UPLOAD_TOOL,
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def upload(
        filename: str,
        content: str | None = None,
        content_base64: str | None = None,
        title: str | None = None,
        created: str | None = None,
        correspondent: int | None = None,
        document_type: int | None = None,
        storage_path: int | None = None,
        tags: list[int] | None = None,
        archive_serial_number: int | None = None,
        custom_fields: list[int] | None = None,
    ) -> str:
        """Upload a new document (POST /api/documents/post_document/).

        Give the file as `content` (text, for a .txt, .md or .eml file) or as
        `content_base64` (any other type, base64-encoded); `filename` needs the
        right extension, because Paperless picks its parser from it. The other
        arguments are optional metadata: IDs of an existing correspondent,
        document type, storage path, tags and custom fields, and `created` as
        an ISO date.

        Paperless consumes the file in the background. This returns the task
        UUID at once; look it up with tasks_list (`task_id`) to see when the
        document exists and which ID it got.
        """
        if (content is None) == (content_base64 is None):
            raise ValueError("Give exactly one of `content` and `content_base64`.")
        if content_base64 is not None:
            try:
                data = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as e:
                raise ValueError(f"`content_base64` is not valid base64: {e}") from e
        else:
            data = content.encode("utf-8")
        scalars = {
            "title": title, "created": created, "correspondent": correspondent,
            "document_type": document_type, "storage_path": storage_path,
            "archive_serial_number": archive_serial_number,
        }
        # A list of pairs, so `tags` and `custom_fields` repeat as form fields.
        fields = [(k, str(v)) for k, v in scalars.items() if v is not None]
        fields += [("tags", str(t)) for t in tags or []]
        fields += [("custom_fields", str(f)) for f in custom_fields or []]
        response = await client.post(
            UPLOAD_PATH,
            files=[("document", (filename, data)), *((k, (None, v)) for k, v in fields)],
        )
        if response.status_code == 400:
            raise ValueError(f"Paperless rejected the upload: {response.text}")
        response.raise_for_status()
        return f"Queued for consumption; task {response.json()}."


def resolve_auth_mode() -> str:
    """Pick token / oauth / both from the environment.

    Unset MCP_AUTH_MODE means `both` when OAuth is configured, else `token` with
    a warning -- so a deployment without the OAuth variables just works.
    An explicit oauth/both without its config is an error (-> startup_error).
    """
    mode = os.environ.get(AUTH_MODE_ENV, "").strip().lower()
    missing = [
        v for v in (BASE_URL_ENV, OAUTH_SECRET_ENV) if not os.environ.get(v, "").strip()
    ]
    if not mode:
        if missing:
            print(f"OAuth disabled: {' and '.join(missing)} not set; accepting raw "
                  f"Paperless API tokens only.", file=sys.stderr)
            return "token"
        return "both"
    if mode not in AUTH_MODES:
        raise RuntimeError(
            f"{AUTH_MODE_ENV}={mode!r} is invalid; use one of {', '.join(AUTH_MODES)}."
        )
    if mode != "token" and missing:
        raise RuntimeError(f"{AUTH_MODE_ENV}={mode} requires {' and '.join(missing)}.")
    return mode


def validate_base_url() -> str:
    """MCP_BASE_URL as an OAuth issuer: HTTPS, or plain http for localhost only."""
    base_url = os.environ[BASE_URL_ENV].strip().rstrip("/")
    try:
        validate_issuer_url(AnyHttpUrl(base_url))
    except ValueError as e:
        raise RuntimeError(
            f"{BASE_URL_ENV}={base_url!r} is not a valid OAuth issuer: {e} "
            f"(plain http is only allowed for localhost)."
        ) from e
    return base_url


def build_oauth_store() -> AsyncKeyValue:
    """Fernet-encrypted file store under OAUTH_STORE_DIR (mcp-login-oauth)."""
    return build_store(OAUTH_STORE_DIR, os.environ[OAUTH_SECRET_ENV], OAUTH_STORE_SALT)


SSO_GROUP = "Signed in through single sign-on, with no Paperless password?"
LOGIN_FIELDS = [
    LoginField("username", "Paperless username", autocomplete="username"),
    LoginField("password", "Paperless password", type="password",
               autocomplete="current-password"),
    LoginField("code", "Two-factor code, if enabled", autocomplete="one-time-code",
               inputmode="numeric"),
    LoginField("token", "API token", type="password", autocomplete="off",
               group=SSO_GROUP,
               hint="Paste the API token from My Profile in Paperless instead, and "
                    "leave the fields above empty."),
]


def build_oauth_provider(
    mode: str,
    paperless: httpx2.AsyncClient | None = None,
    store: AsyncKeyValue | None = None,
) -> LoginOAuthProvider:
    """OAuth whose login is the Paperless login (mcp-login-oauth).

    Username, password and optional two-factor code are traded for the user's
    API token at Paperless's /api/token/ (which creates it if the user has
    none). Single-sign-on accounts have no password to trade, so they paste the
    token instead, checked against /api/profile/. The token is the grant, read
    by PaperlessTokenAuth. In `both` mode a bearer that isn't one of ours is
    forwarded as a raw API token; Paperless judges it. `paperless` and `store`
    are injectable for testing.

    There is no on_revoke: Paperless keeps ONE API token per user, shared by
    every client of that user and by the web UI, so revoking one client must
    never touch it.
    """
    paperless = paperless or httpx2.AsyncClient(base_url=server_url(), timeout=60)

    async def check_login(form) -> dict:
        # Credentials go straight to Paperless and are never stored or logged.
        pasted = strip_scheme(form["token"])
        if pasted:
            # Any authenticated endpoint tells us whether the token is real.
            response = await paperless.get(
                "/api/profile/", headers={"Authorization": f"Token {pasted}"}
            )
            if not response.is_success:
                raise LoginRejected(
                    f"Paperless rejected the API token (HTTP {response.status_code})."
                )
            return {"paperless_token": pasted}
        if not form["username"] or not form["password"]:
            raise LoginRejected("Enter your username and password, or an API token.")
        body = {"username": form["username"], "password": form["password"]}
        if code := form["code"].strip():
            body["code"] = code
        response = await paperless.post("/api/token/", json=body)
        if not response.is_success:
            raise LoginRejected(_rejection(response))
        return {"paperless_token": response.json()["token"]}

    async def verify_static(token: str) -> dict:
        return {"paperless_token": token}

    return LoginOAuthProvider(
        base_url=validate_base_url(),
        store=store if store is not None else build_oauth_store(),
        service_name="Paperless-ngx",
        fields=LOGIN_FIELDS,
        check_login=check_login,
        verify_static=verify_static if mode == "both" else None,
        token_prefix=OAUTH_TOKEN_PREFIX,
    )


def build_server(
    client: httpx2.AsyncClient | None = None,
    auth: LoginOAuthProvider | None = None,
    spec: dict | None = None,
) -> FastMCP:
    """Turn the Paperless OpenAPI spec into a FastMCP server, one tool per
    enabled operation. The API token is supplied per request by the client (see
    TokenCaptureMiddleware / PaperlessTokenAuth), so no token is read here.

    `client` and `spec` are injectable for testing; in production the spec is
    downloaded from Paperless (see resolve_spec) and the default client targets
    PAPERLESS_URL and authenticates from the per-request token. `auth` enables
    OAuth (see build_oauth_provider); None keeps plain token pass-through.
    """
    if spec is None:
        spec, source = resolve_spec()
        print(f"OpenAPI spec: {source}", file=sys.stderr)
    version = api_version(spec)
    if client is None:
        client = httpx2.AsyncClient(
            base_url=server_url(),
            auth=PaperlessTokenAuth(),
            headers=paperless_headers(version),
            timeout=60,
        )

    # Most of the API fits FastMCP's JSON-in/JSON-out assumption. What doesn't
    # is handled in one of three ways, because it fails at different layers:
    #
    #   * RESPONSES that aren't a JSON object (bare arrays and scalars, the
    #     bodiless trash endpoints) -- a *metadata* problem, fixed by clearing
    #     the output schema -> describe_tool.
    #   * FILE RESPONSES (download, preview, thumb) and the multipart FILE
    #     UPLOAD (post_document) -- *behavior* problems that need a real
    #     request, so the generated tools are left out of the spec and replaced
    #     -> register_file_tools / register_upload_tool.
    #   * operations that must never be tools at all -> NEVER_EXPOSED.
    #
    # mcp_component_fn can only adjust component metadata, so it can't fix the
    # behavior cases; that's why they are handled by exclusion + replacement.
    mcp = FastMCP.from_openapi(
        openapi_spec=select_operations(spec, resolve_toolsets()),
        client=client,
        name="Paperless-ngx MCP",
        instructions=INSTRUCTIONS,
        auth=auth,
        # The live API doesn't always match its own spec (nullable fields typed
        # as plain strings, paginated wrappers), so response validation would
        # reject otherwise-successful calls. Return the real response instead.
        validate_output=False,
        mcp_component_fn=describe_tool,
    )
    register_file_tools(mcp, client)
    register_upload_tool(mcp, client)
    register_health(mcp)
    return mcp


def build_error_server(error: BaseException) -> FastMCP:
    """Stand-in MCP server that reports a startup failure over a live
    connection instead of dying with an opaque error. Reached if the OpenAPI
    spec can't be loaded or the auth configuration is invalid.
    """
    summary = str(error).strip() or error.__class__.__name__
    detail = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    instructions = (
        f"This Paperless-ngx MCP server FAILED TO START and exposes no Paperless "
        f"tools.\n\nReason: {summary}\n\nFix the configuration and restart. Call "
        f"the `startup_error` tool for the full error."
    )
    mcp = FastMCP(
        name="Paperless-ngx MCP (startup failed)",
        instructions=instructions,
    )
    register_health(mcp)

    @mcp.tool
    def startup_error() -> str:
        """Explain why this Paperless-ngx MCP server failed to start."""
        return (
            "The Paperless-ngx MCP server failed to start, so no Paperless tools "
            f"are available.\n\n--- Full error ---\n{detail}"
        )

    return mcp


def wrap_app(inner, mode: str):
    """Put the auth-mode's ASGI gate in front of FastMCP's app."""
    if mode == "token":
        return TokenCaptureMiddleware(inner)
    if mode == "both":
        # Token-mode clients send a raw token or Paperless's own `Token <x>`;
        # FastMCP only reads Bearer, then verify_static passes it through.
        return BearerPrefixMiddleware(inner, schemes=("Token",))
    return inner


def serve(mcp: FastMCP, mode: str = "token") -> None:
    """Serve an MCP server over streamable HTTP using the MCP_* environment
    configuration. In `token` mode TokenCaptureMiddleware gates the endpoint;
    otherwise FastMCP's OAuth middleware does (see build_oauth_provider)."""
    host = os.environ.get(MCP_HOST_ENV, DEFAULT_HOST)
    port = int(os.environ.get(MCP_PORT_ENV, DEFAULT_PORT))
    path = os.environ.get(MCP_PATH_ENV, DEFAULT_PATH)

    # FastMCP's streamable-HTTP transport does DNS-rebinding protection: by
    # default it 421s any Host header that isn't localhost. This server is meant
    # to be reached by LAN IP or (behind a reverse proxy) a public domain, and
    # the token is the real gate -- so leave the Host allowlist open by default
    # and only restrict when MCP_ALLOWED_HOSTS is set.
    allowed = os.environ.get(MCP_ALLOWED_HOSTS_ENV, "").strip()
    if allowed:
        hosts = [h.strip() for h in allowed.split(",") if h.strip()]
        inner = mcp.http_app(path=path, allowed_hosts=hosts)
        print(f"Host protection ON; allowed hosts (plus localhost): {hosts}",
              file=sys.stderr)
    else:
        inner = mcp.http_app(path=path, host_origin_protection=False)
        print(f"Host protection OFF (any Host accepted) -- set "
              f"{MCP_ALLOWED_HOSTS_ENV} to restrict.", file=sys.stderr)
    app = wrap_app(inner, mode)

    print(f"Serving Paperless-ngx MCP on http://{host}:{port}{path} "
          f"(auth mode: {mode})",
          file=sys.stderr)
    uvicorn.run(app, host=host, port=port)


def main():
    mode = "token"
    try:
        mode = resolve_auth_mode()
        auth = None if mode == "token" else build_oauth_provider(mode)
        mcp = build_server(auth=auth)
    except Exception as e:
        print(f"Error: failed to build Paperless-ngx MCP server: {e}",
              file=sys.stderr)
        mode = "token"
        mcp = build_error_server(e)
    serve(mcp, mode)


if __name__ == "__main__":
    main()
