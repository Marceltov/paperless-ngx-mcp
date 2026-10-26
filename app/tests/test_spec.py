import asyncio
import json

import httpx2
import pytest

import server
from tests._fake import SPEC, build

MINI = {"openapi": "3.0.3", "info": {"version": "9.9.9 (42)"}, "paths": {"/api/tags/": {}}}


def _serve(monkeypatch, responder):
    """Replace httpx2.get (what download_spec uses) and record the calls."""
    calls = []

    def fake_get(url, **kw):
        calls.append((url, kw))
        return responder(len(calls), httpx2.Request("GET", url))

    monkeypatch.setattr(server.httpx2, "get", fake_get)
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    return calls


def _ok(_n, request):
    return httpx2.Response(200, text=json.dumps(MINI), request=request)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (server.SPEC_ENV, server.SPEC_WAIT_ENV, server.API_VERSION_ENV,
                server.TOOLSETS_ENV, server.SERVER_ENV):
        monkeypatch.delenv(var, raising=False)


def test_spec_is_downloaded_from_the_paperless_instance(monkeypatch):
    monkeypatch.setenv(server.SERVER_ENV, "https://paperless.example/")
    calls = _serve(monkeypatch, _ok)
    spec, source = server.resolve_spec()
    assert spec == MINI
    assert source == "https://paperless.example/api/schema/"
    assert calls[0][0] == source and calls[0][1]["params"] == {"format": "json"}


def test_download_retries_while_paperless_boots(monkeypatch):
    def booting(n, request):
        if n == 1:
            raise httpx2.ConnectError("refused", request=request)
        if n == 2:
            return httpx2.Response(502, request=request)
        return _ok(n, request)

    calls = _serve(monkeypatch, booting)
    assert server.download_spec("http://paperless:8000/api/schema/", wait=60) == MINI
    assert len(calls) == 3


def test_falls_back_to_the_bundled_spec_when_download_fails(monkeypatch, capsys):
    monkeypatch.setenv(server.SPEC_WAIT_ENV, "0")

    def down(_n, request):
        raise httpx2.ConnectError("refused", request=request)

    _serve(monkeypatch, down)
    spec, source = server.resolve_spec()
    assert spec == SPEC and "bundled" in source
    assert "Falling back to the bundled snapshot" in capsys.readouterr().err


def test_a_refused_schema_is_not_retried(monkeypatch):
    # 401/403/404 won't fix themselves; don't sit out the whole wait.
    calls = _serve(monkeypatch, lambda _n, r: httpx2.Response(403, request=r))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        server.download_spec("http://paperless:8000/api/schema/", wait=60)
    assert len(calls) == 1


def test_swagger_ui_url_is_read_as_the_schema_url(monkeypatch):
    # People copy the address of the Swagger page, /api/schema/view/.
    monkeypatch.setenv(server.SPEC_ENV, "https://paperless.example/api/schema/view/")
    calls = _serve(monkeypatch, _ok)
    server.resolve_spec()
    assert calls[0][0] == "https://paperless.example/api/schema/"


def test_explicit_spec_override_has_no_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv(server.SPEC_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(RuntimeError, match="not found"):
        server.resolve_spec()
    path = tmp_path / "spec.yaml"
    path.write_text("openapi: 3.0.3\ninfo: {version: '1 (7)'}\npaths: {/api/tags/: {}}\n")
    monkeypatch.setenv(server.SPEC_ENV, str(path))
    spec, source = server.resolve_spec()
    assert spec["paths"] == {"/api/tags/": {}} and source == str(path)


@pytest.mark.parametrize("text", ["", "[]", "just: yaml"])
def test_parse_spec_rejects_non_specs(text):
    with pytest.raises(RuntimeError, match="OpenAPI spec"):
        server.parse_spec(text, "test")


def test_api_version_comes_from_the_spec(monkeypatch):
    assert server.api_version(MINI) == "42"
    assert server.api_version({"info": {"version": "2.14.7"}}) is None
    assert server.paperless_headers("42") == {"Accept": "application/json; version=42"}
    assert server.paperless_headers(None) == {"Accept": "application/json"}
    monkeypatch.setenv(server.API_VERSION_ENV, "7")
    assert server.api_version(MINI) == "7"


def _tool_names(monkeypatch, toolsets: str | None = None) -> set[str]:
    if toolsets is not None:
        monkeypatch.setenv(server.TOOLSETS_ENV, toolsets)
    mcp = build(lambda request: httpx2.Response(404))
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_default_toolsets_leave_out_administration(monkeypatch):
    names = _tool_names(monkeypatch)
    assert {"documents_list", "tags_create", "search_retrieve"} <= names
    assert not any(n.startswith(("users_", "mail_", "workflows_", "config_")) for n in names)


def test_toolsets_can_be_narrowed_or_opened(monkeypatch):
    only_tags = _tool_names(monkeypatch, "tags")
    # The hand-written document tools belong to `documents`... but are always
    # registered; everything generated is from the one toolset.
    generated = only_tags - set(server.FILE_PATHS) - {server.UPLOAD_TOOL}
    assert generated and all(n.startswith("tags_") for n in generated)
    everything = _tool_names(monkeypatch, "all")
    assert {"users_list", "workflows_list", "mail_rules_list"} <= everything


def test_default_keyword_adds_to_the_defaults(monkeypatch):
    defaults = _tool_names(monkeypatch)
    extended = _tool_names(monkeypatch, "default,mail_accounts,mail_rules")
    assert defaults < extended
    assert {"mail_accounts_list", "mail_rules_create"} <= extended - defaults
    assert not any(n.startswith(("users_", "workflows_")) for n in extended)


def test_credential_operations_are_never_tools(monkeypatch):
    # Even with every toolset on: these mint or replace the user's API token,
    # or change how the account logs in.
    names = _tool_names(monkeypatch, "all")
    forbidden = {
        "token_create", "profile_generate_auth_token_create", "profile_totp_create",
        "profile_totp_destroy", "profile_totp_retrieve",
        "profile_disconnect_social_account_create", "oauth_callback_retrieve",
        "bulk_download", "documents_update_version",
    }
    assert not forbidden & names


def test_put_is_dropped_where_patch_exists(monkeypatch):
    names = _tool_names(monkeypatch, "all")
    assert "tags_partial_update" in names and "tags_update" not in names


def _operation(path: str, method: str) -> dict:
    trimmed = server.select_operations(SPEC, None)
    return trimmed["paths"][path][method]


def test_request_bodies_are_json_only():
    # selection_data lists multipart first; FastMCP would send that.
    body = _operation("/api/documents/selection_data/", "post")["requestBody"]
    assert list(body["content"]) == ["application/json"]


def test_fields_parameter_is_comma_joined():
    fields = next(
        p for p in _operation("/api/documents/", "get")["parameters"] if p["name"] == "fields"
    )
    assert fields["explode"] is False


def test_notes_query_id_survives_only_on_delete():
    def query_ids(method):
        return [
            p for p in _operation("/api/documents/{id}/notes/", method)["parameters"]
            if p["name"] == "id" and p["in"] == "query"
        ]

    assert not query_ids("get") and not query_ids("post")
    assert query_ids("delete")
