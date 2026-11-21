"""Add typed broker callbacks to the exact Tencent 2.4.10 auth implementation.

No login is started here. This is an explicit, version-checked local adaptation;
there is no terminal-output scraping and no PIN or token logging.
"""
from pathlib import Path
import hashlib
import json
import os

STATE = Path.home() / ".openclaw-vera"
ROOT = Path(__file__).resolve().parents[1]
MARKER = "// HOUKONG_TYPED_AUTH_V1"


def find_package() -> Path:
    found = list((STATE / "npm/projects").glob("*/node_modules/@tencent-weixin/openclaw-weixin/package.json"))
    matches = [p.parent for p in found if json.loads(p.read_text(encoding="utf-8"))["version"] == "2.4.10"]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one installed Tencent 2.4.10 package.")
    return matches[0]


def replace_once(source: str, before: str, after: str) -> str:
    if source.count(before) != 1:
        raise RuntimeError("Tencent auth patch anchor changed; refusing partial patch.")
    return source.replace(before, after, 1)


def patch(root: Path) -> None:
    try:
        from scripts.setup_openclaw_wechat_delivery_patch import patch_delivery
    except ModuleNotFoundError:
        from setup_openclaw_wechat_delivery_patch import patch_delivery
    patch_delivery(root)
    patch_inbound_guard(root)
    patch_lifecycle(root)
    try:
        from scripts.setup_openclaw_wechat_intake_patch import patch_intake
    except ModuleNotFoundError:
        from setup_openclaw_wechat_intake_patch import patch_intake
    patch_intake(root)
    # Intake touches the same pinned monitor and delivery modules. Refresh their
    # reviewed receipts after all idempotent adaptations have settled.
    patch_delivery(root)
    patch_lifecycle(root)
    target = root / "dist/src/auth/login-qr.js"
    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        target.write_bytes(source.encode("utf-8"))
        receipt_path = root / "houkong-auth-patch.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["after_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return
    original_hash = hashlib.sha256(source.encode()).hexdigest()
    source = replace_once(source, "const code = await readVerifyCodeFromStdin(verifyPrompt);",
                          "const code = opts.hkIo?.readPin ? await opts.hkIo.readPin({ retry: Boolean(activeLogin.pendingVerifyCode), signal: opts.hkIo.signal }) : await readVerifyCodeFromStdin(verifyPrompt);")
    source = replace_once(source, 'case "need_verifycode": {', 'case "confirmed_but_need_verifycode":\n                case "need_verifycode": {')
    source = replace_once(source, "while (Date.now() < deadline) {", "while (Date.now() < deadline) {\n        opts.hkIo?.signal?.throwIfAborted();")
    source = replace_once(source, "activeLogin.status = statusResponse.status;",
                          "opts.hkIo?.signal?.throwIfAborted();\n            activeLogin.status = statusResponse.status;\n            opts.hkIo?.onStatus?.({ state: statusResponse.status, qr_url: activeLogin.qrcodeUrl });")
    source = replace_once(source, "let activeLogin = activeLogins.get(opts.sessionKey);", "let activeLogin = activeLogins.get(opts.sessionKey);\n    if (activeLogin) activeLogin.hkIo = opts.hkIo;")
    source = replace_once(source, "await displayQRCode(qrResponse.qrcode_img_content);",
                          "if (activeLogin.hkIo?.onQr) activeLogin.hkIo.onQr(qrResponse.qrcode_img_content);\n        else await displayQRCode(qrResponse.qrcode_img_content);")
    source += "\n" + MARKER + "\nexport function cancelHouKongLogin(sessionKey) { activeLogins.delete(sessionKey); }\n"
    # Remove credential-bearing debug facts even when verbose diagnostics are enabled.
    import re
    source = re.sub(r'logger\.(?:info|debug)\(`[^`]*\$\{qrResponse\.qrcode_img_content\}[^`]*`\);', 'logger.info("QR received (link withheld)");', source)
    source = re.sub(r'logger\.debug\(`pollQRStatus: body=\$\{rawText\.substring\(0, 200\)\}`\);', 'logger.debug("QR status response received");', source)
    backup = target.with_suffix(".js.houkong-original")
    if not backup.exists():
        backup.write_bytes(target.read_bytes())
    temporary = target.with_suffix(".js.tmp")
    temporary.write_bytes(source.encode("utf-8"))
    os.replace(temporary, target)
    receipt = {"package": "@tencent-weixin/openclaw-weixin", "version": "2.4.10", "patch": "houkong-typed-auth-v1", "before_sha256": original_hash, "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    (root / "houkong-auth-patch.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def patch_inbound_guard(root: Path) -> None:
    target = root / "dist/src/messaging/authorization.js"
    source = target.read_text(encoding="utf-8")
    marker = "// HOUKONG_INBOUND_GUARD_V1"
    original_hash = hashlib.sha256(source.encode()).hexdigest()
    guard_source = (ROOT / "integrations/openclaw-wechat/inbound-guard.js").read_bytes()
    (target.parent / "houkong-inbound-guard.js").write_bytes(guard_source)
    if marker not in source:
        backup = target.with_suffix(".js.houkong-original")
        if not backup.exists():
            backup.write_bytes(target.read_bytes())
        source = 'import { authorizeHouKongInbound } from "./houkong-inbound-guard.js";\n' + source
        source = replace_once(source, "export async function resolveWeixinInboundAuthorization(params) {",
            "export async function resolveWeixinInboundAuthorization(params) {\n    " + marker +
            "\n    if (!(await authorizeHouKongInbound(params))) return { senderAllowed: false, commandAuthorized: false }; ")
        temporary = target.with_suffix(".js.tmp")
        temporary.write_bytes(source.encode("utf-8"))
        os.replace(temporary, target)
    else:
        target.write_bytes(source.encode("utf-8"))
    receipt = {"package": "@tencent-weixin/openclaw-weixin", "version": "2.4.10", "patch": "houkong-inbound-guard-v1", "before_sha256": original_hash,
               "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "guard_sha256": hashlib.sha256(guard_source).hexdigest()}
    (root / "houkong-inbound-guard.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def patch_lifecycle(root: Path) -> None:
    """Expose bounded account lifecycle helpers without privileged Gateway RPC."""
    hashes = {}
    specifications = {
        "channel.js": "// HOUKONG_CHANNEL_HEALTH_SHARED_V2",
        "monitor/monitor.js": "// HOUKONG_CHANNEL_POLL_HEALTH_V1",
        "auth/accounts.js": "// HOUKONG_ACCOUNT_ENABLE_V1",
        "api/session-guard.js": "// HOUKONG_SESSION_RESUME_V1",
    }
    shared_source = (ROOT / "integrations/openclaw-wechat/channel-health.js").read_bytes()
    shared_target = root / "dist/src/houkong-channel-health.js"
    shared_target.write_bytes(shared_source)
    for relative, marker in specifications.items():
        target = root / "dist/src" / relative
        source = target.read_text(encoding="utf-8")
        if marker not in source:
            previous = target.with_suffix(".js.houkong-original")
            if not previous.exists():
                previous.write_bytes(target.read_bytes())
            if relative == "channel.js":
                # Upgrade only our wrapper and exports. Restoring the entire
                # original module would erase the separately reviewed send guard.
                old_start = "            const originalSetStatus = ctx.setStatus;"
                old_end = "            const account = ctx.account;"
                if "// HOUKONG_CHANNEL_HEALTH_V1" in source:
                    start = source.index(old_start)
                    end = source.index(old_end, start)
                    anchor = source[start:end]
                    source = source.replace("\n// HOUKONG_CHANNEL_HEALTH_V1\nconst houkongAccountStatuses = new Map();\nexport function getHouKongChannelStatuses() { return [...houkongAccountStatuses.values()].map(value=>({...value})); }\n", "\n")
                    source = source.replace("\n// HOUKONG_CHANNEL_HEALTH_RESET_V1\nexport function resetHouKongChannelStatus(accountId) { houkongAccountStatuses.set(accountId,{accountId,running:false,loggedOut:false,lastError:null}); }\n", "\n")
                else:
                    anchor = "            const account = ctx.account;\n            const aLog = logger.withAccount(account.accountId);\n            aLog.debug(`about to call monitorWeixinProvider`);"
                replacement = """            const originalSetStatus = ctx.setStatus;
            const accountId = ctx.account.accountId;
            const houkongGeneration = houkongChannelHealth.begin(accountId);
            const recordStatus = value => {
                const event = value?.houkongHealthEvent;
                const updated = houkongChannelHealth.update(accountId,houkongGeneration,{
                    running:value?.running,lastStartAt:value?.lastStartAt,lastEventAt:value?.lastEventAt,
                    pollOk:event==='poll_ok',networkError:event==='network_error',apiError:event==='api_error',expired:event==='auth_expired',
                });
                if (!updated) return; // A previous generation must not stop a replacement worker.
                const {houkongHealthEvent,...safeStatus} = value ?? {};
                originalSetStatus?.(safeStatus);
            };
            ctx = {...ctx,setStatus:recordStatus};
            ctx.abortSignal?.addEventListener('abort',()=>recordStatus({running:false}),{once:true});
"""
                if "// HOUKONG_CHANNEL_HEALTH_V1" not in target.read_text(encoding="utf-8"):
                    replacement += "            const account = ctx.account;\n            const aLog = logger.withAccount(account.accountId);\n            aLog.debug(`about to call monitorWeixinProvider`);"
                source = replace_once(source, anchor, replacement)
                source = 'import { createChannelHealth } from "./houkong-channel-health.js";\n' + source
                source += "\n" + marker + "\nconst houkongChannelHealth = createChannelHealth();\nexport function getHouKongChannelStatuses() { return houkongChannelHealth.snapshots(); }\nexport function resetHouKongChannelStatus(accountId) { return houkongChannelHealth.reset(accountId); }\n"
            elif relative == "monitor/monitor.js":
                source = replace_once(source, "    while (!abortSignal?.aborted) {", "    let houkongPolling = false;\n    while (!abortSignal?.aborted) {")
                source = replace_once(source, "            const resp = await getUpdates({", "            houkongPolling = true;\n            const resp = await getUpdates({")
                source = replace_once(source, "            aLog.debug(`getUpdates response:", "            houkongPolling = false;\n            aLog.debug(`getUpdates response:")
                source = replace_once(source, "                    pauseSession(accountId);", "                    pauseSession(accountId);\n                    setStatus?.({accountId,running:true,connected:false,loggedOut:true,lastError:'auth_expired',houkongHealthEvent:'auth_expired'});")
                source = replace_once(source, "                consecutiveFailures += 1;", "                setStatus?.({accountId,running:true,lastError:'api_error',houkongHealthEvent:'api_error'});\n                consecutiveFailures += 1;")
                source = replace_once(source, "            setStatus?.({ accountId, lastEventAt: Date.now() });", "            setStatus?.({accountId,running:true,connected:true,loggedOut:false,lastError:null,lastEventAt:Date.now(),houkongHealthEvent:'poll_ok'});")
                source = replace_once(source, "            const classified = classifyFetchError(err);", "            if (houkongPolling) setStatus?.({accountId,running:true,lastError:'network_error',houkongHealthEvent:'network_error'});\n            houkongPolling = false;\n            const classified = classifyFetchError(err);")
                source += "\n" + marker + "\n"
            elif relative == "auth/accounts.js":
                source += "\n" + marker + """
export async function setHouKongAccountEnabled(accountId,enabled) {
    const id = normalizeAccountId(accountId);
    if (!id) throw new Error('Account identity is required.');
    await mutateConfigFile({afterWrite:{mode:'auto'},mutate(draft){
        draft.channels ??= {};
        const channel = draft.channels['openclaw-weixin'] ??= {};
        channel.accounts ??= {};
        channel.accounts[id] = {...channel.accounts[id],enabled:enabled===true};
        channel.channelConfigUpdatedAt = new Date().toISOString();
    }});
}
"""
            else:
                source += "\n" + marker + "\nexport function clearHouKongSessionPause(accountId) { pauseUntilMap.delete(accountId); }\n"
            temporary = target.with_suffix(".js.tmp")
            temporary.write_bytes(source.encode("utf-8"))
            os.replace(temporary, target)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    hashes["houkong-channel-health.js"] = hashlib.sha256(shared_target.read_bytes()).hexdigest()
    (root / "houkong-lifecycle-patch.json").write_text(json.dumps({"version":"2.4.10","patch":"houkong-lifecycle-v1","files":hashes}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    root = find_package()
    patch(root)
    print("Tencent 2.4.10 typed PIN/status broker adaptation is installed; no login started.")
