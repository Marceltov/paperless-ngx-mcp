"""Enforce that every default tool is exercised by a live test."""

import asyncio
import re
from pathlib import Path

import httpx2

import server
from tests._fake import build

_LIVE_DIR = Path(__file__).resolve().parent / "live"

# Default tools with NO live test, each for a reason the dev stack can't
# satisfy. Keep this list short: a tool belongs here only if it can't be run
# against the seeded instance, not because a test is missing.
_UNTESTED = {
    # Need an AI backend configured in Paperless.
    "documents_ai_suggestions_retrieve", "documents_chat_create",
    # Need an SMTP server.
    "documents_email_document", "email_documents",
    # PDF surgery; the seed holds text files only.
    "documents_edit_pdf", "documents_merge", "documents_merge_as_versions",
    "documents_rotate", "documents_remove_password",
    # Need a document with several versions.
    "documents_delete_version", "documents_update_version_label",
    # Kick off background jobs that outlive the test and rewrite the index.
    "documents_reprocess", "run_task",
}


def _default_tool_names() -> set[str]:
    mcp = build(lambda request: httpx2.Response(404))
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _live_sources() -> str:
    lines = []
    # _client.py counts too: its helpers make tool calls on the tests' behalf.
    for py in sorted(_LIVE_DIR.glob("*.py")):
        # Drop line comments so a commented-out mention can't count as coverage.
        lines += [line.split("#", 1)[0] for line in py.read_text().splitlines()]
    return "\n".join(lines)


def test_all_default_tools_have_a_live_test(monkeypatch):
    monkeypatch.delenv(server.TOOLSETS_ENV, raising=False)
    # Tool names as string literals anywhere in the live tests: several tests
    # call tools through a parametrized table rather than a literal call site.
    exercised = set(re.findall(r'["\']([a-z_]+)["\']', _live_sources()))
    uncovered = sorted(_default_tool_names() - _UNTESTED - exercised)
    assert not uncovered, f"tools with no live test: {uncovered}"


def test_untested_list_holds_only_real_untested_tools(monkeypatch):
    monkeypatch.delenv(server.TOOLSETS_ENV, raising=False)
    names = _default_tool_names()
    assert not sorted(_UNTESTED - names), "stale names in _UNTESTED"


def test_no_unknown_tool_names_called(monkeypatch):
    monkeypatch.setenv(server.TOOLSETS_ENV, "all")
    called = set(re.findall(r'(?:call_tool|call)\(\s*["\']([a-z_]+)["\']', _live_sources()))
    unknown = sorted(called - _default_tool_names())
    assert not unknown, f"tests call unknown tool names: {unknown}"
