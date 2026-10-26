// Offline auto-mode browser contract. All assets and API calls intercepted; no provider traffic.
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
    const base = {name: 'Legacy', id: 'legacy', revision: 1, token_id: 11, test_token_id: 22, model: 'model-a', reference_model: 'reference-a', protocol: 'chat', max_input_usd: 2, max_output_usd: 2, max_cache_read_usd: 2, max_cache_write_usd: 2, min_cache_hit_percent: 40, min_success_percent: 90, threshold_percent: 90, verification_ttl_hours: 24, refresh_minutes: 10, max_candidates: 10, max_members: 3, max_probe_requests: 60, max_probe_usd: 1, enabled: false, status: 'draft', candidates: [], members: []};
    let policies = [{...base}], serial = 0;
    const jobs = [{id: 'refresh', pool_id: 'legacy', kind: 'auto_refresh', status: 'done'}, {id: 'audit', pool_id: 'legacy', kind: 'auto_audit', status: 'done'}];
    const tokens = [{id: 11, name: 'Production', status: 1}, {id: 22, name: 'Tests', status: 1}];
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url()), method = req.method();
      if (url.origin !== 'http://pool-auto.invalid') { external.push(req.url()); return route.abort(); }
      const reply = (json, status = 200) => route.fulfill({json, status});
      if (method === 'GET' && url.pathname === '/admin/observability') return reply({}, 404);
      if (url.pathname.startsWith('/admin/')) {
        if (method !== 'GET') { assert.equal(req.headers()['x-csrf-token'], 'csrf'); writes.push({path: url.pathname, method, body: req.postDataJSON()}); }
        if (url.pathname === '/admin/session') return reply({username: 'admin', csrf_token: 'csrf'});
        if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: []});
        if (url.pathname === '/admin/health') return reply({status: 'ok'});
        if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
        if (url.pathname === '/admin/a6/tokens') return reply({items: tokens});
        if (url.pathname === '/admin/a6/pools') {
          if (method === 'GET') return reply({pools: policies, tokens, catalog: [{id: 'reference-a'}], jobs, capabilities: {}});
          assert.equal(method, 'POST');
          const policy = {...req.postDataJSON(), id: 'new-' + ++serial, revision: 1, status: 'draft', candidates: [], members: []};
          policies.push(policy); return reply(policy, 201);
        }
        const match = url.pathname.match(/^\/admin\/a6\/pools\/([^/]+)(?:\/(unban))?$/);
        if (match) {
          const index = policies.findIndex(policy => policy.id === match[1]);
          assert.notEqual(index, -1);
          if (match[2] === 'unban') {
            assert.equal(method, 'POST');
            const {channel_id} = req.postDataJSON();
            policies[index].bans = policies[index].bans.map(row => row.channel_id === channel_id ? {...row, banned: false, failures: 0} : row);
            return reply({ok: true});
          }
          if (method === 'PUT') {
            const payload = req.postDataJSON(); assert.equal(payload.revision, policies[index].revision);
            policies[index] = {...policies[index], ...payload, revision: payload.revision + 1}; return reply(policies[index]);
          }
          if (method === 'DELETE') { policies.splice(index, 1); return reply({ok: true}); }
        }
        throw Error('Unexpected mock API: ' + method + ' ' + url.pathname);
      }
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.goto('http://pool-auto.invalid/');
    await page.locator('#app').waitFor({state: 'visible'});
    await page.locator('#tab-market').click();
    await page.locator('#pool-name').waitFor({state: 'attached'});
    const openSettings = async () => {
      await page.locator('#pool-section-tab-settings').click();
      if (!await page.locator('#pool-settings').evaluate(node => node.open)) await page.locator('#pool-settings > summary').click();
    };
    const save = () => page.locator('#pool-save').click();
    const cancel = () => page.locator('#confirm-dialog button[value=cancel]').click();
    const consent = () => page.locator('#confirm-action').click();
    const waitRevision = n => page.waitForFunction(n => document.querySelector('#pool-form-state').textContent.includes('Ревизия ' + n + ' ·'), n);
    const refresh = async () => {
      const response = page.waitForResponse(r => r.url().endsWith('/admin/a6/pools') && r.request().method() === 'GET');
      await page.locator('#pools-refresh').click(); await response;
    };
    assert.equal(await page.locator('#pool-auto-publish').isDisabled(), false);
    assert.equal(await page.locator('#pool-auto-publish').isChecked(), false);
    assert.equal(await page.locator('#pool-enabled').isChecked(), false);
    assert.equal(await page.locator('#pool-auto_daily_usd').inputValue(), '5');
    assert.equal(await page.locator('#pool-auto_daily_requests').inputValue(), '600');
    assert.match(await page.locator('#pool-auto-publish').locator('..').textContent(), /каждые 6 часов\. Аудит участников — каждый час/);
    await page.locator('.pool-card').first().click();
    await openSettings();
    assert.match(await page.locator('#pool-jobs').textContent(), /Автоматический отбор и публикация \(6ч\)/);
    assert.match(await page.locator('#pool-jobs').textContent(), /Проверка участников \(1ч\)/);
    assert.equal(await page.locator('#pool-auto-publish').isChecked(), false, 'legacy pool must remain manual');
    await page.locator('#pool-name').fill('Legacy edited'); await save(); await waitRevision(2);
    assert.equal(policies[0].enabled, false); assert.equal(policies[0].auto_publish, false);
    assert.equal(await page.locator('#confirm-dialog').isVisible(), false);
    const beforeInvalid = writes.length;
    await page.locator('#pool-auto-publish').check(); await save();
    await page.waitForFunction(() => document.querySelector('#pools-error').textContent.includes('сначала отметьте'));
    assert.equal(writes.length, beforeInvalid); assert.equal(await page.locator('#pool-enabled').isChecked(), false);
    await page.locator('#pool-enabled').check(); await save();
    await page.locator('#confirm-dialog').waitFor({state: 'visible'});
    const description = await page.locator('#confirm-description').textContent();
    assert.match(description, /постоянные автоматические платные запросы/);
    assert.match(description, /600 запросов и 5 USD/); assert.match(description, /60 запросов и 1 USD/);
    assert.match(description, /рабочего токена ID 11/); assert.match(description, /модели «model-a»/);
    assert.match(description, /pins.*не используются/); assert.match(description, /fallback выключен/);
    assert.equal(writes.length, beforeInvalid); await cancel();
    assert.equal(policies[0].auto_publish, false);
    await save(); await consent(); await waitRevision(3);
    assert.equal(policies[0].auto_publish, true); assert.equal(policies[0].enabled, true);
    await page.locator('#pool-name').fill('Automatic renamed'); await save(); await waitRevision(4);
    assert.equal(await page.locator('#confirm-dialog').isVisible(), false, 'unchanged automatic budgets need no repeated consent');
    await page.locator('#pool-auto_daily_usd').fill('6'); await save();
    await page.locator('#confirm-dialog').waitFor({state: 'visible'}); await cancel();
    assert.equal(policies[0].auto_daily_usd, 5);
    await save(); await consent(); await waitRevision(5); assert.equal(policies[0].auto_daily_usd, 6);
    await page.locator('#pool-auto_daily_requests').fill('601'); await save(); await consent(); await waitRevision(6);
    await page.locator('#pool-max_probe_usd').fill('2'); await save(); await consent(); await waitRevision(7);
    await page.locator('#pool-max_probe_requests').fill('61'); await save(); await consent(); await waitRevision(8);
    await page.locator('#pool-auto_daily_requests').fill('500'); await save(); await waitRevision(9);
    assert.equal(await page.locator('#confirm-dialog').isVisible(), false, 'budget decrease requires no consent');
    policies[0].auto_state = {next_refresh_at: 1800000000, next_audit_at: 1800003600, last_error: 'safe <img src=x onerror=alert(1)>', daily_requests: 42, daily_reserved_usd: 1.5, day: '2027-01-15', remote_state: {token_id: 11, model: 'model-a', fallback_enabled: false, secret: 'SECRET_REMOTE_NEVER_RENDER'}};
    policies[0].bans = [{channel_id: '<img src=x onerror=alert(1)>', failures: 4, banned: true, laststatus: 'suspect', lastreason: 'bad <img src=x onerror=alert(1)>'}, {channel_id: 56, failures: 2, banned: false, laststatus: 'verified', lastreason: 'below threshold'}];
    await refresh();
    await page.locator('#pool-section-tab-automation').click();
    await page.waitForFunction(() => document.querySelector('#pool-auto-state').textContent.includes('42 / 500'));
    const autoText = await page.locator('#pool-auto-state').textContent();
    const expectedDate = await page.evaluate(() => new Intl.DateTimeFormat('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'}).format(new Date(1800000000 * 1000)));
    assert.ok(autoText.includes(expectedDate)); assert.match(autoText, /дневной лимит: \$6\.000000/); assert.match(autoText, /2027-01-15/);
    assert.match(autoText, /Фактические списания пока не загружены/);
    assert.match(autoText, /Старый расчётный резерв, не списание: \$1\.500000/);
    assert.doesNotMatch(autoText, /USD зарезервировано \/ использовано/);
    assert.match(autoText, /Последняя ошибка/); assert.doesNotMatch(autoText, /SECRET_REMOTE/);
    policies[0].auto_state.last_error = 'auto_remote_route_conflict'; await refresh();
    await page.waitForFunction(() => document.querySelector('#pool-auto-state').textContent.includes('Автоматическая перезапись заблокирована'));
    assert.match(await page.locator('#pool-auto-state').textContent(), /Один раз получите ручной Предпросмотр/);
    assert.match(await page.locator('#pool-bans').textContent(), /Заблокировано каналов: 1/);
    assert.match(await page.locator('#pool-bans').textContent(), /ошибок: 4 \/ 4/);
    assert.equal(await page.locator('#pool-bans button').count(), 1);
    assert.equal(await page.locator('#pool-bans img, #pool-auto-state img').count(), 0);
    const unbanBefore = writes.length;
    await page.locator('#pool-bans summary').click();
    await page.locator('#pool-bans button').click(); await page.locator('#confirm-dialog').waitFor({state: 'visible'});
    assert.equal(writes.length, unbanBefore); await cancel(); assert.equal(writes.length, unbanBefore);
    await page.locator('#pool-bans button').click(); await consent();
    await page.waitForFunction(() => document.querySelector('#pool-bans').textContent.includes('Заблокировано каналов: 0'));
    assert.deepEqual(writes.at(-1), {path: '/admin/a6/pools/legacy/unban', method: 'POST', body: {channel_id: '<img src=x onerror=alert(1)>'}});
    assert.equal(await page.locator('#pool-bans button').count(), 0);
    await openSettings();
    await page.locator('#pool-auto-publish').uncheck(); await page.locator('#pool-enabled').uncheck(); await save(); await waitRevision(10);
    assert.equal(policies[0].auto_publish, false); assert.equal(policies[0].enabled, false);
    assert.equal(await page.locator('#confirm-dialog').isVisible(), false);
    await page.locator('#pool-new').click();
    for (const [key, value] of [['name', 'Created automatic'], ['model', 'model-b'], ['max_input_usd', '2'], ['max_output_usd', '2'], ['max_cache_read_usd', '2'], ['max_cache_write_usd', '2']]) await page.locator('#pool-' + key).fill(value);
    await page.locator('#pool-token_id').selectOption('11'); await page.locator('#pool-test_token_id').selectOption('22'); await page.locator('#pool-reference_model').selectOption('reference-a');
    await page.locator('#pool-auto-publish').check(); await page.locator('#pool-enabled').check();
    await save(); await page.locator('#confirm-dialog').waitFor({state: 'visible'}); assert.equal(policies.length, 1);
    await consent(); await page.waitForFunction(() => document.querySelectorAll('.pool-card').length === 2);
    assert.equal(policies[1].auto_publish, true); assert.equal(policies[1].auto_daily_usd, 5); assert.equal(policies[1].auto_daily_requests, 600);
    await page.locator('#pool-section-tab-overview').click();
    await page.locator('#pool-delete').click(); await consent(); await page.waitForFunction(() => document.querySelectorAll('.pool-card').length === 1);
    assert.equal(policies[0].auto_publish, false, 'creating an auto pool must not enable an existing pool');
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log('PASS offline pool auto: explicit CRUD toggle, legacy false, enabled validation, activation/daily/per-run consent, decrease/disable, epoch schedules, budget usage, bans, confirmed unban, safe DOM');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
