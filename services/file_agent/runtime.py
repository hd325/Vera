"""Trusted task directories, bounded MCP subprocesses and local Office previews."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

MAX_INPUT_BYTES = 50 * 1024 * 1024
from services.resource_limits import validate_pdf
MAX_TASK_BYTES = 150 * 1024 * 1024
MAX_OUTPUTS = 8
TOOL_TIMEOUT = 55


class FileAgentError(ValueError):
    """A safe, user-facing failure, without credentials or internal paths."""


def user_key(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


def safe_child(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise FileAgentError("檔案路徑超出本次任務範圍。")
    return resolved


class TaskRuntime:
    def __init__(self, uploads: Path, user_id: str, conversation_id: str):
        self.user_id = user_id
        self.task_id = uuid.uuid4().hex
        self.root = (uploads / "file_agent" / user_key(user_id) / self.task_id).resolve()
        self.inputs = self.root / "inputs"
        self.outputs = self.root / "outputs"
        self.inputs.mkdir(parents=True, exist_ok=False)
        self.outputs.mkdir()
        self.paths: dict[str, Path] = {}
        self.records: dict[str, dict] = {}
        self.generated: dict[str, dict] = {}
        self.ocr_text: dict[str, str] = {}
        self.read_ids: set[str] = set()
        self.asset_ids: set[str] = set()
        self.read_ranges: dict[str, list[tuple[int, int]]] = {}
        self.deadline = time.monotonic() + 600
        self.conversation_id = conversation_id
        self.write_state("created")

    def write_state(self, state: str, **extra):
        value = {"task_id": self.task_id, "user_key": user_key(self.user_id),
                 "conversation_id": self.conversation_id, "state": state, **extra}
        temporary = self.root / "task.tmp"
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.root / "task.json")

    def add_input(self, record: dict, source: Path, extracted_text: str = ""):
        if len(self.records) >= 12:
            raise FileAgentError("每次最多引用 12 份檔案，請分批處理。")
        if source.stat().st_size > MAX_INPUT_BYTES:
            raise FileAgentError("引用檔案超過 50 MB，請縮小後重試。")
        file_id = str(record["id"])
        suffix = source.suffix.lower()
        target = self.inputs / (uuid.uuid4().hex + suffix)
        shutil.copyfile(source, target)
        self.paths[file_id] = target
        self.records[file_id] = dict(record)
        self.ocr_text[file_id] = extracted_text
        self.check_quota()

    def check_quota(self):
        size = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        if size > MAX_TASK_BYTES:
            raise FileAgentError("本次檔案任務超過 150 MB 空間限制。")
        if len(self.generated) > MAX_OUTPUTS:
            raise FileAgentError("一次最多產生 8 個檔案。")

    def write_checkpoint(self, *, agent=None):
        """Save only trusted task-relative paths; callers choose when a step settled."""
        paths = {key: safe_child(self.root, value).relative_to(self.root).as_posix() for key, value in self.paths.items()}
        value = {"version": 1, "task_id": self.task_id, "user_key": user_key(self.user_id),
                 "conversation_id": self.conversation_id, "paths": paths, "records": self.records,
                 "generated": {key: {k: v for k, v in item.items() if k != "path"} for key, item in self.generated.items()},
                 "ocr_text": self.ocr_text, "read_ids": sorted(self.read_ids), "asset_ids": sorted(self.asset_ids),
                 "read_ranges": self.read_ranges}
        if agent is not None:
            value["agent"] = agent
        temporary = self.root / ("checkpoint." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.root / "checkpoint.json")

    @classmethod
    def restore_checkpoint(cls, uploads: Path, user_id: str, conversation_id: str, task_id: str):
        if not isinstance(task_id, str) or len(task_id) != 32 or any(c not in "0123456789abcdef" for c in task_id):
            raise FileAgentError("任務檢查點無效。")
        root = (uploads / "file_agent" / user_key(user_id) / task_id).resolve()
        try:
            value = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
            if value.get("version") != 1 or value.get("user_key") != user_key(user_id) or value.get("conversation_id") != conversation_id or value.get("task_id") != task_id:
                raise ValueError()
            runtime = cls.__new__(cls)
            runtime.user_id, runtime.conversation_id, runtime.task_id = user_id, conversation_id, task_id
            runtime.root, runtime.inputs, runtime.outputs = root, root / "inputs", root / "outputs"
            runtime.paths = {key: safe_child(root, root / path) for key, path in value["paths"].items()}
            if any(not path.is_file() for path in runtime.paths.values()):
                raise ValueError()
            runtime.records, runtime.generated, runtime.ocr_text = value["records"], value["generated"], value["ocr_text"]
            runtime.read_ids, runtime.asset_ids = set(value["read_ids"]), set(value["asset_ids"])
            runtime.read_ranges = value["read_ranges"]
            runtime.browser_progress = value.get("agent")
            runtime.deadline = time.monotonic() + 600
            runtime.check_quota()
            return runtime
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise FileAgentError("任務檢查點不完整，已保留原件，請重新選擇該文件。") from error

    async def call(self, operation: str, arguments: dict) -> dict:
        """The only model-to-filesystem boundary; no paths accepted from the model."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        from .skills import TOOL_DEFINITIONS
        allowed = {tool["function"]["name"] for tool in TOOL_DEFINITIONS}
        if operation not in allowed:
            raise FileAgentError("不支援此檔案工具。")
        self.check_quota()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise FileAgentError("文件任務已超過 10 分鐘限制，請拆分要求。")
        context = {"task_id": self.task_id, "user_key": user_key(self.user_id),
                   "paths": {key: str(value) for key, value in self.paths.items()},
                   "output_dir": str(self.outputs), "ocr_text": self.ocr_text,
                   "extraction": {key: {"status": record.get("extraction_status"),
                        "truncated": record.get("is_truncated", False)} for key, record in self.records.items()}}
        manifest = self.root / "mcp-context.json"
        manifest.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
        # Allow only OS essentials, never application secrets or provider keys.
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC"}}
        env.update(TEMP=str(self.root), TMP=str(self.root), PYTHONIOENCODING="utf-8",
                   PYTHONUTF8="1", FILE_AGENT_CONTEXT=str(manifest))
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "services.file_agent.mcp_server"], env=env,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        try:
            async with asyncio.timeout(min(TOOL_TIMEOUT, remaining)):
                with (self.root / "mcp-stderr.log").open("a", encoding="utf-8") as errors:
                    async with stdio_client(parameters, errlog=errors) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            result = await session.call_tool(operation, arguments)
                            if result.isError:
                                raise FileAgentError("檔案工具未能完成此操作，請核對檔案及修改位置。")
                            content = [item.text for item in result.content if item.type == "text"]
                            value = json.loads("\n".join(content))
        except TimeoutError as exc:
            raise FileAgentError("檔案工具執行逾時，已停止本次操作。") from exc
        except FileAgentError:
            raise
        except Exception as exc:
            raise FileAgentError("本地 MCP 檔案工具無法執行，請重試或檢查依賴。") from exc
        if not isinstance(value, dict):
            raise FileAgentError("檔案工具傳回無效結果。")
        if value.get("error"):
            raise FileAgentError(str(value["error"])[:400])
        if operation == "read_file" and len(json.dumps(value, ensure_ascii=False)) > 76000:
            raise FileAgentError("本次讀取內容過大，請減小 read_file 的 limit 後分頁讀取；不可跳過其餘資料。")
        if operation == "read_file":
            identifier = str(arguments.get("file_id", ""))
            if value.get("capabilities", {}).get("text_readable") is False:
                self.asset_ids.add(identifier)
            pagination = value.get("pagination", value)
            start = int(pagination.get("start", arguments.get("start", 0)))
            total = pagination.get("total")
            if isinstance(total, int):
                returned = int(pagination.get("returned", 0))
                intervals = self.read_ranges.setdefault(identifier, [])
                intervals.append((start, start + returned))
                end = 0
                for left, right in sorted(intervals):
                    if left > end:
                        break
                    end = max(end, right)
                if end >= total and identifier not in self.asset_ids:
                    self.read_ids.add(identifier)
            elif start == 0 and not pagination.get("has_more") and identifier not in self.asset_ids:
                self.read_ids.add(identifier)
        if operation in {"create_file", "edit_file"}:
            file_id = value.get("file_id")
            path = safe_child(self.outputs, Path(value.get("path", "")))
            if not isinstance(file_id, str) or not path.is_file() or not path.stat().st_size:
                raise FileAgentError("工具沒有產生完整檔案。")
            self.paths[file_id] = path
            self.generated[file_id] = {**value, "parent_id": arguments.get("file_id")
                                       if operation == "edit_file" else None}
        self.check_quota()
        # Absolute local paths are private implementation details, never model context.
        return {key: value for key, value in value.items() if key not in {"path", "output_dir"}}

    def cleanup_temporary(self, retained_ids=()):
        """Remove only this task's disposable copies and uncommitted outputs."""
        shutil.rmtree(safe_child(self.root, self.inputs), ignore_errors=True)
        retained_paths = {self.paths[key].resolve() for key in retained_ids if key in self.paths}
        for path in self.outputs.iterdir():
            if path.is_file() and safe_child(self.root, path) not in retained_paths:
                path.unlink(missing_ok=True)
        (self.root / "mcp-context.json").unlink(missing_ok=True)

    def prune_untracked_outputs(self):
        retained = {path.resolve() for path in self.paths.values()}
        for path in self.outputs.iterdir():
            if path.is_file() and safe_child(self.root, path) not in retained:
                path.unlink()

    @classmethod
    def discard_checkpoint(cls, uploads, owner, task_id):
        if len(task_id) != 32 or any(char not in "0123456789abcdef" for char in task_id):
            return
        owner_root = safe_child(uploads, uploads / "file_agent" / user_key(owner))
        root = safe_child(owner_root, owner_root / task_id)
        if not root.is_dir():
            return
        try:
            state = json.loads((root / "task.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if state.get("state") == "complete" or state.get("user_key") != user_key(owner) or state.get("task_id") != task_id:
            return
        for name in ("inputs", "outputs"):
            folder = safe_child(root, root / name)
            shutil.rmtree(folder, ignore_errors=True)
        for name in ("checkpoint.json", "mcp-context.json"):
            (root / name).unlink(missing_ok=True)



@lru_cache(maxsize=256)
def _preview_lock(directory):
    return threading.Lock()


# One converter fits a 2-core/4-GB host; cached previews never queue behind it.
_preview_converter_slots = threading.BoundedSemaphore(1)


def office_preview(source: Path, directory: Path) -> Path:
    target = directory / "preview.pdf"
    if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return target
    with _preview_lock(str(directory.resolve())):
        if target.is_file() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return target
        with _preview_converter_slots:
            return _render_office_preview(source, directory)


def _short_windows_path(path: Path) -> str:
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
            return buffer.value
    return str(path)


def _render_office_preview(source: Path, directory: Path) -> Path:
    """Render locally with an isolated profile and no external document connections."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "preview.pdf"
    if target.is_file() and target.stat().st_mtime >= source.stat().st_mtime:
        return target
    candidate = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")
    executable = str(candidate) if candidate.is_file() else (shutil.which("soffice.exe") or shutil.which("soffice"))
    if not executable:
        raise FileAgentError("本機缺少 LibreOffice，無法產生 Office 預覽。")
    # Refuse linked/macro-bearing packages before invoking Office's renderer.
    from .skills import FileSkills
    FileSkills({"preview": source}, directory / "validation")._source("preview")
    # LibreOffice's own nested Windows files still encounter MAX_PATH limits.
    profile = directory / ("p" + uuid.uuid4().hex[:8])
    profile.mkdir()
    settings = profile / "user"
    settings.mkdir()
    (settings / "registrymodifications.xcu").write_text('''<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
</oor:items>''', encoding="utf-8")
    staged_source = directory / ("source" + source.suffix.lower())
    staged_pdf = directory / "source.pdf"
    shutil.copyfile(source, staged_source)
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
        "PROGRAMFILES", "PROGRAMFILES(X86)", "ALLUSERSPROFILE", "COMSPEC", "PATHEXT"}}
    env.update(TEMP=_short_windows_path(directory), TMP=_short_windows_path(directory))
    try:
        arguments = [executable, "-env:UserInstallation=" + Path(_short_windows_path(profile)).as_uri(), "--headless",
             "--convert-to", "pdf", "--outdir", _short_windows_path(directory), _short_windows_path(staged_source)]
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env)
        job = None
        try:
            # The isolated parser worker already bounds this descendant.
            process.communicate(timeout=45)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        if process.returncode != 0 or not staged_pdf.is_file() or not staged_pdf.stat().st_size:
            raise FileAgentError("LibreOffice 無法預覽此檔案，仍可下載原始檔案。")
        if staged_pdf.stat().st_size > MAX_INPUT_BYTES:
            raise FileAgentError("Office 預覽超過 50 MB 限制，請下載原檔開啟。")
        import fitz
        with fitz.open(staged_pdf) as pdf:
            validate_pdf(pdf)
            if not pdf.page_count:
                raise FileAgentError("Office 預覽沒有有效頁面。")
        staged_pdf.replace(target)
        return target
    except subprocess.TimeoutExpired as exc:
        raise FileAgentError("Office 預覽轉換逾時，請下載後開啟。") from exc
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        staged_source.unlink(missing_ok=True)
        staged_pdf.unlink(missing_ok=True)
