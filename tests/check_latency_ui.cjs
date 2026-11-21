/* Offline fixture only: owns an ephemeral server and mocks all API/provider requests. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');

if (process.env.VERA_LIVE_ORIGIN || process.env.VERA_FIXTURE_URL || process.env.VERA_BEFORE_HTML) {
    throw new Error('This check owns its offline fixture; unset external URL and HTML overrides.');
}
const root = path.resolve(__dirname, '..');
const assets = new Set(['index.html', 'speech-input.js', 'task-manager.css', 'task-manager.js',
    'account-settings.css', 'account-settings.js', 'wechat-connection.css', 'wechat-connection.js',
    'chat-interactions.css', 'chat-interactions.js', 'marked.js', 'dompurify.min.js',
    'font-awesome.css', 'fonts/fontawesome-webfont.woff2', 'logo.png']);
const types = { '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript',
    '.woff2': 'font/woff2', '.png': 'image/png' };
const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const name = decodeURIComponent(url.pathname.replace(/^\/static\//, '').replace(/^\//, '')) || 'index.html';
    if (!assets.has(name)) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'Content-Type': types[path.extname(name)] || 'application/octet-stream' });
    res.end(fs.readFileSync(path.join(root, name)));
});
const captchaUrl = 'https://api-js.jumdata.com/action-captcha/action-captcha.min.js';
let origin;

(async () => {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    origin = `http://127.0.0.1:${server.address().port}`;
    const browser = await chromium.launch({ headless: true, channel: process.env.VERA_BROWSER_CHANNEL || 'chrome' });
    const report = [];
    try {
        for (const mode of ['enabled', 'disabled', 'retry']) {
            const context = await browser.newContext({ serviceWorkers: 'block' });
            let scripts = 0, configs = 0;
            const errors = [];
            await context.route('**/*', route => {
                const url = new URL(route.request().url());
                if (url.href === captchaUrl) {
                    scripts++;
                    if (mode === 'retry' && scripts === 1) return route.abort();
                    return route.fulfill({ contentType: 'application/javascript', body:
                        "window.JumeiActionCaptcha=function(id,cb,opts){this.show=()=>cb({ret:0,ticket:'fixture-ticket',randstr:'fixture-random'})}" });
                }
                if (url.origin !== origin) return route.abort();
                if (url.pathname === '/api/captcha/config') {
                    configs++;
                    return route.fulfill({ json: mode === 'disabled' ? { enabled: false } :
                        { enabled: true, app_id: 'synthetic-app', script_url: captchaUrl } });
                }
                if (url.pathname.startsWith('/api/')) {
                    return route.fulfill({ json: { code: 200, data: [], tasks: [], models: [] } });
                }
                return route.continue();
            });
            try {
                const page = await context.newPage();
                page.on('pageerror', error => errors.push(error.message));
                await page.goto(origin, { waitUntil: 'domcontentloaded' });
                await page.waitForFunction(() => typeof requestCaptchaTicket === 'function');
                await page.evaluate(() => warmCaptcha().catch(() => {}));
                assert.equal(configs, 1);
                assert.equal(scripts, mode === 'disabled' ? 0 : mode === 'retry' ? 2 : 1);
                const result = await page.evaluate(() => requestCaptchaTicket());
                assert.deepEqual(result, mode === 'disabled' ? null :
                    { captcha_ticket: 'fixture-ticket', captcha_randstr: 'fixture-random' });
                await page.evaluate(() => requestCaptchaTicket());
                assert.equal(scripts, mode === 'disabled' ? 0 : mode === 'retry' ? 2 : 1);
                assert.deepEqual(errors, []);
                report.push({ mode, scripts, configs, passed: true });
            } finally { await context.close(); }
        }
        console.log(JSON.stringify(report));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => server.close());
