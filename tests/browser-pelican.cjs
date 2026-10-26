// Offline Pelican contract. Intercept every request; never call live models/services.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); }
catch { ({chromium} = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const origin = 'http://pelican-browser.invalid';
(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [], external = [], unexpected = [], requests = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const schedule = window.setTimeout.bind(window), cancel = window.clearTimeout.bind(window), polls = new Map();
      window.setTimeout = (callback, delay, ...args) => {
        const id = schedule(() => { polls.delete(id); callback(...args); }, delay);
        if ([5000, 15000].includes(delay)) polls.set(id, {delay, callback: () => callback(...args)});
        return id;
      };
      window.clearTimeout = id => { polls.delete(id); cancel(id); };
      window.__pelicanPollDelays = () => [...polls.values()].map(poll => poll.delay);
      window.__runPelicanPoll = () => {
        const entry = [...polls.entries()].at(-1);
        if (!entry) throw new Error('No scheduled poll');
        const [id, poll] = entry; polls.delete(id); cancel(id); poll.callback();
      };
      window.__setPageHidden = hidden => { Object.defineProperty(document, 'hidden', {configurable: true, value: hidden}); document.dispatchEvent(new Event('visibilitychange')); };
    });
    const providers = [{id: 'provider-a', name: 'Provider <img src=x onerror=alert(1)>', models: ['upstream-a', 'upstream-b']}, {id: 'provider-b', name: 'Provider B', models: ['upstream-c']}, {id: 'provider-empty', name: 'Empty provider', models: []}];
    const makeRun = overrides => ({id: 'run-a', provider_id: 'provider-a', provider_name: providers[0].name, model: 'upstream-b', protocol: 'responses', status: 'queued', phase: 'queued', created_at: '2026-10-26T11:00:00Z', finished_at: null, elapsed_ms: 0, error_kind: null, checks: {safe_svg: null, animation: null, nonce: null}, svg_available: false, usage: null, timeout_seconds: 300, scene: 'seaside-boardwalk', generation_count: 1, ...overrides});
    let runs = [], postFailure = null, listFailure = null, expired = false, holdList = null, releaseList = null;
    const svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 240"><script>window.parent.__svgScriptExecuted=true;fetch("https://must-not-run.invalid/")</script><rect width="400" height="240" fill="skyblue"/><circle cx="160" cy="140" r="30" fill="white"><animate attributeName="cx" values="160;200;160" dur="2s" repeatCount="indefinite"/></circle></svg>';
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), method = request.method();
      if (url.origin !== origin) { external.push(request.url()); return route.abort(); }
      const reply = (json, status = 200) => route.fulfill({json, status});
      requests.push({path: url.pathname, method});
      if (url.pathname.startsWith('/admin/')) {
        if (method !== 'GET') {
          assert.equal(request.headers()['x-csrf-token'], 'csrf');
          writes.push({path: url.pathname, method, body: request.postDataJSON()});
        }
        if (url.pathname === '/admin/session') return reply(expired ? {error: {code: 'unauthorized'}} : {username: 'admin', csrf_token: 'csrf'}, expired ? 401 : 200);
        if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: []});
        if (url.pathname === '/admin/health') return reply({status: 'ok'});
        if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
        if (url.pathname === '/admin/observability') return reply({}, 404);
        if (url.pathname === '/admin/pelican' && method === 'GET') {
          if (holdList) { const promise = holdList; holdList = null; await promise; }
          if (expired) return reply({error: {code: 'unauthorized', message: 'SECRET-do-not-render'}}, 401);
          if (listFailure) return reply({error: {code: 'unsafe SECRET <svg onload=alert(1)>', message: 'SECRET-do-not-render'}}, listFailure);
          return reply({providers, runs, scene: {id: 'seaside-boardwalk', title: 'Пеликан на велосипеде — морская набережная', prompt: 'SECRET-do-not-render'}, timeout_seconds: 300, max_output_tokens: 16000, max_parallel: 1});
        }
        if (url.pathname === '/admin/pelican' && method === 'POST') {
          assert.deepEqual(request.postDataJSON(), {provider_id: 'provider-a', model: 'upstream-b', protocol: 'responses', confirm_cost: true, effort: 'high'});
          if (postFailure) return reply({error: {code: postFailure, message: 'SECRET-do-not-render'}}, 409);
          runs = [makeRun()]; return reply(runs[0], 202);
        }
        if (url.pathname === '/admin/pelican/run-a/cancel' && method === 'POST') {
          assert.deepEqual(request.postDataJSON(), {});
          runs = [{...runs[0], status: 'cancelled', phase: 'finished', finished_at: '2026-10-26T11:00:10Z', elapsed_ms: 10000}]; return reply(runs[0]);
        }
        if (url.pathname === '/admin/pelican/run-a/svg' && method === 'GET') return route.fulfill({body: svg, contentType: 'image/svg+xml', headers: {'Content-Security-Policy': "default-src 'none'; script-src 'none'; sandbox", 'X-Content-Type-Options': 'nosniff'}});
        if (url.pathname === '/admin/logout' && method === 'POST') { expired = true; return reply({ok: true}); }
        unexpected.push(method + ' ' + url.pathname); return reply({error: {code: 'unexpected_offline_api'}}, 500);
      }
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) { unexpected.push(method + ' ' + url.pathname); return route.abort(); }
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    const listCount = () => requests.filter(request => request.path === '/admin/pelican' && request.method === 'GET').length;
    const starts = () => writes.filter(write => write.path === '/admin/pelican');
    const pollReady = async delay => page.waitForFunction(delay => window.__pelicanPollDelays().includes(delay), delay);
    const refresh = async () => { const response = page.waitForResponse(response => response.url() === origin + '/admin/pelican' && response.request().method() === 'GET'); await page.locator('#pelican-refresh').click(); await response; await page.waitForFunction(() => !document.querySelector('#pelican-refresh').disabled); };
    const poll = async delay => { await pollReady(delay); const response = page.waitForResponse(response => response.url() === origin + '/admin/pelican' && response.request().method() === 'GET'); await page.evaluate(() => window.__runPelicanPoll()); await response; await pollReady(delay); };
    await page.goto(origin + '/'); await page.locator('#app').waitFor({state: 'visible'});
    assert.equal(listCount(), 0, 'other tabs never fetch Pelican');
    await page.locator('#tab-pelican').click(); await pollReady(15000);
    assert.equal(listCount(), 1);
    for (const tab of ['stats', 'settings', 'detector', 'detector-log', 'market']) assert.equal(await page.locator('#' + tab + '-view').isVisible(), false, tab + ' hidden');
    assert.equal(await page.locator('#tab-pelican').getAttribute('aria-current'), 'page');
    assert.equal(await page.locator('#pelican-provider').inputValue(), 'provider-a');
    assert.equal(await page.locator('#pelican-model').inputValue(), 'upstream-a');
    assert.equal(await page.locator('#pelican-model option').count(), 2);
    await page.locator('#pelican-provider').selectOption('provider-b');
    assert.equal(await page.locator('#pelican-model').inputValue(), 'upstream-c');
    await page.locator('#pelican-provider').selectOption('provider-empty'); assert.equal(await page.locator('#pelican-start').isDisabled(), true);
    await page.locator('#pelican-provider').selectOption('provider-a'); await page.locator('#pelican-model').selectOption('upstream-b'); await page.locator('#pelican-protocol').selectOption('responses');
    assert.equal(await page.locator('#pelican-timeout').getAttribute('readonly'), '');
    assert.equal(await page.locator('#pelican-effort').inputValue(), '');
    assert.deepEqual(await page.locator('#pelican-effort option').evaluateAll(os => os.map(o => o.value)), ['', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh']);
    await page.locator('#pelican-effort').selectOption('high');
    assert.match(await page.locator('#pelican-view').textContent(), /Структурные проверки.*не доказывают подлинность/);
    assert.match(await page.locator('#pelican-view').textContent(), /без автоматической оценки качества/);
    assert.equal(await page.locator('#pelican-view img, #pelican-view svg, #pelican-view object, #pelican-view embed, #pelican-view iframe').count(), 0);
    assert.equal(writes.length, 0, 'navigation/selection cannot spend money');
    await poll(15000);
    assert.equal(await page.locator('#pelican-model').inputValue(), 'upstream-b');
    assert.equal(await page.locator('#pelican-protocol').inputValue(), 'responses');
    await page.locator('#pelican-start').click(); await page.locator('#confirm-dialog').waitFor({state: 'visible'});
    assert.match(await page.locator('#confirm-description').textContent(), /ровно один платный запрос/);
    assert.match(await page.locator('#confirm-description').textContent(), /5 минут/);
    assert.match(await page.locator('#confirm-description').textContent(), /effort high/);
    assert.equal(await page.locator('#pelican-effort').inputValue(), 'high');
    assert.equal(starts().length, 0);
    assert.deepEqual(await page.evaluate(() => window.__pelicanPollDelays()), [], 'no polling during consent');
    await page.locator('#confirm-dialog button[value=cancel]').click(); await pollReady(15000); assert.equal(starts().length, 0);
    await page.locator('#pelican-start').click(); await page.locator('#confirm-action').click(); await pollReady(5000);
    assert.equal(starts().length, 1); assert.equal(await page.locator('#pelican-start').isDisabled(), true);
    assert.match(await page.locator('#pelican-runs').textContent(), /В очереди/);
    assert.equal(await page.locator('#pelican-runs img').count(), 0, 'no preview before SVG is available');
    runs = [makeRun({status: 'running', phase: 'generating', elapsed_ms: 12345})];
    await poll(5000); assert.match(await page.locator('#pelican-runs').textContent(), /Генерация SVG.*12,3 с.*5 минут/);
    assert.equal(starts().length, 1, 'poll never retries generation');
    await page.getByRole('button', {name: 'Отменить генерацию', exact: true}).click(); await pollReady(15000);
    assert.match(await page.locator('#pelican-runs').textContent(), /Отменено/);
    assert.equal(writes.filter(write => write.path.endsWith('/cancel')).length, 1);
    runs = [makeRun({status: 'done', phase: 'finished', svg_available: true, elapsed_ms: 25000, finished_at: '2026-10-26T11:00:25Z', checks: {safe_svg: true, animation: true, nonce: true}, usage: {input_tokens: 123, output_tokens: 456}})];
    await refresh();
    const image = page.locator('#pelican-runs img');
    await page.waitForFunction(() => { const image = document.querySelector('#pelican-runs img'); return image?.complete && image.naturalWidth > 0; });
    assert.equal(await image.getAttribute('src'), '/admin/pelican/run-a/svg');
    assert.equal(await image.evaluate(node => node.namespaceURI), 'http://www.w3.org/1999/xhtml');
    assert.equal(await page.locator('#pelican-view svg, #pelican-view object, #pelican-view embed, #pelican-view iframe, #pelican-view script').count(), 0);
    assert.equal(await page.evaluate(() => window.__svgScriptExecuted), undefined, 'SVG scripts never run');
    assert.match(await page.locator('#pelican-runs').textContent(), /Безопасный SVG: пройдена/);
    assert.match(await page.locator('#pelican-runs').textContent(), /123 \/ 456/);
    const previewRequests = requests.filter(request => request.path.endsWith('/svg')).length;
    runs = [{...runs[0], checks: {safe_svg: true, animation: false, nonce: false}}];
    await refresh(); assert.equal(requests.filter(request => request.path.endsWith('/svg')).length, previewRequests, 'poll preserves existing image rather than reloading animation');
    assert.match(await page.locator('#pelican-runs').textContent(), /Завершено/);
    assert.match(await page.locator('#pelican-runs').textContent(), /Анимация: не пройдена/);
    assert.match(await page.locator('#pelican-runs').textContent(), /Маркер запроса: не пройдена/);
    assert.equal(await page.locator('#pelican-runs img').count(), 1, 'safe completed SVG still shown when semantic structural checks fail');
    assert.equal(await page.locator('#pelican-view').textContent().then(value => value.includes('SECRET')), false);
    // Failed mutation remains visible even after the follow-up GET; no automatic retry.
    postFailure = 'pelican_busy'; await page.locator('#pelican-start').click(); await page.locator('#confirm-action').click(); await pollReady(15000);
    await page.locator('#pelican-error').waitFor({state: 'visible'}); assert.match(await page.locator('#pelican-error').textContent(), /Уже выполняется генерация/);
    assert.equal(starts().length, 2); postFailure = null;
    // Unknown upstream values/bodies never become UI text or markup.
    runs = [makeRun({status: 'error', phase: 'SECRET-do-not-render', error_kind: '<img src=x onerror=alert(1)>SECRET', elapsed_ms: 300000})];
    await refresh(); assert.equal(await page.locator('#pelican-runs').textContent().then(value => value.includes('SECRET')), false);
    assert.equal(await page.locator('#pelican-runs img').count(), 0);
    runs = [makeRun({status: 'error', phase: 'finished', error_kind: 'pelican_sensitive_output'})];
    await refresh(); assert.match(await page.locator('#pelican-runs').textContent(), /Ответ скрыт: обнаружены чувствительные данные/);
    assert.equal(await page.locator('#pelican-runs img').count(), 0);
    listFailure = 502; await refresh(); assert.match(await page.locator('#pelican-error').textContent(), /HTTP 502/); assert.equal(await page.locator('#pelican-error').textContent().then(value => value.includes('SECRET')), false); listFailure = null;
    // Active tab request cancellation; no background polling after changing tabs.
    holdList = new Promise(resolve => { releaseList = resolve; });
    await page.evaluate(() => document.querySelector('#pelican-refresh').click());
    await page.waitForFunction(() => document.querySelector('#pelican-refresh').disabled);
    const busyCount = listCount(); await page.evaluate(() => document.querySelector('#pelican-refresh').click()); assert.equal(listCount(), busyCount);
    await page.locator('#tab-settings').click(); releaseList();
    assert.deepEqual(await page.evaluate(() => window.__pelicanPollDelays()), []);
    assert.equal(await page.locator('#pelican-view').isVisible(), false);
    await page.locator('#tab-pelican').click(); await pollReady(15000);
    await page.evaluate(() => window.__setPageHidden(true)); assert.deepEqual(await page.evaluate(() => window.__pelicanPollDelays()), []);
    await page.evaluate(() => window.__setPageHidden(false)); await pollReady(15000);
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'mobile layout has no document overflow');
    // Expired session clears all previews/options and stops scheduling new requests.
    expired = true; await refresh(); await page.locator('#login-view').waitFor({state: 'visible'});
    assert.deepEqual(await page.evaluate(() => window.__pelicanPollDelays()), []);
    assert.equal(await page.locator('#pelican-runs').textContent(), ''); assert.equal(await page.locator('#pelican-provider option').count(), 0);
    const expiredCount = listCount(); await page.evaluate(() => { document.querySelector('#pelican-refresh').click(); document.querySelector('#pelican-form').dispatchEvent(new Event('submit', {cancelable: true})); });
    assert.equal(listCount(), expiredCount); assert.equal(starts().length, 2);
    // New session works; explicit logout aborts a pending list and prevents new requests.
    expired = false; await page.locator('#session-retry').click(); await page.locator('#app').waitFor({state: 'visible'});
    if (await page.locator('#nav-toggle').isVisible()) await page.locator('#nav-toggle').click();
    await page.locator('#tab-pelican').click(); await pollReady(15000);
    holdList = new Promise(resolve => { releaseList = resolve; });
    await page.evaluate(() => document.querySelector('#pelican-refresh').click()); await page.waitForFunction(() => document.querySelector('#pelican-refresh').disabled);
    await page.locator('#logout').click(); releaseList(); await page.locator('#login-view').waitFor({state: 'visible'});
    assert.deepEqual(await page.evaluate(() => window.__pelicanPollDelays()), []);
    assert.equal(await page.locator('#pelican-runs').textContent(), '');
    assert.equal(writes.filter(write => write.path === '/admin/logout').length, 1);
    assert.deepEqual(external, []); assert.deepEqual(unexpected, []); assert.deepEqual(errors, []);
    console.log('PASS offline Pelican browser: navigation, consent, exact payload, runs, cancellation, safe image, errors, polling, session expiry, mobile');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
