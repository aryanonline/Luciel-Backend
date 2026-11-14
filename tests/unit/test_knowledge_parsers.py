"""Unit tests for app.knowledge.parsers (Architecture §3.2.3 file parsers).

No DB, no network — pure format-extraction logic against small in-memory
fixtures built with pypdf/python-docx writers, exactly the formats the upload
endpoint now dispatches to instead of a blind utf-8 decode.
"""

from __future__ import annotations

import io

import pytest
from app.knowledge.parsers import ParseError, parse_file


def _make_pdf_bytes(text: str) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    # pypdf's writer has no text-drawing API without reportlab; instead we embed
    # a simple content stream that draws the given text, which pypdf's reader can
    # extract back out via /Contents. This keeps the fixture dependency-light.
    content = f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode()
    from pypdf.generic import DictionaryObject, NameObject, NumberObject, StreamObject

    stream = StreamObject()
    stream.set_data(content)
    stream[NameObject("/Length")] = NumberObject(len(content))
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_ref = writer._add_object(font)
    resources = DictionaryObject()
    font_dict = DictionaryObject()
    font_dict[NameObject("/F1")] = font_ref
    resources[NameObject("/Font")] = font_dict
    page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_pdf15_bytes(page_texts: list[str]) -> bytes:
    """A PDF laid out the way most producers have written one since PDF 1.5: the
    catalog, page tree, pages and font sit in a compressed object stream, and the
    cross-reference table is itself a Flate stream with the PNG Up predictor.
    pypdf's writer only emits a classic xref table (the fixture above), so this one
    is assembled by hand."""
    import zlib

    n = len(page_texts)
    first_page = 4
    first_content = first_page + n
    objstm_num = first_content + n
    xref_num = objstm_num + 1
    kids = b" ".join(b"%d 0 R" % (first_page + i) for i in range(n))
    packed = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, n),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i in range(n):
        packed[first_page + i] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (first_content + i)
        )
    header = body = b""
    for num, obj in packed.items():  # "objnum offset" pairs, offsets strictly increasing
        header += b"%d %d " % (num, len(body))
        body += obj + b"\n"

    out = io.BytesIO()
    out.write(b"%PDF-1.5\n")
    offsets: dict[int, int] = {}

    def put_stream(num: int, entries: bytes, data: bytes) -> None:
        offsets[num] = out.tell()
        out.write(b"%d 0 obj\n<< %s /Length %d >>\nstream\n" % (num, entries, len(data)))
        out.write(data + b"\nendstream\nendobj\n")

    for i, text in enumerate(page_texts):
        content = f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode()
        put_stream(first_content + i, b"/Filter /FlateDecode", zlib.compress(content))
    put_stream(
        objstm_num,
        b"/Type /ObjStm /N %d /First %d /Filter /FlateDecode" % (len(packed), len(header)),
        zlib.compress(header + body),
    )
    offsets[xref_num] = out.tell()
    # Cross-reference stream rows, /W [1 4 2]: type 0 free, 1 at a byte offset, 2 at an
    # index in the object stream. Each row is PNG Up-predicted: the filter byte 2, then
    # the byte-wise difference from the row above.
    order = list(packed)
    rows = [(0, 0, 65535)] + [
        (2, objstm_num, order.index(num)) if num in packed else (1, offsets[num], 0)
        for num in range(1, xref_num + 1)
    ]
    previous, predicted = bytes(7), b""
    for kind, field2, field3 in rows:
        row = bytes([kind]) + field2.to_bytes(4, "big") + field3.to_bytes(2, "big")
        predicted += b"\x02" + bytes((a - b) % 256 for a, b in zip(row, previous, strict=True))
        previous = row
    xref = zlib.compress(predicted)
    out.write(
        b"%d 0 obj\n<< /Type /XRef /Size %d /W [1 4 2] /Root 1 0 R /Filter /FlateDecode "
        b"/DecodeParms << /Predictor 12 /Columns 7 >> /Length %d >>\nstream\n"
        % (xref_num, xref_num + 1, len(xref))
    )
    out.write(xref + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % offsets[xref_num])
    return out.getvalue()


def _encrypted(raw: bytes, *, user_password: str) -> bytes:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(raw)))
    writer.encrypt(
        user_password=user_password,
        owner_password="owner-only",  # pragma: allowlist secret
        algorithm="AES-256",
    )
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    import docx

    document = docx.Document()
    for p in paragraphs:
        document.add_paragraph(p)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_pdf_extracts_text():
    raw = _make_pdf_bytes("Starter engagement is 899 dollars")
    parsed = parse_file(raw, filename="brochure.pdf", content_type="application/pdf")
    assert "899" in parsed.text
    assert parsed.row_count is None


def test_pdf_empty_raises_empty_document():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)  # no content stream at all
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(ParseError) as exc_info:
        parse_file(buf.getvalue(), filename="blank.pdf", content_type="application/pdf")
    assert exc_info.value.code == "empty_document"


def test_pdf_corrupt_raises_unparseable():
    with pytest.raises(ParseError) as exc_info:
        parse_file(b"not a pdf at all", filename="fake.pdf", content_type="application/pdf")
    assert exc_info.value.code == "unparseable_pdf"


def test_pdf_with_object_and_xref_streams_extracts_every_page_in_order():
    # The minimal fixture above has a classic xref table, so it never reaches the paths
    # most real PDFs take: an object stream and a predictor-encoded xref stream (pypdf
    # 7.0 rewrote the PNG Up predictor and refuses an object stream whose offsets are
    # not strictly increasing). The exact text pins the page order and the blank-line
    # page separator the chunker splits on.
    raw = _make_pdf15_bytes(
        ["Opening hours are nine to five", "Decks from 40 dollars per square foot"]
    )
    parsed = parse_file(raw, filename="handbook.pdf", content_type="application/pdf")
    assert parsed.text == "Opening hours are nine to five\n\nDecks from 40 dollars per square foot"


def test_pdf_encrypted_without_an_open_password_extracts_text():
    # A "protected" PDF that only restricts printing or editing opens with the empty user
    # password, which pypdf tries by itself. AES goes through pypdf's cryptography
    # provider (pypdf 7.0 dropped support for cryptography<43).
    raw = _encrypted(_make_pdf_bytes("Starter engagement is 899 dollars"), user_password="")
    parsed = parse_file(raw, filename="locked.pdf", content_type="application/pdf")
    assert parsed.text == "Starter engagement is 899 dollars"


def test_pdf_needing_an_open_password_raises_unparseable():
    open_password = "letmein"  # pragma: allowlist secret
    raw = _encrypted(
        _make_pdf_bytes("Starter engagement is 899 dollars"), user_password=open_password
    )
    with pytest.raises(ParseError) as exc_info:
        parse_file(raw, filename="secret.pdf", content_type="application/pdf")
    assert exc_info.value.code == "unparseable_pdf"


def test_docx_extracts_paragraphs_and_tables():
    raw = _make_docx_bytes(["Our office hours are nine to five.", "Weekdays only."])
    parsed = parse_file(
        raw,
        filename="hours.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert "nine to five" in parsed.text
    assert "Weekdays only" in parsed.text
    assert parsed.row_count is None


def test_docx_corrupt_raises_unparseable():
    with pytest.raises(ParseError) as exc_info:
        parse_file(
            b"not a docx",
            filename="fake.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    assert exc_info.value.code == "unparseable_docx"


def test_docx_no_text_raises_empty_document():
    raw = _make_docx_bytes([])
    with pytest.raises(ParseError) as exc_info:
        parse_file(
            raw,
            filename="empty.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    assert exc_info.value.code == "empty_document"


def test_csv_renders_structured_rows():
    raw = b"name,price\nStarter,899\nGrowth,1499\n"
    parsed = parse_file(raw, filename="pricing.csv", content_type="text/csv")
    assert parsed.row_count == 2
    assert "name: Starter" in parsed.text
    assert "price: 899" in parsed.text
    assert "name: Growth" in parsed.text
    # Rows are blank-line separated so a chunk boundary lands on a whole record.
    assert "\n\n" in parsed.text


def test_csv_header_only_raises_empty_document():
    raw = b"name,price\n"
    with pytest.raises(ParseError) as exc_info:
        parse_file(raw, filename="empty.csv", content_type="text/csv")
    assert exc_info.value.code == "empty_document"


def test_csv_no_rows_raises_empty_document():
    with pytest.raises(ParseError) as exc_info:
        parse_file(b"", filename="empty.csv", content_type="text/csv")
    assert exc_info.value.code == "empty_document"


def test_csv_extension_detected_without_content_type():
    raw = b"a,b\n1,2\n"
    parsed = parse_file(raw, filename="data.csv", content_type="application/octet-stream")
    assert parsed.row_count == 1


def test_txt_tolerant_decode_never_raises():
    raw = b"hello \xff\xfe world"  # stray non-utf-8 bytes
    parsed = parse_file(raw, filename="notes.txt", content_type="text/plain")
    assert "hello" in parsed.text
    assert "world" in parsed.text
    assert parsed.row_count is None


def test_unknown_extension_falls_back_to_txt():
    raw = b"plain content here"
    parsed = parse_file(raw, filename="notes.xyz", content_type=None)
    assert parsed.text == "plain content here"
    assert parsed.row_count is None


def test_content_type_wins_over_extension():
    # A .txt-named file whose declared content-type is actually CSV should still
    # be parsed as CSV — content-type is checked first (Architecture §3.2.3).
    raw = b"a,b\n1,2\n"
    parsed = parse_file(raw, filename="mislabeled.txt", content_type="text/csv")
    assert parsed.row_count == 1
