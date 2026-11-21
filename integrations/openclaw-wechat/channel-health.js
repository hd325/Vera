// Cross-worker channel health. This file stores no messages, tokens or URLs.
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, statSync, unlinkSync, writeFileSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';

const SOURCE = 'houkong-shared-channel-health-v1';
const pause = new Int32Array(new SharedArrayBuffer(4));

export function createChannelHealth({stateDir=process.env.HOUKONG_WECHAT_STATE_DIR||path.join(os.homedir(),'.openclaw-houkong-wechat'),now=Date.now,staleAfterMs=120000,lockTimeoutMs=2000}={}) {
  const directory=path.join(path.resolve(stateDir),'state','houkong-channel-health');
  function filename(accountId) {
    if(typeof accountId!=='string'||!accountId||accountId.length>512)throw new Error('A channel account identity is required.');
    return path.join(directory,createHash('sha256').update(accountId).digest('hex')+'.json');
  }
  function read(file) {
    try {
      const value=JSON.parse(readFileSync(file,'utf8'));
      return value?.source===SOURCE&&typeof value.accountId==='string'&&typeof value.generation==='string'?value:null;
    }catch{return null;}
  }
  function locked(file,operation) {
    mkdirSync(directory,{recursive:true,mode:0o700});
    const lock=file+'.lock';
    let fd;
    const until=Date.now()+lockTimeoutMs;
    while(fd===undefined){
      try{fd=openSync(lock,'wx',0o600);}
      catch(error){
        if(error.code!=='EEXIST')throw error;
        // A crashed writer cannot keep health updates locked indefinitely.
        try{if(Date.now()-statSync(lock).mtimeMs>30000){unlinkSync(lock);continue;}}catch{}
        if(Date.now()>=until)throw new Error('The channel health update is busy.');
        Atomics.wait(pause,0,0,10);
      }
    }
    try{return operation();}
    finally{closeSync(fd);try{unlinkSync(lock);}catch{}}
  }
  function write(file,value) {
    const temporary=file+'.'+process.pid+'.'+randomUUID()+'.tmp';
    try{writeFileSync(temporary,JSON.stringify(value),{mode:0o600});renameSync(temporary,file);}
    finally{try{unlinkSync(temporary);}catch{}}
  }
  function begin(accountId,{running=true}={}) {
    const file=filename(accountId),generation=randomUUID(),stamp=now();
    return locked(file,()=>{
      write(file,{source:SOURCE,accountId,generation,running:running===true,loggedOut:false,
        lastStartAt:stamp,lastEventAt:null,lastPollAt:null,lastErrorAt:null,lastError:null,
        consecutiveErrors:0,updatedAt:stamp});
      return generation;
    });
  }
  function update(accountId,generation,event={}) {
    const file=filename(accountId);
    return locked(file,()=>{
      const value=read(file);
      if(!value||value.generation!==generation)return false;
      const stamp=now();
      if(typeof event.running==='boolean')value.running=event.running;
      if(Number.isFinite(event.lastStartAt))value.lastStartAt=event.lastStartAt;
      if(Number.isFinite(event.lastEventAt))value.lastEventAt=event.lastEventAt;
      if(event.pollOk===true){
        value.running=true;value.lastPollAt=stamp;value.lastEventAt=stamp;
        value.consecutiveErrors=0;value.lastError=null;value.lastErrorAt=null;value.loggedOut=false;
      } else if(event.expired===true){
        value.loggedOut=true;value.lastError='auth_expired';value.lastErrorAt=stamp;
      } else if(event.networkError===true){
        value.consecutiveErrors+=1;value.lastError='network_error';value.lastErrorAt=stamp;
      } else if(event.apiError===true){
        value.consecutiveErrors=0;value.lastError='api_error';value.lastErrorAt=stamp;
      }
      value.updatedAt=stamp;
      write(file,value);
      return true;
    });
  }
  function project(value) {
    if(!value)return null;
    let state='unknown',connected=null;
    if(value.loggedOut===true){state='expired';connected=false;}
    else if(value.running===false){state='disconnected';connected=false;}
    else if(value.consecutiveErrors>=3&&value.lastError==='network_error'){state='offline';connected=false;}
    else if(value.running===true&&Number.isFinite(value.lastPollAt)&&now()-value.lastPollAt<=staleAfterMs&&!value.lastError){state='online';connected=true;}
    return {...value,state,connected};
  }
  function snapshot(accountId){return project(read(filename(accountId)));}
  function snapshots(){
    if(!existsSync(directory))return [];
    return readdirSync(directory).filter(name=>/^[a-f0-9]{64}\.json$/.test(name))
      .map(name=>project(read(path.join(directory,name)))).filter(Boolean);
  }
  return {begin,update,snapshot,snapshots,reset:accountId=>begin(accountId,{running:false})};
}
