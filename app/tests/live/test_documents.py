"""Reading, searching and editing the seeded documents."""

from tests.live._client import call, client, find_document, payload, rows, run_async

INVOICE = "ACME invoice AC-2026-0317"


def _invoice_id() -> int:
    async def go():
        async with client() as c:
            return (await find_document(c, INVOICE))["id"]

    return run_async(go())


def test_list_filters_and_trims_fields():
    result = payload(run_async(call(
        "documents_list",
        {"correspondent__name__iexact": "ACME Hardware", "fields": ["id", "title"]},
    )))
    assert result["count"] == 2
    # `fields` reached Paperless as one comma-separated value: both survive.
    assert all(set(row) == {"id", "title"} for row in result["results"])


def test_full_text_query_finds_a_document_by_its_content():
    found = rows(run_async(call("documents_list", {"query": "workbench"})))
    assert [row["title"] for row in found] == ["ACME invoice AC-2026-0522"]


def test_retrieve_returns_the_text_content():
    document = payload(run_async(call("documents_retrieve", {"id": _invoice_id()})))
    assert document["title"] == INVOICE
    assert "Cordless drill" in document["content"]


def test_global_search_and_autocomplete():
    hits = payload(run_async(call("search_retrieve", {"query": "Northwind"})))
    assert hits["total"] >= 1
    assert "electricity" in payload(run_async(call("search_autocomplete_list", {"term": "elec"})))


def test_partial_update_changes_only_what_is_sent():
    async def go():
        async with client() as c:
            document = await find_document(c, INVOICE)
            try:
                updated = payload(await c.call_tool(
                    "documents_partial_update",
                    {"id": document["id"], "archive_serial_number": 4711},
                ))
            finally:
                await c.call_tool(
                    "documents_partial_update",
                    {"id": document["id"], "archive_serial_number": None},
                )
            return document, updated

    before, after = run_async(go())
    assert after["archive_serial_number"] == 4711
    assert after["title"] == before["title"] and after["tags"] == before["tags"]


def test_notes_round_trip():
    async def go():
        async with client() as c:
            document_id = (await find_document(c, INVOICE))["id"]
            notes = payload(await c.call_tool(
                "documents_notes_create", {"id": document_id, "note": "itest note"}
            ))
            note_id = next(n["id"] for n in notes if n["note"] == "itest note")
            listed = payload(await c.call_tool("documents_notes_list", {"id": document_id}))
            # DELETE takes both IDs: the document in the path, the note in the query.
            left = payload(await c.call_tool(
                "documents_notes_destroy", {"id__path": document_id, "id__query": note_id}
            ))
            return note_id, listed, left

    note_id, listed, left = run_async(go())
    assert note_id in [n["id"] for n in listed]
    assert note_id not in [n["id"] for n in left]


def test_document_details():
    document_id = _invoice_id()

    async def go():
        async with client() as c:
            return {
                name: payload(await c.call_tool(name, {"id": document_id}))
                for name in (
                    "documents_metadata_retrieve", "documents_suggestions_retrieve",
                    "documents_history_list", "documents_root", "document_share_links",
                )
            }

    details = run_async(go())
    assert details["documents_metadata_retrieve"]["original_mime_type"] == "text/plain"
    assert "2026-03-14" in details["documents_suggestions_retrieve"]["dates"]
    assert isinstance(details["documents_history_list"], list)
    assert details["documents_root"]["root_id"] == document_id
    assert isinstance(details["document_share_links"], list)


def test_download_and_preview_return_the_file():
    document_id = _invoice_id()
    for tool in ("documents_download_retrieve", "documents_preview_retrieve"):
        resource = run_async(call(tool, {"id": document_id})).content[0].resource
        # The seeded files are text, which FastMCP embeds as text, not base64.
        assert resource.mime_type == "text/plain"
        assert "INVOICE No. AC-2026-0317" in resource.text


def test_thumbnail_is_an_image():
    content = run_async(call("documents_thumb_retrieve", {"id": _invoice_id()})).content[0]
    assert content.type == "image" and content.mime_type.startswith("image/")


def test_bulk_edit_adds_and_removes_a_tag():
    async def go():
        async with client() as c:
            document_id = (await find_document(c, INVOICE))["id"]
            tag = payload(await c.call_tool("tags_create", {"name": "itest bulk tag"}))["id"]
            try:
                await c.call_tool("bulk_edit", {
                    "documents": [document_id], "method": "add_tag", "parameters": {"tag": tag},
                })
                tagged = payload(await c.call_tool("documents_retrieve", {"id": document_id}))
                selection = payload(await c.call_tool(
                    "documents_selection_data_create", {"documents": [document_id]}
                ))
            finally:
                await c.call_tool("tags_destroy", {"id": tag})
            return tag, tagged, selection

    tag, tagged, selection = run_async(go())
    assert tag in tagged["tags"]
    counts = {row["id"]: row["document_count"] for row in selection["selected_tags"]}
    assert counts[tag] == 1


def test_statistics_and_next_asn():
    assert payload(run_async(call("statistics_retrieve")))["documents_total"] >= 6
    assert isinstance(payload(run_async(call("documents_next_asn_retrieve"))), int)
