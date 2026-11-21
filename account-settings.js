/* One settings surface; account state never crosses a login identity. */
(() => {
  'use strict';
  const categories = {preference:'學習偏好',goal:'學習目標',background:'背景',learning:'學習重點'};
  let overlay, dialog, state = {}, identity = '', revision = 0, returnFocus, previousOverflow;
  let settingsRequest = null, memories = [], memoryRequest = 0, summaryRequest = 0, saving = false, memoryQuery = '', memoryCategory = 'all';
  let legacyTheme; try { legacyTheme=localStorage.getItem('theme'); } catch (_) {}
  const session = () => typeof logoutInProgress !== 'undefined' && logoutInProgress ? '' : typeof sessionId === 'string' ? sessionId : '';
  const account = () => { try { return JSON.parse(localStorage.getItem('userInfo') || '{}').account || ''; } catch { return ''; } };
  const currentIdentity = () => JSON.stringify([session(),account()]);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  function action(text, fn, cls = '') {
    const b = node('button',text,'settings-action ' + cls); b.type = 'button';
    b.addEventListener('click',async () => { if (b.disabled) return; b.disabled = true; try { await fn(); } catch(e) { status(e.message,true); } finally { b.disabled = false; } }); return b;
  }
  async function api(path, method='GET', body) {
    const expected = currentIdentity(), token = session(); if (!token) throw new Error('請先登入。');
    const r = await fetch('/api' + path,{method,headers:{'X-Session-ID':token,...(body === undefined ? {} : {'Content-Type':'application/json'})},...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const data = await r.json();
    if (expected !== currentIdentity()) throw new Error('帳號已切換，請重新開啟設定。');
    if (!r.ok || data.code && data.code !== 200) throw new Error(typeof data.detail === 'string' ? data.detail : data.message || '操作未完成，請重試。');
    if(path==='/account/settings'&&method==='GET')return {...data.data,_theme_initialized:data.meta?.theme_initialized!==false};
    return data.data;
  }
  function status(message='',error=false) { if (!overlay) return; const n=overlay.querySelector('#settings-status'); n.textContent=message; n.classList.toggle('is-error',error); }
  function applyTheme(theme) {
    if (!['light','dark'].includes(theme)) return;
    document.documentElement.setAttribute('data-theme',theme);
    try { localStorage.setItem('theme',theme); } catch (_) {}
    if (typeof updateThemeIcon === 'function') updateThemeIcon(theme);
  }
  function sync() {
    if (!overlay) return;
    for (const input of overlay.querySelectorAll('[data-setting]')) {
      const key=input.dataset.setting;
      if (input.type === 'radio') input.checked = state[key] === input.value;
      else input.checked = state[key] === true;
      input.disabled = !identity || !('theme' in state) || saving || (account() === '0000' && ['auto_memory','use_memory'].includes(key));
    }
    overlay.querySelector('#settings-demo-note').hidden = account() !== '0000';
  }
  function resetIdentity() {
    const next = currentIdentity();
    if (next === identity) return false;
    identity = next; ++revision; ++memoryRequest; ++summaryRequest; settingsRequest = null; saving=false;state = {}; memories = [];
    window.WeChatConnection?.clear();
    if(overlay){resetMemoryFilters();const draft=document.getElementById("settings-memory-new");if(draft)draft.value="";status();}
    overlay?.querySelector('#settings-memory-list')?.replaceChildren();
    resetMemoryFilters();
    overlay?.querySelector('#settings-learning-body')?.replaceChildren();
    if (overlay) sync();
    return true;
  }
  async function load() {
    resetIdentity(); if (!session()) return;
    if (settingsRequest) return settingsRequest;
    const expected = identity, generation = revision;
    settingsRequest = api('/account/settings').then(async data => {
      if (identity !== expected || generation !== revision) return;
      const migrationKey='houkong.theme.migrated.v1:'+account();
      try {
        if (!localStorage.getItem(migrationKey)) {
          if (data._theme_initialized===false && ['light','dark'].includes(legacyTheme) && legacyTheme !== data.theme) data=await api('/account/settings','PATCH',{theme:legacyTheme});
          localStorage.setItem(migrationKey,'1');
        }
      } catch (_) { /* A denied browser cache must not prevent settings from loading. */ }
      if (identity !== expected || generation !== revision) return;
      state=data; applyTheme(state.theme); sync(); status();
      window.dispatchEvent(new CustomEvent('houkong:settings',{detail:{...state}}));
    }).catch(e => { if (identity === expected && generation === revision) status(e.message,true); })
      .finally(() => { if (identity === expected && generation === revision) settingsRequest=null; });
    return settingsRequest;
  }
  async function save(patch) {
    resetIdentity(); const expected=identity, generation=revision;
    const previous={...state};state={...state,...patch};saving=true;sync();
    try {
      const data=await api('/account/settings','PATCH',patch);
      if (identity !== expected || generation !== revision) return;
      state=data; applyTheme(state.theme); status('已儲存');
      window.dispatchEvent(new CustomEvent('houkong:settings',{detail:{...state}}));
      return state;
    } catch(error) { if(identity===expected&&generation===revision)state=previous;throw error;
    } finally { if(identity===expected&&generation===revision){saving=false;sync();} }
  }
  function settingRow(parent,key,label,description) {
    const row=node('label',null,'settings-row'), text=node('span',null,'settings-row-text');
    const name=node('span',label,'settings-label'); text.append(name);
    if(description) text.append(node('span',description,'settings-hint'));
    const input=node('input'); input.type='checkbox'; input.dataset.setting=key; input.setAttribute('aria-label',label);
    input.addEventListener('change',async()=>{ const value=input.checked; input.disabled=true; try { await save({[key]:value}); } catch(e) { sync(); status(e.message,true); } });
    row.append(text,input); parent.append(row);
  }
  function section(title,parent) { const s=node('section',null,'settings-section'); if(title) s.append(node('h4',title)); parent.append(s); return s; }
  // 通用頁下的二級頁面：入口行點擊後整頁切換，頂部「‹ 返回」回到通用。
  const subpages = {wechat:'微信連接',reviews:'審核與發布',learning:'學習數據'};
  const pages = ['general','memory',...Object.keys(subpages)];
  let currentPage = 'general';
  function entry(key,hint,parent) {
    const b=node('button',null,'settings-entry'); b.type='button'; b.id='settings-entry-'+key; b.setAttribute('aria-controls','settings-page-'+key);
    const text=node('span',null,'settings-row-text'); text.append(node('span',subpages[key],'settings-label')); if(hint) text.append(node('span',hint,'settings-hint'));
    const arrow=node('span','›','settings-entry-arrow'); arrow.setAttribute('aria-hidden','true'); b.append(text,arrow);
    b.addEventListener('click',()=>{ selectPage(key); document.getElementById('settings-back')?.focus(); });
    parent.append(b);
  }
  function subpage(key,parent) {
    const page=node('div',null,'settings-subpage'); page.id='settings-page-'+key; page.hidden=true;
    page.setAttribute('role','region'); page.setAttribute('aria-label',subpages[key]); parent.append(page); return page;
  }
  function build() {
    overlay=node('div',null,'account-settings'); overlay.id='account-settings'; overlay.hidden=true;
    dialog=node('section',null,'settings-window'); dialog.setAttribute('role','dialog'); dialog.setAttribute('aria-modal','true'); dialog.setAttribute('aria-labelledby','settings-title');
    const sidebar=node('aside',null,'settings-sidebar'); const heading=node('h2','設定'); heading.id='settings-title'; sidebar.append(heading);
    const nav=node('div',null,'settings-nav'); nav.setAttribute('role','tablist'); nav.setAttribute('aria-label','設定類別'); nav.setAttribute('aria-orientation','vertical');
    for(const [key,label,mark] of [['general','通用','⚙'],['memory','記憶','◈']]) {
      const tab=node('button',null,'settings-tab'); tab.type='button'; tab.id='settings-tab-' + key; tab.setAttribute('role','tab'); tab.setAttribute('aria-controls','settings-page-' + key); tab.dataset.page=key;
      const icon=node('span',mark); icon.setAttribute('aria-hidden','true'); tab.append(icon,node('span',label));
      tab.addEventListener('click',()=>selectPage(key));
      tab.addEventListener('keydown',e=>{ if(['ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Home','End'].includes(e.key)){ e.preventDefault(); const target=e.key === 'Home'?'general':e.key === 'End'?'memory':key === 'general'?'memory':'general'; selectPage(target); document.getElementById('settings-tab-'+target).focus(); } });
      nav.append(tab);
    }
    const mobile=matchMedia('(max-width:650px)'),orient=()=>nav.setAttribute('aria-orientation',mobile.matches?'horizontal':'vertical');orient();mobile.addEventListener('change',orient);
    sidebar.append(nav); const main=node('div',null,'settings-main'), top=node('header',null,'settings-top'), title=node('h3','通用'); title.id='settings-page-title';
    const close=node('button','×','settings-close'); close.type='button'; close.id='settings-close'; close.setAttribute('aria-label','關閉設定'); close.addEventListener('click',closeSettings);
    const back=node('button',null,'settings-back'); back.type='button'; back.id='settings-back'; back.hidden=true; back.setAttribute('aria-label','返回通用設定');
    back.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>';
    back.append(document.createTextNode('返回'));
    back.addEventListener('click',()=>{ const from=currentPage; selectPage('general'); document.getElementById('settings-entry-'+from)?.focus(); });
    const heading2=node('div',null,'settings-top-title'); heading2.append(back,title); top.append(heading2,close);
    const scroll=node('div',null,'settings-scroll'), feedback=node('p',null,'settings-status'); feedback.id='settings-status'; feedback.setAttribute('role','status'); feedback.setAttribute('aria-live','polite'); scroll.append(feedback);
    const general=node('div'), memory=node('div');
    for(const [key,page] of [['general',general],['memory',memory]]) { page.id='settings-page-'+key; page.setAttribute('role','tabpanel'); page.setAttribute('aria-labelledby','settings-tab-'+key); scroll.append(page); }
    const appearance=section('外觀',general), row=node('div',null,'settings-row'); row.append(node('span','主題','settings-label'));
    const choices=node('div',null,'settings-theme'); choices.setAttribute('role','group'); choices.setAttribute('aria-label','主題');
    for(const [value,label] of [['light','淺色'],['dark','深色']]) {
      const labelNode=node('label'),input=node('input'); input.type='radio'; input.name='settings-theme'; input.value=value; input.dataset.setting='theme';
      input.addEventListener('change',async()=>{ if(!input.checked)return; input.disabled=true; try { await save({theme:value}); } catch(e) { sync(); status(e.message,true); } }); labelNode.append(input,document.createTextNode(label)); choices.append(labelNode);
    }
    row.append(choices); appearance.append(row);
    const learning=section('學習偏好',general);
    settingRow(learning,'auto_practice','自動提供練習','學習化學時，適時用一道小題確認理解；可略過或暫停。');
    settingRow(learning,'auto_web_search','自動聯網','由 AI 判斷問題是否需要上網查；按下「搜尋模式」則一定聯網。');
    settingRow(learning,'auto_think','自動深度思考','由 AI 判斷問題是否需要先仔細推理；按下「思考模式」則一定思考。');
    const more=section('',general); more.classList.add('settings-entries');
    entry('wechat','連接自己的微信後，可在微信繼續小智會話。',more);
    entry('reviews','查看上傳資料的審核與發布狀態。',more);
    entry('learning','作答紀錄、錯題回顧與知識庫檢索。',more);
    const exit=section('',general); exit.classList.add('settings-logout-section');
    const logout=document.getElementById('logout-btn');
    if(logout) { logout.className='settings-action danger settings-logout'; logout.removeAttribute('title'); logout.replaceChildren(document.createTextNode('退出登入')); exit.append(logout); }
    const wechatPage=subpage('wechat',scroll), reviewPage=subpage('reviews',scroll), learningPage=subpage('learning',scroll);
    window.WeChatConnection?.mount(wechatPage);
    const review=document.getElementById('review-overlay');
    if(review) { reviewPage.append(review); const inner=review.querySelector('[role="dialog"]'); inner?.removeAttribute('role'); inner?.removeAttribute('aria-modal'); }
    const stats=node('div'); stats.id='settings-learning-body'; learningPage.append(stats);
    const learningActions=node('div',null,'settings-actions');
    learningActions.append(action('重新整理',loadSummary),action('清除作答紀錄',async()=>{ if(await confirmAction('清除所有練習的作答紀錄？題目與教材會保留。','清除作答紀錄')) { await api('/learning/data','DELETE'); window.ChatInteractions?.refresh(); await loadSummary(); status('作答紀錄已清除'); } },'danger'));
    learningPage.append(learningActions);
    const telemetry=node('section',null,'settings-retrieval'); telemetry.id='settings-retrieval';
    const metrics=document.getElementById('metrics-panel'); if(metrics) { metrics.removeAttribute('role'); metrics.removeAttribute('aria-modal'); metrics.removeAttribute('tabindex'); telemetry.append(metrics); const label=metrics.querySelector('#metrics-panel-title'); if(label)label.textContent='知識庫與檢索'; }
    learningPage.append(telemetry);
    const memoryOptions=section('對話記憶',memory);
    memoryOptions.append(node('p','記住新對話中的學習偏好與目標，在之後的聊天適時使用。','settings-hint'));
    settingRow(memoryOptions,'auto_memory','自動儲存記憶','自動整理啟用後的新對話，不回溯舊聊天。');
    settingRow(memoryOptions,'use_memory','回答時參考記憶','依本次問題使用相關記憶，可隨時關閉。');
    const demo=node('p','ICT 共用帳號不會自動儲存或讀取個人記憶。','settings-hint'); demo.id='settings-demo-note'; demo.hidden=true; memoryOptions.append(demo);
    const managed=section('已儲存的記憶',memory); managed.append(node('p','可新增、修改或刪除。刪除記憶後，小智不會再從記憶中讀取這項內容。','settings-hint'));
    const form=node('form',null,'settings-memory-form'),input=node('textarea'); input.id='settings-memory-new'; input.maxLength=500; input.placeholder='例如：希望先看例子，再學定義。'; input.setAttribute('aria-label','新增記憶內容');
    const formRow=node('div',null,'settings-actions'),category=node('select'); category.setAttribute('aria-label','記憶類型'); for(const [v,t] of Object.entries(categories))category.append(new Option(t,v));
    const submit=node('button','新增記憶','settings-action primary'); submit.type='submit'; formRow.append(category,submit); form.append(input,formRow);
    form.addEventListener('submit',async e=>{e.preventDefault();if(!input.value.trim())return; submit.disabled=true;try{const created=await api('/memories','POST',{content:input.value.trim(),category:category.value});input.value='';resetMemoryFilters();await loadMemories(created.id);status('記憶已新增');}catch(err){status(err.message,true);}finally{submit.disabled=false;}});
    managed.append(form); const memoryActions=node('div',null,'settings-actions'); memoryActions.append(action('重新整理',loadMemories),action('刪除全部記憶',async()=>{ if(await confirmAction('刪除全部已儲存的記憶？此操作無法還原。','刪除全部記憶')){await api('/memories','DELETE');await loadMemories();status('所有記憶已刪除');} },'danger')); managed.append(memoryActions);
    const tools=node('div',null,'settings-memory-tools'),search=node('input'),filter=node('select');search.type='search';search.id='settings-memory-search';search.placeholder='搜尋記憶';search.setAttribute('aria-label','搜尋記憶');filter.id='settings-memory-filter';filter.setAttribute('aria-label','篩選記憶類型');filter.append(new Option('全部類型','all'));for(const [value,label]of Object.entries(categories))filter.append(new Option(label,value));
    search.addEventListener('input',()=>{memoryQuery=search.value;renderMemories();});filter.addEventListener('change',()=>{memoryCategory=filter.value;renderMemories();});tools.append(search,filter);managed.append(tools);const count=node('p',null,'settings-hint');count.id='settings-memory-count';managed.append(count);
    const list=node('div',null,'settings-memory-list'); list.id='settings-memory-list'; list.setAttribute('aria-live','polite'); managed.append(list);
    main.append(top,scroll);dialog.append(sidebar,main);overlay.append(dialog);document.body.append(overlay);
    overlay.addEventListener('click',e=>{if(e.target===overlay)closeSettings();});
    overlay.addEventListener('keydown',e=>{
      if(document.querySelector('.file-preview-overlay.active, #custom-dialog-overlay.show'))return;
      if(e.key==='Escape'){e.preventDefault();e.stopPropagation();closeSettings();return;}
      if(e.key!=='Tab')return;
      const items=[...dialog.querySelectorAll('button,input,select,textarea,summary,[tabindex="0"]')].filter(n=>!n.disabled&&n.tabIndex>=0&&n.getClientRects().length);
      const first=items[0],last=items[items.length-1];
      if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
    });
    document.getElementById('open-account-settings')?.addEventListener('click',()=>open()); selectPage('general');
  }
  function selectPage(key) {
    if(!pages.includes(key))key='general';
    const leaving=currentPage; currentPage=key;
    const sub=key in subpages, tabKey=key==='memory'?'memory':'general';
    if(key!=='wechat')window.WeChatConnection?.suspend();
    for(const tab of overlay.querySelectorAll('[role="tab"]')){const selected=tab.dataset.page===tabKey;tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;}
    for(const page of pages){const n=document.getElementById('settings-page-'+page);if(n)n.hidden=page!==key;}
    document.getElementById('settings-page-title').textContent=sub?subpages[key]:key==='memory'?'記憶':'通用';
    document.getElementById('settings-back').hidden=!sub;
    const review=document.getElementById('review-overlay'),metrics=document.getElementById('metrics-panel');
    if(review)review.hidden=key!=='reviews'||overlay.hidden;
    if(metrics)metrics.hidden=key!=='learning'||overlay.hidden;
    if(leaving!==key){status();const box=overlay.querySelector('.settings-scroll');if(box)box.scrollTop=0;}
    if(overlay.hidden)return;
    if(key==='reviews'){if(typeof hydrateReviewData==='function')hydrateReviewData();if(typeof renderReviewPanel==='function')renderReviewPanel();if(typeof loadReviewFiles==='function')void loadReviewFiles();}
    else if(key==='learning'){void loadSummary();void window.prefetchMetricsPanel?.();}
    else if(key==='wechat')window.WeChatConnection?.open();
    else if(key==='memory')void loadMemories();
  }
  function open(page='general',part) {
    if(!overlay||!session())return;
    resetIdentity(); const fresh=overlay.hidden;
    if(fresh){returnFocus=document.activeElement;previousOverflow=document.body.style.overflow;overlay.hidden=false;document.body.style.overflow='hidden';}
    // 舊呼叫 open('general','reviews'|'learning'|'retrieval') 直接進入對應二級頁面。
    const target=part==='retrieval'?'learning':(part in subpages?part:page);
    selectPage(target);sync();void load();
    if(fresh)document.getElementById(target in subpages?'settings-back':'settings-close').focus();
  }
  function closeSettings() {
    if(!overlay||overlay.hidden)return;
    window.WeChatConnection?.suspend();
    overlay.hidden=true;document.body.style.overflow=previousOverflow || '';
    for(const id of ['review-overlay','metrics-panel']){const n=document.getElementById(id);if(n)n.hidden=true;}
    if(returnFocus?.isConnected)returnFocus.focus();returnFocus=null;
  }
  async function confirmAction(message,title) {
    const result=typeof customConfirm==='function'?await customConfirm(message,title):window.confirm(message);
    if(overlay&&!overlay.hidden&&!overlay.contains(document.activeElement))document.getElementById('settings-close').focus();
    return result;
  }
  async function loadMemories(highlight) {
    const expected=currentIdentity(),sequence=++memoryRequest;
    try {
      const data=await api('/memories');if(expected!==currentIdentity()||sequence!==memoryRequest)return;
      memories=Array.isArray(data.memories)?data.memories:[];renderMemories(highlight);
    } catch(e){if(expected===currentIdentity()&&sequence===memoryRequest)status(e.message,true);}
  }
  function resetMemoryFilters(){memoryQuery='';memoryCategory='all';const search=document.getElementById('settings-memory-search'),filter=document.getElementById('settings-memory-filter');if(search)search.value='';if(filter)filter.value='all';}
  function renderMemories(highlight) {
    const list=document.getElementById('settings-memory-list'),hadFocus=list.contains(document.activeElement);list.replaceChildren();
    const query=memoryQuery.normalize('NFKC').toLocaleLowerCase().trim(),filtered=memories.filter(m=>(memoryCategory==='all'||m.category===memoryCategory)&&(!query||m.content.normalize('NFKC').toLocaleLowerCase().includes(query)));
    document.getElementById('settings-memory-count').textContent='顯示 '+filtered.length+' / '+memories.length+' 則記憶';
    if(!filtered.length){list.append(node('p',memories.length?'沒有符合條件的記憶。':'尚未儲存記憶。','settings-hint'));if(hadFocus)document.getElementById('settings-close').focus();return;}
    for(const m of filtered){
      const card=node('article',null,'settings-memory-card');card.dataset.memoryId=m.id;card.tabIndex=-1;
      const text=node('p',m.content),meta=node('div',categories[m.category]||'記憶','settings-memory-meta'),buttons=node('div',null,'settings-actions');
      const date=new Date(m.updated_at||m.created_at||'');if(!Number.isNaN(date.valueOf()))meta.append(document.createTextNode(' · 更新於 '+new Intl.DateTimeFormat('zh-Hant',{dateStyle:'medium',timeStyle:'short',timeZone:'Asia/Macau'}).format(date)));
      if(m.source?.conversation_id)meta.append(action('來源對話',async()=>{closeSettings();if(typeof switchToConversation==='function')await switchToConversation(m.source.conversation_id);},'settings-memory-source'));
      else if(m.source_deleted)meta.append(document.createTextNode(' · 原聊天已刪除'));
      buttons.append(action('編輯',()=>{
        const editor=node('form',null,'settings-memory-edit'),input=node('textarea');input.value=m.content;input.maxLength=500;input.setAttribute('aria-label','編輯記憶內容');
        const category=node('select');category.setAttribute('aria-label','編輯記憶類型');for(const [value,label]of Object.entries(categories))category.append(new Option(label,value));category.value=m.category;
        const controls=node('div',null,'settings-actions'),saveButton=node('button','儲存','settings-action primary');saveButton.type='submit';controls.append(saveButton,action('取消',()=>renderMemories(m.id)));editor.append(input,category,controls);card.replaceChildren(editor);input.focus();
        editor.addEventListener('submit',async e=>{e.preventDefault();if(!input.value.trim())return;saveButton.disabled=true;try{await api('/memories/'+encodeURIComponent(m.id),'PATCH',{content:input.value.trim(),category:category.value});await loadMemories(m.id);status('記憶已更新');}catch(err){status(err.message,true);saveButton.disabled=false;}});
      }),action('刪除',async()=>{if(await confirmAction('刪除這則記憶？','刪除記憶')){await api('/memories/'+encodeURIComponent(m.id),'DELETE');await loadMemories();status('記憶已刪除');}},'danger'));
      card.append(text,meta,buttons);list.append(card);
      if(highlight===m.id){card.classList.add('is-highlighted');requestAnimationFrame(()=>{card.scrollIntoView({block:'nearest'});card.focus({preventScroll:true});});}
    }
    if(hadFocus&&!highlight)list.querySelector('.settings-memory-card')?.focus({preventScroll:true});
  }
  async function loadSummary() {
    const expected=currentIdentity(),sequence=++summaryRequest,body=document.getElementById('settings-learning-body');
    try{
      const data=await api('/learning/summary');if(expected!==currentIdentity()||sequence!==summaryRequest)return;
      body.replaceChildren();if(data.available===false){body.append(node('p','共用帳號不累積個人學習數據。','settings-hint'));return;}
      const stats=node('div',null,'settings-learning-grid');for(const [label,value]of [['已作答',data.total||0],['答對',data.correct||0],['正確率',data.accuracy==null?'—':Math.round(data.accuracy)+'%']]){const cell=node('div',null,'settings-learning-stat');cell.append(node('span',label),node('strong',String(value)));stats.append(cell);}body.append(stats);
      if(!data.total)body.append(node('p','完成練習後，這裡會顯示你的作答紀錄。','settings-hint'));
      if(data.needs_practice?.length){body.append(node('h4','可以再練習的重點'));const list=node('ul',null,'settings-learning-list');for(const item of data.needs_practice.slice(0,8))list.append(node('li',item.topic+' · '+item.wrong+' 題仍需鞏固'));body.append(list);}
      if(data.wrong_questions?.length)body.append(learningRows('錯題回顧',data.wrong_questions));
      if(data.recent?.length)body.append(learningRows('最近作答',data.recent));
    }catch(e){if(expected===currentIdentity()&&sequence===summaryRequest){body.replaceChildren(node('p',e.message,'settings-status is-error'));}}
  }
  function learningRows(title,rows){
    const details=node('details',null,'settings-disclosure');details.append(node('summary',title+'（'+rows.length+'）'));const list=node('ul',null,'settings-learning-list');
    for(const item of rows.slice(0,20)){
      const li=node('li');li.append(node('div',(item.correct?'✓ ':'○ ')+(item.stem||item.topic||'練習題')),node('p',(item.topic?item.topic+' · ':'')+(item.correct?'回答正確':'需要再鞏固'),'settings-hint'));
      const buttons=node('div',null,'settings-actions');
      if(item.task_id)buttons.append(action('查看解析',()=>openLearning(item,false)),action('重新練習',()=>openLearning(item,true)));
      else if(item.explanation)li.append(node('p',item.explanation,'settings-hint'));
      li.append(buttons);list.append(li);
    }
    details.append(list);return details;
  }
  async function openLearning(item,newAttempt){
    if(!window.BackgroundTasks?.openPractice){status('練習介面暫時未能載入，請重新整理。',true);return;}
    closeSettings();await window.BackgroundTasks.openPractice(item.task_id,{attemptId:item.attempt_id,newAttempt});
  }
  window.AccountSettings={open,close:closeSettings,load,save,get:()=>({...state}),owns:n=>!!overlay?.contains(n),isOpen:()=>!!overlay&&!overlay.hidden,
    collapse(part){if(part==='reviews'&&currentPage==='reviews'&&overlay&&!overlay.hidden)selectPage('general');},
    page:()=>currentPage,
    async showMemory(id){resetMemoryFilters();open('memory');await loadMemories(id);},
    clear(){closeSettings();window.WeChatConnection?.clear();identity='';++revision;++memoryRequest;++summaryRequest;state={};memories=[];settingsRequest=null;saving=false;resetMemoryFilters();const draft=document.getElementById("settings-memory-new");if(draft)draft.value="";status();document.getElementById('settings-memory-list')?.replaceChildren();document.getElementById('settings-learning-body')?.replaceChildren();},
    refreshLearning(){if(overlay&&!overlay.hidden&&currentPage==='learning')void loadSummary();}
  };
  function init(){build();void load();window.addEventListener('houkong:identity',()=>{resetIdentity();void load();});window.addEventListener('houkong:learning',()=>window.AccountSettings.refreshLearning());}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();
