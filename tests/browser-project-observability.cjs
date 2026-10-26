// Offline reporting contract: read-only local endpoints, no services or providers.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const origin = 'http://project-observability.invalid';
(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], requests = [], unexpected = [];
    let reportMissing = false, eventsMissing = false, reportCalls = 0, eventCalls = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const schedule = window.setTimeout.bind(window), cancel = window.clearTimeout.bind(window), polls = new Map();
      window.setTimeout = (callback, delay, ...args) => {
        const id = schedule(() => { polls.delete(id); callback(...args); }, delay);
        if ([5000, 15000].includes(delay)) polls.set(id, () => callback(...args));
        return id;
      };
      window.clearTimeout = id => { polls.delete(id); cancel(id); };
      window.__runPoolPoll = () => {
        const [id, callback] = [...polls.entries()].at(-1) || [];
        if (!callback) throw new Error('No pool poll');
        polls.delete(id); cancel(id); callback();
      };
    });
    const candidate = { channel_id: 55, listing_id: 7, eligible: true, verification_status: 'verified', supplier: 'Seller' };
    const policy = { id: 'pool-a', name: 'Pool A', revision: 1, model: 'model-a', reference_model: 'model-a', protocol: 'chat', token_id: 11, test_token_id: 22, test_token_ids: [22], enabled: true, auto_publish: true, status: 'published', candidates: [candidate], members: [candidate], auto_state: { next_refresh_at: 1792936800, next_audit_at: 1792933200, refresh_not_before: 1793023200, refresh_reason: 'cooldown_wait' } };
    const job = { id: 'job-a', pool_id: 'pool-a', kind: 'auto_refresh', status: 'done', created_at: '2026-10-25T10:00:00Z', result: { checked_this_run: 2, passed_this_run: 1, cached_verified: 4, published_members: 3, removed_members: [88], stop_reason: 'target_reached' } };
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, origin, 'no external requests');
      const reply = (json, status = 200) => route.fulfill({ json, status });
      if (url.pathname.startsWith('/admin/')) {
        requests.push({ path: url.pathname, method: request.method() });
        assert.equal(request.method(), 'GET', 'reporting performs no writes');
        if (url.pathname === '/admin/session') return reply({ username: 'admin', csrf_token: 'csrf' });
        if (url.pathname === '/admin/stats') return reply({ generated_at: '2026-10-25T12:00:00Z', summary: { final_requests: 0 }, providers: [], timeline: [], recent_requests: [], recent_errors: [] });
        if (url.pathname === '/admin/health') return reply({ status: 'ok' });
        if (url.pathname === '/admin/config') return reply({ revision: 1, settings: {}, providers: [], models: [] });
        if (url.pathname === '/admin/observability') {
          reportCalls++;
          if (reportMissing) return reply({ error: { code: 'not_found' } }, 404);
          return reply({ generated_at: '2026-10-25T12:00:00Z', gateway: { requests_24h: 0, attempts_24h: 0 }, provider_checks: { results_24h: 9, verified_24h: 6, suspect_24h: 1, unknown_24h: 1, error_24h: 1, upstream_attempts_24h: 27, interval_minutes: 30 }, a6_checks: { jobs_24h: 3, probes_24h: 14, errors_24h: 2, actual_cost_usd_utc_day: 0.0123, pending_reserved_usd: 0.004 }, scope_note: 'Окна различаются <img src=x onerror=alert(1)>' });
        }
        if (url.pathname === '/admin/a6/pools') return reply({ pools: [policy, { ...policy, id: 'pool-b', name: 'Pool B' }], jobs: [job], tokens: [{ id: 11, name: 'Working', status: 1 }, { id: 22, name: 'Test', status: 1 }], catalog: [{ id: 'model-a' }], capabilities: {} });
        if (/^\/admin\/a6\/pools\/pool-[ab]\/events$/.test(url.pathname)) {
          eventCalls++; assert.equal(url.search, '?limit=50');
          if (eventsMissing) return reply({}, 404);
          return reply({ events: url.pathname.includes('pool-b') ? [] : [{ created_at: '2026-10-25T11:00:00Z', added: [55, { channel_id: 66 }, { api_key: 'SECRET' }, '<script>'], removed: [88], reason: 'safe <img src=x>', source: 'auto_refresh', job_id: 'job-a', secret: 'SECRET' }] });
        }
        unexpected.push(url.pathname); return reply({}, 500);
      }
      const asset = { '/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css' }[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({ body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto(origin + '/');
    await page.locator('.activity-source').first().waitFor();
    assert.equal(reportCalls, 1);
    assert.equal(await page.locator('.activity-source').count(), 3);
    const report = await page.locator('.activity-sources').textContent();
    assert.match(report, /0 запросов \/ 24 ч/); assert.match(report, /27 upstream-проб \/ 24 ч/); assert.match(report, /14 проб \/ 24 ч/);
    assert.match(report, /мониторинг простаивает/); assert.match(report, /Окна учёта источников могут различаться/);
    assert.match(report, /\$0\.012300/); assert.match(report, /\$0\.004000/); assert.match(report, /UTC/);
    assert.equal(await page.locator('.activity-sources img').count(), 0);
    await page.locator('#tab-market').click(); await page.locator('.pool-card').first().waitFor();
    await page.locator('.pool-card').first().click();
    assert.equal(eventCalls, 0, 'overview selection does not read events');
    assert.match(await page.locator('#pool-current-state').textContent(), /Ожидание отбора: пауза повторной проверки кандидатов/);
    assert.match(await page.locator('#pool-current-state').textContent(), /не раньше/);
    await page.locator('#pool-section-tab-checks').click();
    assert.equal(eventCalls, 0, 'collapsed history stays lazy');
    const result = await page.locator('.pool-run-result').textContent();
    assert.match(result, /Проверено в этом запуске: 2/); assert.match(result, /Прошли в этом запуске: 1/); assert.match(result, /Свежие прежние проверки: 4/); assert.match(result, /Опубликовано участников: 3/); assert.match(result, /Удалено участников: 1/);
    await page.locator('#pool-membership-history > summary').click();
    await page.locator('.pool-membership-event').waitFor();
    assert.equal(eventCalls, 1);
    const events = await page.locator('#pool-events').textContent();
    assert.match(events, /Добавлены ID: 55, 66/); assert.match(events, /удалены ID: 88/); assert.match(events, /job-a/); assert.doesNotMatch(events, /SECRET|<script>/);
    assert.equal(await page.locator('#pool-events img').count(), 0);
    const polled = page.waitForResponse(response => response.url() === origin + '/admin/a6/pools');
    await page.evaluate(() => window.__runPoolPoll()); await polled;
    assert.equal(eventCalls, 1, 'normal pool polling does not refresh events');
    assert.equal(reportCalls, 1, 'hidden statistics do not fetch report');
    await page.locator('#pool-events-refresh').click();
    await page.waitForFunction(() => !document.querySelector('#pool-events-refresh').disabled);
    assert.equal(eventCalls, 2);
    await page.locator('.pool-card').nth(1).click();
    assert.equal(await page.locator('.pool-membership-event').count(), 0, 'selection clears old history');
    await page.locator('#pool-section-tab-checks').click();
    await page.waitForFunction(() => document.querySelector('#pool-events').textContent.includes('Изменений состава'));
    assert.equal(eventCalls, 3);
    eventsMissing = true;
    await page.locator('#pool-events-refresh').click();
    await page.waitForFunction(() => document.querySelector('#pool-events-status').textContent.includes('История недоступна'));
    assert.equal(await page.locator('#pool-restart').isEnabled(), true, 'history 404 does not break checks');
    reportMissing = true;
    await page.locator('#tab-stats').click();
    await page.waitForFunction(() => document.querySelector('#activity-sources-status').textContent.includes('временно недоступен'));
    assert.equal(await page.locator('#metric-requests').textContent(), '0');
    assert.equal(await page.locator('#stats-error').isVisible(), false, 'optional report 404 does not break stats');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    assert.ok(requests.every(request => request.method === 'GET'));
    console.log('PASS project observability: separated sources, optional endpoint, lazy safe history, run/catalog counts, schedule waits, unchanged polling');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
