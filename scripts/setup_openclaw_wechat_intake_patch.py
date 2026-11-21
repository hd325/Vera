"""Pinned Tencent 2.4.10 intake journal, short collection window and task control.

This never starts login, emits a message, prints an envelope or reads a key.
"""
from pathlib import Path
import hashlib
import json
import os

ROOT = Path(__file__).resolve().parents[1]
MARKER = "// HOUKONG_MANAGED_INTAKE_V1"


def replace_once(source, anchor, replacement):
    if source.count(anchor) != 1:
        raise RuntimeError("Pinned Tencent intake anchor changed; refusing an unreviewed patch.")
    return source.replace(anchor, replacement, 1)


def patch_intake(root: Path):
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    if package["version"] != "2.4.10":
        raise RuntimeError("Managed intake requires Tencent 2.4.10.")
    helper = root / "dist/src/messaging/houkong-intake.js"
    helper.write_bytes((ROOT / "integrations/openclaw-wechat/intake.js").read_bytes())
    hashes = {"messaging/houkong-intake.js": hashlib.sha256(helper.read_bytes()).hexdigest()}
    for relative in ("monitor/monitor.js", "messaging/process-message.js"):
        target = root / "dist/src" / relative
        source = target.read_text(encoding="utf-8")
        if MARKER not in source:
            backup = target.with_suffix(".js.houkong-before-intake")
            if not backup.exists():
                backup.write_bytes(target.read_bytes())
            if relative == "monitor/monitor.js":
                source = 'import { ManagedInboundQueue, postHouKongControl } from "../messaging/houkong-intake.js";\n' + source
                anchor = "    const configManager = new WeixinConfigManager({ baseUrl, token }, log);"
                source = replace_once(source, anchor, anchor + """
    const houkongQueue = new ManagedInboundQueue({
        accountId,
        onError: () => errLog('Incoming task was interrupted; its private journal is retained.'),
        onControl: (action,full,signal) => postHouKongControl({accountId,action,full,signal}),
        processMessage: async (full,signal) => {
            const cachedConfig = await configManager.getForUser(full.from_user_id ?? '',full.context_token);
            const taskSignal = abortSignal ? AbortSignal.any([abortSignal,signal]) : signal;
            taskSignal.throwIfAborted();
            await processOneMessage(full,{
                accountId,config,channelRuntime,baseUrl,cdnBaseUrl,token,
                typingTicket:cachedConfig.typingTicket,log:opts.runtime?.log??(()=>{}),errLog,
                abortSignal:taskSignal,
            });
        },
    });
    abortSignal?.addEventListener('abort',()=>houkongQueue.stop(),{once:true});
""")
                cursor = """            if (resp.get_updates_buf != null && resp.get_updates_buf !== "") {
                saveGetUpdatesBuf(syncFilePath, resp.get_updates_buf);
                getUpdatesBuf = resp.get_updates_buf;
                aLog.debug(`Saved new get_updates_buf (${getUpdatesBuf.length} bytes)`);
            }
"""
                source = replace_once(source, cursor, "")
                start = source.index("            const list = resp.msgs ?? [];")
                end = source.index("\n        }\n        catch (err)", start)
                source = source[:start] + """            const list = resp.msgs ?? [];
            // Persist the entire batch before acknowledging its provider cursor.
            // File generation runs in independent per-peer lanes, never in this poll.
            houkongQueue.enqueueBatch(list);
            if (list.length) {
                const now = Date.now();
                setStatus?.({accountId,lastEventAt:now,lastInboundAt:now});
            }
""" + cursor + source[end:]
                source = replace_once(source, '    aLog.info(`Monitor ended`);', '    houkongQueue.stop();\n    aLog.info(`Monitor ended`);')
            else:
                source = 'import { mediaDescriptors, downloadedMediaRecord, applyInboundMediaContext } from "./houkong-intake.js";\n' + source
                start = source.index("    if (mediaItem) {\n        const label = refMediaItem ? \"ref\" : \"inbound\";")
                end = source.index("    const mediaDownloadMs = Date.now() - mediaDownloadStart;", start)
                source = source[:start] + """    const houkongMediaRecords = [];
    const houkongMediaItems = mediaDescriptors(full);
    if (!houkongMediaItems.length && mediaItem) houkongMediaItems.push(mediaItem);
    for (const incomingItem of houkongMediaItems) {
        deps.abortSignal?.throwIfAborted();
        const downloaded = await downloadMediaFromItem(incomingItem,{
            cdnBaseUrl:deps.cdnBaseUrl,saveMedia:deps.channelRuntime.media.saveMediaBuffer,
            mediaSubdir:getActiveQuoteMediaSubdir(deps.accountId),log:deps.log,errLog:deps.errLog,
            label:refMediaItem?'ref':'inbound',
        });
        houkongMediaRecords.push(downloadedMediaRecord(incomingItem,downloaded));
        if (houkongMediaRecords.length===1) Object.assign(mediaOpts,downloaded);
    }
    if (refMediaItem) mediaOpts.referencedMedia = true;
""" + source[end:]
                anchor = "    const ctx = weixinMessageToMsgContext(full, deps.accountId, mediaOpts);"
                source = replace_once(source, anchor, anchor + """
    applyInboundMediaContext(ctx,houkongMediaRecords);
    if (full.houkong_resume_turn_id) ctx.HouKongResumeTurnId = full.houkong_resume_turn_id;
    ctx.HouKongSourceMessageIds = full.houkong_source_message_ids ?? [];
    deps.abortSignal?.throwIfAborted();
""")
                source = replace_once(source, "                    disableBlockStreaming: true,", "                    disableBlockStreaming: true,\n                    abortSignal: deps.abortSignal,")
            source += "\n" + MARKER + "\n"
            temporary = target.with_suffix(".js.tmp")
            temporary.write_bytes(source.encode("utf-8"))
            os.replace(temporary, target)
        if relative == "monitor/monitor.js" and "// HOUKONG_ADMISSION_ACK_V1" not in source:
            source = replace_once(source, 'import { ManagedInboundQueue, postHouKongControl } from "../messaging/houkong-intake.js";',
                'import { ManagedInboundQueue, postHouKongControl, hasHouKongTurn } from "../messaging/houkong-intake.js";')
            source = replace_once(source, "                abortSignal:taskSignal,\n            });",
                "                abortSignal:taskSignal,\n            });\n            // HOUKONG_ADMISSION_ACK_V1\n            if (!hasHouKongTurn({account_id:accountId,event_id:full.message_id})) throw new Error('Business admission was not acknowledged; keep the incoming message queued.');")
            temporary = target.with_suffix(".js.tmp")
            temporary.write_bytes(source.encode("utf-8"))
            os.replace(temporary, target)
        if relative == "messaging/process-message.js" and "// HOUKONG_MESSAGE_TEXT_V1" not in source:
            source = replace_once(source, "    ctx.HouKongSourceMessageIds = full.houkong_source_message_ids ?? [];",
                "    ctx.HouKongSourceMessageIds = full.houkong_source_message_ids ?? [];\n    // HOUKONG_MESSAGE_TEXT_V1\n    ctx.HouKongMessageText = (full.item_list ?? []).filter(item => item.type === 1).map(item => item.text_item?.text ?? '').filter(Boolean).join('\\n');")
            temporary = target.with_suffix(".js.tmp")
            temporary.write_bytes(source.encode("utf-8"))
            os.replace(temporary, target)
        hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
    (root / "houkong-intake-patch.json").write_text(
        json.dumps({"version": "2.4.10", "patch": "houkong-managed-intake-v1", "files": hashes}, indent=2) + "\n", encoding="utf-8")
    # The reviewed intake additions share two files with earlier adaptations.
    for receipt_name, relative in (("houkong-lifecycle-patch.json", "monitor/monitor.js"),
                                   ("houkong-delivery-patch.json", "messaging/process-message.js")):
        receipt_path = root / receipt_name
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if relative not in receipt.get("files", {}):
            raise RuntimeError("The prior reviewed Tencent adaptation is missing.")
        receipt["files"][relative] = hashes[relative]
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    state = Path.home() / ".openclaw-vera"
    roots = list((state / "npm/projects").glob("*/node_modules/@tencent-weixin/openclaw-weixin"))
    if len(roots) != 1:
        raise RuntimeError("Expected one pinned Tencent installation.")
    patch_intake(roots[0])
    print("Pinned incoming task journal is installed; no messages sent.")
