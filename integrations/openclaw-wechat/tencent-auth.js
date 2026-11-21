import { readFile, readdir } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

export async function loadTencent(stateDir) {
  const roots = [];
  const projects = path.join(stateDir,'npm','projects');
  for (const name of await readdir(projects)) {
    const root = path.join(projects,name,'node_modules','@tencent-weixin','openclaw-weixin');
    const metadata = await readFile(path.join(root,'package.json'),'utf8').then(JSON.parse).catch(() => null);
    if (metadata?.version === '2.4.10') roots.push(root);
  }
  if (roots.length !== 1) throw new Error('Exactly one pinned Tencent package is required.');
  const root = roots[0];
  const receipt = await readFile(path.join(root,'houkong-auth-patch.json'),'utf8').then(JSON.parse);
  if (receipt.patch !== 'houkong-typed-auth-v1') throw new Error('Typed authentication adaptation is missing.');
  const guardReceipt=await readFile(path.join(root,'houkong-inbound-guard.json'),'utf8').then(JSON.parse);
  const lifecycle=await readFile(path.join(root,'houkong-lifecycle-patch.json'),'utf8').then(JSON.parse);
  const delivery=await readFile(path.join(root,'houkong-delivery-patch.json'),'utf8').then(JSON.parse);
  const intakeReceipt=await readFile(path.join(root,'houkong-intake-patch.json'),'utf8').then(JSON.parse);
  const digest=bytes=>createHash('sha256').update(bytes).digest('hex');
  if(lifecycle.patch!=='houkong-lifecycle-v1')throw new Error('Account lifecycle adaptation is unavailable.');
  for(const [file,expected]of Object.entries(lifecycle.files))if(digest(await readFile(path.join(root,'dist','src',file)))!==expected)throw new Error('Account lifecycle adaptation changed.');
  if(delivery.patch!=='houkong-outbox-only-v1')throw new Error('Exclusive outbox delivery adaptation is unavailable.');
  for(const [file,expected]of Object.entries(delivery.files))if(digest(await readFile(path.join(root,'dist','src',file)))!==expected)throw new Error('Exclusive outbox delivery adaptation changed.');
  if(intakeReceipt.patch!=='houkong-managed-intake-v1')throw new Error('Durable channel intake adaptation is unavailable.');
  for(const [file,expected]of Object.entries(intakeReceipt.files))if(digest(await readFile(path.join(root,'dist','src',file)))!==expected)throw new Error('Durable channel intake adaptation changed.');
  if(guardReceipt.patch!=='houkong-inbound-guard-v1'||guardReceipt.after_sha256!==digest(await readFile(path.join(root,'dist','src','messaging','authorization.js')))||guardReceipt.guard_sha256!==digest(await readFile(path.join(root,'dist','src','messaging','houkong-inbound-guard.js')))||receipt.after_sha256!==digest(await readFile(path.join(root,'dist','src','auth','login-qr.js'))))throw new Error('Pinned channel policy adaptation changed; reinstall the reviewed patches.');
  const load = rel => import(pathToFileURL(path.join(root,'dist','src',rel)).href);
  const [auth,accounts,channel,filter,sessionGuard,deliveryGate,intake] = await Promise.all([load('auth/login-qr.js'),load('auth/accounts.js'),load('channel.js'),load('messaging/markdown-filter.js'),load('api/session-guard.js'),load('messaging/houkong-outbound-gate.js'),load('messaging/houkong-intake.js')]);
  if (typeof auth.cancelHouKongLogin !== 'function') throw new Error('Authentication adaptation did not load.');
  return {root,auth,accounts,channel:channel.weixinPlugin,filter,sessionGuard,deliveryGate,intake,getChannelStatuses:channel.getHouKongChannelStatuses,resetChannelStatus:channel.resetHouKongChannelStatus};
}

export class TencentBindingBroker {
  constructor({tencent,config,runtime}) { this.tencent=tencent; this.config=config; this.runtime=runtime; this.requests=new Map(); this.revokedAccounts=new Set(); }
  publicState(item) {
    const states={starting:'pending',waiting:'waiting_scan',verifying:'waiting_confirmation',need_pin:'pin_required',confirmed:'verified'};
    return {request_id:item.requestId,state:states[item.state]??item.state,qr_url:item.qrUrl ?? null,expires_at:Math.floor(item.expiresAt/1000),account_id:item.accountId ?? null,sender_id:item.senderId ?? null,verification_kind:item.state==='need_pin'?'pin':null,identity:item.senderId ? {kind:'verified_channel_sender',id:item.senderId}:null,error:item.error ?? null};
  }
  async start(requestId) {
    if (!/^[a-zA-Z0-9_-]{8,160}$/.test(requestId ?? '')) throw new Error('Invalid binding request.');
    const previous = this.requests.get(requestId);
    if (previous) return this.publicState(previous);
    const active = [...this.requests.values()].filter(x=>!['cancelled','expired','failed','connected'].includes(x.state));
    if (active.length >= 3) throw new Error('Too many active binding requests.');
    const item = {requestId,state:'starting',expiresAt:Date.now()+8*60*1000,abort:new AbortController()};
    this.requests.set(requestId,item);
    const started = await this.tencent.auth.startWeixinLoginWithQr({accountId:'hkbind-'+randomUUID(),apiBaseUrl:this.tencent.accounts.DEFAULT_BASE_URL,verbose:false});
    if (!started.qrcodeUrl || !started.sessionKey) {
      item.state='failed';
      item.error=/fetch failed|ECONN|ETIMEDOUT|ENOTFOUND|network/i.test(String(started.message??''))?'upstream_unreachable':'qr_unavailable';
      return this.publicState(item);
    }
    item.sessionKey=started.sessionKey; item.qrUrl=started.qrcodeUrl; item.state='waiting';
    item.task=this.poll(item).catch(()=>{ if(item.state!=='cancelled'){item.state='failed';item.error='authorization_failed';} });
    return this.publicState(item);
  }
  async poll(item) {
    const result = await this.tencent.auth.waitForWeixinLogin({sessionKey:item.sessionKey,apiBaseUrl:this.tencent.accounts.DEFAULT_BASE_URL,timeoutMs:8*60*1000,verbose:false,hkIo:{
      signal:item.abort.signal,
      onStatus:({state,qr_url})=>{ if(qr_url)item.qrUrl=qr_url; if(!['need_pin','confirmed','connected','cancelled'].includes(item.state)) item.state=state==='scaned'?'verifying':'waiting'; },
      onQr:qr=>{item.qrUrl=qr;item.state='waiting';},
      readPin:({signal})=>new Promise((resolve,reject)=>{
        item.state='need_pin'; item.resolvePin=resolve;
        signal.addEventListener('abort',()=>{item.resolvePin=null;reject(new Error('Binding cancelled.'));},{once:true});
      }),
    }});
    if (item.abort.signal.aborted) return;
    if (result.connected && result.botToken && result.accountId && result.userId) {
      // Tokens remain solely inside this privileged broker until website confirmation.
      item.credentials=result; item.accountId=result.accountId.trim().toLowerCase().replace(/[^a-z0-9_-]/g,'-'); item.senderId=result.userId;
      item.state='confirmed'; item.qrUrl=null;
    } else { item.state=Date.now()>item.expiresAt?'expired':'failed';item.error=result.alreadyConnected?'already_bound_requires_reconnect':'authorization_failed'; }
  }
  get(requestId) {
    const item=this.requests.get(requestId);
    if (!item) throw new Error('Binding request is unavailable.');
    if(Date.now()>item.expiresAt && !['connected','cancelled'].includes(item.state)){this.cancel(requestId);item.state='expired';}
    return this.publicState(item);
  }
  verify(requestId,{pin,choice}={}) {
    const item=this.requests.get(requestId);
    if (!item || item.state!=='need_pin' || !item.resolvePin || choice !== undefined) throw new Error('No supported verification step is pending.');
    if(typeof pin!=='string'||!/^\d{1,12}$/.test(pin)) throw new Error('Enter the digits shown by WeChat.');
    const resolve=item.resolvePin;item.resolvePin=null;item.state='verifying';resolve(pin);
    return this.publicState(item);
  }
  async confirm(requestId,{account_id,sender_id}) {
    const item=this.requests.get(requestId);
    if (!item || item.state!=='confirmed' || !item.credentials || item.accountId!==account_id || item.senderId!==sender_id) throw new Error('Verified binding identity does not match.');
    const result=item.credentials;
    this.tencent.accounts.saveWeixinAccount(item.accountId,{token:result.botToken,baseUrl:result.baseUrl,userId:result.userId});
    this.tencent.accounts.registerWeixinAccountId(item.accountId);
    this.tencent.accounts.clearStaleAccountsForUserId?.(item.accountId,result.userId);
    this.tencent.resetChannelStatus?.(item.accountId);
    this.tencent.sessionGuard?.clearHouKongSessionPause(item.accountId);
    await this.tencent.accounts.setHouKongAccountEnabled(item.accountId,true);
    item.credentials=undefined;item.state='connected';
    this.revokedAccounts.delete(item.accountId);
    await this.tencent.accounts.triggerWeixinChannelReload();
    return this.publicState(item);
  }
  cancel(requestId) {
    const item=this.requests.get(requestId);
    if(!item)return {state:'cancelled'};
    item.abort.abort();if(item.sessionKey)this.tencent.auth.cancelHouKongLogin(item.sessionKey);
    item.credentials=undefined;item.qrUrl=null;item.state='cancelled';
    return this.publicState(item);
  }
  async stopAccount(accountId) {
    // The pinned official adapter has no logout implementation. Stop is explicit;
    // backend binding revocation remains the immediate authorization boundary.
    this.revokedAccounts.add(accountId);
    this.tencent.accounts?.clearWeixinAccount?.(accountId);
    this.tencent.accounts?.unregisterWeixinAccountId?.(accountId);
    await this.tencent.accounts.setHouKongAccountEnabled(accountId,false);
    return {state:'disconnected'};
  }
  stop() { for(const key of this.requests.keys())this.cancel(key); }
}
