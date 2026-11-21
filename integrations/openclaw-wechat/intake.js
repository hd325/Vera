// Persist incoming provider envelopes before advancing its cursor. No envelope,
// URL, sender, message body or credential is written to the application logger.
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import path from 'node:path';
import os from 'node:os';

const pause = new Int32Array(new SharedArrayBuffer(4));
const digest = value => createHash('sha256').update(value).digest('hex');
const structured = value => JSON.parse(JSON.stringify(value));
const statePath = (accountId,stateDir=process.env.HOUKONG_WECHAT_STATE_DIR||path.join(os.homedir(),'.openclaw-houkong-wechat')) => path.join(path.resolve(stateDir),'state','houkong-inbound',digest(accountId)+'.json');

function mutateJournal(accountId,operation,stateDir) {
  if(typeof accountId!=='string'||!accountId||accountId.length>512)throw new Error('A channel account is required.');
  const file=statePath(accountId,stateDir),lock=file+'.lock',deadline=Date.now()+2000;let fd;
  mkdirSync(path.dirname(file),{recursive:true,mode:0o700});
  while(fd===undefined){
    try{fd=openSync(lock,'wx',0o600);}
    catch(error){
      if(error.code!=='EEXIST')throw error;
      try{if(Date.now()-statSync(lock).mtimeMs>30000){unlinkSync(lock);continue;}}catch{}
      if(Date.now()>=deadline)throw new Error('The private inbound journal is busy.');
      Atomics.wait(pause,0,0,10);
    }
  }
  try{
    const state=existsSync(file)?JSON.parse(readFileSync(file,'utf8')):{version:1,accountId,groups:[],seen:[],archive:[],turns:{}};
    if(state.version!==1||state.accountId!==accountId||!Array.isArray(state.groups)||!Array.isArray(state.seen))throw new Error('The private inbound journal is invalid.');
    state.archive??=[];state.turns??={};state.interrupts??=[];
    const result=operation(state),temporary=file+'.'+randomUUID()+'.tmp';
    try{writeFileSync(temporary,JSON.stringify(state),{mode:0o600});renameSync(temporary,file);}
    finally{try{unlinkSync(temporary);}catch{}}
    return result;
  }finally{closeSync(fd);try{unlinkSync(lock);}catch{}}
}

export function recordHouKongTurn({account_id,event_id,turn_id,attempt=1,stateDir}) {
  if(!event_id||!/^[\w-]{1,180}$/.test(turn_id??''))throw new Error('A trusted turn identity is required.');
  if(!Number.isSafeInteger(attempt)||attempt<1)throw new Error('A trusted task attempt is required.');
  return mutateJournal(account_id,state=>{
    if((state.turns[turn_id]?.attempt??1)>attempt)throw new Error('The task attempt was replaced.');
    const found=[...state.groups,...state.archive].find(group=>mergeInboundMessages(group.messages).message_id===event_id);
    if(!found)throw new Error('The admitted provider envelope was not journaled.');
    const original=state.turns[turn_id]?.full??mergeInboundMessages(found.messages);
    state.turns[turn_id]={eventId:event_id,sender:found.sender,full:structured(original),attempt};
    const entries=Object.entries(state.turns);if(entries.length>2000)state.turns=Object.fromEntries(entries.slice(-2000));
    return {recorded:true};
  },stateDir);
}

export function hasHouKongTurn({account_id,event_id,stateDir}) {
  try{
    const state=JSON.parse(readFileSync(statePath(account_id,stateDir),'utf8'));
    return state.accountId===account_id&&Object.values(state.turns??{}).some(item=>item.eventId===event_id);
  }catch{return false;}
}

export function enqueueHouKongResume({turn_id,account_id,sender_id,stateDir}) {
  if(!/^[\w-]{1,180}$/.test(turn_id??'')||!sender_id)throw new Error('A trusted resume identity is required.');
  return mutateJournal(account_id,state=>{
    const mapping=state.turns[turn_id];
    if(!mapping||mapping.sender!==sender_id)throw new Error('The original task envelope is not available for this account and peer.');
    const existing=state.groups.find(group=>group.resumeTurnId===turn_id&&['pending','active'].includes(group.status));
    if(existing)return {queued:true,duplicate:true};
    const original=[...state.groups,...state.archive].find(group=>group.sender===sender_id&&mergeInboundMessages(group.messages).message_id===mapping.eventId);
    if(!original&&!mapping.full)throw new Error('The original task envelope is unavailable.');
    const full=structured(mapping.full??mergeInboundMessages(original.messages)),stamp=Date.now(),id=randomUUID();
    // Inputs are already staged by the trusted business service. A resume must
    // not depend on expiring provider download links or create another copy.
    full.item_list=(full.item_list??[]).filter(item=>item.type===1);
    if(!full.item_list.length)full.item_list=[{type:1,text_item:{text:'繼續原任務，沿用已保存原件及進度。'}}];
    full.message_id='hk-resume-'+id;full.houkong_resume_turn_id=turn_id;
    if(original){state.archive.push(structured(original));state.archive=state.archive.slice(-100);state.groups=state.groups.filter(group=>group.id!==original.id);}
    state.groups.push({id,sender:sender_id,ids:[full.message_id],messages:[full],status:'pending',attempts:0,frozen:true,action:null,resumeTurnId:turn_id,createdAt:stamp,dueAt:stamp,deadline:stamp});
    return {queued:true,duplicate:false};
  },stateDir);
}

export function pauseHouKongTurn({turn_id,account_id,sender_id,attempt,stateDir}) {
  if(!/^[\w-]{1,180}$/.test(turn_id??'')||!sender_id)throw new Error('A trusted task identity is required.');
  return mutateJournal(account_id,state=>{
    const mapping=state.turns[turn_id];
    if(!mapping||mapping.sender!==sender_id)throw new Error('The task does not belong to this account and peer.');
    if(attempt!==undefined&&attempt!==(mapping.attempt??1))return {paused:false,stale_attempt:true};
    const original=state.groups.find(group=>group.sender===sender_id&&mergeInboundMessages(group.messages).message_id===mapping.eventId);
    if(!original)return {paused:false,already_finished:true};
    original.status='paused';original.error='paused';original.pauseRequested=true;
    state.interrupts.push({sender:original.sender,groupId:original.id});
    return {paused:true,run_interruption_requested:true};
  },stateDir);
}

export async function postHouKongControl({accountId,action,full,signal}) {
  const token=process.env.HOUKONG_WECHAT_SERVICE_TOKEN;
  const base=new URL(process.env.HOUKONG_WECHAT_BACKEND_URL||'http://127.0.0.1:8000');
  if(!token||base.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(base.hostname))throw new Error('Private task control is unavailable.');
  const response=await fetch(new URL('/internal/wechat/control',base),{method:'POST',headers:{'Content-Type':'application/json','X-Houkong-Wechat-Token':token},
    body:JSON.stringify({account_id:accountId,sender_id:full.from_user_id,event_id:providerMessageId(full),action}),signal:AbortSignal.any([AbortSignal.timeout(15000),...(signal?[signal]:[])])});
  if(!response.ok){const error=new Error('Private task control was refused.');error.status=response.status;throw error;}
  const result=await response.json();
  if(result.code!==undefined&&result.code!==200)throw new Error('Private task control was refused.');
  return result.data??result;
}

export function providerMessageId(message) {
  const id = message?.message_id ?? message?.item_list?.find(item=>typeof item?.msg_id==='string'&&item.msg_id)?.msg_id;
  // Missing IDs must be deterministic across a repeated getUpdates cursor.
  return typeof id==='string'&&id.trim() ? id.trim() : 'hk-envelope-'+digest(JSON.stringify(message));
}

export function controlAction(message) {
  const items=message?.item_list??[];
  if(items.some(item=>item.type!==1))return null;
  const text=items.filter(item=>item.type===1).map(item=>item.text_item?.text??'').join('\n').trim();
  if(/^(?:\/pause|暫停|暂停|暫停任務|暂停任务|先停一下|停止任務|停止任务)$/iu.test(text))return 'pause';
  if(/^(?:\/resume|繼續|继续|繼續執行|继续执行|繼續任務|继续任务|恢復任務|恢复任务)$/iu.test(text))return 'resume';
  return null;
}

export function mergeInboundMessages(messages) {
  if(!messages.length)throw new Error('An inbound group cannot be empty.');
  const sender=messages[0].from_user_id;
  if(!sender||messages.some(item=>item.from_user_id!==sender))throw new Error('An inbound group must have one verified channel peer.');
  const ids=messages.map(providerMessageId);
  const texts=messages.flatMap(message=>(message.item_list??[]).filter(item=>item.type===1&&typeof item.text_item?.text==='string').map(item=>item.text_item.text));
  const other=messages.flatMap(message=>(message.item_list??[]).filter(item=>item.type!==1));
  const text=texts.filter(Boolean).join('\n');
  const last=messages.at(-1);
  return {...structured(messages[0]),context_token:last.context_token??messages[0].context_token,
    message_id:ids.length===1?ids[0]:'hk-group-'+digest(JSON.stringify(ids)),
    item_list:[...(text?[{type:1,text_item:{text}}]:[]),...structured(other)],
    houkong_source_message_ids:ids};
}

export function mediaDescriptors(full) {
  return (full?.item_list??[]).filter(item=>{
    const media=item.image_item?.media??item.video_item?.media??item.file_item?.media??item.voice_item?.media;
    return [2,3,4,5].includes(item.type)&&Boolean(media?.encrypt_query_param||media?.full_url)&&!(item.type===3&&item.voice_item?.text);
  });
}

export function downloadedMediaRecord(item,result) {
  const local=result?.decryptedPicPath??result?.decryptedVideoPath??result?.decryptedFilePath??result?.decryptedVoicePath;
  if(!local)throw new Error('The incoming attachment was not downloaded; processing is waiting for that file.');
  const contentType=result?.fileMediaType??result?.voiceMediaType??(result?.decryptedPicPath?'image/*':result?.decryptedVideoPath?'video/mp4':'application/octet-stream');
  const original=item?.file_item?.file_name;
  return {path:local,contentType,...(typeof original==='string'&&original?{original_filename:path.basename(original.replaceAll('\\','/'))}:{})};
}

export function applyInboundMediaContext(ctx,records) {
  if(!records?.length)return ctx;
  ctx.MediaPaths=records.map(item=>item.path);
  ctx.MediaTypes=records.map(item=>item.contentType);
  ctx.media=records.map(item=>({...item}));
  // Keep the first attachment for native SDK consumers, but the business bridge
  // reads the complete typed media array, including original filenames.
  ctx.MediaPath=records[0].path;ctx.MediaType=records[0].contentType;
  return ctx;
}

export class ManagedInboundQueue {
  constructor({accountId,processMessage,onControl,onError=()=>{},stateDir=process.env.HOUKONG_WECHAT_STATE_DIR||path.join(os.homedir(),'.openclaw-houkong-wechat'),now=Date.now,idleMs=2000,maxWaitMs=5000,maxPending=256,autoStart=true}={}) {
    if(!accountId||typeof processMessage!=='function')throw new Error('A channel account and processor are required.');
    this.accountId=accountId;this.processMessage=processMessage;this.onControl=onControl;this.onError=onError;this.now=now;
    this.idleMs=idleMs;this.maxWaitMs=maxWaitMs;this.maxPending=maxPending;this.generation=randomUUID();this.active=new Map();this.stopped=false;this.timer=null;
    this.directory=path.join(path.resolve(stateDir),'state','houkong-inbound');this.stateDir=stateDir;
    mkdirSync(this.directory,{recursive:true,mode:0o700});
    this.file=statePath(accountId,stateDir);
    this.change(state=>{
      state.generation=this.generation;
      for(const group of state.groups)if(group.status==='active')group.status='pending';
    },true);
    if(autoStart)this.wake(0);
  }
  read() {
    if(!existsSync(this.file))return {version:1,accountId:this.accountId,generation:this.generation,groups:[],seen:[]};
    const value=JSON.parse(readFileSync(this.file,'utf8'));
    if(value.version!==1||value.accountId!==this.accountId||!Array.isArray(value.groups)||!Array.isArray(value.seen))throw new Error('The private inbound journal is invalid.');
    return value;
  }
  change(operation,replaceGeneration=false) {
    return mutateJournal(this.accountId,state=>{
      if(!replaceGeneration&&state.generation!==this.generation)return {obsolete:true};
      return operation(state);
    },this.stateDir);
  }
  enqueueBatch(messages) {
    if(this.stopped)throw new Error('The incoming channel has stopped.');
    const stamp=this.now();
    const result=this.change(state=>{
      const known=new Set([...state.seen,...state.groups.flatMap(group=>group.ids)]);let accepted=0;
      const incoming=[];
      for(const original of messages??[]){
        if(!original?.from_user_id)throw new Error('The incoming message has no channel peer.');
        const id=providerMessageId(original);
        if(known.has(id))continue;
        known.add(id);incoming.push({original,id});
      }
      if(state.groups.reduce((total,group)=>total+group.ids.length,0)+incoming.length>this.maxPending)throw new Error('The private incoming queue is full; its provider cursor was not advanced.');
      for(const {original,id}of incoming){
        const full=structured(original);delete full.houkong_resume_turn_id;delete full.houkong_source_message_ids;
        const action=controlAction(full);
        const previous=[...state.groups].reverse().find(group=>group.sender===full.from_user_id);
        const append=previous&&previous.status==='pending'&&!previous.frozen&&!previous.action&&!action&&stamp<=previous.dueAt&&stamp<previous.deadline;
        if(append){previous.messages.push(full);previous.ids.push(id);previous.dueAt=Math.min(stamp+this.idleMs,previous.deadline);}
        else state.groups.push({id:randomUUID(),sender:full.from_user_id,ids:[id],messages:[full],status:'pending',attempts:0,frozen:false,action,
          createdAt:stamp,dueAt:action?stamp:stamp+this.idleMs,deadline:stamp+this.maxWaitMs});
        accepted++;
      }
      return {accepted};
    });
    if(result?.obsolete)throw new Error('The incoming channel generation was replaced.');
    this.wake(0);return result;
  }
  wake(delay=100) {
    if(this.stopped)return;
    clearTimeout(this.timer);this.timer=setTimeout(()=>{this.timer=null;void this.pump();},Math.max(0,delay));this.timer.unref?.();
  }
  async pump() {
    if(this.stopped)return;
    try{
      const ready=this.change(state=>{
        const chosen=[],blocked=new Set(),controlBlocked=new Set();
        for(const interrupt of state.interrupts){
          const active=this.active.get(interrupt.sender);
          if(active?.groupId===interrupt.groupId)active.controller.abort();
        }
        state.interrupts=[];
        for(const group of state.groups){
          if(group.pauseRequested){
            const active=this.active.get(group.sender);
            if(active?.groupId===group.id)active.controller.abort();
            group.pauseRequested=false;
          }
          if(['paused','failed'].includes(group.status))continue;
          if(group.action){
            const key='control:'+group.sender;
            if(!controlBlocked.has(group.sender)&&!this.active.has(key)&&group.status==='pending'&&group.dueAt<=this.now()){
              group.status='active';group.frozen=true;chosen.push(structured(group));
            }
            controlBlocked.add(group.sender);continue;
          }
          if(blocked.has(group.sender))continue;
          blocked.add(group.sender);
          if(group.status!=='pending'||this.active.has(group.sender)||group.dueAt>this.now())continue;
          group.status='active';group.frozen=true;chosen.push(structured(group));
        }
        return chosen;
      });
      if(ready?.obsolete){this.stop();return;}
      for(const group of ready){
        const controller=new AbortController(),key=group.action?'control:'+group.sender:group.sender;
        this.active.set(key,{controller,groupId:group.id});
        void (async()=>{
          try{
            const full=mergeInboundMessages(group.messages);
            if(group.action){
              if(typeof this.onControl!=='function')throw new Error('Task control is unavailable.');
              try { await this.onControl(group.action,full,controller.signal); }
              catch(error){if(group.action==='resume'&&error.status===404)await this.processMessage(full,controller.signal);else throw error;}
              if(group.action==='pause'){
                this.change(state=>{for(const current of state.groups)if(current.sender===group.sender&&current.status==='active'&&!current.action){current.status='paused';current.error='paused';}});
                this.active.get(group.sender)?.controller.abort();
              }
            }else await this.processMessage(full,controller.signal);
            this.change(state=>{
              const current=state.groups.find(item=>item.id===group.id);if(!current)return;
              if(current.status==='paused')return;
              if(!current.action){state.archive.push(structured(current));state.archive=state.archive.slice(-100);}
              state.seen.push(...current.ids);state.seen=state.seen.slice(-4000);state.groups=state.groups.filter(item=>item.id!==group.id);
            });
          }catch{
            this.change(state=>{
              const current=state.groups.find(item=>item.id===group.id);if(!current)return;
              if(current.status==='paused')return;
              current.attempts++;current.status=this.stopped?'pending':current.attempts>=3?'failed':'pending';
              current.dueAt=this.now()+Math.min(30000,1000*2**current.attempts);current.error='processing_interrupted';
            });
            this.onError();
          }finally{this.active.delete(key);this.wake(0);}
        })();
      }
    }catch{this.onError();}
    this.wake(100);
  }
  stop() { this.stopped=true;clearTimeout(this.timer);for(const active of this.active.values())active.controller.abort(); }
  pending() { return this.read().groups.map(group=>({id:group.id,status:group.status,count:group.ids.length,action:group.action})); }
}
