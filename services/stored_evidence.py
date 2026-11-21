"""Pure, page-aware evidence from a current record's already extracted text.

These helpers do not grant access or read storage. Callers must authorize the
current published record before supplying its text, and again when resolving a
saved citation. The chunk format is shared with the normal knowledge index.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from services.chunker import PageAwareChunker


_DOCUMENT_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_CHUNK_ID = re.compile(rf"(?P<document_id>{_DOCUMENT_ID})_P(?P<page>[1-9][0-9]*)_C(?P<index>[0-9]{{3,}})", re.ASCII)
_RECORD_METADATA = (
    "subject", "audience", "owner", "grade", "review_status", "publisher_role",
    "audience_class", "audience_grade", "material_type", "study_period",
    "uploaded_at", "published_at", "original_name", "original_filename",
)
_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "tif", "heic"}
_PAGE_MARKER = re.compile(r"^---\s*第\s*(\d+)\s*页\s*---$", re.MULTILINE)


def _restore_source_spans(chunks: list[dict[str, Any]], text: str) -> None:
    """Keep stripped/merged chunk text anchored to its actual source page."""
    markers = list(_PAGE_MARKER.finditer(text))
    pages = [
        (int(marker.group(1)), marker.end(), markers[index + 1].start() if index + 1 < len(markers) else len(text))
        for index, marker in enumerate(markers)
    ] if markers else [(1, 0, len(text))]
    consumed: dict[tuple[int, int], int] = {}
    for chunk in chunks:
        start, end = chunk.get("char_start"), chunk.get("char_end")
        page = next((span for span in pages if span[0] == chunk.get("page")
                     and isinstance(start, int) and span[1] <= start < span[2]), None)
        if page is None:
            chunk["char_start"] = chunk["char_end"] = None
            continue
        _, page_start, page_end = page
        page_key = (page_start, page_end)
        if isinstance(end, int) and start <= end <= page_end and text[start:end] == chunk["content"]:
            consumed[page_key] = max(consumed.get(page_key, page_start), end)
            continue
        cursor = max(page_start, consumed.get(page_key, page_start), start)
        first = last = None
        lines = [line.strip() for line in chunk["content"].splitlines() if line.strip()]
        for line in lines:
            position = text.find(line, cursor, page_end)
            if position < 0:
                first = last = None
                break
            if first is None:
                first = position
            last = cursor = position + len(line)
        if first is None or last is None:
            # Page navigation remains available; an unverified highlight does not.
            chunk["char_start"] = chunk["char_end"] = None
            continue
        chunk["char_start"], chunk["char_end"] = first, last
        chunk["content"] = text[first:last]
        consumed[page_key] = last


def parse_stored_chunk_id(chunk_id: str) -> str | None:
    """Extract a syntactically valid ID; this is never proof of authorization.

    The complete ID must subsequently match a chunk rebuilt from the exact
    current record. A forged suffix or a prefix resembling another ID cannot
    establish file ownership or access.
    """
    if not isinstance(chunk_id, str) or len(chunk_id) > 256:
        return None
    match = _CHUNK_ID.fullmatch(chunk_id)
    if not match:
        return None
    index_text = match.group("index")
    index = int(index_text)
    if index < 1 or f"{index:03d}" != index_text:
        return None
    return match.group("document_id")


def normalize_stored_text(text: str) -> str:
    """Convert structured file-agent preview JSON into page-marked plain text."""
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return text
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    if not isinstance(parsed, dict):
        return text
    items = parsed.get("items")
    if isinstance(items, list) and items:
        zero_based = any(isinstance(item, dict) and item.get("index") == 0 for item in items)
        page_blocks: list[str] = []
        paragraphs: list[str] = []
        for pos, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            item_text = str(item.get("text") or "").strip()
            if not item_text:
                continue
            item_type = str(item.get("type") or "")
            raw_idx = item.get("index", pos)
            page_num = (int(raw_idx) + 1 if zero_based else int(raw_idx)) if isinstance(raw_idx, int) else pos + 1
            if item_type in {"page", "slide", "sheet"}:
                page_blocks.append(f"--- 第{max(1, page_num)}页 ---\n{item_text}")
            else:
                paragraphs.append(item_text)
        if page_blocks:
            return "\n\n".join(page_blocks)
        if paragraphs:
            return "--- 第1页 ---\n" + "\n".join(paragraphs)
    if isinstance(parsed.get("content"), str) and parsed["content"].strip():
        return parsed["content"]
    return text


def chunks_from_record(record: Mapping[str, Any], text: str) -> list[dict[str, Any]]:
    """Build deterministic chunks without a vector store, OCR or provider call."""
    document_id = record.get("id")
    if not isinstance(document_id, str) or not re.fullmatch(_DOCUMENT_ID, document_id, re.ASCII):
        return []
    text = normalize_stored_text(text)
    if not isinstance(text, str) or not text.strip():
        return []
    filename = str(record.get("name") or record.get("original_name") or record.get("original_filename") or "")
    extension = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    indexed_text = f"--- 第1页 ---\n{text}" if extension in _IMAGE_EXTENSIONS and "--- 第" not in text else text
    metadata = {field: record[field] for field in _RECORD_METADATA if record.get(field) is not None}
    chunks = PageAwareChunker().chunk(
        indexed_text, document_id=document_id, filename=filename, extra_meta=metadata,
    )
    _restore_source_spans(chunks, indexed_text)
    for chunk in chunks:
        chunk["retrieval_sources"] = ["stored_text"]
    return chunks
