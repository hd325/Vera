/* The web account owns the connection; channel credentials never enter browser storage. */
(() => {
  'use strict';
  const terminal = new Set(['expired', 'cancelled', 'failed', 'conflict', 'rejected']);
  const labels = {unbound:'未綁定', disconnected:'已斷開', connected:'已連接', active:'已連接',
    expired:'授權已過期', revoked:'授權已失效', unauthorized:'授權已失效', unavailable:'服務準備中',
    pending:'等待掃碼', qr_ready:'等待掃碼', awaiting_scan:'等待掃碼', waiting_scan:'等待掃碼', scanned:'等待微信確認',
    awaiting_verification:'需要驗證', verification_required:'需要驗證', awaiting_confirmation:'等待網站確認',
    awaiting_confirm:'等待網站確認', waiting_confirmation:'等待微信確認', verified:'等待網站確認', pin_required:'需要 PIN 驗證', choice_required:'請選擇授權方式', confirmed:'已確認', failed:'連接失敗', conflict:'綁定衝突',
    cancelled:'已取消', rejected:'授權未通過', connection_error:'連接異常'};
  let host, content, statusNode, bindingPanel;
  let identity = '', generation = 0, connection = null, pending = null, conversations = [];
  let opened = false, busy = false, notice = '', noticeError = false, pollTimer, clockTimer, pollFailures = 0;
  let healthTimer, healthController = null, healthRevision = 0, healthNotice = false;
  let loaded = false, qrBlob = null, verificationDraft = {id:'',pin:'',choice:''};
  const controllers = new Set();
  const session = () => typeof logoutInProgress !== 'undefined' && logoutInProgress ? '' : typeof sessionId === 'string' ? sessionId : '';
  const account = () => { try { return JSON.parse(localStorage.getItem('userInfo') || '{}').account || ''; } catch (_) { return ''; } };
  const displayAccount = () => connection?.account || account();
  const currentIdentity = () => JSON.stringify([session(), account()]);
  const el = (tag, text, cls) => { const n=document.createElement(tag); if(text != null)n.textContent=text; if(cls)n.className=cls; return n; };
  const isConnected = () => connection && ['connected', 'active'].includes(connection.state);
  const state = () => String(pending?.state || (isConnected() && connection?.connection_health==='offline' ? 'connection_error' : connection?.state) || 'unbound');
  const identityLabel = value => typeof value === 'string' ? value : value?.display_name || value?.label || value?.id || value?.account_id || value?.sender_id || '';
  const stale = epoch => epoch !== generation || identity !== currentIdentity() || !opened;
  function expiryTime(value) {
    if(typeof value==='number'||typeof value==='string'&&/^\d+(?:\.\d+)?$/.test(value)){
      const numeric=Number(value);return Number.isFinite(numeric)?(numeric<1e12?numeric*1000:numeric):NaN;
    }
    return typeof value==='string'?Date.parse(value):NaN;
  }
  const remaining = () => Math.max(0, Math.ceil((expiryTime(pending?.expires_at) - Date.now()) / 1000));
  function cancelHealth() { clearTimeout(healthTimer);healthTimer=null;++healthRevision;healthController?.abort();healthController=null; }
  function clearTimers() { clearTimeout(pollTimer); clearInterval(clockTimer); pollTimer=null; clockTimer=null;cancelHealth(); }
  function releaseQr() { if(qrBlob)URL.revokeObjectURL(qrBlob); qrBlob=null; }
  function showNotice(message='', error=false) { notice=message; noticeError=error; if(statusNode){statusNode.textContent=notice;statusNode.classList.toggle('is-error',error);} }
  async function api(path, method='GET', body, epoch=generation, suppliedController=null) {
    const token=session(); if(!token)throw new Error('請先登入。');
    const controller=suppliedController||new AbortController(); controllers.add(controller);
    try {
      const response=await fetch('/api/wechat'+path,{method,signal:controller.signal,headers:{'X-Session-ID':token,...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{body:JSON.stringify(body)})});
      const result=await response.json().catch(()=>({}));
      if(stale(epoch))throw new DOMException('Account or view changed','AbortError');
      if(!response.ok || result.code && result.code!==200)throw new Error(typeof result.detail==='string'?result.detail:result.message||'操作未完成，請重試。');
      return result.data;
    } finally { controllers.delete(controller); }
  }
  function button(text, fn, kind='') {
    const b=el('button',text,'settings-action '+kind);b.type='button';b.disabled=busy;
    b.addEventListener('click',()=>void run(fn));return b;
  }
  async function run(fn) {
    if(busy)return; const epoch=generation;cancelHealth();healthNotice=false;busy=true; showNotice(); render();
    try { await fn(); }
    catch(error){if(!stale(epoch)&&error.name!=='AbortError')showNotice(error.message,true);}
    finally {if(!stale(epoch)){busy=false;render();scheduleHealth();}}
  }
  function cancelRemote(id, token) {
    if(!id||!token)return;
    void fetch('/api/wechat/bind-requests/'+encodeURIComponent(id),{method:'DELETE',headers:{'X-Session-ID':token},keepalive:true}).catch(()=>{});
  }
  function reset({cancel=true}={}) {
    const token=JSON.parse(identity||'[]')[0];
    if(cancel&&pending&&!terminal.has(pending.state))cancelRemote(pending.id,token);
    ++generation;clearTimers();controllers.forEach(c=>c.abort());controllers.clear();releaseQr();
    identity=currentIdentity();connection=null;pending=null;conversations=[];busy=false;loaded=false;notice='';noticeError=false;pollFailures=0;healthNotice=false;verificationDraft={id:'',pin:'',choice:''};
    if(host){opened=false;render();}
  }
  function suspend() {
    if(!opened&&!pending)return;
    reset();
  }
  async function loadConnection() {
    const epoch=generation;
    connection=await api('/connection','GET',undefined,epoch);loaded=true;
    render();
    if(isConnected()){await loadConversations(epoch);}
    scheduleHealth();
  }
  function scheduleHealth(delay=10000) {
    clearTimeout(healthTimer);healthTimer=null;
    if(!opened||!host||!loaded||pending||busy||document.hidden||healthController)return;
    const epoch=generation,revision=healthRevision;
    healthTimer=setTimeout(async()=>{
      healthTimer=null;
      if(stale(epoch)||!host||revision!==healthRevision||document.hidden||pending||busy)return;
      const controller=new AbortController();healthController=controller;
      try {
        const updated=await api('/connection','GET',undefined,epoch,controller);
        if(stale(epoch)||revision!==healthRevision||document.hidden||pending||busy)return;
        connection=updated;
        if(healthNotice){showNotice();healthNotice=false;}
        render();
      } catch(error) {
        if(!stale(epoch)&&revision===healthRevision&&!document.hidden&&error.name!=='AbortError'){
          healthNotice=true;showNotice('暫時無法更新微信連接狀態，將自動重試。',true);
        }
      } finally {
        if(healthController===controller)healthController=null;
        if(!stale(epoch)&&revision===healthRevision)scheduleHealth();
      }
    },delay);
  }
  async function loadConversations(epoch=generation) {
    const result=await api('/conversations','GET',undefined,epoch);
    conversations=Array.isArray(result?.conversations)?result.conversations:[];render();
  }
  function poll() {
    clearTimeout(pollTimer);
    if(!pending||terminal.has(pending.state)||!opened)return;
    const epoch=generation,id=pending.id;
    pollTimer=setTimeout(async()=>{
      try {
        const result=await api('/bind-requests/'+encodeURIComponent(id),'GET',undefined,epoch);
        if(pending?.id!==id)return;
        pending=result;pollFailures=0;showNotice();
        if(['connected','active','confirmed'].includes(pending.state)){pending=null;clearTimers();await loadConnection();showNotice('微信已連接。');}
        render();poll();
      } catch(error) {
        if(stale(epoch)||error.name==='AbortError')return;
        ++pollFailures;showNotice('暫時無法更新連接狀態，正在重試。',true);poll();
      }
    }, Math.min(15000,2000*2**Math.min(pollFailures,3)));
  }
  function startClock() {
    clearInterval(clockTimer);
    clockTimer=setInterval(()=>{
      if(!pending||!opened){clearInterval(clockTimer);return;}
      if(Number.isFinite(expiryTime(pending.expires_at))&&remaining()===0&&!terminal.has(pending.state)){
        pending={...pending,state:'expired'};clearTimers();render();return;
      }
      const counter=document.getElementById('wechat-qr-expiry');
      if(counter)counter.textContent=expiryText();
    },1000);
  }
  function expiryText() {
    if(!Number.isFinite(expiryTime(pending?.expires_at)))return '請按微信畫面完成授權。';
    const n=remaining();return n>0?`授權碼將於 ${Math.floor(n/60)}:${String(n%60).padStart(2,'0')} 後過期`:'授權碼已過期，請重新取得。';
  }
  async function startBinding() {
    if(pending){await api('/bind-requests/'+encodeURIComponent(pending.id),'DELETE');pending=null;clearTimers();}
    const result=await api('/bind-requests','POST',{});pending=result;pollFailures=0;startClock();render();poll();
    requestAnimationFrame(()=>bindingPanel?.querySelector('input,button')?.focus());
  }
  async function cancelBinding() {
    const id=pending?.id;if(id)await api('/bind-requests/'+encodeURIComponent(id),'DELETE');
    pending=null;clearTimers();releaseQr();showNotice('已取消這次授權。');render();
  }
  function qrImage(request) {
    const img=el('img');img.alt='本次微信綁定授權二維碼';img.width=216;img.height=216;
    if(typeof request.qr_data_url==='string'&&/^data:image\/(png|svg\+xml|gif|webp);(?:base64,|utf8,|charset=utf-8,)/i.test(request.qr_data_url))img.src=request.qr_data_url;
    else if(typeof request.qr_svg==='string'&&/^\s*<svg[\s>]/i.test(request.qr_svg)){
      qrBlob=URL.createObjectURL(new Blob([request.qr_svg],{type:'image/svg+xml'}));img.src=qrBlob;
    } else return null;
    return img;
  }
  function addIdentity(parent,label,value) { const row=el('div',null,'wechat-detail');row.append(el('dt',label),el('dd',value||'—'));parent.append(row); }
  function renderBinding() {
    releaseQr();
    const request=pending, phase=state();
    bindingPanel=el('section',null,'wechat-binding');bindingPanel.id='wechat-binding';bindingPanel.setAttribute('aria-labelledby','wechat-binding-title');
    const heading=el('h5','連接你的微信');heading.id='wechat-binding-title';bindingPanel.append(heading);
    bindingPanel.append(el('p','這次授權將連接到小智帳號 '+displayAccount()+'。請使用你自己的微信完成驗證，並在本頁最後確認。','settings-hint'));
    const phaseNode=el('p',labels[phase]||'等待授權','wechat-phase');phaseNode.setAttribute('role','status');bindingPanel.append(phaseNode);
    if(request.error)bindingPanel.append(el('p',typeof request.error==='string'?request.error:request.error.message||'授權未完成。','wechat-error'));
    if(!terminal.has(phase)&&!['awaiting_confirmation','awaiting_confirm','verified'].includes(phase)){
      const image=qrImage(request);
      if(image){const frame=el('div',null,'wechat-qr');frame.append(image);bindingPanel.append(frame);}
      else if(!['awaiting_verification','verification_required'].includes(phase))bindingPanel.append(el('p','授權二維碼尚未準備好，請重新取得或稍後重試。','settings-hint'));
      const expiry=el('p',expiryText(),'settings-hint wechat-expiry');expiry.id='wechat-qr-expiry';bindingPanel.append(expiry);
    }
    const kind=String(request.verification_kind||'');
    if(!terminal.has(phase)&&phase!=='verified'&&['pin','choice','authorization_choice','owner_choice'].includes(kind)){
      const form=el('form',null,'wechat-verification');
      if(kind==='pin'){
        const label=el('label','輸入微信顯示的 PIN','settings-label'),input=el('input');input.id='wechat-pin';input.type='text';input.inputMode='numeric';input.autocomplete='one-time-code';input.maxLength=32;input.required=true;input.disabled=busy;input.value=verificationDraft.id===request.id?verificationDraft.pin:'';label.htmlFor=input.id;form.append(label,input);
      } else {
        const legend=el('legend','選擇授權方式'),group=el('fieldset');group.append(legend);
        const choices=Array.isArray(request.verification_choices)?request.verification_choices:[{value:'own',label:'本人授權'},{value:'delegate',label:'代他人授權'}];
        for(const item of choices){const value=typeof item==='string'?item:item.value,label=el('label',null,'wechat-choice'),input=el('input');input.type='radio';input.name='wechat-verification-choice';input.value=value;input.required=true;input.disabled=busy;input.checked=verificationDraft.id===request.id&&verificationDraft.choice===value;label.append(input,document.createTextNode(typeof item==='string'?item:item.label||value));group.append(label);}form.append(group);
      }
      const submit=el('button','提交驗證','settings-action primary');submit.type='submit';submit.disabled=busy;form.append(submit);
      form.addEventListener('submit',event=>{
        event.preventDefault();const pin=form.querySelector('#wechat-pin')?.value.trim(),choice=form.querySelector('input[type=radio]:checked')?.value;
        void run(async()=>{pending=await api('/bind-requests/'+encodeURIComponent(request.id)+'/verification','POST',kind==='pin'?{pin}:{choice});render();poll();});
      });bindingPanel.append(form);
    }
    if(phase==='verified'){
      const details=el('dl',null,'wechat-details');addIdentity(details,'小智帳號',displayAccount());addIdentity(details,'已驗證微信',identityLabel(request.identity));bindingPanel.append(details);
      bindingPanel.append(el('p','請核對以上帳號。確認後，這個微信才能存取此小智帳號的資料。','settings-hint'));
      bindingPanel.append(button('確認連接此帳號',async()=>{await api('/bind-requests/'+encodeURIComponent(request.id)+'/confirm','POST',{});pending=null;clearTimers();releaseQr();await loadConnection();showNotice('微信已連接。');},'primary'));
    }
    const actions=el('div',null,'settings-actions');actions.append(button(terminal.has(phase)?'重新取得二維碼':'刷新二維碼',startBinding));actions.append(button('取消授權',cancelBinding));bindingPanel.append(actions);content.append(bindingPanel);
  }
  async function disconnect() {
    const confirm=typeof customConfirm==='function'?await customConfirm('解除後，原微信將立即停止存取此帳號，也不再接收新回覆。','解除微信綁定'):window.confirm('解除微信綁定？');
    if(!confirm)return;
    await api('/connection','DELETE');pending=null;clearTimers();await loadConnection();showNotice('已解除微信綁定。');
  }
  function renderConnected() {
    const details=el('dl',null,'wechat-details');addIdentity(details,'已連接微信',identityLabel(connection.identity));
    addIdentity(details,'使用模型',typeof connection.model==='object'?connection.model.name||connection.model.id:connection.model);content.append(details);
    const conversationLabel=el('label','微信目前使用的會話','settings-label'),select=el('select');select.id='wechat-conversation';conversationLabel.htmlFor=select.id;
    select.append(new Option(conversations.length?'請選擇本人會話':'暫無可選會話',''));
    for(const item of conversations)select.append(new Option(item.title||'未命名會話',item.id));select.value=connection.conversation_id||'';select.disabled=busy||!conversations.length;
    select.addEventListener('change',()=>{const id=select.value;if(!id)return;void run(async()=>{const result=await api('/conversation','PUT',{conversation_id:id});connection={...connection,...result};showNotice('微信會話已更新。');});});
    const row=el('div',null,'wechat-field');row.append(conversationLabel,select,el('p','只有在這裡選擇，才會切換微信的會話；瀏覽網站其他聊天不會改變它。','settings-hint'));content.append(row);
    const skillLabel=el('label','微信教學人物','settings-label'),skills=el('select');skills.id='wechat-skill';skillLabel.htmlFor=skills.id;
    const supported=connection.capabilities?.skills;
    const options=Array.isArray(supported)?supported:[{id:'none',label:'一般小智'},{id:'chemistry-teacher',label:'化學老師'}];
    for(const item of options){const option=new Option(typeof item==='string'?item:item.label||item.name||item.id,typeof item==='string'?item:item.id);option.disabled=item.available===false;skills.append(option);}
    skills.value=connection.skill_id||'none';skills.disabled=busy;
    skills.addEventListener('change',()=>{const skill_id=skills.value;void run(async()=>{const result=await api('/preferences','PATCH',{skill_id});connection={...connection,...result};showNotice('微信教學人物已更新。');});});
    const skillRow=el('div',null,'wechat-field');skillRow.append(skillLabel,skills);content.append(skillRow);
    // 開啟＝每條微信訊息都強制；關閉＝由 AI 自行判斷是否需要。
    for(const [key,title,hint] of [['force_web','強制聯網','開啟後每條微信訊息都上網查；關閉時由 AI 自動判斷是否需要聯網。'],['force_think','強制思考','開啟後每條微信訊息都先深度思考；關閉時由 AI 自動判斷。']]){
      const label=el('label',null,'wechat-choice'),input=el('input');input.type='checkbox';input.id='wechat-'+key.replace('_','-');input.checked=connection[key]===true;input.disabled=busy;
      label.append(input,document.createTextNode(title));
      input.addEventListener('change',()=>{const value=input.checked;void run(async()=>{const result=await api('/preferences','PATCH',{[key]:value});connection={...connection,...result};showNotice('微信'+title+(value?'已開啟。':'已關閉，改由 AI 自動判斷。'));});});
      const switchRow=el('div',null,'wechat-field');switchRow.append(label,el('p',hint,'settings-hint'));content.append(switchRow);
    }
    const actions=el('div',null,'settings-actions');
    if(connection.conversation_id)actions.append(button('查看微信會話',async()=>{const id=connection.conversation_id;window.AccountSettings?.close();if(typeof switchToConversation==='function')await switchToConversation(id);}));
    actions.append(button('切換綁定',async()=>{await disconnect();if(!isConnected())await startBinding();}),button('解除綁定',disconnect,'danger'),button('重新整理',loadConnection));content.append(actions);
  }
  function render() {
    if(!host)return;
    const focused=document.activeElement,focusInside=content.contains(focused),focusId=focusInside?focused.id:'',focusText=focusInside&&focused.tagName==='BUTTON'?focused.textContent:'',focusChoice=focusInside&&focused.name==='wechat-verification-choice'?focused.value:'';
    const pin=content.querySelector('#wechat-pin'),choice=content.querySelector('input[name="wechat-verification-choice"]:checked');
    const previousRequest=content.querySelector('#wechat-binding')?.dataset.requestId;
    if(previousRequest&&(pin||choice))verificationDraft={id:previousRequest,pin:pin?.value||'',choice:choice?.value||''};
    const accountNode=host.querySelector('#wechat-account');accountNode.textContent=displayAccount()||'尚未登入';
    content.replaceChildren();statusNode=el('p',notice,'settings-status');statusNode.id='wechat-status';statusNode.setAttribute('role','status');statusNode.setAttribute('aria-live','polite');statusNode.classList.toggle('is-error',noticeError);content.append(statusNode);
    if(!opened)return;
    if(!loaded){content.append(el('p',busy?'正在讀取連接狀態…':'暫時未能讀取微信連接狀態。','settings-hint'));if(!busy)content.append(button('重試讀取連接狀態',loadConnection));return;}
    const phase=state(),badge=el('span',labels[phase]||'未連接','wechat-state');badge.dataset.state=phase;content.append(badge);
    if(connection.reason)content.append(el('p',connection.reason,'settings-hint'));
    if(pending){renderBinding();bindingPanel.dataset.requestId=pending.id;restoreFocus();return;}
    if(isConnected()){renderConnected();restoreFocus();return;}
    const unavailable=connection.available===false||displayAccount()==='0000';
    if(displayAccount()==='0000')content.append(el('p','共用展示帳號不能連接個人微信，請改用自己的小智帳號。','settings-hint'));
    else if(unavailable&&!connection.reason)content.append(el('p','微信服務正在準備，完成設定後即可連接。','settings-hint'));
    const actions=el('div',null,'settings-actions'),bind=button(['revoked','expired','unauthorized','disconnected'].includes(phase)?'重新連接微信':'綁定微信',startBinding,'primary');bind.disabled=busy||unavailable;
    actions.append(bind,button('重新整理',loadConnection));content.append(actions);
    restoreFocus();
    function restoreFocus(){
      if(!focusInside)return;
      const target=focusId?document.getElementById(focusId):focusChoice?[...content.querySelectorAll('input[name="wechat-verification-choice"]')].find(n=>n.value===focusChoice):focusText?[...content.querySelectorAll('button')].find(n=>n.textContent===focusText):null;
      if(target&&!target.disabled)target.focus({preventScroll:true});
    }
  }
  function mount(parent) {
    // 宿主是設定裡的「微信連接」二級頁面；進入頁面時 open()，離開時 suspend()。
    if(host)return;host=el('section',null,'wechat-connection');host.id='settings-wechat';host.setAttribute('aria-labelledby','settings-page-title');
    const intro=el('p','連接自己的微信後，可在微信繼續小智會話。','settings-hint');
    const owner=el('p',null,'wechat-account');owner.append(document.createTextNode('目前小智帳號：'),el('strong',displayAccount(),'wechat-account-value'));owner.lastChild.id='wechat-account';host.append(intro,owner);
    content=el('div',null,'wechat-content');host.append(content);parent.append(host);identity=currentIdentity();render();
    window.addEventListener('houkong:identity',()=>reset());
    document.addEventListener('visibilitychange',()=>{
      if(document.hidden){cancelHealth();return;}
      if(opened&&pending)poll();
      else if(opened)scheduleHealth(0);
    });
  }
  function openView() {
    if(!host||!window.AccountSettings?.isOpen())return;
    if(identity!==currentIdentity())reset();
    if(opened)return;
    opened=true;void run(loadConnection);
  }
  window.WeChatConnection={mount,open:openView,suspend,clear:()=>reset(),isOpen:()=>opened};
})();
