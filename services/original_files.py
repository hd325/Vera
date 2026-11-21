"""Reauthorize original files before returning attachment metadata."""
from pathlib import Path

from fastapi import HTTPException


async def original_artifacts(host, owner, file_ids, *, formats=()):
    """Reauthorize actual originals; callers may reuse this at publication time."""
    artifacts = []
    for fid in dict.fromkeys(file_ids):
        record = await host.data_store.find_file_by_id(fid)
        if not record and hasattr(host, "_authorized_turn_file"):
            record = await host._authorized_turn_file(fid, owner)
        if not record or not host.check_file_permission(record, owner):
            raise HTTPException(403, "原檔案不存在或沒有使用權限。")
        name = record.get("original_filename") or record.get("original_name") or record.get("name") or "file"
        extension = Path(name).suffix.lower().lstrip(".")
        if extension == "markdown":
            extension = "md"
        if formats and extension not in formats:
            continue
        path = host.resolve_storage_path(record.get("storage_path"), record)
        if not path or not Path(path).is_file():
            raise HTTPException(410, "找到資料記錄，但原始文件已遺失，請重新上傳。")
        size = Path(path).stat().st_size
        artifacts.append({"id": fid, "name": name, "filename": name, "size": size,
                          "file_size": size, "has_original": True, "version": record.get("version", 1),
                          "parent_id": record.get("parent_id"), "source_kind": "original",
                          "description": "原始文件，可下載完整內容。"})
    return artifacts
