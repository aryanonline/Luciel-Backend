"""Unit tests for app.knowledge.parsers (Architecture §3.2.3 file parsers).

No DB, no network — pure format-extraction logic against small in-memory
fixtures built with pypdf/python-docx writers, exactly the formats the upload
endpoint now dispatches to instead of a blind utf-8 decode.
"""

from __future__ import annotations

import base64
import io
import zlib

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


def _make_pdf15_bytes(page_texts: list[str], *, offsets_in_order: bool = True) -> bytes:
    """A PDF laid out the way most producers have written one since PDF 1.5: the
    catalog, page tree, pages and font sit in a compressed object stream, and the
    cross-reference table is itself a Flate stream with the PNG Up predictor.
    pypdf's writer only emits a classic xref table (the fixture above), so this one
    is assembled by hand. ``offsets_in_order=False`` lists the object stream's
    "objnum offset" pairs in reverse: each pair still gives its object's real offset,
    but the offsets no longer increase as ISO 32000 requires."""
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
    pairs: list[tuple[int, int]] = []  # (objnum, offset), offsets strictly increasing
    body = b""
    for num, obj in packed.items():
        pairs.append((num, len(body)))
        body += obj + b"\n"
    if not offsets_in_order:
        pairs.reverse()
    header = b"".join(b"%d %d " % pair for pair in pairs)

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
    index = {num: i for i, (num, _offset) in enumerate(pairs)}  # position in the header
    rows = [(0, 0, 65535)] + [
        (2, objstm_num, index[num]) if num in index else (1, offsets[num], 0)
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


def _classic_pdf(objects: list[bytes]) -> bytes:
    """Objects 1..n, object 1 the catalog, behind a classic cross-reference table."""
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (num, body))
    xref_at = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    out.write(b"".join(b"%010d 00000 n \n" % offset for offset in offsets))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\n" % (len(objects) + 1))
    out.write(b"startxref\n%d\n%%%%EOF\n" % xref_at)
    return out.getvalue()


#: An encoder for each abbreviated filter name the fixture below writes.
_ABBREVIATED_FILTER_ENCODERS = {
    "/Fl": zlib.compress,
    "/AHx": lambda data: data.hex().encode() + b">",
    "/A85": lambda data: base64.a85encode(data) + b"~>",
}

#: A ToUnicode CMap that maps every printable ASCII code to itself.
_ASCII_TO_UNICODE_CMAP = (
    b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
    b"/CMapName /Ascii def /CMapType 2 def\n"
    b"1 begincodespacerange <00> <FF> endcodespacerange\n"
    b"1 beginbfrange <20> <7E> <0020> endbfrange\n"
    b"endcmap CMapName currentdict /CMap defineresource pop end end\n"
)


def _filtered_stream(data: bytes, filters: list[str]) -> bytes:
    for name in reversed(filters):  # a reader applies the filters first to last
        data = _ABBREVIATED_FILTER_ENCODERS[name](data)
    entry = b""
    if len(filters) == 1:
        entry = b"/Filter %s " % filters[0].encode()
    elif filters:
        entry = b"/Filter [%s] " % " ".join(filters).encode()
    return b"<< %s/Length %d >>\nstream\n%s\nendstream" % (entry, len(data), data)


def _make_pdf_with_abbreviated_filters(
    *, content_filters: list[str] | None = None, font_map_filters: list[str] | None = None
) -> bytes:
    """Two pages. Page 1's content stream is encoded with ``content_filters``, and its font
    carries a ToUnicode CMap encoded with ``font_map_filters``. Both use the abbreviated
    filter names that ISO 32000 reserves for inline images, which some producers write on
    ordinary streams too. Page 2 uses neither."""
    page = (
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] "
        b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
    )
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica%s >>"
    return _classic_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
            page % (5, 7),
            page % (6, 8),
            font % (b" /ToUnicode 9 0 R" if font_map_filters else b""),
            font % b"",
            _filtered_stream(
                b"BT /F1 12 Tf 10 100 Td (Clinic opens at nine) Tj ET", content_filters or []
            ),
            _filtered_stream(b"BT /F1 12 Tf 10 100 Td (Second page plain text) Tj ET", []),
            _filtered_stream(_ASCII_TO_UNICODE_CMAP, font_map_filters or []),
        ]
    )


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
    from pypdf.errors import FileNotDecryptedError

    open_password = "letmein"  # pragma: allowlist secret
    raw = _encrypted(
        _make_pdf_bytes("Starter engagement is 899 dollars"), user_password=open_password
    )
    with pytest.raises(ParseError) as exc_info:
        parse_file(raw, filename="secret.pdf", content_type="application/pdf")
    assert exc_info.value.code == "unparseable_pdf"
    # Refused for the password, not for any other failure that maps to the same code.
    assert isinstance(exc_info.value.__cause__, FileNotDecryptedError)


@pytest.mark.parametrize(
    "content_filters",
    [["/Fl"], ["/AHx"], ["/A85"], ["/A85", "/Fl"]],
    ids=["Fl", "AHx", "A85", "A85-Fl"],
)
def test_pdf_with_abbreviated_filter_names_extracts_text(content_filters):
    # pypdf 6 decoded these names with a hidden DeprecationWarning. pypdf 7.0 raises
    # DeprecationError instead (py-pdf/pypdf#4306), which made the whole file
    # unparseable_pdf. app.knowledge.parsers keeps reading them as pypdf 6 did.
    raw = _make_pdf_with_abbreviated_filters(content_filters=content_filters)
    parsed = parse_file(raw, filename="menu.pdf", content_type="application/pdf")
    assert parsed.text == "Clinic opens at nine\n\nSecond page plain text"


def test_pdf_whose_font_map_uses_abbreviated_filters_extracts_every_page():
    # The case reported upstream (py-pdf/pypdf#4371, open): /Filter [/A85 /Fl] on one
    # font's ToUnicode CMap. Under pypdf 7.0 that one stream rejected the whole
    # document, so page 2's plain text was lost with it.
    raw = _make_pdf_with_abbreviated_filters(font_map_filters=["/A85", "/Fl"])
    parsed = parse_file(raw, filename="menu.pdf", content_type="application/pdf")
    assert parsed.text == "Clinic opens at nine\n\nSecond page plain text"


def test_pdf_whose_object_stream_lists_offsets_out_of_order_is_refused():
    # pypdf 7.0 refuses an object stream whose offsets do not strictly increase, even
    # outside strict mode (py-pdf/pypdf#4364, a security fix: duplicate offsets created
    # one object many times). It refuses a stream that lists every object's real offset
    # in another order too, which pypdf 6 read in full, though ISO 32000 requires
    # increasing offsets. Accepted deliberately rather than worked around: such a file is
    # unparseable_pdf, a 422 on upload, and a Drive sync keeps and flags it as unreadable.
    # If a later pypdf relaxes the check, this test fails, so the change gets noticed.
    from pypdf.errors import PdfReadError

    raw = _make_pdf15_bytes(["Page one text", "Page two text"], offsets_in_order=False)
    with pytest.raises(ParseError) as exc_info:
        parse_file(raw, filename="handbook.pdf", content_type="application/pdf")
    assert exc_info.value.code == "unparseable_pdf"
    assert isinstance(exc_info.value.__cause__, PdfReadError)


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
