// Offline proxy/access regression. All HTTP intercepted; no server, live proxy or paid calls.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const origin = 'http://proxy-access.invalid';
(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], forbidden = [], writes = [], checks = [], verifies = [];
    const serverSecret = 'SERVER-SECRET-MUST-NEVER-RENDER';
    let conflict = false;
    let config = { revision: 1, settings: { request_timeout_s: 120, connect_timeout_s: 10, read_timeout_s: 60, cooldown_s: 30, failure_threshold: 3, max_request_bytes: 1024, max_response_bytes: 2048, max_buffer_events: 100, max_concurrent_requests: 2, max_attempts: 2, trust_private_upstreams: false },
      outbound_proxies: [{ id: 'px1', name: 'Saved <proxy>', scheme: 'socks5h', host: 'proxy.invalid', port: 1080, username: 'user', has_password: true, password: serverSecret }, { id: 'px2', name: 'Backup', scheme: 'https', host: 'backup.invalid', port: 443, username: '', has_password: false }],
      api_access: { enabled: false, allowed_ips: ['192.0.2.1'] },
      providers: [{ id: 'p1', name: 'Provider', base_url: 'https://provider.invalid/v1', priority: 1, enabled: true, has_api_key: true, api_key: serverSecret, proxy_id: 'px1', routing_mode: 'openrouter', service_tier: 'priority', provider_pin: 'vendor/exact' }], models: [] };
    let a6 = { configured: true, has_pat: true, pat_masked: '***', pat: serverSecret, user_id: 123, proxy_id: 'px1' };
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== origin) { forbidden.push(url.href); return route.abort(); }
      if (url.pathname === '/admin/config' && req.method() === 'PUT') {
        const body = req.postDataJSON(); writes.push(body);
        if (conflict) return route.fulfill({ status: 409, json: { error: { code: 'revision_conflict' } } });
        assert.equal(body.revision, config.revision);
        config = { ...body, revision: config.revision + 1, outbound_proxies: body.outbound_proxies.map(p => ({ ...p, password: serverSecret, has_password: p.clear_password ? false : p.password ? true : p.has_password })) };
        return route.fulfill({ json: config });
      }
      if (/\/admin\/proxies\/[^/]+\/check$/.test(url.pathname)) { checks.push({ path: url.pathname, body: req.postDataJSON() }); return route.fulfill({ json: { ok: true, ip: '198.51.100.8', elapsed_ms: 12 } }); }
      if (url.pathname === '/admin/a6/verify') { verifies.push(req.postDataJSON()); return route.fulfill({ json: { ok: true, user_id: 123, tokens_total: 0 } }); }
      if (url.pathname === '/admin/a6' && req.method() === 'PUT') { const body = req.postDataJSON(); writes.push({ a6: body }); a6 = { ...a6, proxy_id: body.proxy_id }; return route.fulfill({ json: a6 }); }
      const replies = { '/admin/session': { username: 'offline', csrf_token: 'csrf' }, '/admin/config': config, '/admin/stats': { summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: [] }, '/admin/health': {}, '/admin/observability': {}, '/admin/keys': { keys: [] }, '/admin/access': { client_ip: '2001:db8::42', api_access: config.api_access, forwarded_headers_trusted: false }, '/admin/a6': a6, '/admin/a6/pools': { pools: [], jobs: [], tokens: [], catalog: [], capabilities: {} } };
      if (url.pathname.startsWith('/admin/')) { assert.equal(req.method(), 'GET', 'unexpected mutation ' + url.pathname); assert.ok(Object.hasOwn(replies, url.pathname), url.pathname); return route.fulfill({ json: replies[url.pathname] }); }
      const asset = { '/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css' }[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({ body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    const settings = name => page.locator('[data-settings="' + name + '"]');
    const profile = id => page.locator('[data-proxy-id="' + id + '"]');
    const openProviderNetwork = async () => {
      const card = page.locator('[data-provider-id="p1"]');
      for (const selector of ['details.provider-edit', 'details.provider-network']) {
        const details = card.locator(selector);
        if (await details.getAttribute('open') === null) await details.locator(':scope > summary').click();
      }
    };
    const assertNoServerSecrets = async () => {
      assert.ok(!(await page.content()).includes(serverSecret));
      assert.ok(!(await page.locator('input').evaluateAll(inputs => inputs.map(i => i.value).join('\n'))).includes(serverSecret));
    };
    await page.goto(origin);
    await page.locator('#tab-settings').click();
    await openProviderNetwork();
    await page.locator('[data-provider-proxy="p1"]').waitFor();
    await assertNoServerSecrets();
    assert.equal(await page.locator('[data-provider-proxy="p1"]').inputValue(), 'px1');
    await settings('proxies').click();
    assert.equal(await profile('px1').locator('input[type=password]').inputValue(), '');
    await page.locator('[data-proxy-check="px1"]').click();
    await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
    assert.match(await page.locator('#confirm-description').textContent(), /внешний запрос/);
    await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelector('[data-proxy-id="px1"] .test-result').textContent.includes('198.51.100.8'));
    assert.deepEqual(checks, [{ path: '/admin/proxies/px1/check', body: {} }]);
    await page.locator('#add-proxy').click();
    const added = page.locator('.proxy-card').last();
    const newId = await added.getAttribute('data-proxy-id');
    await added.getByLabel('Название профиля', { exact: true }).fill('New profile');
    await added.getByLabel('Хост прокси', { exact: false }).fill('new.invalid');
    await added.getByLabel('Новый пароль прокси', { exact: false }).fill('offline-new-secret');
    await added.locator('select').selectOption('socks5');
    assert.equal(await added.locator('[data-proxy-check]').isDisabled(), true);
    await settings('providers').click();
    await openProviderNetwork();
    await page.locator('[data-provider-proxy="p1"]').selectOption('px2');
    await page.locator('#tab-access').click();
    await settings('api-access').click();
    await page.locator('#api-access-enabled').check();
    await page.locator('#api-access-ips').fill('192.168.1.1/24\n2001:db8::/32\n192.0.2.9');
    await page.locator('#refresh-access-ip').click();
    await page.waitForFunction(() => document.getElementById('access-client-ip').textContent.includes('2001:db8::42'));
    assert.equal(await page.locator('#api-access-ips').inputValue(), '192.168.1.1/24\n2001:db8::/32\n192.0.2.9');
    await page.locator('#tab-settings').click();
    await settings('proxies').click();
    assert.equal(await profile(newId).locator('input[type=password]').inputValue(), 'offline-new-secret', 'draft password survives section/provider changes');
    await profile(newId).getByRole('button', { name: 'Выше', exact: true }).click();
    assert.equal(await page.locator('.proxy-card').nth(1).getAttribute('data-proxy-id'), newId);
    assert.equal(writes.length, 0, 'create/reorder/field changes never autosave');
    await page.locator('#save-proxies').click();
    await page.waitForFunction(() => !document.getElementById('config-fieldset').disabled && document.getElementById('save-proxies').hidden);
    assert.equal(writes.length, 1);
    assert.equal(writes[0].outbound_proxies[0].password, undefined, 'blank existing password omitted');
    assert.equal(writes[0].outbound_proxies[1].password, 'offline-new-secret');
    assert.equal(writes[0].providers[0].api_key, undefined, 'existing provider key preserved by omission');
    assert.equal(writes[0].providers[0].proxy_id, 'px1', 'proxy section does not submit provider draft');
    assert.equal(writes[0].providers[0].routing_mode, 'openrouter');
    assert.equal(writes[0].providers[0].service_tier, 'priority');
    assert.equal(writes[0].providers[0].provider_pin, 'vendor/exact');
    assert.deepEqual(writes[0].api_access, { enabled: false, allowed_ips: ['192.0.2.1'] }, 'proxy section does not submit whitelist draft');
    await assertNoServerSecrets();
    assert.equal(await profile(newId).locator('input[type=password]').inputValue(), '');
    await page.locator('#tab-market').click();
    await page.locator('#a6-tab-creds').click();
    await page.waitForFunction(() => document.getElementById('a6-proxy-id').value === 'px1');
    assert.equal(await page.locator('#a6-pat').inputValue(), '');
    await page.locator('#a6-proxy-id').selectOption(newId);
    await page.locator('#a6-reload').click();
    await page.waitForFunction(() => !document.getElementById('a6-reload').disabled);
    assert.equal(await page.locator('#a6-proxy-id').inputValue(), newId, 'reload retains unsaved proxy selection');
    await page.locator('#a6-verify').click();
    await page.waitForFunction(() => !document.getElementById('a6-verify').disabled);
    assert.equal(verifies[0].proxy_id, newId, 'verify uses unsaved selection');
    assert.equal(verifies[0].pat, '', 'blank PAT preserves existing secret');
    await page.locator('#a6-save').click();
    await page.waitForFunction(() => !document.getElementById('a6-save').disabled);
    assert.equal(writes.at(-1).a6.proxy_id, newId);
    await page.locator('#a6-use-proxy').uncheck();
    await page.locator('#a6-verify').click();
    await page.waitForFunction(() => !document.getElementById('a6-verify').disabled);
    assert.equal(verifies.at(-1).proxy_id, null, 'direct verify explicitly sends null');
    await page.locator('#tab-settings').click();
    await settings('providers').click();
    await openProviderNetwork();
    await page.locator('[data-provider-use-proxy="p1"]').uncheck();
    await page.locator('[data-provider-save]').click();
    await page.waitForFunction(() => !document.getElementById('config-fieldset').disabled);
    assert.equal(writes.at(-1).providers[0].proxy_id, null);
    await settings('proxies').click();
    await profile('px2').getByRole('button', { name: 'Удалить', exact: true }).click();
    await page.locator('#confirm-action').click();
    await profile('px2').waitFor({ state: 'detached' });
    assert.equal(await profile('px2').count(), 0);
    conflict = true;
    await page.locator('#save-proxies').click();
    await page.locator('#proxies-save-error').waitFor({ state: 'visible' });
    assert.equal(await profile('px2').count(), 0, 'conflict retains deletion draft');
    assert.equal(await page.locator('#save-proxies').isEnabled(), true);
    await page.locator('#tab-access').click(); await settings('api-access').click();
    assert.equal(await page.locator('#api-access-ips').inputValue(), '192.168.1.1/24\n2001:db8::/32\n192.0.2.9', 'whitelist retained through provider/A6 edits and conflict');
    await page.setViewportSize({ width: 375, height: 812 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    await assertNoServerSecrets();
    assert.deepEqual(errors, []); assert.deepEqual(forbidden, []);
    console.log('PASS offline proxy/access: masked secrets, draft create/reorder/delete, explicit CAS/conflict, provider policies/key retention, saved-only disclosed IP check, whitelist retention, A6 unsaved verify/save/direct proxy, mobile overflow; zero live calls');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
