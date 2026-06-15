"""Text parsing + chunking for ingestion (Architecture §3.2).

Parsing of binary formats (PDF/DOCX) is a pluggable stage; this module owns the
chunking primitive (token-ish windowing with overlap) used by every ingest path.
Kept dependency-light: chunk by paragraphs, packing into ~chunk_size-char windows
with overlap so retrieval has coherent units.
"""

from __future__ import annotations

DEFAULT_CHUNK_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 150


def chunk_text(
    text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS, overlap: int = DEFAULT_OVERLAP_CHARS
) -> list[str]:
    """Split text into overlapping chunks on paragraph boundaries where possible."""
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= chunk_chars:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                chunks.append(current)
            if len(para) <= chunk_chars:
                current = para
            else:
                # Hard-wrap an over-long paragraph with overlap.
                start = 0
                while start < len(para):
                    chunks.append(para[start : start + chunk_chars])
                    start += chunk_chars - overlap
                current = ""
    if current:
        chunks.append(current)
    return chunks
