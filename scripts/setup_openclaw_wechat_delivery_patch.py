"""Give the pinned Tencent adapter a single, persisted-outbox sending path."""
from pathlib import Path
import hashlib
import json
import os

ROOT = Path(__file__).resolve().parents[1]
MARKER = "// HOUKONG_OUTBOX_ONLY_V1"


def patch_delivery(root: Path):
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package["version"] != "2.4.10":
        raise RuntimeError("Delivery adaptation requires Tencent 2.4.10.")
    destination = root / "dist/src/messaging/houkong-outbound-gate.js"
    destination.write_bytes((ROOT / "integrations/openclaw-wechat/outbound-gate.js").read_bytes())
    files = {"messaging/houkong-outbound-gate.js": hashlib.sha256(destination.read_bytes()).hexdigest()}
    specifications = {
        "api/api.js": (
            'import { authorizeHouKongSend } from "../messaging/houkong-outbound-gate.js";\n',
            "export async function sendMessage(params) {",
            "export async function sendMessage(params) {\n    " + MARKER +
            "\n    if (!(await authorizeHouKongSend(params))) return { ret: 0, houkong_suppressed: true };"),
        "messaging/process-message.js": (
            'import { isHouKongManagedProfile } from "./houkong-outbound-gate.js";\n',
            "        deliver: async (payload) => {",
            "        deliver: async (payload) => {\n            " + MARKER +
            "\n            if (isHouKongManagedProfile()) return;"),
    }
    for relative, (header, anchor, replacement) in specifications.items():
        target = root / "dist/src" / relative
        source = target.read_text(encoding="utf-8")
        if MARKER not in source:
            if source.count(anchor) != 1:
                raise RuntimeError("Pinned delivery anchor changed; refusing partial adaptation.")
            backup = target.with_suffix(".js.houkong-original")
            if not backup.exists():
                backup.write_bytes(target.read_bytes())
            source = header + source.replace(anchor, replacement, 1)
        temporary = target.with_suffix(".js.tmp")
        temporary.write_bytes(source.encode("utf-8"))
        os.replace(temporary, target)
        files[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    (root / "houkong-delivery-patch.json").write_text(
        json.dumps({"version": "2.4.10", "patch": "houkong-outbox-only-v1", "files": files}, indent=2) + "\n",
        encoding="utf-8")


if __name__ == "__main__":
    state = Path.home() / ".openclaw-vera"
    roots = list((state / "npm/projects").glob("*/node_modules/@tencent-weixin/openclaw-weixin"))
    if len(roots) != 1:
        raise RuntimeError("Expected one pinned Tencent installation.")
    patch_delivery(roots[0])
    print("Pinned outbox-only delivery adaptation installed; no messages sent.")
