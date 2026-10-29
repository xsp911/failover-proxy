// Offline optional-price/prediction contracts. No live A6API, providers, or paid calls.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const prices = ['max_input_usd', 'max_output_usd', 'max_cache_read_usd', 'max_cache_write_usd'];
(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    const errors = [], external = [], writes = [];
    page.on('pageerror', error => errors.push(error.message));
    let policies = [];
    const modelName = 'Reference <img src=x onerror=alert(1)>';
    const candidate = {channel_id: 55, listing_id: 7, supplier: 'Offline seller', input_micros: 1000000, output_micros: 2000000, cache_read_micros: 20, cache_hit_percent: 90, success_percent: 99, eligible: true, verification_status: 'verified', probability: 0.95, actual_channel_id: 987654, predicted_model: 'reference-a', predicted_probability: 0.976};
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url()), method = req.method();
      if (url.origin !== 'http://optional.invalid') { external.push(req.url()); return route.abort(); }
      const reply = (json, status = 200) => route.fulfill({json, status});
      if (method === 'GET' && url.pathname === '/admin/observability') return reply({}, 404);
      if (url.pathname.startsWith('/admin/')) {
        if (method !== 'GET') {
          assert.equal(req.headers()['x-csrf-token'], 'csrf');
          writes.push({path: url.pathname, method, body: req.postDataJSON()});
        }
        if (url.pathname === '/admin/session') return reply({username: 'admin', csrf_token: 'csrf'});
        if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: []});
        if (url.pathname === '/admin/health') return reply({status: 'ok'});
        if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
        if (url.pathname === '/admin/a6/pools') {
          if (method === 'GET') return reply({pools: policies, catalog: [{id: 'reference-a', display_name: modelName}], tokens: [{id: 11, name: 'Production', status: 1}, {id: 22, name: 'Tests', status: 1}], jobs: [], capabilities: {}});
          assert.equal(method, 'POST');
          const policy = {...req.postDataJSON(), id: 'optional-pool', revision: 1, status: 'draft', members: [], candidates: []};
          policies.push(policy); return reply(policy, 201);
        }
        if (url.pathname === '/admin/a6/pools/optional-pool' && method === 'PUT') {
          policies[0] = {...policies[0], ...req.postDataJSON(), revision: policies[0].revision + 1};
          return reply(policies[0]);
        }
        if (url.pathname === '/admin/a6/pools/optional-pool/preview') return reply({preview_id: 'offline-preview', expires_at: new Date(Date.now() + 60000).toISOString(), can_publish: false, remote_before: {caps: {input_price_micros: 0, output_price_micros: null, cache_read_price_micros: 0, cache_write_price_micros: null, per_request_price_micros: 0}}, draft: {max_input_usd: null, max_output_usd: null, max_cache_read_usd: null, max_cache_write_usd: null, channels: []}});
        throw Error('Unexpected offline API: ' + method + ' ' + url.pathname);
      }
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    const openSection = section => page.locator('#pool-section-tab-' + section).click();
    const openSettings = async () => {
      await openSection('settings');
      if (!await page.locator('#pool-settings').evaluate(node => node.open)) await page.locator('#pool-settings > summary').click();
    };
    const refresh = async () => {
      const response = page.waitForResponse(r => new URL(r.url()).pathname === '/admin/a6/pools' && r.request().method() === 'GET');
      await page.locator('#pools-refresh').click(); await response;
    };
    await page.goto('http://optional.invalid/');
    await page.locator('#app').waitFor({state: 'visible'});
    await page.locator('#tab-market').click();
    await openSettings();
    await page.locator('#pool-name').fill('Optional prices');
    await page.locator('#pool-model').fill('offline-model');
    await page.locator('#pool-token_id').selectOption('11');
    await page.locator('#pool-test_token_id').selectOption('22');
    await page.locator('#pool-reference_model').selectOption('reference-a');
    for (const key of prices) {
      assert.equal(await page.locator('#pool-' + key).inputValue(), '');
      assert.equal(await page.locator('#pool-' + key).getAttribute('required'), null);
      assert.equal(await page.locator('#pool-' + key).getAttribute('placeholder'), 'Без ограничения');
    }
    for (const key of ['max_probe_usd', 'auto_daily_usd', 'max_probe_requests', 'max_candidates', 'threshold_percent']) assert.notEqual(await page.locator('#pool-' + key).getAttribute('required'), null);
    await page.locator('#pool-save').click();
    await page.waitForFunction(() => document.querySelectorAll('.pool-card').length === 1);
    const created = writes.find(write => write.method === 'POST' && write.path === '/admin/a6/pools');
    assert.ok(created, 'blank price fields must permit a POST');
    for (const key of prices) assert.equal(created.body[key], null, key + ' must be explicit JSON null');
    assert.equal(created.body.max_probe_usd, 1);
    assert.equal(created.body.max_probe_requests, 60);
    assert.equal(created.body.threshold_percent, 90);
    await page.locator('.pool-card').click(); await openSettings();
    for (const key of prices) assert.equal(await page.locator('#pool-' + key).inputValue(), '', 'null must hydrate blank');
    // Required budgets still block submitting. Zero prices get the Russian positive-or-blank error.
    const beforeInvalid = writes.length;
    await page.locator('#pool-max_probe_usd').fill(''); await page.locator('#pool-save').click();
    assert.equal(writes.length, beforeInvalid);
    await page.locator('#pool-max_probe_usd').fill('1');
    for (const key of prices) {
      await page.locator('#pool-' + key).fill('0'); await page.locator('#pool-save').click();
      await page.waitForFunction(() => document.querySelector('#pools-error').textContent.includes('положительное ценовое ограничение'));
      assert.equal(writes.length, beforeInvalid);
      await page.locator('#pool-' + key).fill('');
    }
    await page.locator('#pool-name').fill('Edited optional prices'); await page.locator('#pool-save').click();
    await page.waitForFunction(() => document.querySelector('#pool-form-state').textContent.includes('Ревизия 2'));
    const edited = writes.find(write => write.method === 'PUT');
    for (const key of prices) assert.equal(edited.body[key], null);
    await openSection('overview'); await page.locator('#pool-run').click();
    await page.locator('#confirm-dialog').waitFor({state: 'visible'});
    const consent = await page.locator('#confirm-description').textContent();
    assert.match(consent, /без ограничения/i); assert.match(consent, /расчётный/); assert.match(consent, /не гарантия/); assert.match(consent, /каждой пробы/);
    await page.locator('#confirm-dialog button[value=cancel]').click();
    assert.equal(writes.some(write => /\/(run|verify|restart)$/.test(write.path)), false, 'no paid action submitted');
    await openSection('overview'); await page.locator('#pool-preview').click();
    await page.locator('#pool-preview-panel').waitFor({state: 'visible'});
    const preview = await page.locator('#pool-preview-diff').textContent();
    assert.equal((preview.match(/Без ограничения/g) || []).length, 9);
    assert.doesNotMatch(preview, /0\.000000 USD/);
    // Prediction is detector output, never the requested model or actual channel ID.
    const old = {...candidate, channel_id: 58}; delete old.predicted_model; delete old.predicted_probability;
    const raw = 'Unknown <svg onload=alert(1)>';
    policies[0].candidates = [candidate, {...candidate, channel_id: 56, verification_status: 'suspect', predicted_probability: 0.88}, {...candidate, channel_id: 57, predicted_model: raw}, old,
      ...['unknown', 'error', 'unverified', 'checking', 'expired', 'skipped'].map((verification_status, index) => ({...candidate, channel_id: 60 + index, verification_status})),
      {...candidate, channel_id: 70, predicted_model: null, predicted_probability: null}, {...candidate, channel_id: 71, predicted_probability: null}];
    policies[0].members = [candidate];
    await refresh();
    await page.waitForFunction(() => document.querySelectorAll('#pool-candidates tbody tr').length === 12);
    assert.equal(await page.locator('#pool-candidates th').last().textContent(), 'Похож на модель');
    const rows = page.locator('#pool-candidates tbody tr');
    const prediction = index => rows.nth(index).locator('td').last();
    assert.equal(await prediction(0).textContent(), modelName + ' · 97,6%');
    assert.equal(await prediction(1).textContent(), 'Возможное несоответствие: ' + modelName + ' · 88%');
    assert.equal(await prediction(2).textContent(), raw + ' · 97,6%');
    assert.equal(await prediction(3).textContent(), 'Нет данных');
    for (let index = 4; index <= 10; index++) assert.equal(await prediction(index).textContent(), '—');
    assert.equal(await prediction(11).textContent(), modelName, 'missing probability must not fabricate percent');
    assert.match(await prediction(0).getAttribute('title'), /закрытому каталогу.*не доказывает/);
    assert.equal(await rows.first().locator('td').first().getAttribute('title'), 'Запрос направлен в канал: 987654');
    assert.doesNotMatch(await page.locator('#pool-candidates').textContent(), /987654|Фактический канал/);
    assert.equal(await page.locator('#pool-members tbody td').last().textContent(), modelName + ' · 97,6%');
    assert.equal(await page.locator('#pool-candidates img, #pool-candidates svg, #pool-members img').count(), 0);
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log('Offline optional pool prices and predictions: OK');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
