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
