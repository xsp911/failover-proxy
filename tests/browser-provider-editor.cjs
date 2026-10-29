// Offline provider-card contract. No services, live config, credentials, or upstream calls.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({ chromium } = require('playwright')); }
catch { ({ chromium } = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const origin = 'http://provider-editor.invalid';
const copy = value => JSON.parse(JSON.stringify(value));
const initial = () => ({
  revision: 7,
  settings: { request_timeout_s: 120, connect_timeout_s: 10, read_timeout_s: 60, cooldown_s: 30, failure_threshold: 3, max_request_bytes: 1000000, max_response_bytes: 1000000, max_buffer_events: 1000, max_concurrent_requests: 10, max_attempts: 3, trust_private_upstreams: false },
  outbound_proxies: [],
  api_access: { enabled: false, allowed_ips: [] },
  providers: [
    { id: 'provider-a', name: 'Provider A', base_url: 'https://alpha.invalid/v1', enabled: true, priority: 1, has_api_key: true },
    { id: 'provider-b', name: 'Provider B', base_url: 'https://beta.invalid/v1', enabled: true, priority: 2, has_api_key: true }
  ],
  models: [{ id: 'model-a', name: 'model-a', enabled: true, supports_vision: false, context_length: 128000, max_output_tokens: 8000, routes: [{ provider_id: 'provider-a', upstream_model: 'vendor/model-a', enabled: true }, { provider_id: 'provider-b', upstream_model: 'vendor/model-b', enabled: true }] }]
});

async function fixture(browser, { conflict = false, mobile = false } = {}) {
  const context = await browser.newContext({ viewport: mobile ? { width: 375, height: 812 } : { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.setDefaultTimeout(6000);
  const errors = [], unexpected = [], saves = [], methods = [];
  let config = initial();
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    const reply = (json, status = 200) => route.fulfill({ json, status });
    if (url.origin !== origin) {
      unexpected.push(request.method() + ' ' + request.url());
      return route.abort();
    }
    if (url.pathname === '/admin/session') return reply({ username: 'admin', csrf_token: 'csrf-offline' });
    if (url.pathname === '/admin/stats') return reply({ generated_at: '2026-10-26T12:00:00Z', summary: { final_requests: 0 }, providers: [], timeline: [], recent_requests: [], recent_errors: [] });
    if (url.pathname === '/admin/health') return reply({ status: 'ok' });
    if (url.pathname === '/admin/observability') return reply({}, 404);
    if (url.pathname === '/admin/config') {
      methods.push(request.method());
      if (request.method() === 'GET') return reply(config);
      if (request.method() !== 'PUT' || request.headers()['x-csrf-token'] !== 'csrf-offline') {
        unexpected.push('Invalid config write: ' + request.method());
        return reply({ detail: 'Invalid fixture request' }, 400);
      }
      const body = request.postDataJSON();
      saves.push(copy(body));
      if (conflict) return reply({ detail: 'Offline revision conflict: reload explicitly before saving again.' }, 409);
      if (body.revision !== config.revision) return reply({ detail: 'Wrong CAS revision' }, 409);
      // Model a masked server response: newly supplied keys never echo back.
      config = copy(body);
      config.revision += 1;
      config.providers = config.providers.map(provider => {
        const clean = { ...provider, has_api_key: provider.has_api_key || Boolean(provider.api_key) };
        delete clean.api_key;
        return clean;
      });
      return reply(config);
    }
    if (url.pathname.startsWith('/admin/')) {
      unexpected.push(request.method() + ' ' + url.pathname);
      return reply({ detail: 'Unexpected offline endpoint' }, 500);
    }
    const asset = { '/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css' }[url.pathname];
    if (!asset) return route.abort();
    return route.fulfill({ body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html' });
  });
  await page.goto(origin + '/');
  await page.locator('#app').waitFor({ state: 'visible' });
  if (mobile) await page.locator('#nav-toggle').click();
  await page.locator('#tab-settings').click();
  await page.waitForFunction(() => !document.querySelector('#config-fieldset').disabled);
  const card = id => page.locator('[data-provider-id="' + id + '"]');
  const edit = id => card(id).locator('details.provider-edit');
  const save = id => card(id).locator('[data-provider-save]');
  const name = id => card(id).getByLabel('Название', { exact: true });
  const url = id => card(id).getByLabel(/Базовый URL/);
  const key = id => card(id).locator('input[data-provider-secret]');
  const open = async id => {
    if (await edit(id).getAttribute('open') === null) await edit(id).locator(':scope > summary').click();
    assert.notEqual(await edit(id).getAttribute('open'), null, 'provider summary opens its editor');
  };
  const assertSaveHidden = async (id, hidden) => {
    assert.equal(await save(id).evaluate(node => node.hidden || getComputedStyle(node).display === 'none'), hidden, id + ' local save reflects only its own changes');
  };
  const settled = async () => page.waitForFunction(() => !document.querySelector('#config-fieldset').disabled && !document.querySelector('#reload-config').disabled);
  const clean = () => { assert.deepEqual(errors, [], 'no browser exceptions'); assert.deepEqual(unexpected, [], 'no network outside fixture or upstream test calls'); };
  return { context, page, saves, methods, card, edit, save, name, url, key, open, assertSaveHidden, settled, clean };
}

async function isolatedSave(browser) {
  const f = await fixture(browser);
  try {
    for (const id of ['provider-a', 'provider-b']) {
      assert.equal(await f.edit(id).getAttribute('open'), null, 'saved providers start collapsed');
      await f.assertSaveHidden(id, true);
      await f.open(id);
    }
    await f.name('provider-a').fill('Provider A temporary');
    await f.assertSaveHidden('provider-a', false);
    await f.assertSaveHidden('provider-b', true);
    await f.name('provider-b').fill('Provider B draft');
    await f.name('provider-a').fill('Provider A');
    await f.assertSaveHidden('provider-a', true);
    await f.assertSaveHidden('provider-b', false);
    await f.name('provider-a').fill('Provider A changed');
    await f.url('provider-a').fill('https://alpha-edited.invalid/v1');
    await f.key('provider-a').fill('offline-new-key-a');
    await f.url('provider-b').fill('https://beta-draft.invalid/v1');
    await f.key('provider-b').fill('offline-new-key-b');
    await f.assertSaveHidden('provider-a', false);
    await f.assertSaveHidden('provider-b', false);
    await f.save('provider-a').click();
    await f.page.waitForFunction(() => document.querySelector('#config-state').textContent.includes('Ревизия 8'));
    await f.settled();
    assert.equal(f.saves.length, 1, 'one local click performs one CAS PUT');
    const body = f.saves[0];
    assert.equal(body.revision, 7, 'CAS uses last server revision');
    assert.equal(body.providers[0].name, 'Provider A changed');
    assert.equal(body.providers[0].base_url, 'https://alpha-edited.invalid/v1');
    assert.equal(body.providers[0].api_key, 'offline-new-key-a');
    assert.deepEqual(body.providers[1], initial().providers[1], 'other provider comes from saved config, not its draft');
    assert.equal(Object.hasOwn(body.providers[1], 'api_key'), false, 'other unsaved key is never transmitted');
    assert.deepEqual(body.settings, initial().settings);
    assert.deepEqual(body.models, initial().models);
    assert.deepEqual(body.outbound_proxies, []);
    assert.deepEqual(body.api_access, initial().api_access);
    for (const id of ['provider-a', 'provider-b']) await f.open(id);
    assert.equal(await f.name('provider-a').inputValue(), 'Provider A changed');
    assert.equal(await f.key('provider-a').inputValue(), '', 'saved key cleared after masked response');
    assert.match(await f.key('provider-a').getAttribute('placeholder'), /Ключ сохранён/);
    await f.assertSaveHidden('provider-a', true);
    assert.equal(await f.name('provider-b').inputValue(), 'Provider B draft');
    assert.equal(await f.url('provider-b').inputValue(), 'https://beta-draft.invalid/v1');
    assert.equal(await f.key('provider-b').inputValue(), 'offline-new-key-b', 'other new key survives server rebase');
    await f.assertSaveHidden('provider-b', false);
    assert.match(await f.page.locator('#config-state').textContent(), /Есть несохранённые изменения/);
    assert.equal(await f.page.locator('#save-config').isEnabled(), true, 'other draft remains globally dirty');
    assert.equal(await f.page.locator('#config-error').isVisible(), false);
    // The next local save must use the rebased revision and retain the first save.
    await f.save('provider-b').click();
    await f.page.waitForFunction(() => document.querySelector('#config-state').textContent.includes('Ревизия 9'));
    await f.settled();
    assert.equal(f.saves.length, 2);
    assert.equal(f.saves[1].revision, 8);
    assert.equal(f.saves[1].providers[0].name, 'Provider A changed');
    assert.equal(Object.hasOwn(f.saves[1].providers[0], 'api_key'), false, 'previously saved key not echoed into later PUT');
    assert.equal(f.saves[1].providers[1].api_key, 'offline-new-key-b');
    await f.open('provider-b');
    await f.assertSaveHidden('provider-b', true);
    assert.equal(await f.key('provider-b').inputValue(), '');
    assert.match(await f.page.locator('#config-state').textContent(), /Все изменения сохранены/);
    f.clean();
  } finally { await f.context.close(); }
}

async function conflictKeepsDraft(browser) {
  const f = await fixture(browser, { conflict: true });
  try {
    for (const id of ['provider-a', 'provider-b']) await f.open(id);
    await f.name('provider-a').fill('A conflict draft');
    await f.url('provider-a').fill('https://conflict-a.invalid/v1');
    await f.key('provider-a').fill('offline-conflict-a');
    await f.name('provider-b').fill('B conflict draft');
    await f.url('provider-b').fill('https://conflict-b.invalid/v1');
    await f.key('provider-b').fill('offline-conflict-b');
    await f.save('provider-a').click();
    const inlineError = f.card('provider-a').locator('[data-provider-error="provider-a"]');
    await inlineError.waitFor({ state: 'visible' });
    await f.settled();
    assert.match(await inlineError.textContent(), /конфликт|ревизи|другой сессии|conflict|409/i, 'conflict shown inline in target provider card');
    assert.equal(await f.card('provider-b').locator('[data-provider-error="provider-b"]').isVisible(), false, 'other provider has no spurious conflict error');
    // Observe a bounded quiet interval: a rejected CAS must not retry or reload.
    await f.page.waitForTimeout(350);
    assert.equal(f.saves.length, 1, '409 never automatically retries');
    assert.deepEqual(f.methods, ['GET', 'PUT'], '409 never silently reloads configuration');
    for (const [id, name, url, key] of [
      ['provider-a', 'A conflict draft', 'https://conflict-a.invalid/v1', 'offline-conflict-a'],
      ['provider-b', 'B conflict draft', 'https://conflict-b.invalid/v1', 'offline-conflict-b']
    ]) {
      await f.open(id);
      assert.equal(await f.name(id).inputValue(), name);
      assert.equal(await f.url(id).inputValue(), url);
      assert.equal(await f.key(id).inputValue(), key, '409 preserves all unsaved secrets');
      await f.assertSaveHidden(id, false);
      assert.equal(await f.save(id).isEnabled(), true);
    }
    assert.match(await f.page.locator('#config-state').textContent(), /Ревизия 7.*Есть несохранённые изменения/);
    f.clean();
  } finally { await f.context.close(); }
}

async function targetOnlyValidation(browser) {
  const f = await fixture(browser);
  try {
    for (const id of ['provider-a', 'provider-b']) await f.open(id);
    await f.name('provider-a').fill('Valid target');
    await f.name('provider-b').fill('');
    await f.url('provider-b').fill('not-a-valid-url');
    await f.key('provider-b').fill('offline-invalid-draft-key');
    await f.save('provider-a').click();
    await f.page.waitForFunction(() => document.querySelector('#config-state').textContent.includes('Ревизия 8'));
    await f.settled();
    assert.equal(f.saves.length, 1, 'invalid unrelated draft cannot block valid target');
    assert.deepEqual(f.saves[0].providers[1], initial().providers[1]);
    await f.open('provider-b');
    assert.equal(await f.name('provider-b').inputValue(), '');
    assert.equal(await f.url('provider-b').inputValue(), 'not-a-valid-url');
    assert.equal(await f.key('provider-b').inputValue(), 'offline-invalid-draft-key');
    await f.save('provider-b').click();
    await f.page.waitForTimeout(150);
    assert.equal(f.saves.length, 1, 'invalid target is validated before network');
    await f.assertSaveHidden('provider-b', false);
    assert.equal(await f.key('provider-b').inputValue(), 'offline-invalid-draft-key');
    f.clean();
  } finally { await f.context.close(); }
}

async function nonProviderDraftsStayLocal(browser) {
  const f = await fixture(browser);
  try {
    await f.page.locator('[data-settings=general]').click();
    const timeout = () => f.page.locator('#general-fields input[type=number]').first();
    await timeout().fill('321');
    await f.page.locator('[data-settings=models]').click();
    const modelName = () => f.page.locator('#model-editor').getByLabel(/Имя модели \/ клиентский алиас/);
    await f.page.locator('#model-editor .model-edit > summary').first().click();
    await modelName().fill('local-model-draft');
    await f.page.locator('#tab-settings').click();
    await f.open('provider-a');
    await f.name('provider-a').fill('Provider A scoped save');
    await f.save('provider-a').click();
    await f.page.waitForFunction(() => document.querySelector('#config-state').textContent.includes('Ревизия 8'));
    await f.settled();
    assert.equal(f.saves.length, 1);
    assert.deepEqual(f.saves[0].settings, initial().settings, 'local provider save excludes unrelated settings draft');
    assert.deepEqual(f.saves[0].models, initial().models, 'local provider save excludes unrelated model draft');
    await f.assertSaveHidden('provider-a', true);
    await f.assertSaveHidden('provider-b', true);
    assert.match(await f.page.locator('#config-state').textContent(), /Есть несохранённые изменения/);
    await f.page.locator('[data-settings=general]').click();
    assert.equal(await timeout().inputValue(), '321', 'settings draft survives provider response rebase');
    await f.page.locator('[data-settings=models]').click();
    assert.equal(await modelName().inputValue(), 'local-model-draft', 'model draft survives provider response rebase');
    f.clean();
  } finally { await f.context.close(); }
}

async function compactMobileAndDraftDelete(browser) {
  const f = await fixture(browser, { mobile: true });
  try {
    for (const id of ['provider-a', 'provider-b']) {
      assert.equal(await f.edit(id).getAttribute('open'), null, 'saved cards collapsed at 375px');
      await f.assertSaveHidden(id, true);
    }
    const fits = async () => assert.equal(await f.page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, 'provider editor has no horizontal overflow at 375px');
    await fits();
    await f.open('provider-a');
    await fits();
    const remove = f.card('provider-a').locator('button.destructive');
    assert.equal(await remove.count(), 1, 'provider delete has explicit destructive button styling');
    const color = await remove.evaluate(node => getComputedStyle(node).color);
    const rgb = color.match(/[\d.]+/g)?.map(Number);
    assert.ok(rgb && rgb[0] > rgb[1] && rgb[0] > rgb[2], 'destructive delete is visibly red');
    await remove.click();
    await f.page.locator('#confirm-dialog').waitFor({ state: 'visible' });
    assert.match(await f.page.locator('#confirm-description').textContent(), /черновик/);
    assert.equal(await f.card('provider-a').count(), 1, 'confirmation alone cannot remove provider');
    assert.equal(f.saves.length, 0, 'opening confirmation never writes config');
    await f.page.locator('#confirm-dialog').getByRole('button', { name: 'Отмена', exact: true }).click();
    assert.equal(await f.card('provider-a').count(), 1, 'cancel leaves provider intact');
    await remove.click();
    await f.page.locator('#confirm-action').click();
    await f.card('provider-a').waitFor({ state: 'detached' });
    assert.equal(f.saves.length, 0, 'confirmed delete changes draft only');
    assert.equal(await f.card('provider-b').count(), 1);
    assert.match(await f.page.locator('#config-state').textContent(), /Есть несохранённые изменения/);
    await f.assertSaveHidden('provider-b', true);
    await fits();
    f.clean();
  } finally { await f.context.close(); }
}

(async () => {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const failures = [];
    for (const scenario of [isolatedSave, conflictKeepsDraft, targetOnlyValidation, nonProviderDraftsStayLocal, compactMobileAndDraftDelete]) {
      try { await scenario(browser); console.log('PASS ' + scenario.name); }
      catch (error) { failures.push(error); console.error('FAIL ' + scenario.name + ': ' + error.message); }
    }
    if (failures.length) throw new AggregateError(failures, failures.length + ' offline provider editor scenarios failed');
    console.log('PASS provider editor: collapsed cards, local dirty saves, isolated CAS payloads, masked reply rebase, other drafts/secrets retained, 409 no retry, target-only validation, destructive draft-only delete, mobile 375px, offline');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
