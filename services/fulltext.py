"""Local semantic search over authorized source files, with verifiable locations."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from collections import Counter
from contextlib import closing
from itertools import zip_longest
from pathlib import Path

import numpy as np
from utils.image_utils import PHOTO_EXTENSIONS
from services.chunker import MAX_DOCUMENT_CHARS, MAX_DOCUMENT_CHUNKS
from services.resource_limits import MAX_FILE_BYTES, ParserLimitError, validate_pdf, validate_zip

log = logging.getLogger(__name__)
EXTENSIONS = {".docx", ".xlsx", ".pdf", ".pptx", ".md", ".markdown", ".txt", ".csv", ".tsv", ".json"} | PHOTO_EXTENSIONS
_PAGE = re.compile(r"^---\s*第\s*(\d+)\s*[页頁]\s*---[ \t]*$", re.M)
# The local embedding model has a 128-token input window. Keep passages short
# and give the evidence model neighboring passages when it verifies a match.
_PASSAGE_CHARS = 96
_INDEX_LOCK = threading.RLock()
_EMBED_LOCK = threading.Lock()
_embedder = None
_BATCH_SIZE = 32
_MAX_SEARCH_PASSAGES = 16_384


_DIGESTS = {}
_DIGEST_LOCK = threading.Lock()


def _file_digest(path, stat):
    """Content hash of an original file, recomputed only when size or mtime change."""
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _DIGEST_LOCK:
        cached = _DIGESTS.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    with _DIGEST_LOCK:
        _DIGESTS[key] = digest.hexdigest()[:24]
        return _DIGESTS[key]


def _terms(text):
    """Character bigrams for Chinese plus whole ASCII words, Traditional folded to Simplified."""
    text = str(text or "")
    try:
        import zhconv
        text = zhconv.convert(text, "zh-cn")
    except Exception:
        pass
    text = text.lower()
    chars = [c for c in text if "一" <= c <= "鿿"]
    return [a + b for a, b in zip(chars, chars[1:])] + re.findall(r"[a-z0-9]+", text)


def _encode(texts):
    global _embedder
    from services.knowledge.embedder import get_embedder
    with _EMBED_LOCK:
        _embedder = get_embedder()
        encoded = np.asarray(_embedder.embed(texts), dtype=np.float32)
    if encoded.ndim != 2 or encoded.shape[0] != len(texts) or not np.isfinite(encoded).all():
        raise ValueError("全文嵌入結果格式不正確，請重新建立索引。")
    return encoded / np.maximum(np.linalg.norm(encoded, axis=1, keepdims=True), 1e-12)


def _embedding_id():
    from services.knowledge.embedder import embedding_fingerprint, get_embedder
    return embedding_fingerprint(get_embedder())


def _bounded_units(units):
    length = 0
    for page, location, content in units:
        length += len(str(content))
        if length > MAX_DOCUMENT_CHARS:
            raise ParserLimitError("全文索引最多 100 萬字元，請分拆資料。")
        yield page, location, content


def _pages(text):
    matches = list(_PAGE.finditer(text))
    return {int(match[1]): text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()
            for i, match in enumerate(matches)}


def _decode(data):
    for encoding in ("utf-8-sig", "utf-16", "gb18030", "big5"):
        if encoding == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("文字編碼無法辨識")


def _extract(path, saved, ocr, gaps):
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ParserLimitError("檔案超過 50 MB。")
    ext = path.suffix.lower()
    if ext in {".xlsx", ".pptx", ".docx"}:
        validate_zip(path)
    if ext == ".pdf":
        import pymupdf as fitz
        cached = _pages(saved)
        with fitz.open(path) as doc:
            validate_pdf(doc)
            for number, page in enumerate(doc, 1):
                content = page.get_text().strip()
                images = bool(page.get_image_info()) if not content else False
                if not content:
                    content = cached.get(number, "")
                if not content and images and ocr is not None:
                    try:
                        raster = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                        content = ocr(raster.tobytes("png"), f"{path.name} #{number}")
                    except Exception as exc:
                        log.info("PDF OCR incomplete for page %s (%s)", number, type(exc).__name__)
                if not content and images:
                    gaps.append(f"第 {number} 頁缺少辨識文字")
                if "\x00" in content or "\ufffd" in content:
                    gaps.append(f"第 {number} 頁有缺字")
                for line in content.splitlines():
                    yield number, f"第 {number} 頁", line
    elif ext == ".xlsx":
        from openpyxl import load_workbook
        with closing(load_workbook(path, read_only=True, data_only=False, keep_links=False)) as formulas:
            with closing(load_workbook(path, read_only=True, data_only=True, keep_links=False)) as values:
                for number, sheet in enumerate(formulas, 1):
                    # Some writers declare an incorrect used range.
                    sheet.reset_dimensions()
                    values[sheet.title].reset_dimensions()
                    for row_number, (row, cached) in enumerate(zip(
                            sheet.iter_rows(), values[sheet.title].iter_rows(values_only=True)), 1):
                        cells = []
                        for cell, result in zip(row, cached):
                            if cell.value is None:
                                continue
                            value = cell.value
                            if cell.data_type == "f" and result is not None:
                                value = f"{result}（{value}）"
                            cells.append(f"{cell.coordinate} {value}")
                        if cells:
                            yield number, f"{sheet.title} · 第 {row_number} 列", " | ".join(cells)
    elif ext == ".pptx":
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE

        def shape_text(shapes):
            for shape in shapes:
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    yield from shape_text(shape.shapes)
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        yield paragraph.text
                if shape.has_table:
                    for row in shape.table.rows:
                        yield " | ".join(cell.text for cell in row.cells)

        for number, slide in enumerate(Presentation(path).slides, 1):
            location = f"第 {number} 張投影片"
            for line in shape_text(slide.shapes):
                yield number, location, line
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                for line in slide.notes_slide.notes_text_frame.text.splitlines():
                    yield number, f"{location} · 備註", line
    elif ext == ".docx":
        from docx import Document
        from docx.oxml.ns import qn
        # Drawn text boxes and nested tables are included in document order.
        doc = Document(path)
        paragraph_tag, text_tag = qn("w:p"), qn("w:t")

        def paragraphs(element):
            for para in element.iter(paragraph_tag):
                yield "".join(node.text or "" for node in para.iter(text_tag)
                              if next(node.iterancestors(paragraph_tag), None) is para)

        for number, line in enumerate(paragraphs(doc.element.body), 1):
            yield None, f"第 {number} 段", line
        seen = set()
        for section in doc.sections:
            for name, part in (("頁首", section.header), ("頁尾", section.footer),
                               ("首頁頁首", section.first_page_header), ("首頁頁尾", section.first_page_footer),
                               ("偶數頁首", section.even_page_header), ("偶數頁尾", section.even_page_footer)):
                if part.is_linked_to_previous or part.part.partname in seen:
                    continue
                seen.add(part.part.partname)
                for number, line in enumerate(paragraphs(part._element), 1):
                    yield None, f"{name} · 第 {number} 段", line
    else:
        with path.open("rb") as handle:
            raw = handle.read(MAX_DOCUMENT_CHARS * 4 + 5)
        if len(raw) > MAX_DOCUMENT_CHARS * 4 + 4:
            raise ParserLimitError("全文索引最多 100 萬字元，請分拆資料。")
        for number, line in enumerate(_decode(raw).splitlines(), 1):
            yield None, f"第 {number} 行", line


def _fallback(saved, ext):
    pages = _pages(saved)
    if pages:
        for page, text in pages.items():
            for line in text.splitlines():
                yield page, f"第 {page} 頁", line
    else:
        for number, line in enumerate(saved.splitlines(), 1):
            location = f"第 {number} 行" if ext in {".md", ".markdown", ".txt", ".csv", ".tsv", ".json"} else f"保存正文第 {number} 行"
            yield None, location, line


def _passages(units):
    # Consecutive lines from the same page or slide share a passage if they fit.
    pending = ""
    previous = (None, "")
    for page, location, text in units:
        for line in str(text or "").splitlines():
            line = line.strip()
            for start in range(0, len(line), _PASSAGE_CHARS):
                piece = line[start:start + _PASSAGE_CHARS]
                if pending and (previous != (page, location) or len(pending) + len(piece) + 1 > _PASSAGE_CHARS):
                    yield *previous, pending
                    pending = ""
                pending = f"{pending}\n{piece}" if pending else piece
                previous = (page, location)
    if pending:
        yield *previous, pending


class FullTextIndex:
    def __init__(self, directory):
        self.directory = Path(directory)
        self._configure_path()

    def _configure_path(self):
        # Never reuse vectors from another model, backend, tokenizer or dimension.
        self.embedding_id = _embedding_id()
        self.path = self.directory / f"fulltext-v3-{self.embedding_id}.sqlite"

    def _connect(self):
        self._configure_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("全文索引不可使用符號連結資料庫。")
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, version TEXT NOT NULL,
                gaps TEXT NOT NULL, retry_at REAL
            );
            CREATE TABLE IF NOT EXISTS passages (
                document_id TEXT REFERENCES documents(id) ON DELETE CASCADE,
                position INTEGER, page INTEGER, location TEXT NOT NULL,
                content TEXT NOT NULL, vector BLOB NOT NULL,
                PRIMARY KEY (document_id, position)
            );
        """)
        return closing(db)

    def ensure(self, record, original, saved, ocr=None):
        if len(saved) > MAX_DOCUMENT_CHARS:
            raise ParserLimitError("全文索引最多 100 萬字元，請分拆資料。")
        embedding_id = _embedding_id()
        fid = record["id"]
        ext = Path(record.get("original_name") or record.get("name") or "").suffix.lower()
        path = Path(original) if original else None
        is_photo = ext in PHOTO_EXTENSIONS or (path is not None and path.suffix.lower() in PHOTO_EXTENSIONS)
        if is_photo:
            path = None  # A photo's searchable evidence is the persisted OCR text.
        stat = path.stat() if path and path.is_file() else None
        if stat and stat.st_size > MAX_FILE_BYTES:
            raise ParserLimitError("檔案超過 50 MB。")
        if not is_photo and ext not in EXTENSIONS and (not path or path.suffix.lower() not in EXTENSIONS):
            return ["格式缺少全文提取支援"]
        cache_text = hashlib.sha256(saved.encode("utf-8")).hexdigest()
        if is_photo:
            cache_text += ":" + str(record.get("extraction_status") or "")
        # Fingerprint by file name, size and content, never by absolute path or
        # modification time: copying the data between a laptop and the server
        # must not make every stored index look stale and re-encode everything.
        fingerprint = hashlib.sha256(repr((embedding_id, path.name if path else None, stat.st_size if stat else None,
                                            _file_digest(path, stat) if stat else None,
                                            cache_text)).encode()).hexdigest()[:16]
        with self._connect() as db:
            existing = db.execute("SELECT * FROM documents WHERE id = ?", (fid,)).fetchone()
        if (existing and existing["fingerprint"] == fingerprint
                and (ocr is None or existing["retry_at"] is None or existing["retry_at"] > time.time())):
            return json.loads(existing["gaps"])

        # Image recognition and embedding can be slow. Keep them outside the
        # SQLite transaction and the shared write lock so existing sources remain searchable.
        gaps = []
        units = []
        if is_photo:
            units = [(1, "照片 OCR", line) for _, _, line in _fallback(saved, ext)]
            if record.get("is_truncated"):
                gaps.append("保存正文有截斷")
        elif stat:
            try:
                units = list(_bounded_units(_extract(path, saved, ocr, gaps)))
            except ParserLimitError:
                raise
            except Exception as exc:
                log.info("Corpus extraction failed for %s (%s)", fid, type(exc).__name__)
                gaps.append("原檔暫時無法提取正文")
        else:
            gaps.append("原檔缺失，僅能搜尋已保存正文" if saved else "原檔和保存正文均缺失")
        if not units and saved:
            units = _fallback(saved, ext)
            if record.get("is_truncated"):
                gaps.append("保存正文有截斷")
        passages = []
        for passage in _passages(_bounded_units(units)):
            if len(passages) >= MAX_DOCUMENT_CHUNKS:
                raise ParserLimitError("全文索引最多 512 個段落塊，請分拆資料。")
            passages.append(passage)
        if not passages and not (is_photo and record.get("extraction_status") == "empty"):
            gaps.append("沒有可搜尋的正文")
        rows = []
        for start in range(0, len(passages), _BATCH_SIZE):
            batch = passages[start:start + _BATCH_SIZE]
            vectors = _encode([content for _, _, content in batch])
            rows.extend((fid, pos, page, loc, content, vector.tobytes())
                        for pos, ((page, loc, content), vector) in enumerate(zip(batch, vectors), start))

        if stat and (not path.is_file() or path.stat().st_mtime_ns != stat.st_mtime_ns):
            return ["檔案已更新，正在重建全文索引"]
        if _embedding_id() != embedding_id:
            raise ValueError("嵌入後端已變更，請重試建立全文索引。")
        with _INDEX_LOCK, self._connect() as db, db:
            latest = db.execute("SELECT * FROM documents WHERE id = ?", (fid,)).fetchone()
            if latest and (not existing or latest["version"] != existing["version"]):
                if latest["fingerprint"] != fingerprint or len(json.loads(latest["gaps"])) <= len(gaps):
                    return json.loads(latest["gaps"])
            db.execute("DELETE FROM documents WHERE id = ?", (fid,))
            retry_at = time.time() + 900 if ocr is not None and stat and gaps else (0 if stat and gaps else None)
            version = hashlib.sha256(repr((fingerprint, passages)).encode()).hexdigest()[:16]
            db.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
                       (fid, fingerprint, version, json.dumps(gaps), retry_at))
            db.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?)", rows)
        return gaps

    @staticmethod
    def _item(row, record, version):
        fid = record["id"]
        return {"chunk_id": f"full_{fid}_{version}_{row['position']}",
                "document_id": fid, "position": row["position"], "page": row["page"],
                "location": row["location"], "content": row["content"],
                "filename": record.get("name") or record.get("original_name"),
                "retrieval_sources": ["semantic_fulltext"]}

    def candidates(self, records, queries, *, limit=18):
        if not records or not queries:
            return []
        questions = _encode(queries)
        query_terms = set()
        for query in queries:
            query_terms.update(_terms(query))
        entries = []  # [semantic score, document id, position, version, matched term counts, length]
        with self._connect() as db:
            db.execute("BEGIN")
            for fid, record in records.items():
                info = db.execute("SELECT version FROM documents WHERE id = ?", (fid,)).fetchone()
                if not info:
                    continue
                # The file name is part of what a person asks for ("graduation trip
                # itinerary"), so its terms count for every passage of that file.
                name_terms = query_terms.intersection(_terms(record.get("name") or record.get("original_name")))
                cursor = db.execute("SELECT * FROM passages WHERE document_id = ? ORDER BY position", (fid,))
                while batch := cursor.fetchmany(256):
                    usable = []
                    vectors = []
                    for row in batch:
                        if len(row["vector"]) != questions.shape[1] * np.dtype(np.float32).itemsize:
                            continue
                        vector = np.frombuffer(row["vector"], dtype=np.float32)
                        if np.isfinite(vector).all():
                            usable.append(row)
                            vectors.append(vector)
                    if not usable:
                        continue
                    batch = usable
                    matrix = np.stack(vectors)
                    scores = (matrix @ questions.T).max(axis=1)
                    for row, score in zip(batch, scores):
                        counts = Counter(term for term in _terms(row["content"]) if term in query_terms)
                        counts.update(name_terms)
                        entries.append([float(score), fid, row["position"], info["version"],
                                        counts, max(len(row["content"]), 1)])
                        if len(entries) > _MAX_SEARCH_PASSAGES:
                            raise ParserLimitError("全文檢索超過段落預算，請先縮小所選文件範圍。")
            if not entries:
                return []
            # Meaning alone lets a few large files crowd out the one whose title and
            # text say exactly what was asked. Rank also by matching terms (BM25,
            # with the file name included) so both routes can bring a passage in.
            total = len(entries)
            frequency = Counter(term for entry in entries for term in entry[4])
            average = sum(entry[5] for entry in entries) / total

            def term_score(entry):
                value = 0.0
                for term, count in entry[4].items():
                    idf = math.log(1 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5))
                    value += idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * entry[5] / average))
                return value

            lexical = [term_score(entry) for entry in entries]
            by_meaning = sorted(range(total), key=lambda i: -entries[i][0])
            by_terms = [i for i in sorted(range(total), key=lambda i: -lexical[i]) if lexical[i] > 0]
            # Alternate between the two rankings, so each route's best passages
            # get half of the slots rather than being averaged away.
            fused, seen = [], set()
            for pair in zip_longest(by_meaning, by_terms):
                for i in pair:
                    if i is not None and i not in seen and len(fused) < limit:
                        seen.add(i)
                        fused.append(i)
            queue = [(entries[i][0], entries[i][1], entries[i][2], entries[i][3]) for i in fused]
            # Return source order within each context window. Citation IDs always
            # point to a single original passage, even across adjacent windows.
            matches = {}
            for score, fid, position, version in queue:
                rows = db.execute("SELECT position, page, location, content FROM passages "
                                  "WHERE document_id = ? AND position BETWEEN ? AND ? ORDER BY position",
                                  (fid, position - 1, position + 2))
                for row in rows:
                    item = self._item(row, records[fid], version)
                    item["score"] = round(score, 4)
                    matches.setdefault(item["chunk_id"], item)
            return list(matches.values())

    def document_passages(self, record, *, limit=-1):
        """Return original, ordered passages with the current citation IDs."""
        with self._connect() as db:
            db.execute("BEGIN")
            info = db.execute("SELECT version FROM documents WHERE id = ?", (record["id"],)).fetchone()
            if not info:
                return []
            rows = db.execute("SELECT position, page, location, content FROM passages "
                              "WHERE document_id = ? ORDER BY position LIMIT ?", (record["id"], limit))
            return [self._item(row, record, info["version"]) for row in rows]

    def expand_short_sources(self, records, selected):
        """Include the whole short source so nearby exceptions remain visible."""
        expanded = {}
        for fid in dict.fromkeys(item["document_id"] for item in selected):
            matches = [item for item in selected if item["document_id"] == fid]
            all_passages = self.document_passages(records[fid], limit=25)
            if (0 < len(all_passages) <= 24 and len(expanded) + len(all_passages) <= 48
                    and {item["chunk_id"] for item in matches} <= {item["chunk_id"] for item in all_passages}):
                score = max((item.get("score") or 0 for item in matches), default=0)
                expanded.update((item["chunk_id"], {**item, "score": score, "audience": records[fid].get("audience")})
                                for item in all_passages)
            else:
                expanded.update((item["chunk_id"], item) for item in matches)
        return list(expanded.values())

    def context(self, record, version, position):
        with self._connect() as db:
            db.execute("BEGIN")
            info = db.execute("SELECT version FROM documents WHERE id = ?", (record["id"],)).fetchone()
            if not info or info["version"] != version:
                return None
            rows = db.execute("SELECT position, page, location, content FROM passages "
                              "WHERE document_id = ? AND position BETWEEN ? AND ? ORDER BY position",
                              (record["id"], position - 3, position + 3))
            result = {"target": None, "context_before": [], "context_after": []}
            for row in rows:
                item = self._item(row, record, version)
                chunk_id, content = item.pop("chunk_id"), item.pop("content")
                chunk = {"chunk_id": chunk_id, "content": content, "metadata": item}
                if row["position"] == position:
                    result["target"] = chunk
                else:
                    key = "context_before" if row["position"] < position else "context_after"
                    result[key].append(chunk)
            return result if result["target"] else None

    def remove(self, document_id):
        # Removal must also revoke earlier embedding versions, so changing the
        # backend later cannot bring a deleted document back into search.
        with _INDEX_LOCK:
            paths = [self.directory / "fulltext-v2.sqlite", *self.directory.glob("fulltext-v3-*.sqlite")]
            for path in paths:
                if path.is_file() and not path.is_symlink():
                    with closing(sqlite3.connect(path, timeout=30)) as db, db:
                        db.execute("PRAGMA foreign_keys=ON")
                        db.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def parse_chunk_id(chunk_id):
    if not isinstance(chunk_id, str) or not chunk_id.startswith("full_"):
        return None
    parts = chunk_id[5:].rsplit("_", 2)
    if len(parts) != 3 or not re.fullmatch(r"[a-f0-9]{16}", parts[1]) or not parts[2].isascii() or not parts[2].isdigit():
        return None
    return parts[0], parts[1], int(parts[2])
