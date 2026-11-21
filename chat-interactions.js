/* Structured teaching interactions stay outside model-authored Markdown. */
(() => {
  'use strict';
  const cards = new Set(), memoryJobs = new Set();
  const session = () => typeof logoutInProgress !== 'undefined' && logoutInProgress ? '' : typeof sessionId === 'string' ? sessionId : '';
  const safeLink = value => { try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password ? u.href : null; } catch (_) { return null; } };
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
  const active=s=>s.node.isConnected&&s.identity===session()&&!s.disposed;
  async function api(s,path,method='GET',body) {
    if(!active(s))throw new Error('對話已切換。');
    const response=await fetch('/api'+path,{method,headers:{'X-Session-ID':s.identity,...(body===undefined?{}:{'Content-Type':'application/json'})},signal:s.controller.signal,...(body===undefined?{}:{body:JSON.stringify(body)})});
    const value=await response.json();if(!active(s))throw new Error('對話已切換。');
    if(!response.ok||value.code&&value.code!==200)throw new Error(typeof value.detail==='string'?value.detail:value.message||'練習暫時無法載入，請重試。');return value.data;
  }
  function button(text,fn,cls='chat-practice-action') {
    const n=el('button',text,cls);n.type='button';n.addEventListener('click',()=>void fn(n));return n;
  }
  function dispose(s){s.disposed=true;clearTimeout(s.timer);s.controller.abort();cards.delete(s);}
  async function pollMemory(s) {
    if(!active(s)){dispose(s);memoryJobs.delete(s);return;}
    if(document.hidden||window.AccountSettings?.isOpen()){s.timer=setTimeout(()=>void pollMemory(s),3500);return;}
    try {
      const job=await api(s,'/memories/jobs/'+encodeURIComponent(s.id));
      if(!active(s))return;
      if(job.status==='succeeded'&&job.saved_count>0){
        s.node.hidden=false;s.node.textContent='已儲存記憶';
        window.dispatchEvent(new CustomEvent('houkong:memory-updated'));
      }
      if(!['queued','running'].includes(job.status)){memoryJobs.delete(s);return;}
    } catch(e) { if(!active(s)||s.failures++>=3){memoryJobs.delete(s);return;} }
    s.timer=setTimeout(()=>void pollMemory(s),3500);
  }
  function schedule(s) {
    clearTimeout(s.timer);if(!active(s))return dispose(s);
    if(s.task?.skipped||!['queued','running','transferring'].includes(s.task?.status))return;
    s.timer=setTimeout(()=>{
      if(!active(s))return dispose(s);
      if(document.hidden||window.AccountSettings?.isOpen())return schedule(s);
      void load(s);
    },Math.min(15000,2500*Math.max(1,s.failures)));
  }
  async function load(s) {
    if(!active(s))return dispose(s);
    if(s.loading)return;s.loading=true;
    try {
      s.task=await api(s,'/tasks/'+encodeURIComponent(s.id));s.failures=0;s.error='';
      if(s.task.status==='succeeded'&&!s.attemptsLoaded){
        const attempts=await api(s,'/tasks/'+encodeURIComponent(s.id)+'/attempts');
        s.attempt=Array.isArray(attempts)?attempts.find(a=>a.mode==='instant')||null:null;s.attemptsLoaded=true;
      }
      if(active(s))render(s);
    }catch(e){if(active(s)){s.failures++;s.error=e.message;render(s);}}
    finally{s.loading=false;schedule(s);}
  }
  function source(s,q,parent) {
    const label=q.source_label||s.task.result?.source_label||'';
    const kind=q.source_kind||s.task.result?.source_kind||'';
    const text=['local','document'].includes(kind)?'教材改編':kind==='web'?'網頁資料改編':'自編練習';
    const line=el('p',text+(label?' · '+label:'')+(q.page?' · 第 '+q.page+' 頁':''),'chat-practice-source');
    if(q.document_id&&q.chunk_id&&typeof openSourceView==='function'){
      line.append(document.createTextNode(' · '),button('查看來源',()=>openSourceView(q.document_id,q.chunk_id)));
    } else if(typeof q.url==='string'&&safeLink(q.url)){
      const link=el('a','查看來源');link.href=safeLink(q.url);link.target='_blank';link.rel='noopener noreferrer';line.append(document.createTextNode(' · '),link);
    }
    parent.append(line);
  }
  async function choose(s,letter) {
    if(!active(s)||s.busy||s.attempt?.grades?.['0'])return;
    s.busy=true;s.error='';s.selected=letter;render(s);
    try{
      const base='/tasks/'+encodeURIComponent(s.id);
      if(!s.attempt)s.attempt=await api(s,base+'/attempts','POST',{mode:'instant',resume:true});
      if(!s.attempt.grades?.['0']){
        s.attempt=await api(s,base+'/attempts/'+encodeURIComponent(s.attempt.id),'PATCH',{answers:{'0':letter}});
        s.attempt=await api(s,base+'/attempts/'+encodeURIComponent(s.attempt.id)+'/grade-question','POST',{index:0});
      }
      window.dispatchEvent(new CustomEvent('houkong:learning'));
    }catch(e){if(active(s))s.error=e.message;}
    finally{if(active(s)){s.busy=false;render(s);}}
  }
  function render(s) {
    const n=s.node;n.replaceChildren();
    const title=el('div',null,'chat-practice-heading');title.append(el('h4','試一題，看看是否掌握','chat-practice-title'));n.append(title);
    const task=s.task;
    if(s.error){const err=el('p',s.error,'chat-practice-status is-error');err.setAttribute('role','status');n.append(err,button('重新載入',()=>load(s)));}
    if(!task){if(!s.error)n.append(el('p','正在準備練習…','chat-practice-status'));return;}
    if(task.skipped&&!s.expanded){
      n.append(el('p','已略過這道練習。','chat-practice-status'),button('展開題目',()=>{s.expanded=true;render(s);}));return;
    }
    if(['queued','running','transferring'].includes(task.status)){
      const pending=el('p',task.stage||'正在準備一道練習，可先繼續聊天。','chat-practice-status');pending.setAttribute('role','status');pending.setAttribute('aria-live','polite');n.append(pending);appendActions(s,n);return;
    }
    if(task.status!=='succeeded'){
      if(!s.error)n.append(el('p',task.error||'這道練習目前無法使用，可先繼續聊天。','chat-practice-status'));
      if(['failed','interrupted'].includes(task.status))n.append(button('重試出題',async b=>{b.disabled=true;try{s.task=await api(s,'/tasks/'+encodeURIComponent(s.id)+'/retry','POST');s.error='';render(s);void load(s);}catch(e){s.error=e.message;render(s);}}));return;
    }
    const q=task.result?.questions?.[0];if(!q){n.append(el('p','題目暫時無法顯示。','chat-practice-status'));return;}
    n.append(el('p',q.stem||'','chat-practice-stem'));
    const choices=el('div',null,'chat-practice-options'),grade=s.attempt?.grades?.['0'];choices.setAttribute('role','group');choices.setAttribute('aria-label','練習選項');
    for(const [index,value]of (q.options||[]).entries()){
      const letter=String.fromCharCode(65+index),selected=s.selected||s.attempt?.answers?.['0'];
      const choice=button('',()=>{s.selected=letter;render(s);s.node.querySelector('[data-choice="'+letter+'"]')?.focus({preventScroll:true});},'chat-practice-choice');choice.dataset.choice=letter;choice.append(el('strong',letter+'.'),el('span',typeof value==='string'?value:value.text||value.label||''));
      choice.setAttribute('aria-label',letter+'. '+(typeof value==='string'?value:value.text||value.label||''));choice.setAttribute('aria-pressed',String(selected===letter));
      choice.classList.toggle('is-picked',selected===letter);choice.disabled=s.busy||!!grade;
      if(grade){choice.classList.toggle('is-correct',grade.answer===letter);choice.classList.toggle('is-wrong',selected===letter&&!grade.correct);}
      choices.append(choice);
    }
    n.append(choices);
    if(!grade){const submit=button('提交答案',()=>choose(s,s.selected||s.attempt?.answers?.['0']),'chat-practice-submit');submit.disabled=s.busy||!(s.selected||s.attempt?.answers?.['0']);n.append(submit);}
    if(s.busy)n.append(el('p','正在檢查答案…','chat-practice-status'));
    if(grade){
      const feedback=el('div',(grade.correct?'回答正確。':'這題還差一步。正確答案：'+(grade.answer||'見解析')+'。')+(grade.explanation?'\n'+grade.explanation:''),'chat-practice-feedback '+(grade.correct?'is-correct':'is-wrong'));feedback.setAttribute('role','status');feedback.setAttribute('aria-live','polite');n.append(feedback);
      const follow=el('div',null,'chat-practice-actions');
      follow.append(button('再練一次',async()=>{
        if(s.busy)return;s.busy=true;
        try{s.attempt=await api(s,'/tasks/'+encodeURIComponent(s.id)+'/attempts','POST',{mode:'instant'});s.selected=null;s.error='';}catch(e){s.error=e.message;}finally{s.busy=false;if(active(s))render(s);}
      }),button(grade.correct?'換一道題':'再講解這個重點',()=>sendPrompt(grade.correct?'請依照剛才「'+q.stem+'」的重點，再給我一道新的化學選擇題。':'我剛才在「'+q.stem+'」選了 '+(s.attempt.answers?.['0']||'')+'，請換一個例子講解我可能卡住的地方。')));n.append(follow);
    }
    source(s,q,n);appendActions(s,n,!!grade);
  }
  async function sendPrompt(prompt) {
    if(window.HoukongChat?.busy())return;
    window.HoukongChat?.send(prompt);
  }
  function appendActions(s,n,graded=false) {
    const row=el('div',null,'chat-practice-actions');
    if(!graded&&!s.task.skipped)row.append(button('略過這題',async b=>{b.disabled=true;try{s.task=await api(s,'/tutor/'+encodeURIComponent(s.id)+'/skip','POST');clearTimeout(s.timer);render(s);}catch(e){s.error=e.message;render(s);}}));
    row.append(button('暫停自動練習',async b=>{b.disabled=true;try{await window.AccountSettings.save({auto_practice:false});b.textContent='已暫停自動練習';}catch(e){s.error=e.message;render(s);}}));n.append(row);
  }
  function mount(container,message) {
    if(!container||container.querySelector(':scope > .chat-learning'))return;
    const interactions=message.interactions??message.metadata?.interactions??[],refs=message.memory_refs??message.metadata?.memory_refs??[];
    const update=message.memory_update??message.metadata?.memory_update;
    if(!interactions.length&&!refs.length&&!update?.job_id)return;
    const group=el('div',null,'chat-learning');group.dataset.messageId=message.message_id||message.id||'';
    if(refs.length){
      const details=el('details',null,'chat-memory-refs');details.append(el('summary','參考了 '+refs.length+' 則記憶'));
      const list=el('ul');for(const ref of refs){if(!ref?.id)continue;const li=el('li');li.append(button(ref.content||'查看記憶',()=>window.AccountSettings?.showMemory(ref.id),'chat-memory-ref'));list.append(li);}details.append(list);group.append(details);
    }
    if(update?.job_id){
      const badge=button('已儲存記憶',()=>window.AccountSettings?.open('memory'),'chat-memory-ref');badge.hidden=true;group.append(badge);
      const s={node:badge,id:update.job_id,identity:session(),controller:new AbortController(),failures:0,disposed:false};memoryJobs.add(s);
      requestAnimationFrame(()=>void pollMemory(s));
    }
    for(const interaction of interactions){
      if(interaction?.type==='directions'){
        const row=el('div',null,'chat-directions');row.setAttribute('aria-label','繼續學習的方向');
        for(const option of (interaction.options||[]).slice(0,3)){
          if(typeof option.label!=='string'||typeof option.prompt!=='string')continue;
          const b=button(option.label.slice(0,80),()=>sendPrompt(option.prompt.slice(0,2000)),'chat-direction');row.append(b);
        }group.append(row);
      }else if(interaction?.type==='practice'&&typeof interaction.task_id==='string'){
        const card=el('section',null,'chat-practice');card.dataset.taskId=interaction.task_id;card.setAttribute('aria-label','化學隨堂練習');
        const s={node:card,id:interaction.task_id,identity:session(),controller:new AbortController(),task:null,attempt:null,attemptsLoaded:false,failures:0,busy:false,disposed:false,loading:false};cards.add(s);group.append(card);render(s);
        requestAnimationFrame(()=>{if(active(s))void load(s);else dispose(s);});
      }
    }
    const meta=container.querySelector(':scope > .message-meta');container.insertBefore(group,meta);
  }
  function clear(){for(const s of [...cards,...memoryJobs])dispose(s);memoryJobs.clear();document.querySelectorAll('.chat-learning').forEach(n=>n.remove());}
  function refresh(){for(const s of cards){s.attemptsLoaded=false;s.attempt=null;s.selected=null;void load(s);}}
  window.ChatInteractions={mount,clear,refresh};
  const prune=new MutationObserver(()=>{for(const s of cards)if(!active(s))dispose(s);for(const s of memoryJobs)if(!active(s)){dispose(s);memoryJobs.delete(s);}});
  prune.observe(document.getElementById('messages-container')||document.body,{childList:true,subtree:true});
  window.addEventListener('pagehide',clear);
  for(const entry of window.__pendingChatInteractions||[])if(entry.container.isConnected)mount(entry.container,entry.message);
  window.__pendingChatInteractions=[];
})();


/* Mobile chat layout and interactions. */
/* Phone-specific behavior; desktop keeps its existing controls and shortcuts. */
(function () {
    'use strict';
    const root = document.documentElement;
    const mobileQuery = window.matchMedia('(max-width: 768px), (max-width: 1024px) and (max-height: 500px) and (pointer: coarse)');
    const input = document.getElementById('ai-text-input');
    const container = document.getElementById('ai-chat-container');
    const wrapper = document.getElementById('ai-chat-wrapper');
    const area = document.getElementById('messages-area');
    const scroller = document.querySelector('.messages-inner');
    const voice = document.getElementById('ai-voice-btn');
    const send = document.getElementById('ai-send-btn');
    const menu = document.getElementById('composer-menu');
    const refs = document.getElementById('file-tags-container');
    if (!input || !container || !wrapper || !scroller || !menu || !refs || !document.getElementById('composer-menu-close')) return;
    const desktopPlaceholder = input.placeholder;
    const mobile = () => mobileQuery.matches;
    let viewportFrame = 0;
    let latestFrame = 0;
    let refsExpanded = false;
    let keyboardOpen = false;
    const viewportBaselines = new Map();

    function syncComposer() {
        const recording = voice?.getAttribute('aria-pressed') === 'true';
        const busy = container.classList.contains('has-active-turn') || send?.getAttribute('aria-busy') === 'true';
        const hasDraft = Boolean(input.value.trim() || refs.querySelector('[data-attachment-key]'));
        const showSend = mobile() && (busy || (hasDraft && !recording));
        container.classList.toggle('mobile-show-send', showSend);
        const placeholder = mobile() ? '發訊息給 Vera…' : desktopPlaceholder;
        if (input.placeholder !== placeholder) input.placeholder = placeholder;
        const voiceTitle = document.querySelector('#composer-voice-btn .composer-menu-title');
        const voiceLabel = recording ? '停止語音輸入' : '語音輸入';
        if (voiceTitle && voiceTitle.textContent !== voiceLabel) voiceTitle.textContent = voiceLabel;
    }

    const summary = document.createElement('button');
    summary.type = 'button';
    summary.className = 'composer-files-summary';
    summary.id = 'composer-files-summary';
    summary.setAttribute('aria-controls', 'file-tags-container');
    refs.before(summary);
    function syncReferences() {
        const count = refs.querySelectorAll('.file-tag').length;
        if (count < 2) refsExpanded = false;
        summary.hidden = !mobile() || count < 2;
        const text = `已加入 ${count} 個檔案 · ${refsExpanded ? '收起' : '展開'}`;
        if (summary.textContent !== text) summary.textContent = text;
        summary.setAttribute('aria-expanded', String(refsExpanded));
        container.classList.toggle('mobile-file-refs-collapsed', mobile() && count > 1 && !refsExpanded);
        syncComposer();
    }
    summary.addEventListener('click', () => { refsExpanded = !refsExpanded; syncReferences(); });
    new MutationObserver(syncReferences).observe(refs, { childList: true });

    const latest = document.createElement('button');
    latest.type = 'button';
    latest.className = 'chat-latest-button';
    latest.textContent = '↓ 回到最新';
    latest.hidden = true;
    area.append(latest);
    function syncLatest() {
        latestFrame = 0;
        latest.hidden = !mobile() || !area.classList.contains('has-messages') ||
            scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 100;
    }
    function scheduleLatest() { if (!latestFrame) latestFrame = requestAnimationFrame(syncLatest); }
    latest.addEventListener('click', () => {
        shouldAutoStickToBottom = true;
        scrollMessagesToBottom(window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth');
    });
    scroller.addEventListener('scroll', scheduleLatest, { passive: true });
    new MutationObserver(scheduleLatest).observe(document.getElementById('messages-container'), { childList: true, subtree: true, characterData: true });

    const backdrop = document.createElement('div');
    backdrop.className = 'composer-sheet-backdrop';
    backdrop.hidden = true;
    backdrop.setAttribute('aria-hidden', 'true');
    document.body.append(backdrop);
    function syncMenu() { backdrop.hidden = !mobile() || menu.hidden; }
    new MutationObserver(syncMenu).observe(menu, { attributes: true, attributeFilter: ['hidden'] });
    backdrop.addEventListener('click', () => closeComposerMenu({ restoreFocus: true }));
    document.getElementById('composer-menu-close').addEventListener('click', () => closeComposerMenu({ restoreFocus: true }));
    document.getElementById('composer-upload-btn').addEventListener('click', () => {
        closeComposerMenu();
        document.getElementById('panel-file-input').click();
    });
    document.getElementById('composer-files-btn').addEventListener('click', () => {
        closeComposerMenu();
        setFilePanelState(true);
        void loadFiles();
    });
    document.getElementById('composer-voice-btn').addEventListener('click', () => {
        closeComposerMenu();
        voice.click();
    });

    function isEditable(element) {
        return element?.matches('textarea, input:not([type="file"]):not([type="button"]):not([type="checkbox"]):not([type="radio"]), [contenteditable="true"]');
    }
    function syncViewport() {
        viewportFrame = 0;
        if (!mobile()) {
            for (const property of ['--mobile-viewport-height', '--mobile-viewport-top', '--mobile-viewport-bottom']) root.style.removeProperty(property);
            root.classList.remove('mobile-keyboard-open');
            keyboardOpen = false;
            return;
        }
        const viewport = window.visualViewport;
        // Let the browser retain normal pinch zoom and panning.
        if (viewport && Math.abs(viewport.scale - 1) > .02) return;
        const height = viewport?.height ?? window.innerHeight;
        const top = viewport?.offsetTop ?? 0;
        const width = Math.round(viewport?.width ?? window.innerWidth);
        const orientation = window.screen.orientation?.angle ?? window.orientation ?? 0;
        const key = `${orientation}:${width}`;
        const baseline = Math.max(viewportBaselines.get(key) ?? window.innerHeight, height);
        viewportBaselines.set(key, baseline);
        keyboardOpen = baseline - height > Math.max(120, baseline * .18) && (isEditable(document.activeElement) || keyboardOpen);
        root.classList.toggle('mobile-keyboard-open', Boolean(keyboardOpen));
        root.style.setProperty('--mobile-viewport-height', `${height}px`);
        root.style.setProperty('--mobile-viewport-top', `${top}px`);
        root.style.setProperty('--mobile-viewport-bottom', `${Math.max(0, window.innerHeight - height - top)}px`);
        updateDockedLayout(shouldAutoStickToBottom);
        scheduleLatest();
    }
    function scheduleViewport() { if (!viewportFrame) viewportFrame = requestAnimationFrame(syncViewport); }
    window.visualViewport?.addEventListener('resize', scheduleViewport);
    window.visualViewport?.addEventListener('scroll', scheduleViewport);
    window.addEventListener('resize', scheduleViewport);
    window.addEventListener('orientationchange', scheduleViewport);
    window.addEventListener('pageshow', scheduleViewport);
    document.addEventListener('focusin', scheduleViewport);
    document.addEventListener('focusout', scheduleViewport);

    // A transcript can arrive without a native input event. Speech state updates
    // also keep the recording stop control visible until recording finishes.
    new MutationObserver(syncComposer).observe(voice, { attributes: true, attributeFilter: ['aria-pressed', 'aria-busy'] });
    new MutationObserver(syncComposer).observe(send, { attributes: true, attributeFilter: ['aria-busy'] });
    let lastInputWidth = 0;
    if (window.ResizeObserver) {
        new ResizeObserver(entries => {
            const width = entries[0].contentRect.width;
            if (width !== lastInputWidth) {
                lastInputWidth = width;
                if (mobile()) autoResizeTextarea(input);
            }
        }).observe(input);
        new ResizeObserver(() => {
            root.style.setProperty('--mobile-composer-height', `${Math.ceil(wrapper.getBoundingClientRect().height)}px`);
            scheduleLatest();
        }).observe(wrapper);
    }
    let wasMobile = mobile();
    function syncDevice() {
        syncComposer();
        syncReferences();
        syncMenu();
        if (mobile() || wasMobile) autoResizeTextarea(input);
        wasMobile = mobile();
        scheduleViewport();
        scheduleLatest();
    }
    mobileQuery.addEventListener('change', syncDevice);
    function showDraftReferences() {
        refsExpanded = true;
        syncReferences();
    }
    window.VeraMobileChat = { syncComposer, showDraftReferences, isMobileLayout: mobile, usesMobileInput: mobile };
    syncDevice();
})();
