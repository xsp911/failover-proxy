// Offline usability contract. All requests intercepted; no services/provider calls.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); }
catch { ({chromium} = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    const errors = [], external = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    const base = {id: 'p', name: 'DeepSeek <img src=x>', revision: 3, model: 'deepseek-v3.2', reference_model: 'deepseek-v3.2', protocol: 'chat', token_id: 11, test_token_ids: [22,33,44,55], enabled: true, auto_publish: true, max_probe_requests: 60, max_probe_usd: 1, auto_daily_requests: 600, auto_daily_usd: 5, status: 'draft', candidates: [], members: [], auto_state: {next_refresh_at: 1800000000, last_error: 'worker_error'}};
    let policies = [base, {...base, id: 'other', name: 'Other', auto_publish: false}], jobs = Array.from({length: 5}, (_, i) => ({id: 'old-' + i, pool_id: 'p', kind: 'verify', status: 'failed', created_at: new Date(1700000000000 + i * 1000).toISOString(), progress: 0, total: 3}));
    jobs.push({id: 'unrelated', pool_id: 'other', kind: 'verify', status: 'running', progress: 0, total: 3});
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url()), method = req.method();
      if (url.origin !== 'http://usability.invalid') { external.push(req.url()); return route.abort(); }
      const reply = (json, status = 200) => route.fulfill({json, status});
      if (method === 'GET' && url.pathname === '/admin/observability') return reply({}, 404);
      if (method === 'GET' && /^\/admin\/a6\/pools\/[^/]+\/events$/.test(url.pathname)) return reply({events: []});
      if (url.pathname.startsWith('/admin/')) {
        if (method !== 'GET') { assert.equal(req.headers()['x-csrf-token'], 'csrf'); writes.push({path: url.pathname, method, body: req.postDataJSON()}); }
        if (url.pathname === '/admin/session') return reply({username: 'admin', csrf_token: 'csrf'});
        if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: []});
        if (url.pathname === '/admin/health') return reply({status: 'ok'});
        if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
        if (url.pathname === '/admin/a6/tokens') return reply({items: []});
        if (url.pathname === '/admin/a6/pools') return reply({pools: policies, jobs, tokens: [11,22,33,44,55].map(id => ({id, name: 'Key ' + id, status: 1})), catalog: [{id: 'deepseek-v3.2'}], capabilities: {}});
        if (url.pathname === '/admin/a6/pools/p/run') {
          assert.deepEqual(req.postDataJSON(), {confirm_cost: true});
          const job = {id: 'new', pool_id: 'p', kind: base.auto_publish ? 'auto_refresh' : 'verify', status: 'running', phase: 'testing', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), last_activity_at: new Date().toISOString(), progress: 0, total: 3, workers: [22,33,44,55].map((token_id, i) => ({token_id, status: i < 2 ? 'running' : 'queued', phase: i < 2 ? 'testing' : 'idle', request_stage: 'streaming', current_channel_id: i + 1, challenge_id: 'probe', last_activity_at: new Date().toISOString()}))};
          jobs.push(job); return reply(job, 202);
        }
        if (url.pathname === '/admin/a6/pools/jobs/new/cancel') { jobs.find(j => j.id === 'new').status = 'cancelled'; return reply({ok: true}); }
        if (url.pathname === '/admin/a6/pools/p' && method === 'DELETE') { assert.deepEqual(req.postDataJSON(), {revision: 3, confirm_remote: true}); policies = policies.filter(p => p.id !== 'p'); return reply({ok: true}); }
        throw new Error('Unexpected API ' + method + ' ' + url.pathname);
      }
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.goto('http://usability.invalid/'); await page.locator('#app').waitFor({state: 'visible'});
    await page.locator('#tab-market').click(); await page.locator('.pool-card').first().click();
    assert.equal(writes.length, 0, 'selection is free');
    assert.equal(await page.locator('#pool-settings').evaluate(n => n.open), false);
    assert.equal(await page.evaluate(() => !!(document.querySelector('#pool-details').compareDocumentPosition(document.querySelector('#pool-settings')) & Node.DOCUMENT_POSITION_FOLLOWING)), true);
    assert.match(await page.locator('#pool-current-state').textContent(), /Сейчас проверка не выполняется\. Следующий автоматический отбор/);
    assert.match(await page.locator('#pool-worker-state').textContent(), /Выбрано тестовых ключей: 4 \/ 4\. Сейчас проверяют кандидатов: 0 \/ 4/);
    assert.equal(await page.locator('#pool-jobs > .pool-job').count(), 3);
    assert.equal(await page.locator('#pool-jobs > .pool-job').first().locator('strong').first().textContent(), 'Проверка · Ошибка');
    assert.equal(await page.locator('#pool-jobs details').evaluate(n => n.open), false);
    assert.doesNotMatch(await page.locator('#pool-jobs').textContent(), /Пул other/);
    await page.locator('#pool-run').click(); assert.equal(writes.length, 0);
    assert.match(await page.locator('#confirm-description').textContent(), /600 запросов и 5 USD/);
    await page.locator('#confirm-dialog button[value=cancel]').click(); assert.equal(writes.length, 0);
    await page.locator('#pool-run').click(); await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelector('#pool-worker-state').textContent.includes('2 / 4'));
    assert.equal(await page.locator('#pool-cancel').isVisible(), true);
    assert.equal(await page.locator('#pool-run').isDisabled(), true);
    assert.match(await page.locator('#pool-jobs').textContent(), /Этап запроса: Получение ответа/);
    await page.locator('#pool-cancel').click(); await page.waitForFunction(() => document.querySelector('#pool-current-state').textContent.includes('не выполняется'));
    base.auto_publish = false;
    await page.locator('#pools-refresh').click();
    await page.waitForFunction(() => document.querySelector('#pool-workflow-publish').textContent.includes('Вручную'));
    await page.locator('#pool-run').click(); assert.match(await page.locator('#confirm-description').textContent(), /Ручная проверка не публикует маршрут/);
    await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelector('#pool-cancel').hidden === false);
    await page.locator('#pool-section-tab-settings').click();
    if (!await page.locator('#pool-settings').evaluate(node => node.open)) await page.locator('#pool-settings > summary').click();
    await page.locator('#pool-name').fill('Unsaved');
    assert.equal(await page.locator('#pool-delete').isDisabled(), false, 'delete available with dirty settings and active job');
    await page.locator('#pool-section-tab-overview').click();
    await page.locator('#pool-delete').click();
    assert.match(await page.locator('#confirm-description').textContent(), /автоматические проверки остановятся.*Рабочий маршрут будет безопасно закрыт.*Подписки, каналы и токены A6API не удаляются/);
    await page.locator('#confirm-dialog button[value=cancel]').click(); assert.equal(writes.filter(w => w.method === 'DELETE').length, 0);
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    await page.locator('#pool-delete').click(); await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelectorAll('.pool-card').length === 1);
    await page.locator('#pool-new').click(); assert.equal(await page.locator('#pool-settings').evaluate(n => n.open), true);
    assert.equal(await page.locator('#market-pools-panel img').count(), 0);
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log('PASS offline usability: overview first, collapsed existing/open new settings, free selection, idle schedule, selected vs active workers, selected recent jobs/history, run auto/manual paid consent, cancel, safe confirmed deletion with dirty active pool, mobile, safe DOM');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
