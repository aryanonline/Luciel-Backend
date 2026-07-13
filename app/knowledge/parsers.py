"""File-format parsers for knowledge ingestion (Architecture §3.2 / §3.2.3).

The upload endpoint previously did a raw ``bytes.decode("utf-8")`` regardless of
what the admin actually uploaded — a PDF or DOCX file would decode into binary
noise and get chunked/embedded as garbage. This module dispatches by content-type
(falling back to filename extension) to a real per-format extractor:

  - PDF   → pypdf page-text extraction, joined with blank-line separators.
  - DOCX  → python-docx paragraph + table-cell text, in document order.
  - CSV   → structured: each data row is rendered as "header: value" lines so the
            chunker/embedder gets human-readable rows, not a raw comma blob
            (Vision §3.3 "CSV/table import"; Architecture §3.2.3 storage note).
  - TXT / anything else → UTF-8 decode (the pre-existing behavior), tolerant of
            a stray non-UTF-8 byte (`errors="replace"`) rather than raising.

Failure is STRUCTURED, never silent: an unparseable file raises ``ParseError`` with
a stable ``code`` the API layer turns into a 422 with a clear message, instead of
either crashing with a raw exception or (worse) silently ingesting an empty/garbled
document (§3.2.3 "a half-specified connector is how a knowledge base silently goes
stale or serves content it shouldn't" — the same honesty principle applies to a
parse failure on upload).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


class ParseError(Exception):
    """Raised when a file cannot be parsed into text. Carries a stable machine code
    so the API boundary can render a structured 422 rather than a generic 500."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class ParsedFile:
    text: str
    # Row count for structured formats (CSV) — None for free-text formats. Exposed
    # so callers/tests can assert the structured-ingestion shape independent of the
    # rendered text.
    row_count: int | None = None


# Extensions/content-types recognized per format. Content-type is checked first
# (what the browser/client declares); the filename extension is the fallback for
# generic content-types like application/octet-stream that some clients send.
_PDF_TYPES = {"application/pdf"}
_PDF_EXTS = (".pdf",)
_DOCX_TYPES = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
_DOCX_EXTS = (".docx",)
_CSV_TYPES = {"text/csv", "application/csv", "application/vnd.ms-excel"}
_CSV_EXTS = (".csv",)
_TXT_TYPES = {"text/plain"}
_TXT_EXTS = (".txt", ".md", ".text")


def _ext(name: str) -> str:
    idx = name.rfind(".")
    return name[idx:].lower() if idx != -1 else ""


def _format_for(name: str, content_type: str | None) -> str:
    """Resolve to one of 'pdf' | 'docx' | 'csv' | 'txt' (the dispatch key).

    Content-type wins when it names a recognized format; otherwise the filename
    extension decides; unrecognized combinations fall back to 'txt' (matches the
    pre-existing raw-decode behavior for plain/unknown text files).
    """
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    ext = _ext(name)
    if ct in _PDF_TYPES or ext in _PDF_EXTS:
        return "pdf"
    if ct in _DOCX_TYPES or ext in _DOCX_EXTS:
        return "docx"
    if ct in _CSV_TYPES or ext in _CSV_EXTS:
        return "csv"
    if ct in _TXT_TYPES or ext in _TXT_EXTS:
        return "txt"
    return "txt"


def parse_file(raw: bytes, *, filename: str, content_type: str | None) -> ParsedFile:
    """Dispatch to the right extractor and return the extracted text.

    Raises ``ParseError`` (structured, stable ``code``) on an unparseable file
    instead of silently ingesting garbage or raising an unhandled exception.
    """
    fmt = _format_for(filename, content_type)
    if fmt == "pdf":
        return _parse_pdf(raw)
    if fmt == "docx":
        return _parse_docx(raw)
    if fmt == "csv":
        return _parse_csv(raw)
    return _parse_txt(raw)


def _parse_pdf(raw: bytes) -> ParsedFile:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover — dependency is pinned in pyproject
        raise ParseError("parser_unavailable", "PDF parsing is unavailable.") from exc
    try:
        reader = PdfReader(io.BytesIO(raw))
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as exc:
        raise ParseError("unparseable_pdf", f"Could not read PDF: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — any other pypdf failure is a parse failure
        raise ParseError("unparseable_pdf", f"Could not read PDF: {exc}") from exc
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text.strip():
        raise ParseError("empty_document", "The PDF contains no extractable text.")
    return ParsedFile(text=text)


def _parse_docx(raw: bytes) -> ParsedFile:
    try:
        import docx
        from docx.opc.exceptions import PackageNotFoundError
    except ImportError as exc:  # pragma: no cover — dependency is pinned in pyproject
        raise ParseError("parser_unavailable", "DOCX parsing is unavailable.") from exc
    try:
        document = docx.Document(io.BytesIO(raw))
    except PackageNotFoundError as exc:
        raise ParseError("unparseable_docx", f"Could not read DOCX: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — any other python-docx failure
        raise ParseError("unparseable_docx", f"Could not read DOCX: {exc}") from exc
    parts: list[str] = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    text = "\n\n".join(parts)
    if not text.strip():
        raise ParseError("empty_document", "The DOCX contains no extractable text.")
    return ParsedFile(text=text)


def _parse_csv(raw: bytes) -> ParsedFile:
    """CSV → structured rows/text (Vision §3.3, Architecture §3.2.3).

    Each data row is rendered as ``header: value`` lines (one block per row,
    blank-line separated) so a chunk boundary lands on a whole record rather than
    mid-row, and the embedded text is self-describing (the header names travel
    with every row) instead of a bare comma-separated blob.
    """
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("unparseable_csv", f"CSV is not valid UTF-8: {exc}") from exc
    try:
        reader = csv.reader(io.StringIO(decoded))
        rows = list(reader)
    except csv.Error as exc:
        raise ParseError("unparseable_csv", f"Could not parse CSV: {exc}") from exc
    if not rows:
        raise ParseError("empty_document", "The CSV file has no rows.")
    header, *data_rows = rows
    if not data_rows:
        raise ParseError("empty_document", "The CSV file has a header but no data rows.")
    blocks: list[str] = []
    for row in data_rows:
        if not any(cell.strip() for cell in row):
            continue  # skip blank rows
        pairs = []
        for i, cell in enumerate(row):
            col = header[i].strip() if i < len(header) else f"column_{i + 1}"
            pairs.append(f"{col}: {cell.strip()}")
        blocks.append("\n".join(pairs))
    if not blocks:
        raise ParseError("empty_document", "The CSV file has no non-empty data rows.")
    return ParsedFile(text="\n\n".join(blocks), row_count=len(blocks))


def _parse_txt(raw: bytes) -> ParsedFile:
    # Tolerant decode (pre-existing behavior): a stray non-UTF-8 byte must not
    # crash the upload; unlike the old code we never silently return "" — an
    # actually-empty file after decode is still a legitimate empty text file, so
    # that alone is not a parse error (chunk_text handles empty text as zero chunks).
    text = raw.decode("utf-8", errors="replace")
    return ParsedFile(text=text)
