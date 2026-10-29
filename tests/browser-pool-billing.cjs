// Offline actual-billing browser contract: every asset/API response mocked; no backend/provider traffic.
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
    const errors = [], requests = [], unexpected = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => requests.push({url: request.url(), method: request.method()}));
    const billing = {actual_cost_usd: 0.01, pending_reserved_usd: 0.02, pending_requests: 2, settled_requests: 3, total_requests: 5, day: '2026-10-23', history_complete: false, secret: 'SECRET_BILLING_NEVER_RENDER'};
    const policy = {id: 'pool', name: 'Actual billing', revision: 1, token_id: 11, test_token_id: 22, model: 'model-a', reference_model: 'reference-a', protocol: 'chat', enabled: true, auto_publish: true, status: 'active', candidates: [], members: [], auto_daily_usd: 5, auto_daily_requests: 600, auto_state: {day: '2026-10-23', daily_requests: 5, daily_reserved_usd: 0.76}, billing: {...billing}};
    const job = {id: 'job', pool_id: 'pool', kind: 'verify', status: 'done', progress: 1, total: 1, reserved_cost_usd: 0.76, result: {reserved_cost_usd: 0.76, spent: 99, actual_cost_usd: 88}, billing: {...billing, actual_cost_usd: 0.003}};
    const tokens = [{id: 11, name: 'Production', status: 1, key: 'SECRET_TOKEN_NEVER_RENDER'}, {id: 22, name: 'Test', status: 1}];
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== 'http://pool-billing.invalid' || request.method() !== 'GET') { unexpected.push(request.url()); return route.abort(); }
      const reply = json => route.fulfill({json});
      if (url.pathname === '/admin/observability') return route.fulfill({json: {}, status: 404});
      if (/^\/admin\/a6\/pools\/[^/]+\/events$/.test(url.pathname)) return reply({events: []});
      if (url.pathname === '/admin/session') return reply({username: 'admin', csrf_token: 'csrf'});
      if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: []});
      if (url.pathname === '/admin/health') return reply({status: 'ok'});
      if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
      if (url.pathname === '/admin/a6/tokens') return reply({items: tokens});
      if (url.pathname === '/admin/a6/pools') return reply({pools: [policy], jobs: [job], tokens, catalog: [{id: 'reference-a'}], capabilities: {}});
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) { unexpected.push(request.url()); return route.abort(); }
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.goto('http://pool-billing.invalid/');
    await page.locator('#app').waitFor({state: 'visible'});
    await page.locator('#tab-market').click();
    await page.locator('.pool-card').click();
    await page.locator('#pool-section-tab-automation').click();
    const refresh = async () => {
      const response = page.waitForResponse(response => new URL(response.url()).pathname === '/admin/a6/pools');
      await page.locator('#pools-refresh').click(); await response;
      await page.waitForFunction(expected => document.querySelector('#pool-auto-state .pool-billing').textContent.includes(expected), policy.billing ? 'Фактически списано (журнал A6API): $' + policy.billing.actual_cost_usd.toFixed(6) : 'Фактические списания пока не загружены');
    };
    let actual = await page.locator('#pool-auto-state').textContent();
    assert.match(actual, /Фактически списано \(журнал A6API\): \$0\.010000/);
    assert.match(actual, /Ожидает подтверждения: 2 запросов · защитный резерв \$0\.020000/);
    assert.match(actual, /Бюджет: фактические списания \$0\.010000 \+ ожидающий защитный резерв \$0\.020000 · лимит \$5\.000000/);
    assert.match(actual, /Учёт с обновления; прошлые проверки учтены не полностью/);
    assert.match(actual, /это не означает нулевые списания за всю историю/);
    assert.doesNotMatch(actual, /0\.760000|USD зарезервировано \/ использовано/);
    let jobText = await page.locator('#pool-jobs').textContent();
    assert.match(jobText, /Фактически списано \(журнал A6API\): \$0\.003000/);
    assert.doesNotMatch(jobText, /0\.760000|88\.000000|99\.000000|spent/);
    // Settled actual zero must stay zero, never fall back to the old estimate.
    policy.billing = {...billing, actual_cost_usd: 0, pending_reserved_usd: 0, pending_requests: 0, settled_requests: 5, total_requests: 5};
    job.billing = {...policy.billing};
    await refresh();
    actual = await page.locator('#pool-auto-state').textContent();
    assert.match(actual, /Фактически списано \(журнал A6API\): \$0\.000000/);
    assert.match(actual, /Ожидает подтверждения: 0 запросов · защитный резерв \$0\.000000/);
    assert.doesNotMatch(actual, /0\.760000/);
    assert.match(await page.locator('#pool-jobs').textContent(), /Фактически списано \(журнал A6API\): \$0\.000000/);
    policy.billing.history_complete = true; job.billing.history_complete = true;
    await refresh();
    assert.doesNotMatch(await page.locator('#pool-auto-state').textContent(), /прошлые проверки учтены не полностью/);
    // Missing ledger is unavailable, not guessed-zero historical spend.
    delete policy.billing; delete job.billing;
    await refresh();
    actual = await page.locator('#pool-auto-state').textContent();
    assert.match(actual, /Фактические списания пока не загружены/);
    assert.match(actual, /Старый расчётный резерв, не списание: \$0\.760000/);
    assert.doesNotMatch(actual, /Фактически списано \(журнал A6API\):|\$0\.000000/);
    jobText = await page.locator('#pool-jobs').textContent();
    assert.match(jobText, /Фактические списания пока не загружены/);
    assert.match(jobText, /Старый расчётный резерв, не списание: \$0\.760000/);
    delete policy.auto_state.daily_reserved_usd; delete job.reserved_cost_usd; delete job.result.reserved_cost_usd;
    await refresh();
    assert.doesNotMatch(await page.locator('#pool-auto-state').textContent(), /Старый расчётный резерв|\$0\.000000/);
    assert.doesNotMatch(await page.locator('#pool-jobs').textContent(), /Старый расчётный резерв|\$0\.000000/);
    // Untrusted fields remain text; unknown billing fields never render.
    policy.billing = {...billing, actual_cost_usd: 0.01, day: '<img src=x onerror=alert(1)>', secret: 'SECRET_BILLING_NEVER_RENDER'};
    await refresh();
    assert.equal(await page.locator('#pool-auto-state img, #pool-jobs img').count(), 0);
    const body = await page.locator('body').textContent();
    assert.doesNotMatch(body, /SECRET_BILLING_NEVER_RENDER|SECRET_TOKEN_NEVER_RENDER/);
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    assert.ok(requests.some(request => new URL(request.url).pathname === '/admin/a6/pools'));
    for (const request of requests) {
      assert.equal(new URL(request.url).origin, 'http://pool-billing.invalid');
      assert.equal(request.method, 'GET', 'billing render must not create paid/API mutation requests');
    }
    console.log('PASS offline billing: actual vs legacy reserve, scoped jobs, pending/settled zero, unavailable/history coverage, separate budget, safe DOM/secrets, request events');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
