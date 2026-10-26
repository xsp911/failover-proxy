// Offline provider policy contract. No services, live configuration, or upstream calls.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const origin = 'http://provider-policy.invalid';
const initial = () => ({
  revision: 1,
  settings: { request_timeout_s: 120, connect_timeout_s: 10, read_timeout_s: 60, cooldown_s: 30, failure_threshold: 3, max_request_bytes: 1000000, max_response_bytes: 1000000, max_buffer_events: 1000, max_concurrent_requests: 10, max_attempts: 3, trust_private_upstreams: false },
  providers: [{ id: 'provider-a', name: 'Provider A', base_url: 'https://upstream.invalid/v1', enabled: true, priority: 1, has_api_key: true }],
  models: [{ id: 'model-a', name: 'model-a', enabled: true, supports_vision: false, context_length: 128000, max_output_tokens: 8000, routes: [{ provider_id: 'provider-a', upstream_model: 'vendor/model-a', enabled: true }] }]
});
(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [], unexpected = [], saves = [];
    let config = initial();
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, origin, 'all requests stay inside offline fixture');
      const reply = (json, status = 200) => route.fulfill({ json, status });
      if (url.pathname === '/admin/session') return reply({ username: 'admin', csrf_token: 'csrf' });
      if (url.pathname === '/admin/stats') return reply({ generated_at: '2026-10-26T12:00:00Z', summary: { final_requests: 0 }, providers: [], timeline: [], recent_requests: [], recent_errors: [] });
      if (url.pathname === '/admin/health') return reply({ status: 'ok' });
      if (url.pathname === '/admin/observability') return reply({}, 404);
      if (url.pathname === '/admin/config') {
        if (request.method() === 'GET') return reply(config);
        assert.equal(request.method(), 'PUT');
        assert.equal(request.headers()['x-csrf-token'], 'csrf');
        const body = request.postDataJSON();
        saves.push(body); config = { ...body, revision: config.revision + 1 };
        return reply(config);
      }
      if (url.pathname.startsWith('/admin/')) { unexpected.push(request.method() + ' ' + url.pathname); return reply({}, 500); }
      const asset = { '/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css' }[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({ body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html' });
    });
    await page.goto(origin + '/');
    await page.locator('#app').waitFor({ state: 'visible' });
    await page.locator('#tab-settings').click();
    await page.waitForFunction(() => !document.querySelector('#config-fieldset').disabled);
    const mode = () => page.locator('[data-provider-routing-mode]');
    const tier = () => page.locator('[data-provider-service-tier]');
    const pin = () => page.locator('[data-provider-pin]');
    const save = async () => {
      const count = saves.length;
      await page.locator('#save-config').click();
      await page.waitForFunction(() => document.querySelector('#save-config').disabled && document.querySelector('#config-state').textContent.includes('Все изменения сохранены'));
      assert.equal(saves.length, count + 1);
      assert.equal(await page.locator('#config-error').isVisible(), false);
      return saves.at(-1);
    };
    assert.equal(await page.locator('.provider-routing').getAttribute('open'), null, 'advanced policy collapsed by default');
    await page.locator('.provider-routing > summary').click();
    assert.equal(await mode().inputValue(), 'native');
    assert.equal(await tier().inputValue(), '');
    assert.equal(await pin().inputValue(), '');
    assert.deepEqual(await tier().locator('option').evaluateAll(options => options.map(option => option.value)), ['', 'default', 'flex', 'priority', 'fast', 'ultrafast']);
    assert.match(await page.locator('.provider-routing').textContent(), /provider\.only.*allow_fallbacks: false/);
    // Unrelated edits do not inject optional defaults into old configs.
    await page.locator('#provider-editor input[type=number]').fill('2');
    let body = await save();
    for (const field of ['routing_mode', 'service_tier', 'provider_pin']) assert.equal(Object.hasOwn(body.providers[0], field), false);
    assert.equal(Object.hasOwn(body.models[0], 'context_preflight'), false);
    await page.locator('.provider-routing > summary').click();
    await tier().selectOption('priority');
    assert.equal(await mode().inputValue(), 'openrouter', 'tier requires explicit OpenRouter policy in payload');
    await pin().fill('vendor/exact-endpoint');
    body = await save();
    assert.equal(body.providers[0].routing_mode, 'openrouter');
    assert.equal(body.providers[0].service_tier, 'priority');
    assert.equal(body.providers[0].provider_pin, 'vendor/exact-endpoint');
    assert.equal(Object.hasOwn(body.providers[0], 'api_key'), false, 'write-only key is not hydrated');
    await page.locator('.provider-routing > summary').click();
    assert.equal(await tier().inputValue(), 'priority', 'saved tier roundtrips');
    assert.equal(await pin().inputValue(), 'vendor/exact-endpoint', 'exact slug roundtrips');
    await page.locator('[data-settings=models]').click();
    const preflight = () => page.locator('[data-model-context-preflight]');
    assert.deepEqual(await preflight().locator('option').evaluateAll(options => options.map(option => option.value)), ['off', 'observe']);
    assert.equal(await preflight().inputValue(), 'off');
    assert.match(await page.locator('#model-editor').textContent(), /Оценка не точный токенизатор.*observe не отклоняет запросы/);
    await preflight().selectOption('observe');
    body = await save();
    assert.equal(body.models[0].context_preflight, 'observe');
    assert.equal(body.models[0].context_length, 128000);
    assert.equal(body.providers[0].provider_pin, 'vendor/exact-endpoint', 'model edits preserve provider policy');
    assert.equal(await preflight().inputValue(), 'observe');
    await page.locator('[data-settings=providers]').click();
    await page.locator('.provider-routing > summary').click();
    await mode().selectOption('native');
    assert.equal(await tier().inputValue(), ''); assert.equal(await pin().inputValue(), '');
    body = await save();
    assert.equal(body.providers[0].routing_mode, 'native');
    assert.equal(body.providers[0].service_tier, null); assert.equal(body.providers[0].provider_pin, null);
    await page.locator('.provider-routing > summary').click();
    await pin().fill('vendor/only-endpoint');
    assert.equal(await mode().inputValue(), 'openrouter', 'pin alone enables policy');
    assert.equal(await tier().inputValue(), '', 'no automatic service tier');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, 'advanced policy fits mobile');
    assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    console.log('PASS provider policy: omitted defaults, exact roundtrip, collapsed controls, tiers, native clearing, off/observe, no upstream calls, mobile');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
