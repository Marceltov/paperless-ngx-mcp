import asyncio
import base64

import httpx2
import pytest
from fastmcp.exceptions import ToolError

import server
from tests._fake import SPEC, build

PDF = b"%PDF-1.4\n\xff\xfe binary, not UTF-8 \x00"


def _call(handler, tool, args):
    reset = server._incoming_auth.set("tok")
    try:
        return asyncio.run(build(handler).call_tool(tool, args))
    finally:
        server._incoming_auth.reset(reset)


def _file_handler(seen: dict, body: bytes, content_type: str):
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["query"] = dict(request.url.params)
        return httpx2.Response(200, content=body, headers={"content-type": content_type})
    return handler


def test_download_returns_the_binary_file():
    # The generated tool would crash decoding these bytes as JSON.
    seen = {}
    result = _call(
        _file_handler(seen, PDF, "application/pdf"),
        "documents_download_retrieve", {"id": 7, "original": True},
    )
    resource = result.content[0].resource
    assert base64.b64decode(resource.blob) == PDF
    assert resource.mime_type == "application/pdf"
    assert seen == {"path": "/api/documents/7/download/", "query": {"original": "true"}}


def test_thumb_returns_an_image():
    seen = {}
    result = _call(
        _file_handler(seen, b"RIFF....WEBP", "image/webp"), "documents_thumb_retrieve", {"id": 7}
    )
    assert result.content[0].type == "image"
    assert result.content[0].mime_type == "image/webp"
    assert seen["path"] == "/api/documents/7/thumb/"


def test_oversized_file_is_refused(monkeypatch):
    monkeypatch.setattr(server, "MAX_FILE_BYTES", 4)
    with pytest.raises(ToolError, match="documents_retrieve"):
        _call(_file_handler({}, PDF, "application/pdf"), "documents_preview_retrieve", {"id": 7})


def test_upload_sends_a_multipart_form():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content
        return httpx2.Response(200, json="0f3c-task-uuid")

    result = _call(handler, server.UPLOAD_TOOL, {
        "filename": "note.txt", "content": "hello päper", "title": "A note",
        "correspondent": 3, "tags": [1, 2], "created": "2026-01-02",
    })
    assert "0f3c-task-uuid" in result.content[0].text
    assert seen["path"] == "/api/documents/post_document/"
    assert seen["content_type"].startswith("multipart/form-data")
    body = seen["body"]
    assert b'name="document"; filename="note.txt"' in body
    assert "hello päper".encode() in body
    assert b'name="title"\r\n\r\nA note' in body
    assert b'name="correspondent"\r\n\r\n3' in body
    # A repeated field per tag, the way Paperless reads a list from a form.
    assert body.count(b'name="tags"') == 2


def test_upload_accepts_base64_for_binary_files():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = request.content
        return httpx2.Response(200, json="task")

    _call(handler, server.UPLOAD_TOOL, {
        "filename": "scan.pdf", "content_base64": base64.b64encode(PDF).decode(),
    })
    assert PDF in seen["body"]


@pytest.mark.parametrize(
    "args, message",
    [
        ({"filename": "a.txt"}, "exactly one"),
        ({"filename": "a.txt", "content": "x", "content_base64": "eA=="}, "exactly one"),
        ({"filename": "a.pdf", "content_base64": "not base64!"}, "not valid base64"),
    ],
)
def test_upload_rejects_bad_content_arguments(args, message):
    with pytest.raises(ToolError, match=message):
        _call(lambda r: httpx2.Response(200, json="task"), server.UPLOAD_TOOL, args)


def test_upload_reports_paperless_validation_errors():
    def handler(request):
        return httpx2.Response(400, json={"document": ["File type not supported"]})

    with pytest.raises(ToolError, match="File type not supported"):
        _call(handler, server.UPLOAD_TOOL, {"filename": "a.xyz", "content": "x"})


def test_generated_tools_name_their_endpoint_and_carry_no_output_schema():
    tools = {t.name: t for t in asyncio.run(build(lambda r: httpx2.Response(404)).list_tools())}
    assert tools["tags_list"].description.startswith("GET /api/tags/")
    assert tools["documents_destroy"].description.startswith("DELETE /api/documents/{id}/")
    # Paperless answers with arrays, scalars and empty bodies as well as
    # objects; a declared object schema would make clients reject those.
    for name in ("documents_notes_list", "documents_next_asn_retrieve", "trash_list"):
        assert tools[name].output_schema is None


def _tools():
    return {t.name: t for t in asyncio.run(build(lambda r: httpx2.Response(404)).list_tools())}


def _hints(tool):
    a = tool.annotations
    return (a.read_only_hint, a.destructive_hint, a.idempotent_hint)


def test_every_tool_carries_behaviour_hints():
    for name, tool in _tools().items():
        hints = tool.annotations
        assert hints is not None, name
        assert not (hints.read_only_hint and hints.destructive_hint), name
        if name.endswith(("_list", "_retrieve")):
            assert hints.read_only_hint, name
        if name.endswith("_destroy"):
            assert hints.destructive_hint, name


@pytest.mark.parametrize(
    "name, hints",
    [
        ("documents_list", (True, False, True)),
        ("documents_download_retrieve", (True, False, True)),
        ("documents_selection_data_create", (True, False, True)),  # a POST that reads
        ("tags_create", (False, False, False)),
        ("documents_post_document_create", (False, False, False)),
        ("documents_partial_update", (False, False, True)),
        ("documents_destroy", (False, True, True)),
        ("bulk_edit", (False, True, False)),  # POSTs that delete
        ("bulk_edit_objects", (False, True, False)),
        ("documents_delete", (False, True, False)),
        ("trash_create", (False, True, False)),
    ],
)
def test_behaviour_hints(name, hints):
    assert _hints(_tools()[name]) == hints


def test_hinted_and_guided_names_exist_in_the_spec():
    # A renamed operation would silently lose its hint or its guide.
    ids = {
        op["operationId"]
        for ops in SPEC["paths"].values()
        for op in ops.values()
        if isinstance(op, dict) and "operationId" in op
    }
    listed = (
        server.READ_ONLY_POSTS | server.DESTRUCTIVE_POSTS | server.OPEN_WORLD
        | set(server.TOOL_GUIDES)
    )
    assert listed <= ids


def test_large_result_tools_say_how_to_narrow():
    tools = _tools()
    listing = tools["documents_list"].description
    assert listing.startswith("GET /api/documents/\n\n")
    for hint in ("fields", "page_size", "content", "query", "tags__id__all", "count"):
        assert hint in listing
    assert "ENTIRE trash" in tools["trash_create"].description
    assert "task_id" in tools["tasks_list"].description


def test_unguided_tools_get_a_note_from_their_method():
    tools = _tools()
    assert "page_size" in tools["saved_views_list"].description
    # The spec's text for these is a mixin docstring; a sentence replaces it.
    assert "List saved views." in tools["saved_views_list"].description
    assert "Delete one of the share links." in tools["share_links_destroy"].description
    assert not any("Django-Guardian" in t.description for t in tools.values())
    assert not any("serializer" in t.description for t in tools.values())
    assert "cannot be undone" in tools["tags_destroy"].description
    assert "page_size" not in tools["tags_retrieve"].description


def test_server_sends_instructions():
    mcp = build(lambda r: httpx2.Response(404))
    assert "fields" in mcp.instructions and "confirmation" in mcp.instructions


def test_array_response_passes_through():
    notes = [{"id": 1, "note": "hello"}]

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/documents/7/notes/"
        return httpx2.Response(200, json=notes)

    result = _call(handler, "documents_notes_list", {"id": 7})
    assert '"note":"hello"' in result.content[0].text.replace(" ", "")


def test_fields_are_sent_comma_separated():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["fields"] = request.url.params.get_list("fields")
        return httpx2.Response(200, json={"count": 0, "results": []})

    _call(handler, "documents_list", {"fields": ["id", "title"]})
    assert seen["fields"] == ["id,title"]


def test_error_server_reports_the_failure():
    mcp = server.build_error_server(RuntimeError("spec exploded"))
    assert "spec exploded" in mcp.instructions
    names = [t.name for t in asyncio.run(mcp.list_tools())]
    assert names == ["startup_error"]
    result = asyncio.run(mcp.call_tool("startup_error", {}))
    assert "spec exploded" in result.content[0].text


def test_bundled_spec_is_a_paperless_spec():
    assert SPEC["info"]["title"] == "Paperless-ngx REST API"
    assert server.api_version(SPEC)


def test_mail_account_process_takes_only_the_id(monkeypatch):
    # The spec gives the process action the whole account, password included,
    # as a required body; Paperless reads only the ID from the path.
    monkeypatch.setenv(server.TOOLSETS_ENV, "mail_accounts")
    tools = {t.name: t for t in asyncio.run(build(lambda r: httpx2.Response(404)).list_tools())}
    params = tools["mail_account_process"].parameters
    assert set(params["properties"]) == {"id"}
    assert params.get("required") == ["id"]

    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"], seen["path"], seen["body"] = request.method, request.url.path, request.content
        return httpx2.Response(200, json={"result": "OK"})

    result = _call(handler, "mail_account_process", {"id": 3})
    assert seen["method"] == "POST" and seen["path"] == "/api/mail_accounts/3/process/"
    assert seen["body"] in (b"", b"{}", b"null")
    assert "OK" in result.content[0].text
