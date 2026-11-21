/* Offline fixture only: owns an ephemeral loopback server and mocks every API. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium, webkit } = require('playwright');
if (process.env.VERA_LIVE_ORIGIN || process.env.VERA_BEFORE_HTML) {
    throw new Error('This public fixture does not accept live servers or external HTML; unset VERA_LIVE_ORIGIN and VERA_BEFORE_HTML.');
}
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'work', 'mobile-ui-check');
fs.mkdirSync(output, { recursive: true });
const before = null;
const assets = new Set(['index.html', 'speech-input.js', 'task-manager.css', 'task-manager.js',
    'account-settings.css', 'account-settings.js', 'wechat-connection.css', 'wechat-connection.js', 'chat-interactions.css',
    'chat-interactions.js', 'marked.js', 'dompurify.min.js', 'tailwindcss.js', 'pdf.min.js',
    'pdf.worker.min.js', 'font-awesome.css', 'fonts/fontawesome-webfont.woff2', 'logo.png']);
const types = { '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript', '.woff2': 'font/woff2', '.png': 'image/png' };
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname.startsWith('/api/')) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ code: 200, data: [], models: [], tasks: [] }));
    }
    const name = decodeURIComponent(url.pathname.replace(/^\/static\//, '').replace(/^\//, '')) || 'index.html';
    if (name === 'before.html' && before) {
        const html = fs.readFileSync(before, 'utf8').replaceAll('/static/chat-interactions.', '/before-assets/chat-interactions.');
        res.writeHead(200, { 'Content-Type': 'text/html' }); return res.end(html);
    }
    if (name.startsWith('before-assets/') && before && ['chat-interactions.css', 'chat-interactions.js'].includes(path.basename(name))) {
        res.writeHead(200, { 'Content-Type': types[path.extname(name)] });
        return res.end(fs.readFileSync(path.join(path.dirname(before), path.basename(name))));
    }
    if (!assets.has(name)) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'Content-Type': types[path.extname(name)] || 'application/octet-stream' });
    res.end(fs.readFileSync(path.join(root, name)));
});
let origin;
const report = [];
async function open(browser, viewport, options = {}) {
    const context = await browser.newContext({ viewport, serviceWorkers: 'block', ...options });
    await context.addInitScript(() => {
        window.SpeechRecognition = class {
            constructor() { window.__fixtureRecognition = this; }
            start() { this.onstart?.(); }
            stop() { this.onend?.(); }
            abort() { this.onend?.(); }
        };
    });
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await context.route('**/api/**', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify({ code: 200, data: [], tasks: [], models: [] }) }));
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(origin);
    await page.evaluate(() => {
        sessionId = 'mobile-ui-fixture';
        currentConversationId = 'fixture-chat';
        loginPage.classList.add('hidden');
        app.classList.remove('hidden');
        closeSidebar();
        setFilePanelState(false);
        window.VeraMobileChat?.syncComposer();
    });
    await page.locator('#ai-text-input').waitFor({ state: 'visible' });
    await page.waitForTimeout(100);
    return { page, context, errors };
}
async function dimensions(page) {
    return page.evaluate(() => {
        const rect = selector => {
            const r = document.querySelector(selector).getBoundingClientRect();
            return { x: r.x, y: r.y, w: r.width, h: r.height, bottom: r.bottom };
        };
        return {
            card: rect('#ai-chat-container'), input: rect('#ai-text-input'), more: rect('#ai-more-btn'),
            font: getComputedStyle(document.querySelector('#ai-text-input')).fontSize,
            screen: { width: innerWidth, height: innerHeight },
            overflow: document.documentElement.scrollWidth > innerWidth,
        };
    });
}
async function history(page) {
    await page.evaluate(() => {
        const target = document.getElementById('messages-container');
        target.replaceChildren();
        for (let i = 0; i < 18; i++) {
            const row = document.createElement('div'); row.className = 'message-row ' + (i % 2 ? 'ai' : 'user');
            row.innerHTML = `<div class="message-content"><div class="message-bubble">${i % 2 ? '這是一段用於檢查手機閱讀與捲動的回答。'.repeat(5) : '請幫我整理今天的學習重點。'}</div></div>`;
            target.append(row);
        }
        syncConversationLayout();
        scrollMessagesToBottom();
    });
    await page.waitForTimeout(100);
}
async function phoneChecks(browser, label) {
    for (const viewport of [{ width: 320, height: 640 }, { width: 393, height: 852 }, { width: 430, height: 932 }]) {
        const { page, context, errors } = await open(browser, viewport, { isMobile: true, hasTouch: true, deviceScaleFactor: 1 });
        const d = await dimensions(page);
        assert.equal(d.overflow, false);
        assert.ok(d.card.h >= 52 && d.card.h <= 56, JSON.stringify(d));
        assert.ok(d.card.x >= 8 && d.card.x <= 12, JSON.stringify(d));
        assert.ok(d.screen.width - d.card.x - d.card.w <= 12.5, JSON.stringify(d));
        assert.ok(d.card.bottom <= viewport.height && d.card.bottom >= viewport.height - 12, JSON.stringify(d));
        assert.equal(d.font, '16px');
        assert.ok(d.more.w >= 44 && d.more.h >= 44);
        assert.equal(await page.locator('#ai-voice-btn').isVisible(), true);
        assert.equal(await page.locator('#ai-send-btn').isVisible(), false);
        const input = page.locator('#ai-text-input');
        await input.fill('手機輸入測試');
        assert.equal(await page.locator('#ai-send-btn').isVisible(), true);
        assert.equal(await page.locator('#ai-voice-btn').isVisible(), false);
        assert.equal(await page.locator('#ai-send-btn').evaluate(el => getComputedStyle(el, '::before').content.replaceAll('"', '')), '↑');
        await input.press('End');
        await input.press('Enter');
        assert.equal(await input.inputValue(), '手機輸入測試\n');
        // Chinese IME confirmation must not submit or prevent the native event.
        assert.equal(await input.evaluate(el => {
            const event = new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true, cancelable: true });
            el.dispatchEvent(event); return event.defaultPrevented;
        }), false);
        await input.fill('這是一段較長的輸入，用於檢查多行文字的高度與內部捲動。'.repeat(30));
        const long = await dimensions(page);
        assert.ok(long.input.h <= 116.5 && long.input.h > 44);
        assert.equal(await input.evaluate(el => getComputedStyle(el).overflowY), 'auto');
        await input.fill('');
        assert.ok((await dimensions(page)).card.h <= 56);
        await page.locator('#ai-voice-btn').click();
        await page.evaluate(() => {
            const result = Object.assign([{ transcript: '口述文字測試' }], { isFinal: true });
            window.__fixtureRecognition.onresult({ results: [result] });
        });
        assert.equal(await input.inputValue(), '口述文字測試');
        assert.equal(await page.locator('#ai-voice-btn').isVisible(), true);
        assert.equal(await page.locator('#ai-send-btn').isVisible(), false);
        await page.locator('#ai-voice-btn').click();
        await page.locator('#ai-send-btn').waitFor({ state: 'visible' });
        await input.fill('');
        // The stop button remains reachable even when the draft is empty.
        await page.evaluate(() => {
            document.getElementById('ai-chat-container').classList.add('has-active-turn');
            window.VeraMobileChat.syncComposer();
        });
        assert.equal(await page.locator('#ai-send-btn').isVisible(), true);
        await page.evaluate(() => {
            document.getElementById('ai-chat-container').classList.remove('has-active-turn');
            window.VeraMobileChat.syncComposer();
            addFileTag('學習筆記.pdf', 'fixture-file-1', 1024);
            addFileTag('本週練習.docx', 'fixture-file-2', 2048);
        });
        await page.locator('#composer-files-summary').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#file-tags-container').isVisible(), false);
        await page.locator('#composer-files-summary').click();
        assert.equal(await page.locator('#file-tags-container').isVisible(), true);
        await page.locator('.tag-close-btn').first().click();
        assert.equal(await page.locator('.file-tag').count(), 1);
        assert.equal(await page.locator('#composer-files-summary').isVisible(), false);
        await page.evaluate(() => clearFileRefs());
        await page.locator('#ai-more-btn').click();
        assert.equal(await page.locator('#composer-upload-btn').isVisible(), true);
        assert.equal(await page.locator('.composer-sheet-backdrop').isVisible(), true);
        const chooser = page.waitForEvent('filechooser');
        await page.locator('#composer-upload-btn').click();
        assert.equal((await chooser).isMultiple(), true);
        assert.equal(await page.locator('#composer-menu').isVisible(), false);
        await page.locator('#ai-more-btn').click();
        await page.locator('#composer-files-btn').click();
        assert.equal(await page.locator('#file-panel').isVisible(), true);
        await page.evaluate(() => setFilePanelState(false));
        await history(page);
        assert.ok(await page.evaluate(() => {
            const viewport = document.querySelector('.messages-inner').getBoundingClientRect();
            const dock = document.getElementById('ai-chat-container').getBoundingClientRect();
            return viewport.bottom <= dock.top;
        }), 'The message viewport must end above the mobile input dock');
        const messageMargin = await page.locator('.message-row.ai').first().evaluate(el => el.getBoundingClientRect().left);
        assert.ok(messageMargin >= 8 && messageMargin <= 12);
        assert.equal((await dimensions(page)).card.h, d.card.h);
        await page.evaluate(() => { shouldAutoStickToBottom = false; document.querySelector('.messages-inner').scrollTop = 100; });
        await page.locator('.chat-latest-button').waitFor({ state: 'visible' });
        await input.fill('編輯草稿時保留閱讀位置');
        assert.ok(await page.evaluate(() => document.querySelector('.messages-inner').scrollTop < 500));
        await page.locator('.chat-latest-button').click();
        await page.waitForFunction(() => {
            const el = document.querySelector('.messages-inner'); return el.scrollHeight - el.scrollTop - el.clientHeight < 3;
        });
        await input.fill('');
        await page.waitForTimeout(100);
        if (viewport.width === 393) {
            await page.screenshot({ path: path.join(output, `${label}-iphone15-chat.png`) });
            await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
            await page.waitForTimeout(300);
            await page.screenshot({ path: path.join(output, `${label}-iphone15-dark.png`) });
        }
        assert.deepEqual(errors, []);
        report.push({ browser: label, viewport, ...d });
        await context.close();
    }
}
async function interactionChecks(browser, label) {
    const { page, context, errors } = await open(browser, { width: 393, height: 852 }, { isMobile: true, hasTouch: true });
    await page.route('**/api/chat', route => route.fulfill({
        contentType: 'text/event-stream',
        body: 'data: {"type":"content","text":"手機測試回覆"}\n\ndata: {"type":"done","message_id":"fixture-reply"}\n\n',
    }));
    await page.locator('#ai-text-input').fill('請回覆這個問題');
    const request = page.waitForRequest(request => new URL(request.url()).pathname === '/api/chat');
    await page.locator('#ai-send-btn').click();
    assert.equal((await request).postDataJSON().question, '請回覆這個問題');
    await page.getByText('手機測試回覆', { exact: true }).waitFor();
    assert.equal(await page.locator('#ai-text-input').inputValue(), '');
    await page.locator('#ai-voice-btn').waitFor({ state: 'visible' });
    // A live turn must still expose its existing pause/stop handler.
    await page.evaluate(() => {
        window.__fixtureStopped = false;
        browserTurns.set(currentConversationId, {
            session: sessionId, kind: 'chat',
            stop() { window.__fixtureStopped = true; browserTurns.delete(currentConversationId); syncTurnButtons(); },
        });
        syncTurnButtons();
    });
    assert.equal(await page.locator('#ai-send-btn').getAttribute('aria-label'), '停止回答');
    await page.locator('#ai-send-btn').click();
    assert.equal(await page.evaluate(() => window.__fixtureStopped), true);
    // Typed drafts can still enter dictation through the mobile menu.
    await page.locator('#ai-text-input').fill('現有草稿');
    await page.locator('#ai-more-btn').click();
    await page.locator('#composer-voice-btn').click();
    assert.equal(await page.locator('#ai-voice-btn').isVisible(), true);
    assert.equal(await page.locator('#ai-voice-btn').getAttribute('aria-pressed'), 'true');
    await page.locator('#ai-voice-btn').click();
    assert.deepEqual(errors, []);
    report.push({ browser: label, interaction: 'fixture chat send/response, live stop control, dictation from a typed draft passed' });
    await context.close();
}
async function deviceChecks(browser, label) {
    for (const viewport of [{ width: 852, height: 393 }, { width: 820, height: 1180 }, { width: 768, height: 1024 }]) {
        const { page, context, errors } = await open(browser, viewport, { isMobile: true, hasTouch: true });
        const d = await dimensions(page);
        assert.equal(d.overflow, false);
        if (viewport.width === 820) {
            assert.equal(await page.locator('#ai-send-btn').isVisible(), true);
            assert.equal(await page.locator('#ai-voice-btn').isVisible(), true);
            assert.ok(d.more.w >= 44 && d.more.h >= 44);
        } else {
            assert.ok(d.card.h <= 56 && d.card.bottom <= viewport.height);
        }
        await page.locator('#sidebar-open-btn').click();
        assert.equal(await page.locator('#sidebar').isVisible(), true);
        if (viewport.width !== 820) assert.equal(await page.locator('#sidebar-overlay').evaluate(el => getComputedStyle(el).visibility), 'visible');
        await page.locator('#sidebar-close-btn').click();
        assert.equal(await page.locator('#sidebar').getAttribute('aria-hidden'), 'true');
        await page.waitForTimeout(350);
        await page.locator('#ai-text-input').fill('橫豎屏與平板檢查。'.repeat(30));
        if (viewport.height === 393) assert.ok((await dimensions(page)).input.h <= 68.5);
        await page.screenshot({ path: path.join(output, `${label}-${viewport.width}x${viewport.height}.png`) });
        assert.deepEqual(errors, []);
        report.push({ browser: label, deviceViewport: viewport, ...d });
        await context.close();
    }
}
async function keyboardChecks(browser, label) {
    const { page, context, errors } = await open(browser, { width: 393, height: 852 }, { isMobile: true, hasTouch: true });
    await page.evaluate(() => {
        const viewport = new EventTarget();
        Object.assign(viewport, { height: 852, width: 393, offsetTop: 0, scale: 1 });
        Object.defineProperty(window, 'visualViewport', { configurable: true, value: viewport });
        window.__setViewport = (height, top = 0) => {
            Object.assign(viewport, { height, offsetTop: top });
            window.dispatchEvent(new Event('resize'));
        };
    });
    await page.locator('#ai-text-input').focus();
    // Incremental keyboard animation used to defeat the old 80px delta check.
    for (const height of [800, 750, 700, 650, 600, 550, 500, 430]) {
        await page.evaluate(height => window.__setViewport(height), height);
        await page.waitForTimeout(20);
    }
    assert.ok((await dimensions(page)).card.bottom <= 430);
    assert.equal(await page.evaluate(() => document.documentElement.classList.contains('mobile-keyboard-open')), true);
    await page.evaluate(() => window.__setViewport(410, 24));
    await page.waitForTimeout(60);
    const d = await dimensions(page);
    assert.ok(d.card.bottom <= 434 && d.card.bottom >= 420, JSON.stringify(d));
    await page.locator('#ai-more-btn').click();
    const menuBottom = await page.locator('#composer-menu').evaluate(el => el.getBoundingClientRect().bottom);
    assert.ok(menuBottom <= 434.5);
    await page.locator('#composer-menu-close').click();
    await page.evaluate(() => window.__setViewport(852));
    await page.waitForTimeout(60);
    assert.equal(await page.evaluate(() => document.documentElement.classList.contains('mobile-keyboard-open')), false);
    assert.ok((await dimensions(page)).card.bottom >= 840);
    assert.deepEqual(errors, []);
    report.push({ browser: label, keyboard: 'gradual resize, viewport pan, menu position, restoration passed' });
    await context.close();
}
async function desktopChecks(browser) {
    const { page, context, errors } = await open(browser, { width: 1440, height: 900 });
    const after = await dimensions(page);
    assert.equal(await page.locator('#ai-voice-btn').isVisible(), true);
    assert.equal(await page.locator('#ai-send-btn').isVisible(), true);
    assert.equal(await page.locator('.composer-more-plus').isVisible(), false);
    await page.locator('#ai-more-btn').click();
    assert.equal(await page.locator('#composer-upload-btn').isVisible(), false);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#ai-text-input').evaluate(el => {
        const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
        el.dispatchEvent(event); return event.defaultPrevented;
    }), true);
    if (before) {
        await page.goto(`${origin}/before.html`);
        await page.evaluate(() => {
            sessionId = 'mobile-ui-fixture'; loginPage.classList.add('hidden'); app.classList.remove('hidden');
            closeSidebar(); setFilePanelState(false);
        });
        await page.waitForTimeout(100);
        assert.deepEqual(await dimensions(page), after);
    }
    assert.deepEqual(errors, []);
    report.push({ desktop: '1440 × 900 layout matches original; all three controls and Enter-to-send retained', ...after });
    await context.close();
}
(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = `http://127.0.0.1:${server.address().port}`;
    const browser = await chromium.launch({ channel: process.env.VERA_BROWSER_CHANNEL || 'chrome', headless: true });
    try {
        await phoneChecks(browser, 'chromium');
        await keyboardChecks(browser, 'chromium');
        await interactionChecks(browser, 'chromium');
        await deviceChecks(browser, 'chromium');
        await desktopChecks(browser);
    } finally { await browser.close(); }
    if (process.env.VERA_CHECK_WEBKIT === '1') {
        const safari = await webkit.launch({ headless: true });
        try { await phoneChecks(safari, 'webkit'); await keyboardChecks(safari, 'webkit'); await interactionChecks(safari, 'webkit'); await deviceChecks(safari, 'webkit'); }
        finally { await safari.close(); }
    }
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(report, null, 2));
    console.log(`Passed ${report.length} browser scenarios. Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
