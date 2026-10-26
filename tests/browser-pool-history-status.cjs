// Offline history-status contract. Assets/API intercepted; no services or provider traffic.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}, timezoneId: 'UTC'});
    const errors = [], external = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    const now = Date.parse('2026-10-24T03:00:00Z');
    await page.addInitScript(now => { Date.now = () => now; }, now);
    const checkedAt = '2026-10-23T03:00:00Z', future = '2026-10-25T03:00:00Z';
    const reason = 'Safe previous reason <img src=x onerror=alert(1)>';
    const base = {channel_id: 1, listing_id: 2, supplier: 'Seller', eligible: true, verification_status: 'unverified', probability: 99, predicted_model: 'glm-reference', predicted_probability: 0.99, verified_at: checkedAt};
    const candidate = (channel_id, extra = {}) => ({...base, channel_id, ...extra});
    const policy = {id: 'history', name: 'GLM history', revision: 1, token_id: 11, test_token_id: 22, model: 'glm', reference_model: 'glm-reference', protocol: 'chat', enabled: true, auto_publish: true, status: 'published', max_members: 10, candidates: [
      candidate(1),
      ...['suspect', 'unknown', 'error'].map((last_check_status, i) => candidate(2 + i, {last_check_status, last_check_reason: reason, last_checked_at: checkedAt, next_check_at: future})),
      candidate(5, {last_check_status: 'error', last_checked_at: checkedAt, next_check_at: '2026-10-24T02:00:00Z'}),
      candidate(6, {last_check_status: 'suspect', last_checked_at: checkedAt, next_check_at: null}),
      candidate(7, {last_check_status: 'verified', last_checked_at: checkedAt}),
      candidate(8, {verification_status: 'expired', last_check_status: 'verified', last_checked_at: checkedAt}),
      candidate(9, {verification_status: 'verified', last_check_status: 'error', last_check_reason: reason, last_checked_at: checkedAt, next_check_at: future}),
      candidate(10, {last_check_status: 'error', last_checked_at: 'invalid', next_check_at: 'invalid'}),
      candidate(11, {verification_status: 'expired', last_check_status: 'unknown', last_checked_at: checkedAt, next_check_at: future})
    ], members: [candidate(9, {verification_status: 'verified'})], auto_state: {next_refresh_at: now / 1000 + 30}};
    const jobs = [{id: 'audit', pool_id: policy.id, kind: 'auto_audit', status: 'done', result: {refill_scheduled: true}}];
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== 'http://pool-history.invalid') { external.push(req.url()); return route.abort(); }
      const reply = json => route.fulfill({json});
      if (req.method() === 'GET' && url.pathname === '/admin/observability') return route.fulfill({json: {}, status: 404});
      if (req.method() === 'GET' && /^\/admin\/a6\/pools\/[^/]+\/events$/.test(url.pathname)) return reply({events: []});
      if (url.pathname.startsWith('/admin/')) {
        if (req.method() !== 'GET') writes.push(req.url());
        if (url.pathname === '/admin/session') return reply({username: 'admin', csrf_token: 'csrf'});
        if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: []});
        if (url.pathname === '/admin/health') return reply({status: 'ok'});
        if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
        if (url.pathname === '/admin/a6/pools') return reply({pools: [policy], tokens: [{id: 11, name: 'Production', status: 1}, {id: 22, name: 'Tests', status: 1}], catalog: [{id: 'glm-reference', display_name: 'GLM reference'}], jobs, capabilities: {}});
        throw Error('Unexpected mock API: ' + req.method() + ' ' + url.pathname);
      }
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.goto('http://pool-history.invalid/');
    await page.locator('#app').waitFor({state: 'visible'});
    await page.locator('#tab-market').click();
    await page.locator('.pool-card').first().click();
    await page.locator('#pool-section-tab-composition').click();
    const rows = page.locator('#pool-candidates tbody tr');
    assert.equal(await rows.count(), 11);
    assert.equal(await page.locator('#pool-candidates th').count(), 9, 'history must not add columns');
    const cell = i => rows.nth(i).locator('td').nth(6);
    const formatted = value => new Intl.DateTimeFormat('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZone: 'UTC'}).format(new Date(value));
    assert.equal(await cell(0).locator('.pool-chip').textContent(), 'Не проверен');
    assert.equal(await cell(0).locator('p').count(), 0, 'never invent historical checks');
    assert.equal(await cell(0).locator('.pool-verified-at').textContent(), '—', 'unverified cannot show stale proof date');
    for (const [i, label] of [[1, 'Ниже порога'], [2, 'Не определено'], [3, 'Ошибка']]) {
      assert.equal(await cell(i).locator('.pool-chip').textContent(), 'Пауза до ' + formatted(future));
      const history = await cell(i).locator('.pool-check-history').textContent();
      assert.ok(history.includes('Прежняя проверка: ' + label + ' · ' + formatted(checkedAt)));
      assert.ok(history.includes(reason));
      assert.match(await cell(i).textContent(), /Прежний результат сброшен после изменения политики\./);
      assert.equal(await cell(i).locator('.success').count(), 0);
    }
    for (const i of [4, 5, 9]) assert.equal(await cell(i).locator('.pool-chip').textContent(), 'Ожидает повторной проверки', 'elapsed/missing/invalid cooldown must not imply future pause');
    assert.equal(await cell(6).locator('.pool-chip').textContent(), 'Не проверен');
    assert.match(await cell(6).locator('.pool-check-history').textContent(), /Прежняя проверка: Проверен/);
    assert.equal(await cell(6).locator('.success').count(), 0, 'old verified history is not current proof');
    assert.equal(await cell(7).locator('.pool-chip').textContent(), 'Срок истёк');
    assert.match(await cell(7).textContent(), /Срок прежнего результата истёк; требуется повторная проверка\./);
    assert.equal(await cell(8).locator('.pool-chip.success').textContent(), 'Проверен');
    assert.equal(await cell(8).locator('.pool-check-history').count(), 0, 'current proof wins over old history');
    assert.equal(await rows.nth(8).locator('td').nth(8).textContent(), 'GLM reference · 99%');
    assert.equal(await cell(10).locator('.pool-chip').textContent(), 'Пауза до ' + formatted(future));
    assert.match(await cell(10).textContent(), /Срок прежнего результата истёк/);
    for (const i of [0, 1, 2, 3, 4, 5, 6, 7, 9, 10]) {
      assert.equal(await rows.nth(i).locator('td').nth(7).textContent(), '—', 'stale probability is not current proof');
      assert.equal(await rows.nth(i).locator('td').nth(8).textContent(), '—', 'history cannot fabricate predicted model');
    }
    assert.equal(await page.locator('#pool-candidates img').count(), 0, 'historical reasons remain text');
    assert.match(await page.locator('#pool-jobs').textContent(), /Недостаточно участников: запланирован добор/);
    assert.match(await page.locator('#pool-summary').textContent(), /Опубликовано участников1/);
    assert.ok((await page.locator('#pool-auto-state').textContent()).includes(formatted(new Date(now + 30000).toISOString())));
    // Active checking still takes precedence over historical cooldown.
    jobs.push({id: 'active', pool_id: policy.id, kind: 'verify', status: 'running', phase: 'testing', current_channel_id: 2});
    await page.locator('#pools-refresh').click();
    await page.waitForFunction(() => document.querySelectorAll('#pool-candidates tbody tr')[1].dataset.verification === 'checking');
    assert.equal(await cell(1).locator('.pool-chip').textContent(), 'Проверяется');
    assert.equal(await cell(1).locator('.pool-check-history').count(), 0);
    assert.deepEqual(writes, []);
    assert.deepEqual(external, []);
    assert.deepEqual(errors, []);
    console.log('browser-pool-history-status: OK');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
