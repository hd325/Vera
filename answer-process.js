/* GPT-style answer process timeline controller (live streaming + persisted history). */
(() => {
  'use strict';

  const MAX_VISIBLE_LINKS = 4;

  const ICONS = {
    plan: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>',
    retrieval: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7.5"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    fetch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
    photo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
    edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
    validate: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
    think: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14"/></svg>',
    answer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    skill: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 10 12 5 2 10l10 5 10-5Z"/><path d="M6 12v5c3 2 9 2 12 0v-5"/></svg>',
    memory: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/></svg>',
    chevron: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>',
    globe: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><line x1="3" y1="12" x2="21" y2="12"/><path d="M12 3a14 14 0 0 1 3.5 9 14 14 0 0 1-3.5 9 14 14 0 0 1-3.5-9A14 14 0 0 1 12 3z"/></svg>',
    warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function safeUrl(value) {
    if (typeof window.safeWebUrl === 'function') return window.safeWebUrl(value);
    if (typeof value !== 'string') return null;
    try {
      const parsed = new URL(value);
      return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : null;
    } catch (_) {
      return null;
    }
  }

  function formatDuration(ms, { precise = false } = {}) {
    const totalMs = Math.max(0, Number(ms) || 0);
    const totalSec = totalMs / 1000;
    if (totalSec >= 60) {
      const mins = Math.floor(totalSec / 60);
      const secs = Math.floor(totalSec % 60);
      return `${mins} 分 ${secs} 秒`;
    }
    return `${totalSec.toFixed(precise || totalSec < 10 ? 1 : 0)} 秒`;
  }

  function formatStepDuration(ms) {
    const num = Number(ms);
    if (!Number.isFinite(num) || num < 80) return '';
    const sec = num / 1000;
    if (sec >= 60) {
      return `${Math.floor(sec / 60)}m${Math.floor(sec % 60)}s`;
    }
    return `${sec.toFixed(1)}s`;
  }

  function extractThinkAndAnswer(rawText) {
    if (!rawText || typeof rawText !== 'string') {
      return { thinkText: '', answerText: '', thinkingOpen: false };
    }
    const thinkParts = [];
    const openIdx = rawText.lastIndexOf('<think>');
    const closeIdx = rawText.lastIndexOf('</think>');
    const thinkingOpen = openIdx > closeIdx;
    const answerText = rawText.replace(/<think>([\s\S]*?)(?:<\/think>|$)/g, (_, inner) => {
      const trimmed = String(inner || '').trim();
      if (trimmed) thinkParts.push(trimmed);
      return '';
    });
    return {
      thinkText: thinkParts.join('\n\n'),
      answerText,
      thinkingOpen,
    };
  }

  function renderThinkMarkdown(text) {
    const trimmed = String(text || '').trim();
    if (!trimmed) return '';
    if (typeof marked !== 'undefined' && typeof sanitizeAnswerHtml === 'function') {
      return sanitizeAnswerHtml(marked.parse(trimmed));
    }
    const div = document.createElement('div');
    div.textContent = trimmed;
    return div.innerHTML.replace(/\n/g, '<br>');
  }

  function createLinkNode(link) {
    if (!link || typeof link !== 'object') return null;
    const kind = link.kind || 'web';
    const label = String(link.label || '').trim();
    if (!label) return null;
    const sublabel = String(link.sublabel || '').trim();

    if (kind === 'web') {
      const href = safeUrl(link.url);
      if (!href) return null;
      const a = el('a', 'ap-link is-web');
      a.href = href;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.title = sublabel ? `${label} · ${sublabel}` : label;
      const icon = el('span', 'ap-link-icon');
      icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = ICONS.globe;
      a.append(icon, el('span', 'ap-link-label', label));
      if (sublabel) a.append(el('span', 'ap-link-sub', sublabel));
      a.append(el('span', 'ap-link-ext', '↗'));
      return a;
    }

    const btn = el('button', `ap-link is-${kind}`);
    btn.type = 'button';
    btn.title = sublabel ? `${label} · ${sublabel}` : label;
    const icon = el('span', 'ap-link-icon');
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = kind === 'doc' ? ICONS.retrieval : kind === 'memory' ? ICONS.memory : ICONS.file;
    btn.append(icon, el('span', 'ap-link-label', label));
    if (sublabel) btn.append(el('span', 'ap-link-sub', sublabel));

    if (kind === 'doc') {
      if (link.doc_id && link.chunk_id) {
        btn.addEventListener('click', () => {
          if (typeof window.openSourceView === 'function') {
            window.openSourceView(link.doc_id, link.chunk_id);
          }
        });
      } else {
        btn.disabled = true;
      }
    } else if (kind === 'file') {
      if (link.file_id) {
        btn.addEventListener('click', () => {
          if (typeof window.previewFile === 'function') {
            window.previewFile(link.file_id);
          }
        });
      } else {
        btn.disabled = true;
      }
    } else if (kind === 'memory') {
      if (link.memory_id) {
        btn.addEventListener('click', () => {
          window.AccountSettings?.showMemory?.(link.memory_id);
        });
      } else {
        btn.disabled = true;
      }
    }
    return btn;
  }

  function createProcessWidget(messageContent, { live = false, defaultOpen = false, initialConfig = {} } = {}) {
    const root = el('section', 'answer-process');
    root.setAttribute('aria-label', '回答過程');
    if (live) root.classList.add('is-running');

    const header = el('div', 'ap-header');
    const toggleBtn = el('button', 'ap-toggle');
    toggleBtn.type = 'button';
    const statusDot = el('span', `ap-status-dot ${live ? 'is-running' : 'is-done'}`);
    statusDot.setAttribute('aria-hidden', 'true');
    const headerText = el('span', 'ap-header-text', live ? '步驟 1 · 正在處理 · 0.0 秒' : '回答過程');
    const chevron = el('span', 'ap-chevron');
    chevron.setAttribute('aria-hidden', 'true');
    chevron.innerHTML = ICONS.chevron;
    toggleBtn.append(statusDot, headerText, chevron);

    const headerRight = el('div', 'ap-header-right');
    const modelPill = el('span', 'ap-model-pill');
    modelPill.hidden = true;
    headerRight.append(modelPill);

    header.append(toggleBtn, headerRight);

    const body = el('div', 'ap-body');
    root.append(header, body);

    const state = {
      live,
      finished: !live,
      finalStatus: live ? 'running' : 'done',
      startedAt: performance.now(),
      elapsedMs: 0,
      isOpen: defaultOpen,
      manualToggle: false,
      config: { ...initialConfig },
      steps: [],
      stepMap: new Map(),
      thinkText: '',
      thinkingOpen: false,
      thinkExpanded: true,
      expandedLinkSteps: new Set(),
      timerId: null,
    };

    function applyOpenState() {
      root.classList.toggle('is-open', state.isOpen);
      toggleBtn.setAttribute('aria-expanded', String(state.isOpen));
      body.hidden = !state.isOpen;
    }

    function syncModelPill() {
      const name = String(state.config.model_name || '').trim();
      if (name) {
        modelPill.textContent = name;
        modelPill.hidden = false;
      } else {
        modelPill.hidden = true;
      }
    }

    function countableSteps() {
      return state.steps.filter((s) => s.kind !== 'note');
    }

    function updateHeader() {
      const elapsed = state.finished ? state.elapsedMs : Math.max(0, performance.now() - state.startedAt);
      const visibleSteps = countableSteps();
      const activeIndex = visibleSteps.findIndex((s) => s.status === 'running');
      const activeStep = activeIndex >= 0 ? visibleSteps[activeIndex] : null;

      if (!state.finished) {
        root.classList.add('is-running');
        statusDot.className = 'ap-status-dot is-running';
        const stepPrefix = activeStep ? `步驟 ${activeIndex + 1} · ` : '';
        const stageLabel = activeStep ? activeStep.title.replace(/[….\s]+$/, '') : '正在處理';
        headerText.textContent = `${stepPrefix}${stageLabel} · ${formatDuration(elapsed, { precise: true })}`;
      } else {
        root.classList.remove('is-running');
        const dotClass = state.finalStatus === 'error'
          ? 'is-error'
          : ['stopped', 'paused', 'waiting', 'warning'].includes(state.finalStatus)
            ? 'is-warning'
            : 'is-done';
        statusDot.className = `ap-status-dot ${dotClass}`;
        const stepSuffix = visibleSteps.length ? ` · ${visibleSteps.length} 個步驟` : '';
        const durationText = elapsed >= 50 ? formatDuration(elapsed) : '';
        if (state.finalStatus === 'stopped') {
          headerText.textContent = durationText ? `已停止 · 用時 ${durationText}${stepSuffix}` : `已停止${stepSuffix}`;
        } else if (state.finalStatus === 'paused') {
          headerText.textContent = durationText ? `已暫停 · 用時 ${durationText}${stepSuffix}` : `已暫停${stepSuffix}`;
        } else if (state.finalStatus === 'waiting') {
          headerText.textContent = durationText ? `等待補充 · 用時 ${durationText}${stepSuffix}` : `等待補充${stepSuffix}`;
        } else if (state.finalStatus === 'error') {
          headerText.textContent = durationText ? `過程已中斷 · 用時 ${durationText}${stepSuffix}` : `過程已中斷${stepSuffix}`;
        } else {
          headerText.textContent = durationText
            ? `用時 ${durationText}${stepSuffix}`
            : `回答過程${stepSuffix}`;
        }
      }
      syncModelPill();
    }

    function renderBody() {
      const prevThinkBox = body.querySelector('.ap-think-body');
      const prevScrollTop = prevThinkBox ? prevThinkBox.scrollTop : 0;
      const wasAtBottom = prevThinkBox
        ? prevThinkBox.scrollHeight - prevThinkBox.scrollTop - prevThinkBox.clientHeight < 28
        : true;

      body.replaceChildren();
      let stepNumber = 0;

      for (const step of state.steps) {
        if (step.kind === 'note') {
          if (step.note) body.append(el('p', 'ap-note', step.note));
          continue;
        }
        stepNumber += 1;
        const item = el('div', `ap-step is-${step.status || 'done'}`);
        item.dataset.stepId = step.id;

        const row = el('div', 'ap-step-row');
        const main = el('div', 'ap-step-main');

        const iconWrap = el('span', 'ap-step-icon');
        iconWrap.setAttribute('aria-hidden', 'true');
        if (step.status === 'running') {
          iconWrap.append(el('span', 'ap-spinner'));
        } else if (step.status === 'warning' || step.status === 'error') {
          iconWrap.innerHTML = ICONS.warn;
        } else {
          iconWrap.innerHTML = ICONS[step.kind] || ICONS.answer;
        }

        const copy = el('div', 'ap-step-copy');
        copy.append(el('span', 'ap-step-index', `步驟 ${stepNumber}`));
        copy.append(el('span', 'ap-step-title', step.title || '處理中'));
        if (step.detail) {
          copy.append(el('span', 'ap-step-detail', step.detail));
        }
        main.append(iconWrap, copy);

        const meta = el('div', 'ap-step-meta');
        const durLabel = step.status !== 'running' ? formatStepDuration(step.duration_ms) : '';
        if (durLabel) meta.append(el('span', 'ap-step-duration', durLabel));

        const isThinkWithText = step.kind === 'think' && Boolean(state.thinkText);
        if (isThinkWithText) {
          const stepToggle = el('button', 'ap-step-toggle');
          stepToggle.type = 'button';
          stepToggle.setAttribute('aria-expanded', String(state.thinkExpanded));
          stepToggle.setAttribute('aria-label', state.thinkExpanded ? '收起思考細節' : '展開思考細節');
          stepToggle.innerHTML = ICONS.chevron;
          stepToggle.addEventListener('click', () => {
            state.thinkExpanded = !state.thinkExpanded;
            renderBody();
          });
          meta.append(stepToggle);
        }

        row.append(main, meta);
        item.append(row);

        if (step.note) {
          const noteEl = el('p', 'ap-note', step.note);
          noteEl.style.paddingLeft = '23px';
          item.append(noteEl);
        }

        if (Array.isArray(step.links) && step.links.length > 0) {
          const linksWrap = el('div', 'ap-links');
          const expandedAll = state.expandedLinkSteps.has(step.id);
          const visible = expandedAll ? step.links : step.links.slice(0, MAX_VISIBLE_LINKS);
          for (const link of visible) {
            const linkNode = createLinkNode(link);
            if (linkNode) linksWrap.append(linkNode);
          }
          if (!expandedAll && step.links.length > MAX_VISIBLE_LINKS) {
            const moreBtn = el('button', 'ap-links-more', `+${step.links.length - MAX_VISIBLE_LINKS} 個來源`);
            moreBtn.type = 'button';
            moreBtn.addEventListener('click', () => {
              state.expandedLinkSteps.add(step.id);
              renderBody();
            });
            linksWrap.append(moreBtn);
          }
          if (linksWrap.childNodes.length) item.append(linksWrap);
        }

        if (isThinkWithText && state.thinkExpanded) {
          const thinkBox = el('div', 'ap-think-body');
          thinkBox._rawThink = state.thinkText;
          thinkBox.innerHTML = renderThinkMarkdown(state.thinkText);
          item.append(thinkBox);
          requestAnimationFrame(() => {
            if (!thinkBox.isConnected) return;
            if (state.thinkingOpen && wasAtBottom) {
              thinkBox.scrollTop = thinkBox.scrollHeight;
            } else if (prevScrollTop) {
              thinkBox.scrollTop = prevScrollTop;
            }
          });
        }

        body.append(item);
      }
    }

    toggleBtn.addEventListener('click', () => {
      state.manualToggle = true;
      state.isOpen = !state.isOpen;
      applyOpenState();
    });

    function upsertStep(raw) {
      if (!raw || typeof raw !== 'object') return;
      const id = String(raw.id || `step_${state.steps.length + 1}`);
      if (raw.config && typeof raw.config === 'object') {
        Object.assign(state.config, raw.config);
      }
      let existing = state.stepMap.get(id);
      const now = performance.now();
      if (!existing) {
        existing = {
          id,
          kind: String(raw.kind || 'step'),
          status: String(raw.status || 'running'),
          title: String(raw.title || ''),
          detail: String(raw.detail || ''),
          note: String(raw.note || ''),
          links: Array.isArray(raw.links) ? raw.links : [],
          duration_ms: raw.duration_ms,
          _startedAt: now,
        };
        state.stepMap.set(id, existing);
        state.steps.push(existing);
      } else {
        if (raw.kind) existing.kind = String(raw.kind);
        if (raw.status) existing.status = String(raw.status);
        if (raw.title) existing.title = String(raw.title);
        if (typeof raw.detail === 'string') existing.detail = raw.detail;
        if (typeof raw.note === 'string' && raw.note) existing.note = raw.note;
        if (Array.isArray(raw.links)) existing.links = raw.links;
        if (typeof raw.duration_ms === 'number') {
          existing.duration_ms = raw.duration_ms;
        } else if (existing.status !== 'running' && existing.duration_ms == null) {
          existing.duration_ms = Math.max(0, Math.round(now - existing._startedAt));
        }
      }
      updateHeader();
      renderBody();
    }

    function setThinking(thinkText, thinkingOpen) {
      const cleaned = String(thinkText || '').trim();
      state.thinkText = cleaned;
      state.thinkingOpen = Boolean(thinkingOpen);
      if (!cleaned && !thinkingOpen) return;
      state.config.think = true;
      upsertStep({
        id: 'think',
        kind: 'think',
        status: thinkingOpen ? 'running' : 'done',
        title: thinkingOpen ? '正在深度思考…' : '已完成深度思考',
        detail: state.config.model_name || '',
      });
    }

    function markAnswering() {
      const thinkStep = state.stepMap.get('think');
      if (thinkStep && thinkStep.status === 'running') {
        state.thinkingOpen = false;
        upsertStep({
          id: 'think',
          kind: 'think',
          status: 'done',
          title: '已完成深度思考',
          detail: thinkStep.detail || state.config.model_name || '',
        });
      }
      const photoStep = state.stepMap.get('photo');
      if (photoStep && photoStep.status === 'running') {
        upsertStep({
          id: 'photo',
          kind: 'photo',
          status: 'done',
          title: photoStep.title.replace(/^正在/, '已').replace(/[….]+$/, ''),
        });
      }
      const existingAnswer = state.stepMap.get('answer');
      if (!existingAnswer || existingAnswer.status !== 'running') {
        upsertStep({
          id: 'answer',
          kind: 'answer',
          status: 'running',
          title: '正在撰寫回覆…',
          detail: state.config.model_name || '',
        });
      }
    }

    function setModel(info) {
      if (!info || typeof info !== 'object') return;
      if (info.requested_model) state.config.model_id = info.requested_model;
      if (info.model_name) state.config.model_name = info.model_name;
      syncModelPill();
    }

    function setConfig(cfg) {
      if (!cfg || typeof cfg !== 'object') return;
      Object.assign(state.config, cfg);
      syncModelPill();
    }

    function finish(finalStatus = 'done', serverTrace = null, finalThinkText = '') {
      if (state.timerId) {
        clearInterval(state.timerId);
        state.timerId = null;
      }
      state.finished = true;
      state.finalStatus = finalStatus || 'done';
      state.thinkingOpen = false;
      if (finalThinkText) state.thinkText = String(finalThinkText).trim();

      if (serverTrace && typeof serverTrace === 'object') {
        if (typeof serverTrace.elapsed_ms === 'number' && serverTrace.elapsed_ms > 0) {
          state.elapsedMs = serverTrace.elapsed_ms;
        } else {
          state.elapsedMs = Math.max(0, Math.round(performance.now() - state.startedAt));
        }
        if (serverTrace.config && typeof serverTrace.config === 'object') {
          Object.assign(state.config, serverTrace.config);
        }
        if (Array.isArray(serverTrace.steps) && serverTrace.steps.length) {
          state.steps = [];
          state.stepMap.clear();
          for (const s of serverTrace.steps) {
            upsertStep(s);
          }
        }
      } else {
        state.elapsedMs = Math.max(0, Math.round(performance.now() - state.startedAt));
      }

      for (const step of state.steps) {
        if (step.status === 'running') {
          step.status = state.finalStatus === 'done' ? 'done' : state.finalStatus === 'error' ? 'error' : 'warning';
          if (step.status === 'done' && step.title.startsWith('正在')) {
            step.title = '已' + step.title.slice(2).replace(/[….]+$/, '');
          }
          if (step.duration_ms == null) {
            step.duration_ms = Math.max(0, Math.round(performance.now() - step._startedAt));
          }
        }
      }

      if (state.thinkText && !state.stepMap.has('think')) {
        upsertStep({
          id: 'think',
          kind: 'think',
          status: 'done',
          title: '已完成深度思考',
        });
      }

      // 回覆完成後自動摺疊收攏為單行摘要，點擊即可隨時展開回看。
      if (!state.manualToggle) {
        state.isOpen = state.finalStatus === 'error';
      }
      applyOpenState();
      updateHeader();
      renderBody();
    }

    function dispose() {
      if (state.timerId) {
        clearInterval(state.timerId);
        state.timerId = null;
      }
    }

    applyOpenState();
    syncModelPill();

    if (live) {
      state.timerId = setInterval(() => {
        if (!root.isConnected || state.finished) return;
        updateHeader();
      }, 100);
    }

    messageContent.classList.add('has-answer-process');
    const anchor = messageContent.querySelector(':scope > .message-bubble');
    if (anchor) messageContent.insertBefore(root, anchor);
    else messageContent.prepend(root);

    return {
      element: root,
      upsertStep,
      setThinking,
      markAnswering,
      setModel,
      setConfig,
      finish,
      syncAttached: updateHeader,
      dispose,
      hasSteps: () => state.steps.length > 0,
    };
  }

  function buildHistoricalTraceFallback(message, thinkText) {
    const steps = [];
    const config = {};
    if (message.model) config.model_name = String(message.model);

    if (message.skill_context?.status === 'active' && message.skill_context?.name) {
      config.skill_name = message.skill_context.name;
      steps.push({
        id: 'skill',
        kind: 'skill',
        status: 'done',
        title: `已啟用教學模式：${message.skill_context.name}`,
      });
    }

    const citations = Array.isArray(message.citation_map ?? message.citationMap)
      ? (message.citation_map ?? message.citationMap)
      : [];
    if (citations.length) {
      const seen = new Set();
      const docLinks = [];
      for (const c of citations) {
        const docId = c.document_id || c.citation?.document_id || '';
        const name = c.filename || c.citation?.filename || '校內資料';
        const key = docId || name;
        if (seen.has(key)) continue;
        seen.add(key);
        const page = c.page ?? c.citation?.page;
        docLinks.push({
          kind: 'doc',
          label: name,
          sublabel: c.location || c.citation?.location || (page != null ? `第 ${page} 頁` : ''),
          doc_id: docId,
          chunk_id: c.chunk_id || c.citation?.chunk_id || '',
        });
      }
      steps.push({
        id: 'retrieval',
        kind: 'retrieval',
        status: 'done',
        title: `已檢索校內資料 · 命中 ${docLinks.length} 份文件（${citations.length} 段）`,
        links: docLinks.slice(0, 6),
      });
    }

    const webSources = Array.isArray(message.web_sources) ? message.web_sources : [];
    if (webSources.length) {
      config.web = true;
      const webLinks = webSources.slice(0, 6).map((s) => {
        let host = '';
        try { host = new URL(s.url).hostname.replace(/^www\./, ''); } catch (_) {}
        const statusLabel = s.content_status === 'fetched' ? '已讀正文' : '搜尋摘要';
        return {
          kind: 'web',
          label: s.title || host || s.url,
          sublabel: [host, statusLabel].filter(Boolean).join(' · '),
          url: s.url,
          status: s.content_status,
        };
      });
      steps.push({
        id: 'web_fetch',
        kind: 'fetch',
        status: 'done',
        title: `已搜尋並參考 ${webSources.length} 個網頁`,
        links: webLinks,
      });
    }

    if (message.photo_model) {
      steps.push({
        id: 'photo',
        kind: 'photo',
        status: 'done',
        title: '已查看圖片內容',
        detail: `看圖模型 · ${message.photo_model}`,
      });
    }

    if (thinkText) {
      config.think = true;
      steps.push({
        id: 'think',
        kind: 'think',
        status: 'done',
        title: '已完成深度思考',
      });
    }

    if (!steps.length) return null;
    return { elapsed_ms: 0, status: 'done', config, steps };
  }

  function createLive(messageContent, options = {}) {
    const widget = createProcessWidget(messageContent, {
      live: true,
      defaultOpen: true,
      initialConfig: options.initialConfig || {},
    });
    if (options.transferring) {
      widget.upsertStep({
        id: 'upload',
        kind: 'file',
        status: 'running',
        title: '正在傳送附件…',
      });
    } else {
      widget.upsertStep({
        id: 'plan',
        kind: 'plan',
        status: 'running',
        title: '正在理解問題並規劃處理方式…',
      });
    }
    return widget;
  }

  function mountHistorical(messageContent, message, options = {}) {
    if (!messageContent || !message || messageContent.querySelector(':scope > .answer-process')) return null;
    const { thinkText } = extractThinkAndAnswer(message.content || '');
    const trace = message.process_trace ?? message.metadata?.process_trace ?? buildHistoricalTraceFallback(message, thinkText);
    if (!trace || !Array.isArray(trace.steps) || !trace.steps.length) return null;

    const shouldOpen = typeof options.defaultOpen === 'boolean' ? options.defaultOpen : false;
    const widget = createProcessWidget(messageContent, {
      live: false,
      defaultOpen: shouldOpen,
      initialConfig: trace.config || {},
    });
    widget.finish(trace.status || 'done', trace, thinkText);
    return widget;
  }

  function rerenderMarkdown() {
    document.querySelectorAll('.ap-think-body').forEach((box) => {
      if (box._rawThink) box.innerHTML = renderThinkMarkdown(box._rawThink);
    });
  }

  window.AnswerProcess = {
    createLive,
    mountHistorical,
    extractThinkAndAnswer,
    rerenderMarkdown,
  };
})();
