/* Persistent tasks live independently of dialogs and of the current chat. */
(() => {
  'use strict';
  const state = { tasks: [], transfers: new Map(), filter: 'all', selected: null, detail: null,
    attempt: null, ownerSession: '', notice: '', timer: null, saving: Promise.resolve(), pending: {}, grading: false };
  const active = t => ['queued', 'running'].includes(t.status);
  const session = () => typeof logoutInProgress !== 'undefined' && logoutInProgress ? '' : typeof sessionId === 'string' ? sessionId : '';
  const apiBase = () => typeof API_BASE === 'string' ? API_BASE : '/api';
  const safeLink = value => { try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password ? u.href : null; } catch (_) { return null; } };
  const el = (tag, text, className) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (className) n.className = className; return n; };
  const rid = () => crypto.randomUUID();
  const draftClient = rid();let draftSequence=0,answerSequence=0;const draftLatest=new Map();
  let panel, content, entry, badge, lastFocus;
  let practiceOpenEpoch = 0;
  let pollTimer = null, refreshPending = null, pollEpoch = 0, pollFailures = 0;
  async function api(path, method = 'GET', body, options = {}) {
    const identity = session();
    if (!identity) throw new Error('請先登入');
    const response = await fetch(apiBase() + path, { method, signal:options.signal, keepalive:method==='PATCH', headers: { 'X-Session-ID': identity, ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}) }, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await response.json().catch(() => ({}));
    if (identity !== session()) throw new Error('登入帳號已變更');
    if (!response.ok || data.code && data.code !== 200) throw new Error(typeof data.detail === 'string' ? data.detail : data.message || '操作未完成，請重試');
    return data.data;
  }
  function toast(text) { if (typeof showToast === 'function') showToast(text, 'info'); }
  function button(text, action, primary = false) {
    const b = el('button', text, 'task-button' + (primary ? ' primary' : ''));
    b.type = 'button';
    b.addEventListener('click', async () => { b.disabled = true; try { await action(); } catch (e) { report(e); } finally { if (b.isConnected) b.disabled = false; } });
    return b;
  }
  function icon(name) {
    const paths = {
      tasks: ['M9 7h8M9 12h8M9 17h5M6 7h.01M6 12h.01M6 17h.01', 'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z'],
      minimize: ['M6 12h12'],
      upload: ['M12 16V4m-4 4 4-4 4 4', 'M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4'],
      quiz: ['M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z', 'M14 3v6h6M8 13h8M8 17h5'],
      back: ['m10 6-6 6 6 6M4 12h16'],
      empty: ['M8 4h8M9 2h6v4H9Z', 'M7 4H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-1', 'm8 14 3 3 5-6']
    };
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({viewBox:'0 0 24 24', fill:'none', stroke:'currentColor', 'stroke-width':'1.8', 'stroke-linecap':'round', 'stroke-linejoin':'round', 'aria-hidden':'true'})) svg.setAttribute(key, value);
    for (const d of paths[name] || paths.tasks) {
      const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path);
    }
    return svg;
  }
  function report(error) {
    state.notice = error.message || String(error);
    const note = panel.querySelector('.task-notice');
    if (note) { note.textContent = state.notice; note.classList.add('task-error'); }
    toast(state.notice);
  }
  function open() { lastFocus = document.activeElement; panel.hidden = false; renderBadge(); render(); panel.querySelector('button').focus(); refresh(); }
  async function hide() { void flush().catch(report); panel.hidden = true; renderBadge(); scheduleRefresh(); if (lastFocus?.isConnected) lastFocus.focus(); }
  function renderBadge() {
    const ongoing = state.tasks.filter(active).length + state.transfers.size;
    const waiting = state.tasks.filter(t => t.status === 'waiting_confirmation').length;
    const unread = state.tasks.filter(t => t.status === 'succeeded' && !t.seen).length;
    const failed = state.tasks.filter(t => ['failed', 'interrupted'].includes(t.status) && !t.seen).length;
    badge.hidden = !(ongoing || waiting || unread || failed);
    const summary = [ongoing && `${ongoing} 個進行中`, waiting && `${waiting} 個待確認`, unread && `${unread} 個新完成`, failed && `${failed} 個需處理`].filter(Boolean).join('，');
    entry.title = summary ? `任務管理：${summary}` : '任務管理';
    entry.setAttribute('aria-label', entry.title);
    entry.setAttribute('aria-expanded', String(!panel.hidden));
  }
  function invalidateRefresh() {
    ++pollEpoch;
    clearTimeout(pollTimer); pollTimer = null;
  }
  function stopPolling() {
    invalidateRefresh(); pollFailures = 0;
    refreshPending?.controller.abort(); refreshPending = null;
  }
  function scheduleRefresh() {
    clearTimeout(pollTimer); pollTimer = null;
    if (!panel || !session() || refreshPending) return;
    let delay;
    if (state.tasks.some(active)) delay = document.hidden ? 10000 : 2000;
    else if (!document.hidden && (!panel.hidden || pollFailures)) delay = pollFailures ? 2000 : 30000;
    else return;
    if (pollFailures) delay = Math.max(delay, Math.min(30000, 2000 * 2 ** Math.min(pollFailures, 4)));
    pollTimer = setTimeout(() => { pollTimer = null; void refresh(); }, delay);
  }
  function refresh() {
    if (!panel) return Promise.resolve();
    const identity = session();
    if (!identity) { stopPolling(); state.ownerSession = ''; state.tasks = []; state.detail = null; state.attempt = null; state.selected = null; panel.hidden = true; renderBadge(); return Promise.resolve(); }
    if (identity !== state.ownerSession) {
      stopPolling();
      state.ownerSession = identity; state.tasks = []; state.detail = null; state.attempt = null; state.selected = null; state.pending = {}; state.notice = '';
    }
    if (refreshPending) {
      // Mutations invalidate an old snapshot, then request one fresh snapshot after it finishes.
      return refreshPending.epoch === pollEpoch ? refreshPending.promise : refreshPending.promise.then(() => refresh());
    }
    clearTimeout(pollTimer); pollTimer = null;
    const job = { identity, epoch:pollEpoch, controller:new AbortController(), promise:null };
    refreshPending = job;
    job.promise = readTasks(job).finally(() => {
      if (refreshPending === job) { refreshPending = null; scheduleRefresh(); }
    });
    return job.promise;
  }
  async function readTasks(job) {
    const {identity, epoch, controller} = job;
    try {
      const tasks = await api('/tasks', 'GET', undefined, {signal:controller.signal});
      if (identity !== session() || epoch !== pollEpoch) return;
      pollFailures = 0;
      const old = new Map(state.tasks.map(t => [t.id, t.status]));
      state.tasks = tasks;
      for (const task of tasks) if (old.has(task.id) && old.get(task.id) !== task.status && task.status === 'succeeded') toast(`${task.name}：已完成，可在任務管理查看`);
      renderBadge();
      if (!panel.hidden && !state.attempt) {
        if (state.selected) {
          const current = tasks.find(t => t.id === state.selected);
          if (current && current.revision !== state.detail?.revision && document.activeElement?.closest('.task-draft') == null) {
            const detail = await api('/tasks/' + current.id, 'GET', undefined, {signal:controller.signal});
            if (identity !== session() || epoch !== pollEpoch) return;
            if (state.selected === current.id) state.detail = detail;
          }
        }
        if (!document.activeElement?.closest('.task-draft')) render();
      }
    } catch (error) {
      if (!controller.signal.aborted && identity === session() && epoch === pollEpoch) pollFailures += 1;
      // Keep known tasks through a transient failure; the scheduler backs off.
    }
  }
  function statusLabel(t) { return ({ transferring: '傳輸中', queued: '排隊中', running: '處理中', waiting_confirmation: '待確認', succeeded: '已完成', failed: '失敗', interrupted: '已中斷', cancelled: '已取消' })[t.status] || t.status; }
  function progress(parent, task) {
    parent.append(el('div', task.stage || '', 'task-meta'));
    if (active(task) || task.status === 'transferring') {
      const bar = el('progress');
      if (Number.isFinite(task.total) && task.total > 0) {
        bar.max = task.total; bar.value = Math.min(task.completed || 0, task.total);
        const percent = Math.floor(bar.value / task.total * 100);
        parent.append(el('div', `${task.completed || 0} / ${task.total} ${task.unit || ''} · ${percent}%`, 'task-meta'));
      }
      bar.setAttribute('aria-label', task.stage || '處理進度'); parent.append(bar);
    }
    if (task.error) parent.append(el('div', task.error, 'task-error task-notice'));
  }
  async function view(task) {
    await flush(); state.selected = task.id; state.attempt = null; state.notice = '';
    state.detail = await api('/tasks/' + task.id); await api('/tasks/' + task.id + '/seen', 'POST');
    invalidateRefresh(); render(); await refresh();
  }
  async function download(task, answers) {
    const identity = session();
    const response = await fetch(apiBase() + `/tasks/${task.id}/pdf?answers=${answers}`, { headers: { 'X-Session-ID': identity } });
    if (!response.ok) { const value = await response.json(); throw new Error(value.detail || 'PDF 下載失敗，可重試'); }
    const blob = await response.blob(); if (identity !== session()) return;
    const url = URL.createObjectURL(blob), link = el('a'); link.href = url; link.download = task.name.replace(/\.[^.]+$/, '') + (answers ? '_答案解析.pdf' : '_試卷.pdf'); link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  async function remove(task) {
    if (active(task)) await api('/tasks/' + task.id + '/cancel', 'POST');
    await api('/tasks/' + task.id, 'DELETE');
    invalidateRefresh();
    if (state.selected === task.id) { state.selected = null; state.detail = null; state.attempt = null; }
    state.notice = '已刪除任務及作答記錄'; await refresh(); render();
  }
  function actions(parent, task, detailed = false) {
    const row = el('div', null, 'task-actions');
    if (!detailed) row.append(button(task.status === 'succeeded' ? task.kind === 'quiz' ? '查看／答題' : '查看結果' : '查看進度', () => view(task), true));
    if (task.kind === 'quiz' && task.status === 'succeeded') {
      row.append(button('下載試卷 PDF', () => download(task, false)), button('答案解析 PDF', () => download(task, true)));
    }
    if(task.kind==='upload' && task.committed_file_id) row.append(button('下載原檔',async()=>{
      const identity=session(), response=await fetch(apiBase()+`/files/${task.committed_file_id}/download`,{headers:{'X-Session-ID':identity}});
      if(!response.ok)throw new Error('原檔暫時無法下載，請確認資料存取權限');
      const blob=await response.blob();if(identity!==session())return;
      const url=URL.createObjectURL(blob),link=el('a');link.href=url;link.download=task.name;link.click();setTimeout(()=>URL.revokeObjectURL(url),30000);
    }));
    if (task.status === 'failed' || task.status === 'interrupted') row.append(button(task.status==='interrupted'?'繼續任務':'重試', async () => { await api('/tasks/' + task.id + (task.status==='interrupted'?'/resume':'/retry'), 'POST'); invalidateRefresh(); state.selected = null; state.detail = null; await refresh(); }));
    if(active(task)&&task.kind==='quiz')row.append(button('暫停任務',async()=>{await api('/tasks/'+task.id+'/pause','POST');invalidateRefresh();await refresh();}));
    if (active(task)) row.append(button('取消任務', async () => { await api('/tasks/' + task.id + '/cancel', 'POST'); invalidateRefresh(); await refresh(); }));
    const removeButton = button(active(task) ? '取消並刪除' : '刪除任務', () => remove(task));
    removeButton.classList.add('danger'); row.append(removeButton);
    parent.append(row);
  }
  function card(task) {
    const node = el('section', null, 'task-card'); node.dataset.taskId = task.id;
    node.append(taskHeading(task));
    progress(node, task); actions(node, task); return node;
  }
  function taskHeading(task, detailed = false) {
    const heading = el('div', null, detailed ? 'task-card-heading task-detail-heading' : 'task-card-heading');
    const mark = el('span', null, 'task-kind-icon'); mark.append(icon(task.kind === 'quiz' ? 'quiz' : 'upload'));
    const text = el('div', null, 'task-card-title');
    const status = el('span', `${task.kind === 'quiz' ? '出題' : '上傳'} · ${statusLabel(task)}`, 'task-status'); status.dataset.status = task.status;
    text.append(el('h3', task.name), status); heading.append(mark, text); return heading;
  }
  function render() {
    if (!content || panel.hidden) return;
    panel.classList.toggle('is-practice', !!state.attempt);
    content.classList.toggle('task-detail', !!(state.detail && state.selected));
    for (const tab of panel.querySelectorAll('[data-filter]')) {
      const selected = tab.dataset.filter === state.filter;
      tab.classList.toggle('selected', selected); tab.setAttribute('aria-pressed', String(selected));
    }
    // Preserve live input elements while polling; typing must never lose its caret.
    content.replaceChildren();
    const note = el('div', state.notice, 'task-notice'); note.setAttribute('role', 'status'); note.setAttribute('aria-live', 'polite'); content.append(note);
    if (state.detail && state.selected) { renderDetail(); return; }
    for (const transfer of state.transfers.values()) {
      const node = el('section', null, 'task-card'); node.append(taskHeading({...transfer, kind:'upload', status:transfer.error ? 'failed' : 'transferring'})); progress(node, transfer);
      if (transfer.error) node.append(button('重新傳送', () => sendFile(transfer.file, transfer.id)));
      node.append(button('取消傳輸', () => { transfer.xhr?.abort(); state.transfers.delete(transfer.id); renderBadge(); render(); })); content.append(node);
    }
    const filtered = state.tasks.filter(t => state.filter === 'all' || state.filter === 'active' && (active(t) || t.status === 'waiting_confirmation') || state.filter === 'completed' && t.status === 'succeeded' || state.filter === 'failed' && ['failed', 'interrupted', 'cancelled'].includes(t.status));
    for (const task of filtered) content.append(card(task));
    if (!filtered.length && !state.transfers.size) {
      const empty = el('div', null, 'task-empty'); empty.append(icon('empty'), el('h3', state.filter === 'all' ? '暫時沒有任務' : '目前沒有這類任務'), el('p', '上傳檔案或建立練習後，可在這裡查看進度。'));
      content.append(empty);
    }
  }
  function renderDetail() {
    const task = state.detail;
    const back = button('全部任務', async () => { await flush(); state.selected = null; state.detail = null; state.attempt = null; render(); });
    back.classList.add('task-back'); back.prepend(icon('back')); content.append(back, taskHeading(task, true));
    progress(content, task); actions(content, task, true);
    if (task.kind === 'upload' && ['waiting_confirmation', 'running', 'queued'].includes(task.status) && !task.committed_file_id) renderDraft(task);
    if (task.kind === 'quiz' && task.status === 'succeeded') {
      if (state.attempt) { renderPractice(task); return; }
      const modeRow = el('div', null, 'task-mode'), modes = el('select'); modes.setAttribute('aria-label', '答題模式');
      modes.append(new Option('即時反饋', 'instant'), new Option('整卷提交', 'exam'));
      modeRow.append(modes, button('開始新練習', () => begin(task, modes.value), true)); content.append(modeRow);
      content.append(button('繼續上次作答', async () => {
        const list = await api(`/tasks/${task.id}/attempts`);
        if (!list.length) { await begin(task, modes.value); return; }
        state.attempt = list[0]; state.pending = {}; render();
      }));
      content.append(el('p', '即時模式：點選選項或填空後按 Enter 判題。整卷模式：完成後一起提交。', 'task-meta'));
    }
  }
  function renderDraft(task) {
    const form = el('form', null, 'task-draft'); form.addEventListener('submit', event => event.preventDefault());
    task.draft ||= {};
    const touched = new Set(task.touched_fields || []), fieldsByName = new Map(), badges = new Map();
    let latestSave = 0;
    const evidence = el('details', null, 'task-classification-evidence');
    evidence.id = 'task-classification-evidence-' + task.id;
    const evidenceSummary = el('summary', '分類依據'), evidenceBody = el('div', null, 'task-evidence-body');
    evidence.append(evidenceSummary, evidenceBody); content.append(evidence);
    const fieldNames = {audience:'歸屬', subject:'科目', study_period:'學習階段', material_type:'資料類型'};
    const isManual = key => touched.has(key) || key === 'subject' && touched.has('subject_custom') || key === 'material_type' && touched.has('material_custom');
    function sourceFor(key) {
      if (isManual(key)) return {kind:'manual', text:'你已修改此欄位，後續 AI 建議不會覆蓋。'};
      const item = task.classification_evidence?.[key];
      if (item && ['filename','text'].includes(item.source) && typeof item.quote === 'string' && item.quote.trim()) return {kind:'ai', text:item.quote, source:item.source};
      const fallback = task.classification_defaults?.[key];
      return typeof fallback === 'string' && fallback ? {kind:'default', text:fallback} : {kind:'unknown', text:'目前沒有可核對的分類依據，請確認此欄位。'};
    }
    function renderEvidence() {
      evidenceBody.replaceChildren();
      for (const [key, label] of Object.entries(fieldNames)) {
        const info = sourceFor(key), row = el('div', null, 'task-evidence-row');
        row.append(el('strong', label), el('span', info.kind === 'ai' ? (info.source === 'filename' ? '檔案名' : '正文') : info.kind === 'manual' ? '人工設定' : info.kind === 'default' ? '預設值' : '待確認', 'task-evidence-source'));
        row.append(el(info.kind === 'ai' ? 'blockquote' : 'p', info.text)); evidenceBody.append(row);
        const badge = badges.get(key);
        if (badge) { badge.hidden = info.kind !== 'ai' && info.kind !== 'default'; badge.textContent = info.kind === 'default' ? '預設值' : 'AI 建議'; badge.title = info.kind === 'default' ? info.text : '查看此欄位的分類依據'; badge.setAttribute('aria-expanded', String(evidence.open)); }
      }
    }
    const preview = el('div', null, 'task-filename-preview');
    preview.append(el('span', '儲存後的檔案名稱', 'task-preview-label'));
    const previewName = el('strong', task.filename_preview || '分類完成後顯示檔案名稱');
    previewName.setAttribute('aria-live', 'polite'); previewName.setAttribute('aria-atomic', 'true');
    preview.append(previewName, el('span', '同名檔案會在儲存時自動加上序號。', 'task-preview-hint'));
    const profile = (() => { try { return JSON.parse(localStorage.getItem('userInfo') || '{}'); } catch { return {}; } })();
    const fields = {
      audience: ['歸屬', [['personal', '個人'], ['class', '班級'], ['grade', '年級'], ['school', '學校']]],
      study_period: ['學習階段', [['first','第一段'],['second','第二段'],['third','第三段'],['fourth','第四段'],['none','無學習階段']]],
      subject: ['科目', Object.entries({ chinese:'語文',math:'數學',english:'英語',physics:'物理',chemistry:'化學',biology:'生物',civics:'公民',history:'歷史',geography:'地理',computer:'電腦',other:'其他' })],
      material_type: ['資料類型', [['quiz','小測'],['unified_test','統測'],['review_paper','複習卷子'],['exam_paper','統測考試卷'],['exam_outline','考試提綱'],['courseware','課堂課件'],['homework','課後作業'],['other','其他']]],
      notes: ['備註（最多5字）', null]
    };
    if (profile.is_teacher || /teacher|老師|教师|教師/.test(profile.role || '')) fields.target_class = ['發布班級', (profile.publish_classes || []).map(v => [v,v])];
    function updateVisibility() {
      for (const [parent, custom] of [['subject','subject_custom'],['material_type','material_custom']]) {
        const control = fieldsByName.get(custom); if (!control) continue;
        control.group.hidden = task.draft[parent] !== 'other'; control.input.disabled = control.group.hidden;
      }
      const target = fieldsByName.get('target_class');
      if (target) { target.group.hidden = !['class','grade'].includes(task.draft.audience); target.input.disabled = target.group.hidden; }
    }
    async function saveField(key, value) {
      task.draft[key] = value; touched.add(key); task.touched_fields = [...touched];
      const values = {[key]:value};
      if (key === 'subject' || key === 'material_type') {
        const custom = key === 'subject' ? 'subject_custom' : 'material_custom';
        values[custom] = value === 'other' ? fieldsByName.get(custom).input.value : '';
      }
      updateVisibility(); renderEvidence();
      draftLatest.set(task.id,{...draftLatest.get(task.id),...values});
      const sequence = ++draftSequence; latestSave = sequence;
      state.saving = state.saving.catch(() => {}).then(() => api(`/tasks/${task.id}/draft`, 'PATCH', {values,client_id:draftClient,sequence}));
      try {
        const updated = await state.saving;
        if (sequence !== latestSave || !form.isConnected) return;
        if (typeof updated?.filename_preview === 'string') { task.filename_preview = updated.filename_preview; previewName.textContent = updated.filename_preview; }
        if (updated?.classification_evidence) task.classification_evidence = updated.classification_evidence;
        if (updated?.classification_defaults) task.classification_defaults = updated.classification_defaults;
        renderEvidence();
      } catch(error) { report(error); }
    }
    for (const [key, [label, choices]] of Object.entries(fields)) {
      const group = el('div', null, 'task-field'), field = el('label', label), labelRow = el('div', null, 'task-field-label'), input = el(choices ? 'select' : 'input'); input.name = key;
      input.id = `task-${task.id}-${key}`; field.htmlFor = input.id; labelRow.append(field);
      if (fieldNames[key]) {
        const badge = button('AI 建議', () => { evidence.open = !evidence.open; }); badge.className = 'task-ai-label'; badge.hidden = true; badge.setAttribute('aria-controls', evidence.id);
        badge.addEventListener('click', event => event.preventDefault()); labelRow.append(badge); badges.set(key, badge);
      }
      if (choices) { input.append(new Option('請選擇', ''), ...choices.map(([v,t]) => new Option(t,v))); }
      else input.maxLength = key === 'notes' ? 5 : 50;
      input.value = task.draft[key] || ''; group.append(labelRow, input); form.append(group); fieldsByName.set(key, {group,input});
      input.addEventListener(choices ? 'change' : 'input', () => { void saveField(key, input.value); });
      if (key === 'subject' || key === 'material_type') {
        const customKey = key === 'subject' ? 'subject_custom' : 'material_custom';
        const customGroup = el('div', null, 'task-custom-field'), customLabel = el('label', key === 'subject' ? '其他科目名稱（可留空）' : '其他資料類型名稱（可留空）'), customInput = el('input');
        customInput.name = customKey; customInput.maxLength = 50; customInput.value = task.draft[customKey] || ''; customInput.placeholder = '留空時不顯示此分類';
        customLabel.append(customInput); customGroup.append(customLabel); group.append(customGroup); fieldsByName.set(customKey, {group:customGroup,input:customInput});
        customInput.addEventListener('input', () => { void saveField(customKey, customInput.value); });
      }
    }
    evidence.addEventListener('toggle', () => { for (const badge of badges.values()) badge.setAttribute('aria-expanded', String(evidence.open)); });
    updateVisibility(); renderEvidence();
    const confirm = button('確認上傳／提交審核', async () => {
      const values = Object.fromEntries(new FormData(form));
      values.subject_custom = values.subject === 'other' ? (values.subject_custom || '').trim() : '';
      values.material_custom = values.material_type === 'other' ? (values.material_custom || '').trim() : '';
      await state.saving;
      await api(`/tasks/${task.id}/draft`, 'PATCH', { values });
      await api(`/tasks/${task.id}/commit`, 'POST', {});
      invalidateRefresh();
      state.selected = null; state.detail = null; await refresh();
    }, true);
    confirm.disabled = task.status !== 'waiting_confirmation'; form.append(preview, confirm); content.append(form);
    content.append(el('p', task.status === 'waiting_confirmation' ? '分類確認後才正式保存。共享資料沿用原有審核規則。' : '可先填寫分類；分析完成後，人工填寫內容會保留。', 'task-meta'));
  }
  async function begin(task, mode) {
    state.attempt = await api(`/tasks/${task.id}/attempts`, 'POST', { mode }); state.pending = {}; render();
  }
  function queueAnswer(index, answer) {
    state.pending[String(index)] = answer; state.attempt.answers[String(index)] = answer;
    clearTimeout(state.timer); state.timer = setTimeout(() => flush().catch(report), 500);
  }
  async function flush() {
    clearTimeout(state.timer);
    if (!state.attempt || !Object.keys(state.pending).length) return state.saving;
    const taskId = state.selected, attemptId = state.attempt.id, answers = state.pending; state.pending = {};
    const sequence=++answerSequence;
    state.saving = state.saving.catch(() => {}).then(() => api(`/tasks/${taskId}/attempts/${attemptId}`, 'PATCH', { answers,client_id:draftClient,sequence }));
    try { await state.saving; }
    catch(e) { if (state.attempt?.id === attemptId) state.pending = { ...answers, ...state.pending }; throw e; }
  }
  function rebuildPractice() { content.querySelector('.task-paper')?.remove(); renderPractice(state.detail); }
  async function grade(index) {
    if (state.grading) return;
    state.grading = true;
    try {
      await flush(); state.notice = '正在判題…';
      const updated = await api(`/tasks/${state.selected}/attempts/${state.attempt.id}/grade-question`, 'POST', { index });
      state.attempt = {...updated, answers: {...updated.answers, ...state.pending}};
      rebuildPractice();
    } finally { state.grading = false; }
  }
  function renderPractice(task) {
    const attempt = state.attempt, paper = el('article', null, 'task-paper');
    const toolbar = el('div', null, 'task-actions');
    toolbar.append(button('返回試題選單', async () => { await flush(); state.attempt = null; render(); }), button('重新練習', async () => { await flush(); state.attempt = null; await begin(task, attempt.mode); }), button('列印', () => { document.body.classList.add('task-printing'); window.print(); }));
    paper.append(toolbar, el('p', attempt.mode === 'instant' ? '即時反饋模式' : '整卷提交模式', 'task-meta'));
    const grades = Object.values(attempt.grades), correct = grades.filter(g => g.correct).length;
    if (grades.length) paper.append(el('p', `答對 ${correct} / ${grades.length} 題 · 正確率 ${Math.round(correct / grades.length * 100)}%${attempt.submitted ? ' · 已提交' : ` · 全卷 ${task.result.questions.length} 題`}`, 'task-summary'));
    const origins = { original:'原題',variant:'微改題',source_based:'依據資料新編' };
    task.result.questions.forEach((q,index) => {
      const key = String(index), result = attempt.grades[key], section = el('section', null, 'task-question'); section.dataset.question = key;
      section.append(el('span', origins[q.origin] || '練習題', 'task-origin'), el('h3', `${index + 1}. ${q.stem}`));
      if (q.type === 'choice') {
        q.options.forEach((option, optionIndex) => {
          const letter = 'ABCD'[optionIndex];
          const choice = button(`${letter}. ${option}`, async () => { if (state.grading) return; queueAnswer(index, letter); if (attempt.mode === 'instant') await grade(index); else { section.querySelectorAll('.task-choice').forEach(n => {n.classList.remove('picked');n.setAttribute('aria-pressed','false');}); choice.classList.add('picked');choice.setAttribute('aria-pressed','true'); } });
          choice.classList.add('task-choice'); choice.classList.toggle('picked', attempt.answers[key] === letter); choice.setAttribute('aria-pressed', String(attempt.answers[key] === letter)); choice.disabled = !!result || attempt.submitted;
          section.append(choice);
        });
      } else {
        const label = el('label', q.type === 'blank' ? '填寫答案（多空請按順序填寫）' : '你的答案');
        const input = el(q.type === 'short' ? 'textarea' : 'input'); input.value = attempt.answers[key] || ''; input.maxLength = 4000; input.disabled = !!result || attempt.submitted;
        input.addEventListener('input', () => queueAnswer(index,input.value));
        input.addEventListener('keydown', async event => {
          if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229 || q.type === 'short') return;
          event.preventDefault();
          try { queueAnswer(index,input.value); if (attempt.mode === 'instant') await grade(index); else { await flush(); toast('本題答案已保存'); } } catch(e) { report(e); }
        });
        label.append(input); section.append(label);
        if (attempt.mode === 'instant' && !result) section.append(button('提交本題', () => grade(index)));
      }
      if (result) {
        const feedback = el('div', `${result.correct ? '✓ 回答正確' : '✗ 回答錯誤'}\n參考答案：${result.answer}\n解析：${result.explanation}`, 'task-feedback ' + (result.correct ? 'task-success' : 'task-error'));
        feedback.setAttribute('role','status'); section.append(feedback);
        if (q.document_id) section.append(button(`來源：${q.filename || '教材'} 第${q.page || 1}頁 ↗`, () => { if (typeof openDocumentSource === 'function') openDocumentSource(q.document_id,q.page,q.chunk_id); }));
        else if (typeof q.url === 'string' && safeLink(q.url)) {
          const link=el('a','查看網頁來源','task-meta');link.href=safeLink(q.url);link.target='_blank';link.rel='noopener noreferrer';section.append(link);
        } else section.append(el('p',q.source_label || 'AI 自編練習','task-meta'));
      }
      paper.append(section);
    });
    if (!attempt.submitted) paper.append(button('提交全部', async () => {
      await flush(); const missing = task.result.questions.filter((q,i) => !(state.attempt.answers[String(i)] || '').trim()).length;
      let allow = false;
      if (missing) { allow = typeof customConfirm === 'function' ? await customConfirm(`還有 ${missing} 題未作答，仍要提交嗎？`, '提交試卷') : window.confirm(`還有 ${missing} 題未作答，仍要提交嗎？`); if (!allow) return; }
      state.attempt = await api(`/tasks/${task.id}/attempts/${attempt.id}/submit`, 'POST', { allow_unanswered: allow }); rebuildPractice();
    }, true));
    content.append(paper);
  }
  async function sendFile(file, requestId = rid()) {
    const identity = session(); if (!identity) return;
    const transfer = { id:requestId, file, name:file.name,status:'transferring',stage:'正在傳輸檔案',completed:0,total:file.size,unit:'位元組' }; state.transfers.set(requestId,transfer); renderBadge(); render();
    await new Promise(resolve => {
      const xhr = new XMLHttpRequest(); transfer.xhr = xhr; xhr.open('POST',apiBase()+'/tasks/uploads'); xhr.setRequestHeader('X-Session-ID',identity);
      xhr.upload.onprogress = event => { if (identity !== session()) return; transfer.completed=event.loaded; transfer.total=event.lengthComputable ? event.total : null; render(); };
      xhr.upload.onload = () => { transfer.stage='正在保存原件，等待伺服器確認'; transfer.total=null; render(); };
      xhr.onload = async () => {
        if (identity !== session()) { resolve(); return; }
        if (xhr.status >= 200 && xhr.status < 300) { state.transfers.delete(requestId); invalidateRefresh(); await refresh(); }
        else { let message='傳輸失敗，請重試'; try { message=JSON.parse(xhr.responseText).detail || message; } catch {} transfer.error=message; transfer.stage='傳輸失敗'; transfer.total=null; }
        renderBadge(); render(); resolve();
      };
      xhr.onerror = () => { transfer.error='連線中斷，可重試；已接收的任務也會出現在列表'; transfer.stage='傳輸未確認'; transfer.total=null; render(); resolve(); };
      xhr.onabort=()=>resolve(); const form=new FormData(); form.append('file',file); form.append('request_id',requestId); xhr.send(form);
    });
  }
  window.BackgroundTasks = {
    open,
    async openPractice(taskId, options = {}) {
      await flush();
      const epoch = ++practiceOpenEpoch;
      const identity = session(); if (!identity) return;
      state.selected = null; state.detail = null; state.attempt = null; state.pending = {}; state.notice = ''; open();
      try {
        const task = await api('/tasks/' + encodeURIComponent(taskId));
        if (identity !== session() || epoch !== practiceOpenEpoch) return;
        if (task.kind !== 'quiz' || task.status !== 'succeeded') throw new Error('這份練習目前無法作答，請稍後重試。');
        state.selected = task.id; state.detail = task;
        if (options.newAttempt) {
          const attempt = await api('/tasks/' + encodeURIComponent(taskId) + '/attempts','POST',{mode:'instant'});
          if (identity !== session() || epoch !== practiceOpenEpoch) return;
          state.attempt=attempt;state.pending={};render();return;
        }
        const attempts = await api('/tasks/' + encodeURIComponent(taskId) + '/attempts');
        if (identity !== session() || epoch !== practiceOpenEpoch) return;
        const attempt = options.attemptId ? attempts.find(item => item.id === options.attemptId) : attempts[0];
        state.attempt = attempt || null; render();
        if (!attempt) report(new Error('這次作答紀錄已不存在，可開始新練習。'));
      } catch (error) { if (identity === session()) report(error); }
    },
    clear() { ++practiceOpenEpoch;stopPolling();if(panel)panel.hidden=true;state.tasks=[];state.detail=null;state.attempt=null;state.selected=null;state.pending={};state.ownerSession='';draftLatest.clear();for(const t of state.transfers.values())t.xhr?.abort();state.transfers.clear();content?.replaceChildren();if(badge)renderBadge(); },
    async upload(files) {
      // FileList is live: input reset or the end of a drop event can empty it during await.
      const selected = Array.from(files || []);
      if (!selected.length || !session()) return;
      await flush();state.selected=null;state.detail=null;state.attempt=null;open();
      for (const file of selected) { if (!session()) break; await sendFile(file); }
    },
    async quiz(options) { const task=await api('/tasks/quizzes','POST',{...options,request_id:rid()}); invalidateRefresh(); toast('出題已在背景執行，可繼續聊天'); await refresh(); return task; },
    refresh
  };
  function init() {
    panel=el('aside',null,'task-window'); panel.id='background-task-window'; panel.hidden=true; panel.setAttribute('aria-label','任務管理');
    const head=el('header',null,'task-head');
    const heading = el('div', null, 'task-heading'), headingIcon = el('span', null, 'task-heading-icon'); headingIcon.append(icon('tasks'));
    const headingText = el('div'); headingText.append(el('h2', '任務管理'), el('p', '查看上傳與練習進度')); heading.append(headingIcon, headingText);
    const minimize = button('', hide); minimize.className = 'task-minimize'; minimize.title = '最小化'; minimize.setAttribute('aria-label', '最小化'); minimize.append(icon('minimize'));
    head.append(heading, minimize);
    const tabs=el('nav',null,'task-toolbar');
    tabs.setAttribute('aria-label', '任務篩選');
    for(const [key,label] of [['all','全部'],['active','進行中'],['completed','已完成'],['failed','失敗／中斷']]) {
      const tab = button(label,async()=>{ await flush();state.filter=key;state.selected=null;state.detail=null;state.attempt=null;render(); });
      tab.dataset.filter = key; if (key === 'active') tab.title = '進行中與待確認'; tabs.append(tab);
    }
    content=el('div',null,'task-content');panel.append(head,tabs,content);document.body.append(panel);
    entry=button('',()=>panel.hidden ? open() : hide());entry.className='header-btn task-entry';entry.id='open-task-manager';
    entry.setAttribute('aria-controls', panel.id); badge=el('span','', 'task-count');badge.hidden=true;badge.setAttribute('aria-hidden','true');entry.append(icon('tasks'),badge);document.getElementById('toggle-file-panel')?.before(entry);
    panel.addEventListener('keydown',event=>{if(event.key==='Escape'){event.stopPropagation();hide();}});
    window.addEventListener('afterprint',()=>document.body.classList.remove('task-printing'));
    window.addEventListener('pagehide',()=>{
      stopPolling();
      if(state.attempt && !state.attempt.submitted && session()) fetch(apiBase()+`/tasks/${state.selected}/attempts/${state.attempt.id}`,{method:'PATCH',keepalive:true,headers:{'Content-Type':'application/json','X-Session-ID':session()},body:JSON.stringify({answers:state.attempt.answers,client_id:draftClient,sequence:++answerSequence})}).catch(()=>{});
      if(session())for(const [taskId,values]of draftLatest)fetch(apiBase()+`/tasks/${taskId}/draft`,{method:'PATCH',keepalive:true,headers:{'Content-Type':'application/json','X-Session-ID':session()},body:JSON.stringify({values,client_id:draftClient,sequence:++draftSequence})}).catch(()=>{});
    });
    document.addEventListener('visibilitychange', () => { if (document.hidden) scheduleRefresh(); else void refresh(); });
    window.addEventListener('focus', () => { if (!document.hidden) void refresh(); });
    window.addEventListener('pageshow', () => { void refresh(); });
    refresh();
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
