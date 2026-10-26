// Offline SaaS interaction contract. Every request intercepted; no services, paid calls or real secrets.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const origin = 'http://saas-interactions.invalid';
(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], requests = [], forbidden = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.clock.install();
    const malicious = 'Поставщик <img src=x onerror=alert(1)>';
    const secret = 'never-render-secret-test-only';
    const recent = [
      { time: '2026-10-26T12:03:00Z', id: 'req-new', model: 'alpha', provider: 'Zulu', status: 200, total_ms: 120, attempts: 1, input_tokens: 10, output_tokens: 30 },
      { time: '2026-10-26T12:02:00Z', id: 'req-error', model: 'beta', provider: 'Alpha', status: 502, total_ms: 900, attempts: 2 },
      { time: '2026-10-26T12:01:00Z', id: 'req-old', model: 'alpha', provider: 'Delta', status: 201, total_ms: 300, attempts: 1 }
    ];
    const stats = { generated_at: '2026-10-26T12:04:00Z', summary: { final_requests: 3 }, providers: [], timeline: [
      { time: '2026-10-26T12:00:00Z', requests: 12, errors: 1, switches: 2 },
      { time: '2026-10-26T12:01:00Z', requests: 24, errors: 3, switches: 4 },
      { time: '2026-10-26T12:02:00Z', requests: 8, errors: 0, switches: 1 }
    ], recent_requests: recent, recent_errors: [] };
    let changes = [
      { id: 'new', checked_at: '2026-10-26T12:03:00Z', provider_id: 'p1', provider_name: malicious, model: 'alpha', reference_model: 'ref-a', from_status: 'unknown', to_status: 'verified', target_probability: .98, prediction: 'ref-a', source: 'manual', api_key: secret, raw_response: secret },
      { id: 'middle', checked_at: '2026-10-26T12:02:00Z', provider_id: 'p2', provider_name: 'Alpha', model: 'beta', reference_model: 'ref-b', from_status: 'verified', to_status: 'suspect', target_probability: .2, prediction: 'ref-a', source: 'scheduled' },
      { id: 'old', checked_at: '2026-10-26T12:01:00Z', provider_id: 'p3', provider_name: 'Zulu', model: 'gamma', reference_model: 'ref-a', from_status: 'verified', to_status: 'error', target_probability: null, source: 'manual' }
    ];
    const config = { revision: 1, settings: {}, providers: [], models: [] };
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) { forbidden.push(url.href); return route.abort(); }
      if (url.pathname.startsWith('/admin/')) {
        requests.push(url.pathname + url.search);
        assert.equal(request.method(), 'GET', 'presentation controls must not mutate server state');
        const replies = {
          '/admin/session': { username: 'demo', csrf_token: 'test-csrf' },
          '/admin/config': config,
          '/admin/stats': stats,
          '/admin/health': { status: 'ok' },
          '/admin/observability': {},
          '/admin/detector/log': { changes },
          '/admin/detector': { available: true, settings: { revision: 1, enabled: false, targets: [] }, providers: [], states: [], jobs: [], catalog: { models: [] } },
          '/admin/a6/pools': { pools: [], jobs: [], tokens: [], catalog: [], capabilities: {} },
          '/admin/pelican': { providers: [], runs: [] }
        };
        assert.ok(Object.hasOwn(replies, url.pathname), 'unexpected API ' + url.pathname);
        return route.fulfill({ json: replies[url.pathname] });
      }
      const asset = { '/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css' }[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({ body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto(origin + '/');
    await page.locator('#requests-table tr').nth(2).waitFor();
    assert.match(await page.locator('#active-page-title').textContent(), /Обзор/);
    assert.equal(await page.locator('#activity-chart .chart-hit').count(), 3);
    await page.locator('.chart-hit').first().focus();
    assert.equal(await page.locator('.chart-tooltip').isVisible(), true);
    assert.match(await page.locator('.chart-tooltip').textContent(), /Запросы: 12.*Ошибки: 1.*Переключения: 2/);
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('.chart-tooltip').textContent(), /Запросы: 24/);
    const chartHandle = await page.locator('.chart-hit').nth(1).elementHandle();
    await page.clock.runFor(15100);
    await page.waitForFunction(() => document.getElementById('stats-content').getAttribute('aria-busy') === 'false');
    assert.equal(await chartHandle.evaluate(node => node === document.activeElement), true, 'polling preserves chart focus');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.chart-tooltip').isVisible(), false);
    await page.locator('#request-search').focus();
    await page.locator('.chart-hit').last().hover();
    assert.match(await page.locator('.chart-tooltip').textContent(), /Запросы: 8/);
    await page.locator('#request-search').hover();
    const afterPoll = requests.length;
    await page.locator('#request-search').fill('ALPHA');
    assert.equal(await page.locator('#requests-table tr').count(), 3, 'search matches model and provider columns');
    assert.match(await page.locator('#request-count').textContent(), /3 из 3/);
    await page.locator('#request-search').fill('delta');
    assert.equal(await page.locator('#requests-table tr').count(), 1);
    await page.locator('#request-search').fill('req-error');
    assert.equal(await page.locator('#requests-table tr').count(), 1);
    assert.match(await page.locator('#requests-table').textContent(), /502/);
    await page.locator('#request-search').fill('');
    await page.locator('#request-filter').selectOption('error');
    assert.match(await page.locator('#request-count').textContent(), /1 из 3/);
    await page.locator('#request-filter').selectOption('all');
    await page.locator('#request-sort').selectOption('oldest');
    assert.match(await page.locator('#requests-table tr').first().textContent(), /req-old/);
    await page.locator('#request-sort').selectOption('latency');
    assert.match(await page.locator('#requests-table tr').first().textContent(), /req-error/);
    assert.equal(requests.length, afterPoll, 'request table interactions issue no requests');
    await page.locator('#active-page-title').click();
    await page.keyboard.press('/');
    assert.equal(await page.locator('#request-search').evaluate(node => node === document.activeElement), true);
    await page.keyboard.press('Alt+4');
    await page.locator('#detector-log-rows tr').nth(2).waitFor();
    assert.match(await page.locator('#active-page-title').textContent(), /Журнал/);
    const loaded = requests.length;
    await page.locator('#journal-search').fill('BETA');
    assert.equal(await page.locator('#detector-log-rows tr').count(), 1);
    assert.match(await page.locator('#journal-count').textContent(), /1 из 3/);
    await page.locator('#journal-search').fill('');
    await page.locator('#journal-filter').selectOption('verified');
    assert.equal(await page.locator('#detector-log-rows tr').count(), 1);
    await page.locator('#journal-filter').selectOption('all');
    await page.locator('#journal-sort').selectOption('provider');
    assert.equal(await page.locator('#detector-log-rows tr').first().locator('td').nth(1).textContent(), [malicious, 'Alpha', 'Zulu'].sort((a, b) => a.localeCompare(b, 'ru'))[0]);
    await page.locator('#journal-sort').selectOption('oldest');
    assert.match(await page.locator('#detector-log-rows tr').first().textContent(), /Zulu/);
    await page.locator('#journal-search').fill('no-match');
    assert.match(await page.locator('#detector-log-rows').textContent(), /не найдены/);
    await page.locator('#journal-search').fill('');
    await page.locator('#journal-sort').selectOption('newest');
    assert.equal(requests.length, loaded, 'journal controls issue no requests');
    await page.locator('.journal-detail-action').first().focus();
    const focused = await page.locator('.journal-detail-action').first().elementHandle();
    await page.locator('#refresh-detector-log').click();
    await page.waitForFunction(() => !document.getElementById('refresh-detector-log').disabled);
    assert.equal(await focused.evaluate(node => node.isConnected), true, 'unchanged refresh preserves action node');
    await page.locator('.journal-detail-action').first().click();
    await page.locator('#journal-detail').waitFor({ state: 'visible' });
    assert.match(await page.locator('#journal-detail-body').textContent(), /98%/);
    assert.ok(!(await page.locator('body').textContent()).includes(secret));
    assert.equal(await page.locator('#journal-detail img, #detector-log-rows img').count(), 0);
    assert.match(await page.locator('#journal-detail-title').textContent(), /<img/);
    const detailSnapshot = await page.locator('#journal-detail-body').textContent();
    changes = [{ ...changes[0], target_probability: .99 }, ...changes.slice(1)];
    await page.evaluate(() => document.getElementById('refresh-detector-log').click());
    await page.waitForFunction(() => !document.getElementById('refresh-detector-log').disabled);
    assert.equal(await page.locator('#journal-detail-body').textContent(), detailSnapshot, 'refresh does not replace open detail snapshot');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#journal-detail').evaluate(node => node.open), false);
    await page.locator('#journal-search').fill('beta');
    await page.locator('#journal-filter').selectOption('suspect');
    await page.locator('#tab-stats').click();
    await page.locator('#tab-detector-log').click();
    await page.waitForFunction(() => !document.getElementById('refresh-detector-log').disabled);
    assert.equal(await page.locator('#journal-search').inputValue(), 'beta');
    assert.equal(await page.locator('#journal-filter').inputValue(), 'suspect');
    assert.equal(await page.locator('#detector-log-rows tr').count(), 1);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#nav-toggle').click();
    assert.equal(await page.locator('#nav-toggle').getAttribute('aria-expanded'), 'true');
    await page.locator('#tab-settings').click();
    assert.equal(await page.locator('#nav-toggle').getAttribute('aria-expanded'), 'false');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, 'mobile page stays within viewport');
    assert.deepEqual(errors, []); assert.deepEqual(forbidden, []);
    console.log('PASS SaaS: keyboard/hover chart, focus-stable polling, local request/journal search/filter/sort, counts, safe native details, keyboard navigation, persistent filters, mobile menu; zero API writes or external calls');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
