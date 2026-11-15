"""A file input submitted with nothing selected is refused before any upload handler runs.

A browser sends an ``<input type="file">`` left empty as a part with ``filename=""`` and no
bytes. Up to fastapi 0.149 that part reached the handler as an empty UploadFile, and two of
the four upload routes acted on it: POST /sources/upload created a source with no content
under the form's name (201), and POST /sources/{id}/replace swapped a live source's chunks
for nothing (200). The two CSV routes refused it with their own 422 message.
From 0.150 FastAPI reads the part as missing, so every upload route answers the contract
422 ``{"message": "file: Field required"}`` before its handler runs. A NAMED zero-byte file
is still a file and still reaches the handler (an empty .txt is a legitimate document,
app/knowledge/parsers.py): that line is what this pins.

httpx cannot build the request (it leaves out an empty filename), so the multipart body is
written by hand, the way a browser sends it. The session gate is stubbed: it runs before
the body is validated and is pinned on its own in test_closed_account_surface.py. FastAPI's
multipart parsing and validation, the handlers and the error contract are the real ones,
and nothing here reaches the database: the knowledge handlers are stopped at their parser.
"""

from __future__ import annotations

import uuid

import pytest
from app.api import deps
from app.knowledge.parsers import ParseError
from app.main import create_app
from fastapi.testclient import TestClient

BOUNDARY = "luciel-upload-test-boundary"
SOURCES = "/api/v1/admin/knowledge/sources"
SOURCE_ID = "00000000-0000-0000-0000-000000000000"
REACHED = "reached: the handler parsed the file"

# (route, whether it takes a "name" form field, what its handler says about an empty file)
UPLOAD_ROUTES = [
    pytest.param(f"{SOURCES}/upload", True, REACHED, id="knowledge-upload"),
    pytest.param(f"{SOURCES}/csv", True, REACHED, id="knowledge-csv"),
    pytest.param(f"{SOURCES}/{SOURCE_ID}/replace", False, REACHED, id="knowledge-replace"),
    pytest.param(
        "/api/v1/admin/connections/record-source/csv",
        False,
        "The CSV needs a header row naming its columns.",
        id="record-source-csv",
    ),
]


def _browser_multipart(filename: str, *, named: bool) -> dict[str, object]:
    file_part = (
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n\r\n"
    )
    parts = [file_part]
    if named:
        parts.append('Content-Disposition: form-data; name="name"\r\n\r\nPolicies\r\n')
    body = "".join(f"--{BOUNDARY}\r\n{part}" for part in parts) + f"--{BOUNDARY}--\r\n"
    return {
        "content": body.encode(),
        "headers": {"content-type": f"multipart/form-data; boundary={BOUNDARY}"},
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def parse_file(raw: bytes, *, filename: str, content_type: str | None) -> object:
        raise ParseError("reached", "the handler parsed the file")

    monkeypatch.setattr("app.api.v1.admin.knowledge.parse_file", parse_file)
    app = create_app()
    app.dependency_overrides[deps.current_admin] = lambda: deps.Subject(
        admin_id=uuid.uuid4(), user_id=uuid.uuid4(), session_epoch=0
    )
    return TestClient(app)


@pytest.mark.parametrize(("path", "named", "handler_says"), UPLOAD_ROUTES)
def test_a_file_input_with_nothing_selected_never_reaches_the_handler(
    client: TestClient, path: str, named: bool, handler_says: str
) -> None:
    response = client.post(path, **_browser_multipart("", named=named))

    assert response.status_code == 422
    assert response.json() == {"message": "file: Field required"}


@pytest.mark.parametrize(("path", "named", "handler_says"), UPLOAD_ROUTES)
def test_a_named_empty_file_still_reaches_the_handler(
    client: TestClient, path: str, named: bool, handler_says: str
) -> None:
    response = client.post(path, **_browser_multipart("empty.txt", named=named))

    assert response.status_code == 422
    assert response.json() == {"message": handler_says}
