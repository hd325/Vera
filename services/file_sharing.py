"""聊天中「這條訊息附了什麼檔案」的說明，以及經用戶確認後變更檔案公開範圍。

變更範圍不另寫一套規則：把原件重新送進網站的上傳接口（命名、審核、存放位置
全部沿用網站邏輯），成功後移除原來的個人版本。
"""
from __future__ import annotations

import hashlib
import io
import os
import time
from pathlib import Path

from fastapi import HTTPException, UploadFile

PENDING_SECONDS = 30 * 60
SHAREABLE = ("class", "grade", "school", "personal")


def labels(host, record) -> dict:
    return {
        "audience": host._AUDIENCE_LABELS.get(record.get("audience"), "個人"),
        "subject": record.get("subject_custom") or host._SUBJECT_LABELS.get(record.get("subject"), ""),
        "material": record.get("material_custom") or host._MATERIAL_LABELS.get(record.get("material_type"), ""),
        "period": host._PERIOD_LABELS.get(record.get("study_period"), ""),
    }


def classification_text(host, record) -> str:
    value = labels(host, record)
    parts = [value["audience"] + "可見", value["subject"], value["material"], value["period"]]
    return "｜".join(part for part in parts if part)


async def owned_files(host, owner, file_ids) -> list[dict]:
    """只返回本人上傳、目前仍可使用的檔案。"""
    result = []
    for file_id in dict.fromkeys(file_ids or []):
        record = await host.data_store.find_file_by_id(file_id)
        if record and record.get("owner") == owner and host.check_file_permission(record, owner):
            result.append(record)
    return result


async def describe_files(host, owner, file_ids) -> list[dict]:
    """給模型看的附件清單：名稱、分類、文字識別狀態與開頭內容。"""
    described = []
    for file_id in dict.fromkeys(file_ids or []):
        record = await host.data_store.find_file_by_id(file_id)
        if not record or not host.check_file_permission(record, owner):
            continue
        content = await host.data_store.get_file_content(file_id) or ""
        described.append({
            "name": record.get("name", ""),
            "original_name": record.get("original_filename") or record.get("original_name") or "",
            "classification": classification_text(host, record),
            "uploaded_by_user": record.get("owner") == owner,
            "text_recognized": bool(content.strip()),
            "beginning": " ".join(content.split())[:300],
        })
    return described


def attachment_prompt(described) -> str:
    if not described:
        return ""
    lines = []
    for index, item in enumerate(described, 1):
        state = "已識別文字" if item["text_recognized"] else "未能識別出文字，只能當作圖片或原件"
        lines.append(f"{index}. 《{item['name']}》（原檔名：{item['original_name'] or '未知'}；{item['classification']}；{state}）")
    return ("\n\n**本輪訊息附帶的檔案：**\n" + "\n".join(lines) +
            "\n這些檔案是用戶隨本輪訊息一起送來的，已存入其資料庫。用戶說的「這個」「這份」「它」「這張圖」指的就是它們；"
            "請結合檔案內容回應，不要當作沒有收到附件，也不要把要求誤解為與先前話題有關。\n")


def pending_action(conversation) -> dict | None:
    pending = conversation.get("pending_file_action") if conversation else None
    if not isinstance(pending, dict) or time.time() - pending.get("created_at", 0) > PENDING_SECONDS:
        return None
    return pending


def proposal_text(host, records, audience) -> str:
    names = "、".join("《" + record.get("name", "文件") + "》" for record in records)
    scope = {"class": "本班同學", "grade": "全年級同學", "school": "全校", "personal": "只有你自己"}[audience]
    detail = "；".join("《" + record.get("name", "文件") + "》目前為：" + classification_text(host, record) for record in records)
    return ("📎 " + detail + "。\n要把" + names + "改為「" + scope + "可見」嗎？"
            + ("學生公開的資料需要老師審核通過後，其他同學才看得到。" if audience != "personal" else "")
            + "請回覆是否確認；也可以告訴我改成其他範圍，或不用更改。")


async def change_audience(host, owner, file_id, audience) -> dict:
    """Re-submit the original through the website upload path, then retire the old copy."""
    from services.task_api import managed_upload
    if audience not in SHAREABLE:
        raise HTTPException(422, "公開範圍無效。")
    record = await host.data_store.find_file_by_id(file_id)
    if not record or record.get("owner") != owner or not host.check_file_permission(record, owner):
        raise HTTPException(403, "只能更改自己上傳的檔案。")
    if record.get("audience") == audience:
        return record
    # 與網站刪除規則一致：學生只能處理自己的個人檔案；已公開或已提交審核的需由老師處理。
    if not host.can_delete_file_record(record, owner):
        raise HTTPException(403, "這份檔案已公開或已提交審核，需要由老師更改。")
    source = host.resolve_storage_path(record.get("storage_path"), record)
    if not source or not os.path.isfile(source):
        raise HTTPException(409, "原始檔案已不存在，無法更改公開範圍。")
    raw = Path(source).read_bytes()
    content = await host.data_store.get_file_content(file_id) or ""
    extraction = {"content": content, "is_truncated": bool(record.get("is_truncated")),
                  "page_count": record.get("page_count", 0),
                  "extraction_status": record.get("extraction_status") or ("success" if content else "failed")}
    session_id = "scope-change-" + file_id
    name = record.get("original_filename") or record.get("original_name") or record.get("name") or "file"
    token = managed_upload.set(owner)
    try:
        result = await host.upload_file(
            file=UploadFile(filename=name, file=io.BytesIO(raw)), audience=audience,
            subject=record.get("subject") or "other", subject_custom=record.get("subject_custom") or "",
            material_type=record.get("material_type") or "other", material_custom=record.get("material_custom") or "",
            study_period=record.get("study_period") or host.study_period_for_month(host.datetime.now().month),
            notes=(record.get("notes") or "")[:host.MAX_NOTES_CHARS], target_class="", quick_upload="false",
            extraction_result=host.json.dumps(extraction),
            extraction_signature=host.sign_extraction(extraction, hashlib.sha256(raw).hexdigest(), session_id),
            upload_request_id=("scope-" + audience + "-" + file_id)[:64], session_id=session_id)
    finally:
        managed_upload.reset(token)
    new_record = await host.data_store.find_file_by_id(result["data"]["file_id"])
    async with host._upload_commit_lock:
        removed = await host.data_store.delete_file(owner, file_id, None)
    if not removed:
        host.logging.warning("公開範圍已更改，但原個人版本未能移除: %s", file_id)
    return new_record


def result_text(host, changed, failed, audience) -> str:
    scope = {"class": "本班", "grade": "全年級", "school": "全校", "personal": "僅自己"}[audience]
    lines = []
    for record in changed:
        waiting = record.get("review_status") == "pending"
        lines.append("✅ 《" + record.get("name", "文件") + "》已" + ("提交為「" + scope + "可見」，等待老師審核通過後公開" if waiting
                     else "改為「" + scope + "可見」") + "（" + classification_text(host, record) + "）。")
    for name, reason in failed:
        lines.append("⚠️ 《" + name + "》未能更改：" + reason)
    return "\n".join(lines) or "沒有找到可以更改的本人檔案。"
