import { createHash, timingSafeEqual } from 'node:crypto';
import { readFile, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { WECHAT_PROVIDER, WECHAT_MODEL, WECHAT_MODEL_REF } from './model.js';

export const CHANNEL = 'openclaw-weixin';
export const BUSINESS_OPERATIONS = Object.freeze(['inspect_file', 'read_file', 'create_file', 'edit_file', 'validate_file', 'search_files', 'retrieve', 'list_tasks']);
export const MAX_ATTACHMENTS_BYTES = 50 * 1024 * 1024;
export const textOf = (message) => typeof message?.content === 'string' ? message.content : (message?.content ?? []).filter(x => x?.type === 'text').map(x => x.text ?? '').join('\n');
export const hash = (text) => createHash('sha256').update(String(text)).digest('hex');
const stableJson=value=>JSON.stringify(value,(_key,item)=>item&&typeof item==='object'&&!Array.isArray(item)?Object.fromEntries(Object.entries(item).sort(([a],[b])=>a.localeCompare(b))):item);

export function tokenMatches(expected, candidate) {
  if (!expected || typeof candidate !== 'string') return false;
  const a = Buffer.from(expected), b = Buffer.from(candidate);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function inside(root, candidate) {
  const rel = path.relative(root, candidate);
  return rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel));
}

export async function stagedAttachments(media, allowedRoots) {
  const roots = await Promise.all(allowedRoots.map(x => realpath(x).catch(() => null)));
  let total = 0;
  const result = [];
  for (const item of media ?? []) {
    if (!item?.path || item.url && !item.path) throw new Error('Attachment has not been staged locally.');
    const resolved = await realpath(item.path);
    if (!roots.some(root => root && inside(root, resolved))) throw new Error('Attachment is outside managed media storage.');
    const info = await stat(resolved);
    if (!info.isFile() || info.size < 0 || (total += info.size) > MAX_ATTACHMENTS_BYTES) throw new Error('Attachment size limit exceeded.');
    const bytes = await readFile(resolved);
    if (bytes.length !== info.size) throw new Error('Attachment changed while being staged.');
    const originalName=item.original_filename??item.name;
    const name=typeof originalName==='string'&&originalName.length<=180&&originalName===path.basename(originalName)&&!/[\\/:\x00-\x1f]/.test(originalName)
      ? originalName : path.basename(resolved);
    result.push({name, mime: item.contentType || 'application/octet-stream', content_b64: bytes.toString('base64')});
  }
  return result;
}

export class BackendClient {
  constructor({url, token, fetchImpl = fetch}) {
    this.url = url.replace(/\/$/, '');
    this.token = token;
    this.fetch = fetchImpl;
    const parsed = new URL(url);
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(parsed.hostname) || parsed.protocol !== 'http:') throw new Error('Business backend must use loopback HTTP.');
  }
  async request(route, body, capability, method = body === undefined ? 'GET' : 'POST', options = {}) {
    if (!this.token) throw new Error('Missing private service credential.');
    const headers = {'X-Houkong-Wechat-Token': this.token, 'Content-Type': 'application/json'};
    if (capability) headers['X-Wechat-Turn-Capability'] = capability;
    // OCR/context and bounded file tools can legitimately outlive a short HTTP
    // request. Caller cancellation still revokes this attempt immediately.
    const budget=options.timeoutMs??(/\/(context|resume)$/.test(route)?240000:/\/tools\//.test(route)?180000:route.endsWith('/commit')?60000:35000);
    const signals=[AbortSignal.timeout(budget),options.signal].filter(Boolean);
    const response = await this.fetch(this.url + route, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: signals.length===1?signals[0]:AbortSignal.any(signals)});
    if (!response.ok) throw new Error(`Business request refused (${response.status}).`);
    const value = await response.json();
    if (value.code !== undefined && value.code !== 200) throw new Error('Business request was not accepted.');
    return value.data ?? value;
  }
}

export class TurnRegistry {
  constructor(backend) { this.backend = backend; this.sessions = new Map(); this.runs = new Map(); this.receipts = new Map();this.admissions=new Map(); }
  get(sessionKey) { return this.sessions.get(sessionKey); }
  async admit({sessionKey, eventId, accountId, senderId, text, attachments = [], resumeTurnId}) {
    if (!sessionKey || !eventId || !accountId || !senderId) throw new Error('Verified channel identity is incomplete.');
    const existing = this.sessions.get(sessionKey);
    if (existing && existing.eventId !== eventId && !existing.completed) {
      if(!existing.settlement)throw new Error('This conversation already has an active turn.');
      // Native dispatch can return before its best-effort completion hooks have
      // saved the business answer. Keep the next inbox item rather than drop it.
      const deadline=Date.now()+180000;
      while(!existing.completed&&Date.now()<deadline){
        try{await this.heartbeat(sessionKey,existing);}catch{this.retire(existing.id,existing);}
        if(this.get(sessionKey)!==existing)throw new Error('The previous turn was replaced while waiting.');
        if(!existing.completed)await Promise.race([existing.settlement,new Promise(resolve=>setTimeout(resolve,500))]);
      }
      if(!existing.completed)throw new Error('The previous turn has not settled.');
    }
    if (existing?.eventId === eventId && !existing.completed) return null;
    const response = resumeTurnId
      ? await this.backend.request(`/internal/wechat/turns/${encodeURIComponent(resumeTurnId)}/resume`,{account_id:accountId,sender_id:senderId})
      : await this.backend.request('/internal/wechat/turns', {event_id: eventId, account_id: accountId, sender_id: senderId, text, attachments});
    if(response.id)this.admissions.set(sessionKey,{id:response.id,eventId,accountId,attempt:response.attempt??1});
    if (response.duplicate || response.created === false || !['queued', 'running'].includes(response.state)) return null;
    if (!response.id || !response.capability) throw new Error('No admitted turn capability was returned.');
    const turn = {...response, eventId, sessionKey, accountId, senderId, completed: false,controller:new AbortController()};
    turn.settlement=new Promise(resolve=>{turn.resolveSettlement=resolve;});
    this.sessions.set(sessionKey, turn);
    return turn;
  }
  async context(sessionKey) {
    const turn = this.get(sessionKey);
    if (!turn || turn.completed) throw new Error('No active business turn.');
    if(turn.contextReady)return turn.context;
    const context = await this.backend.request(`/internal/wechat/turns/${encodeURIComponent(turn.id)}/context`, {}, turn.capability,'POST',{signal:turn.controller?.signal});
    if(this.get(sessionKey)!==turn||turn.completed)throw new Error('Context authority was retired.');
    turn.context = context;
    turn.contextReady = true;
    if(context.completed){turn.completed=true;turn.resolveSettlement?.();}
    return context;
  }
  async heartbeat(sessionKey, expectedTurn) {
    const turn = this.get(sessionKey);
    if(expectedTurn && turn!==expectedTurn)throw new Error('The heartbeat turn was replaced.');
    if (!turn || turn.completed) throw new Error('Business authority expired.');
    return this.backend.request(`/internal/wechat/turns/${encodeURIComponent(turn.id)}/heartbeat`, {}, turn.capability);
  }
  async tool(sessionKey, operation, arguments_, expectedTurn, {callId,signal}={}) {
    if (!BUSINESS_OPERATIONS.includes(operation)) throw new Error('Unsupported business operation.');
    const turn = this.get(sessionKey);
    if(expectedTurn && turn!==expectedTurn)throw new Error('The originating turn was replaced.');
    if (!turn || turn.completed || !turn.contextReady) throw new Error('No active business authority.');
    // Identity, routes and capabilities never come from model arguments.
    for (const name of ['owner', 'owner_id', 'user_id', 'account_id', 'sender_id', 'recipient', 'to', 'path', 'url', 'capability']) {
      if (Object.hasOwn(arguments_ ?? {}, name)) throw new Error('Authority and paths are not tool arguments.');
    }
    const signals=[signal,turn.controller?.signal].filter(Boolean);
    const result=await this.backend.request(`/internal/wechat/turns/${encodeURIComponent(turn.id)}/tools/${operation}`, {arguments: arguments_ ?? {},...(callId?{call_id:callId}:{})}, turn.capability,'POST',{signal:signals.length?AbortSignal.any(signals):undefined});
    if(this.get(sessionKey)!==turn||turn.completed||turn.authorityLost)throw new Error('Tool authority was retired.');
    return result;
  }
  async commit(sessionKey, advancementKey, text, expectedTurn) {
    if(!expectedTurn)throw new Error('The originating turn is required for commit.');
    const receiptKey = sessionKey + '\0' + advancementKey;
    if (this.receipts.has(receiptKey)) return {status: 'duplicate'};
    const turn = this.get(sessionKey);
    if(expectedTurn && turn!==expectedTurn)throw new Error('The accepted turn was replaced.');
    if (!turn) throw new Error('Missing admitted turn for commit.');
    if(turn.completed){if(turn.commitText===text)return {status:'duplicate'};throw new Error('The originating turn is closed.');}
    const result = await this.backend.request(`/internal/wechat/turns/${encodeURIComponent(turn.id)}/commit`, {text, advancement_key: advancementKey, artifacts: []}, turn.capability,'POST',{signal:turn.controller?.signal});
    turn.completed = true;
    turn.commitText=text;
    turn.resolveSettlement?.();
    this.receipts.set(receiptKey, true);
    if (this.receipts.size > 500) this.receipts.delete(this.receipts.keys().next().value);
    return {status: result.duplicate ? 'duplicate' : 'committed'};
  }
  async abort(sessionKey) {
    const turn = this.get(sessionKey);
    if (turn && !turn.completed) {
      try { await this.backend.request(`/internal/wechat/turns/${encodeURIComponent(turn.id)}/abort`, {}, turn.capability); }
      finally { turn.completed = true;turn.authorityLost=true;turn.controller?.abort();turn.resolveSettlement?.(); }
    }
  }
  retire(turnId, expectedTurn) {
    for(const turn of this.sessions.values())if(turn.id===turnId&&(!expectedTurn||turn===expectedTurn)&&!turn.completed){turn.completed=true;turn.authorityLost=true;turn.controller?.abort();turn.resolveSettlement?.();}
  }
  currentRun(runId, turnId) {
    const turn=this.runs.get(runId);
    return turn?.id===turnId&&turn.nativeRunId===runId&&this.get(turn.sessionKey)===turn&&!turn.completed?turn:null;
  }
  retireRun(runId, turnId) {
    const turn=this.currentRun(runId,turnId);
    if(turn)this.retire(turnId,turn);
  }
  noteEndedRun(runId, turnId) {
    const turn=this.currentRun(runId,turnId);
    if(turn)turn.endedAt=Date.now();
  }
}

export function sharedTurnRegistry(backend) {
  if(!backend.token)return new TurnRegistry(backend);
  // Code-mode tool discovery can register this plugin again in the same host.
  // Keep admission/tool leases scoped to this private profile across registers.
  const symbol=Symbol.for('houkong-wechat.turn-registry.v2');
  const registries=globalThis[symbol]??=new Map();
  const key=backend.url+'\0'+hash(backend.token);
  if(!registries.has(key))registries.set(key,new TurnRegistry(backend));
  return registries.get(key);
}

export async function beginAdmittedRun(registry,ctx) {
  const turn=registry.get(ctx.sessionKey);
  if(!turn||!turn.contextReady||turn.completed||turn.authorityLost||turn.engineFailed||!turn.engineAssembled||!ctx.runId||turn.finalizing)throw new Error('Business run authority is unavailable.');
  await registry.heartbeat(ctx.sessionKey,turn);
  if(registry.get(ctx.sessionKey)!==turn||turn.completed||turn.authorityLost)throw new Error('The run authority was replaced.');
  turn.nativeRunId=ctx.runId;turn.nativeGeneration=(turn.nativeGeneration??0)+1;
  turn.freshOutput=null;turn.endEvidence=null;turn.endedAt=null;
  registry.runs.set(ctx.runId,turn);return turn;
}

export function modelHistory(messages, tokenBudget = 48000) {
  const kept = [];
  let size = 0;
  for (const item of [...(messages ?? [])].reverse()) {
    if (!['user', 'assistant', 'toolResult'].includes(item?.role)) continue;
    const content = typeof item.content === 'string' && ['assistant','toolResult'].includes(item.role)
      ? [{type:'text',text:item.content}] : item.content;
    const normalized = {...item, content, timestamp: item.timestamp ?? Date.now()};
    if (item.role === 'assistant') {
      normalized.api ??= 'openai-completions'; normalized.provider ??= WECHAT_PROVIDER; normalized.model ??= WECHAT_MODEL;
      normalized.usage ??= {input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
      normalized.stopReason ??= 'stop';
    }
    const estimate = Math.ceil(JSON.stringify(normalized).length / 2);
    if (kept.length && size + estimate > tokenBudget) break;
    kept.unshift(normalized); size += estimate;
  }
  return {messages:kept, estimatedTokens:size};
}

export function createContextEngine(registry, verificationAllowed) {
  const leases=new Map();
  return {
    info: {id:'houkong-wechat',name:'HouKong canonical conversations',version:'0.1.0',ownsCompaction:true,acceptedHostParams:['sessionKey','prompt'],transcriptSemantics:{currentTurnFence:'before-current-turn-entry-v1',turnAdvancementIdempotency:'atomic-idempotent-v1'},hostRequirements:{'agent-run':{requiredCapabilities:['assemble-before-prompt'],unsupportedMessage:'Use the native OpenClaw runtime.'}}},
    async ingest({sessionKey}) { if (!verificationAllowed() && !registry.get(sessionKey)) throw new Error('Unadmitted context.'); return {ingested:false}; },
    async assemble({sessionKey,messages,tokenBudget}) {
      if (verificationAllowed()) {
        const result=modelHistory(messages, tokenBudget);
        const fixture=process.env.HOUKONG_WECHAT_VERIFY_IMAGE;
        if(fixture){
          const root=await realpath(path.join(process.env.HOUKONG_WECHAT_STATE_DIR,'verification'));
          const resolved=await realpath(fixture);
          if(!inside(root,resolved)||path.basename(resolved)!=='synthetic.png')throw new Error('Invalid synthetic image fixture.');
          const bytes=await readFile(resolved);
          if(bytes.length>1024*1024||!bytes.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10])))throw new Error('Invalid synthetic PNG fixture.');
          result.messages.push({role:'user',timestamp:Date.now(),content:[{type:'text',text:'Synthetic installation check image.'},{type:'image',data:bytes.toString('base64'),mimeType:'image/png'}]});
          result.estimatedTokens+=1000;
        }
        return result;
      }
      const turn=registry.get(sessionKey);
      try {
        const captured=leases.get(sessionKey);
        if(captured && captured!==turn)throw new Error('Context turn was replaced.');
        leases.set(sessionKey,turn);
        const context = await registry.context(sessionKey);
        const result = modelHistory(context.messages, Math.min(tokenBudget ?? 48000, 48000));
        turn.engineAssembled=true;turn.engineFailed=false;
        // before_prompt_build supplies the canonical system context once.
        return result;
      } catch(error) { if(turn)turn.engineFailed=true;throw error; }
    },
    async commitTurn({sessionKey,sessionId,advancementKey,admission,terminal,messages}) {
      if (verificationAllowed()) return {status:'committed'};
      // The host drains durable transcript advancements BEFORE assembling a new
      // run. They can belong to yesterday's answer and have no current lease.
      // Acknowledge only their own immutable ledger; never choose a business
      // recipient, turn, or answer from today's session registry here.
      if(!advancementKey||!admission?.entryId||!terminal?.entryId||!Array.isArray(messages))throw new Error('A native transcript boundary is required.');
      const result=await registry.backend.request('/internal/wechat/native-commits',{
        advancement_key:advancementKey,
        session_hash:hash(sessionKey??sessionId??admission.sessionKey),
        admission_hash:hash(stableJson(admission)),terminal_hash:hash(stableJson(terminal)),payload_hash:hash(stableJson(messages)),
      });
      return {status:result.duplicate?'duplicate':'committed'};
    },
    async compact() { return {ok:true,compacted:false,reason:'Canonical history is bounded by the business context window.'}; },
  };
}

export function captureAdmittedOutput(registry,event,ctx={}) {
  const runId=event.runId??ctx.runId,turn=registry.runs.get(runId);
  if(!runId||!turn||registry.get(turn.sessionKey)!==turn||turn.completed||turn.authorityLost)return false;
  if(ctx.sessionKey&&ctx.sessionKey!==turn.sessionKey)return false;
  if(event.provider!==WECHAT_PROVIDER||![WECHAT_MODEL,WECHAT_MODEL_REF].includes(event.model))return false;
  // lastAssistant may be the old history tail. Only assistantTexts is produced
  // by this run's fresh stream subscription in the pinned native runtime.
  const texts=(event.assistantTexts??[]).filter(value=>typeof value==='string'&&value.trim()).map(value=>value.trim());
  if(!texts.length)return false;
  turn.freshOutput={runId,texts};
  return true;
}

export async function finalizeReadyRun(registry,runId) {
  const admitted=registry.runs.get(runId);
  if(!admitted||registry.get(admitted.sessionKey)!==admitted||admitted.authorityLost)return false;
  if(admitted.finalizing)return admitted.finalizing;
  if(admitted.completed)return true;
  if(!admitted.endEvidence||!admitted.freshOutput)return false;
  const {text}=admitted.endEvidence,{texts}=admitted.freshOutput;
  const candidates=[...texts,texts.join(''),texts.join('\n'),texts.join('\n\n')];
  if(!text||!candidates.includes(text)){
    registry.runs.delete(runId);await registry.abort(admitted.sessionKey).catch(()=>{});return false;
  }
  // Set before the first awaited I/O: either hook can arrive first or twice.
  admitted.finalizing=(async()=>{
    try{await registry.commit(admitted.sessionKey,'accepted-run-'+runId,text,admitted);return true;}
    catch(error){await registry.abort(admitted.sessionKey).catch(()=>{});throw error;}
    finally{if(registry.runs.get(runId)===admitted)registry.runs.delete(runId);}
  })();
  return admitted.finalizing;
}

export class SendingPermits {
  constructor() { this.permits = new Map(); }
  key(accountId, to, text) { return hash(JSON.stringify([accountId,to,text])); }
  grant(accountId, to, text) { const key=this.key(accountId,to,text); this.permits.set(key,Date.now()+60000); return () => this.permits.delete(key); }
  allowed(accountId,to,text) { const until=this.permits.get(this.key(accountId,to,text)); return !!until && until>Date.now(); }
}

export async function finalizeAdmittedRun(registry,event,ctx) {
  const runId=event.runId??ctx.runId;
  const admitted=registry.runs.get(runId);
  if(!runId||!admitted||registry.get(ctx.sessionKey)!==admitted)return false;
  if(!event.success){registry.runs.delete(runId);await registry.abort(ctx.sessionKey).catch(()=>{});return true;}
  if(admitted.completed)return true;
  const final=[...(event.messages??[])].reverse().find(message=>message?.role==='assistant');
  const text=textOf(final).trim();
  admitted.endEvidence={runId,text};admitted.endedAt=Date.now();
  return finalizeReadyRun(registry,runId);
}
