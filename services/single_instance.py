"""One backend writer per data directory, with an OS-owned lifetime lock."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


class BackendAlreadyRunning(RuntimeError):
    """The same data directory is already owned by another backend process."""


def project_instance_id(data_dir):
    canonical = os.path.normcase(os.path.realpath(os.fspath(data_dir)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


class BackendInstanceLock:
    def __init__(self, data_dir):
        self.path = Path(data_dir).resolve() / "storage" / "backend.instance.lock"
        self._file = None

    def acquire(self):
        if self._file is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise BackendAlreadyRunning("此資料目錄已有小智後端正在運行，已停止重複啟動。") from error
        self._file = handle
        return self

    def release(self):
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        # Keep the inode: unlinking a lock file permits two independent locks.

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


def local_backend_is_running(data_dir, *, service, version, port=8000, timeout=1.0):
    """Read loopback health before a reloader can bind a second wildcard socket."""
    try:
        request = Request(f"http://127.0.0.1:{int(port)}/api/health", headers={"Accept": "application/json"})
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                return False
            raw = response.read(16385)
        if len(raw) > 16384:
            return False
        health = json.loads(raw)
        if not isinstance(health, dict) or health.get("status") != "healthy" or health.get("service") != service:
            return False
        marker = health.get("instance", {})
        if isinstance(marker, dict) and marker.get("project_id"):
            return marker["project_id"] == project_instance_id(data_dir)
        # Compatibility with the already-running pre-lock release.
        return health.get("version") == version
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError):
        return False
