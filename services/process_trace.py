"""Structured real-time answer process tracking for GPT-style progress UI."""
from __future__ import annotations

import json
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

_WEB_STATUS_LABELS = {
    "fetched": "已讀正文",
    "snippet_only": "搜尋摘要",
    "fetching": "讀取中",
    "unavailable": "連結",
}


def _clean_text(value: Any, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def web_step_links(sources: list[dict] | None, *, limit: int = 6, force_status: str | None = None) -> list[dict]:
    """Build compact clickable web links for a process step."""
    links: list[dict] = []
    seen: set[str] = set()
    for item in sources or ():
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        host = (urlsplit(url).hostname or "").removeprefix("www.")
        status = force_status or str(item.get("content_status") or "unavailable")
        badge = _WEB_STATUS_LABELS.get(status, "")
        sublabel = " · ".join(part for part in (host, badge) if part)
        links.append({
            "kind": "web",
            "label": _clean_text(item.get("title") or host or url, 96),
            "sublabel": sublabel[:80],
            "url": url[:2048],
            "status": status,
        })
        if len(links) >= limit:
            break
    return links


def doc_step_links(chunks: list[dict] | None, *, limit: int = 6) -> list[dict]:
    """Build deduplicated clickable school-document links from retrieved chunks."""
    grouped: dict[str, dict] = {}
    order: list[str] = []
    for item in chunks or ():
        if not isinstance(item, dict):
            continue
        anchor = item.get("citation") if isinstance(item.get("citation"), dict) else {}
        doc_id = str(item.get("document_id") or anchor.get("document_id") or "").strip()
        chunk_id = str(item.get("chunk_id") or anchor.get("chunk_id") or "").strip()
        filename = _clean_text(item.get("filename") or anchor.get("filename") or "校內資料", 96)
        location = _clean_text(item.get("location") or anchor.get("location") or "", 48)
        page = item.get("page") if item.get("page") is not None else anchor.get("page")
        loc_text = location or (f"第 {page} 頁" if page not in (None, "") else "")
        key = doc_id or filename
        if key not in grouped:
            if len(order) >= limit:
                continue
            order.append(key)
            grouped[key] = {
                "kind": "doc",
                "label": filename,
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "_locs": [],
                "_count": 0,
            }
        entry = grouped[key]
        entry["_count"] += 1
        if not entry["chunk_id"] and chunk_id:
            entry["chunk_id"] = chunk_id
        if loc_text and loc_text not in entry["_locs"]:
            entry["_locs"].append(loc_text)
    links: list[dict] = []
    for key in order:
        entry = grouped[key]
        locs = entry.pop("_locs")
        count = entry.pop("_count")
        parts = []
        if locs:
            parts.append("、".join(locs[:3]) + (" 等" if len(locs) > 3 else ""))
        if count > 1:
            parts.append(f"{count} 段")
        entry["sublabel"] = " · ".join(parts)[:80]
        links.append(entry)
    return links


def file_step_links(files: list[dict] | None, *, limit: int = 6, sublabel: str = "") -> list[dict]:
    """Build clickable file preview links for photos, attachments, or generated artifacts."""
    links: list[dict] = []
    seen: set[str] = set()
    for item in files or ():
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("id") or item.get("file_id") or "").strip()
        name = _clean_text(item.get("name") or item.get("filename") or item.get("original_name") or "", 96)
        if not name or (file_id and file_id in seen):
            continue
        if file_id:
            seen.add(file_id)
        entry = {"kind": "file", "label": name, "file_id": file_id}
        if sublabel:
            entry["sublabel"] = sublabel[:48]
        links.append(entry)
        if len(links) >= limit:
            break
    return links


def memory_step_links(refs: list[dict] | None, *, limit: int = 4) -> list[dict]:
    """Build clickable memory links."""
    links: list[dict] = []
    for item in refs or ():
        if not isinstance(item, dict):
            continue
        mem_id = str(item.get("id") or "").strip()
        content = _clean_text(item.get("content") or "", 64)
        if not mem_id or not content:
            continue
        links.append({"kind": "memory", "label": content, "memory_id": mem_id})
        if len(links) >= limit:
            break
    return links


class ProcessTracker:
    """Collects chronological process steps and serializes SSE + persisted trace."""

    def __init__(self, started_at: float | None = None, config: dict[str, Any] | None = None):
        self.started_at = perf_counter() if started_at is None else started_at
        self.config: dict[str, Any] = dict(config or {})
        self._steps: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._step_started: dict[str, float] = {}

    def update_config(self, **fields: Any) -> None:
        for key, value in fields.items():
            if value is not None:
                self.config[key] = value

    def step(
        self,
        step_id: str,
        kind: str,
        status: str,
        title: str,
        *,
        detail: str = "",
        note: str = "",
        links: list[dict] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = perf_counter()
        existing = self._steps.get(step_id)
        if existing is None:
            self._order.append(step_id)
            self._step_started[step_id] = now
            record: dict[str, Any] = {
                "id": step_id,
                "kind": kind,
                "status": status,
                "title": _clean_text(title, 140),
            }
            self._steps[step_id] = record
        else:
            record = existing
            record["kind"] = kind or record.get("kind", "step")
            record["status"] = status
            if title:
                record["title"] = _clean_text(title, 140)
            if status == "running" and step_id not in self._step_started:
                self._step_started[step_id] = now

        if detail:
            record["detail"] = _clean_text(detail, 180)
        elif detail == "" and existing is None:
            record.pop("detail", None)

        if note:
            record["note"] = _clean_text(note, 240)

        if links is not None:
            record["links"] = links[:8]

        if meta:
            record["meta"] = {**(record.get("meta") or {}), **meta}

        if status != "running":
            started = self._step_started.get(step_id, now)
            duration_ms = max(0, int((now - started) * 1000))
            if duration_ms >= 50 or "duration_ms" not in record:
                record["duration_ms"] = duration_ms

        return {
            "type": "process_step",
            **record,
            "elapsed_ms": max(0, int((now - self.started_at) * 1000)),
            "config": dict(self.config),
        }

    def sse(
        self,
        step_id: str,
        kind: str,
        status: str,
        title: str,
        *,
        detail: str = "",
        note: str = "",
        links: list[dict] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> str:
        payload = self.step(
            step_id, kind, status, title,
            detail=detail, note=note, links=links, meta=meta,
        )
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

    def ingest(self, event_data: dict[str, Any]) -> None:
        """Record an already-formed process_step emitted by a sub-pipeline."""
        step_id = str(event_data.get("id") or "").strip()
        if not step_id:
            return
        self.step(
            step_id,
            str(event_data.get("kind") or "step"),
            str(event_data.get("status") or "done"),
            str(event_data.get("title") or ""),
            detail=str(event_data.get("detail") or ""),
            note=str(event_data.get("note") or ""),
            links=event_data.get("links") if isinstance(event_data.get("links"), list) else None,
            meta=event_data.get("meta") if isinstance(event_data.get("meta"), dict) else None,
        )

    def finalize(self, final_status: str = "done") -> dict[str, Any]:
        now = perf_counter()
        steps = []
        for step_id in self._order:
            record = dict(self._steps[step_id])
            if record.get("status") == "running":
                record["status"] = "done" if final_status == "done" else "warning"
                started = self._step_started.get(step_id, now)
                record["duration_ms"] = max(0, int((now - started) * 1000))
                if record["status"] == "done" and record.get("title", "").startswith("正在"):
                    record["title"] = "已" + record["title"][2:].rstrip("….")
            steps.append(record)
        return {
            "elapsed_ms": max(0, int((now - self.started_at) * 1000)),
            "status": final_status,
            "config": dict(self.config),
            "steps": steps,
        }
