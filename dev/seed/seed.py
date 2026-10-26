"""Fill an empty dev Paperless-ngx with sample data through its REST API.

Run by the `seed` service in docker-compose.yaml. It creates a few
correspondents, document types and tags and a custom field, then uploads the
text files in documents/ with that metadata and waits until Paperless has
consumed them. An instance that already holds documents is left alone, so the
service is safe to run on every `docker compose up`.
"""

import os
import sys
import time
from pathlib import Path

import httpx2

URL = os.environ.get("PAPERLESS_URL", "http://paperless:8000").rstrip("/")
USER = os.environ.get("PAPERLESS_ADMIN_USER", "admin")
PASSWORD = os.environ.get("PAPERLESS_ADMIN_PASSWORD", "paperless-mcp")
DOCUMENTS = Path(__file__).parent / "documents"
CONSUME_TIMEOUT = 300

CORRESPONDENTS = ["ACME Hardware", "Northwind Energy", "Harbour Mutual", "Tax Office Leeds"]
DOCUMENT_TYPES = ["Invoice", "Contract", "Statement", "Letter"]
TAGS = ["household", "utilities", "insurance", "taxes", "todo"]
CUSTOM_FIELD = {"name": "Amount due", "data_type": "monetary"}

# file name -> (title, created, correspondent, document type, tags)
FILES = {
    "invoice-acme-2026-03.txt": (
        "ACME invoice AC-2026-0317", "2026-03-14", "ACME Hardware", "Invoice", ["household"]),
    "invoice-acme-2026-05.txt": (
        "ACME invoice AC-2026-0522", "2026-05-09", "ACME Hardware", "Invoice", ["household", "todo"]),
    "electricity-contract.txt": (
        "Electricity supply contract", "2026-01-02", "Northwind Energy", "Contract", ["utilities"]),
    "electricity-statement-2026-q1.txt": (
        "Electricity statement Q1 2026", "2026-04-03", "Northwind Energy", "Statement", ["utilities"]),
    "home-insurance-policy.txt": (
        "Home contents insurance policy", "2026-02-20", "Harbour Mutual", "Contract", ["insurance", "household"]),
    "tax-office-letter.txt": (
        "Reminder: income tax return 2025", "2026-06-18", "Tax Office Leeds", "Letter", ["taxes", "todo"]),
}


def login() -> httpx2.Client:
    """An API client holding the admin's token. The admin user is created by
    Paperless on first start, which can lag a little behind the healthcheck."""
    for _ in range(60):
        response = httpx2.post(
            f"{URL}/api/token/", json={"username": USER, "password": PASSWORD}, timeout=30
        )
        if response.is_success:
            return httpx2.Client(
                base_url=URL,
                headers={"Authorization": f"Token {response.json()['token']}"},
                timeout=60,
            )
        time.sleep(2)
    sys.exit(f"could not log in to {URL} as {USER}: HTTP {response.status_code}")


def create_all(api: httpx2.Client, path: str, names: list[str]) -> dict[str, int]:
    ids = {}
    for name in names:
        response = api.post(path, json={"name": name})
        response.raise_for_status()
        ids[name] = response.json()["id"]
    return ids


def document_count(api: httpx2.Client) -> int:
    response = api.get("/api/documents/", params={"page_size": 1})
    response.raise_for_status()
    return response.json()["count"]


def main() -> None:
    api = login()
    if document_count(api):
        print("Paperless already holds documents; nothing to seed.")
        return

    correspondents = create_all(api, "/api/correspondents/", CORRESPONDENTS)
    document_types = create_all(api, "/api/document_types/", DOCUMENT_TYPES)
    tags = create_all(api, "/api/tags/", TAGS)
    api.post("/api/custom_fields/", json=CUSTOM_FIELD).raise_for_status()

    for name, (title, created, correspondent, document_type, tag_names) in FILES.items():
        fields = [
            ("title", title),
            ("created", created),
            ("correspondent", str(correspondents[correspondent])),
            ("document_type", str(document_types[document_type])),
            *(("tags", str(tags[t])) for t in tag_names),
        ]
        response = api.post(
            "/api/documents/post_document/",
            files=[
                ("document", (name, (DOCUMENTS / name).read_bytes(), "text/plain")),
                *((key, (None, value)) for key, value in fields),
            ],
        )
        response.raise_for_status()
        print(f"queued {name}")

    deadline = time.monotonic() + CONSUME_TIMEOUT
    while (count := document_count(api)) < len(FILES):
        if time.monotonic() > deadline:
            sys.exit(f"only {count} of {len(FILES)} documents consumed after {CONSUME_TIMEOUT}s")
        time.sleep(2)
    print(f"Seeded {len(FILES)} documents.")


if __name__ == "__main__":
    main()
