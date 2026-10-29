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


(async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox']});try{
 {const f=await fixture(browser);try{
  const p=f.page;await f.open('provider-a');await f.name('provider-a').fill('untouched-draft');
  for(const section of ['models','proxies','general','providers']){await p.locator('[data-settings="'+section+'"]').click();assert(await p.locator('#config-actions').isHidden())}
  await p.locator('[data-settings=models]').click();const model=p.locator('[data-model-id="model-a"]');assert.equal(await model.locator('.model-edit').getAttribute('open'),null);await model.locator('.model-edit>summary').click();await model.getByLabel(/Имя модели \/ клиентский алиас/).fill('renamed-model');await model.locator('[data-model-save]').click();await f.settled();assert.equal(f.saves.length,1);assert.equal(f.saves[0].providers[0].name,'Provider A');assert.equal(f.saves[0].models[0].name,'renamed-model');assert.equal(await f.name('provider-a').inputValue(),'untouched-draft');
  await p.locator('[data-settings=general]').click();await p.locator('#general-fields input').first().fill('200');await p.locator('#save-general').click();await f.settled();assert.equal(f.saves[1].settings.request_timeout_s,200);assert.equal(f.saves[1].providers[0].name,'Provider A');assert.equal(await f.name('provider-a').inputValue(),'untouched-draft');
  await p.locator('[data-settings=proxies]').click();await p.locator('#add-proxy').click();const proxy=p.locator('[data-proxy-id]').first();await proxy.getByLabel('Название профиля',{exact:true}).fill('new-profile');await proxy.getByLabel(/Хост прокси/).fill('127.0.0.1');await proxy.getByLabel('Порт',{exact:true}).fill('3128');await proxy.getByLabel(/Новый пароль прокси/).fill('fixture-only-secret');await p.locator('#save-proxies').click();await f.settled();assert.equal(f.saves[2].outbound_proxies[0].password,'fixture-only-secret');assert.equal(f.saves[2].providers[0].name,'Provider A');
  await p.locator('#tab-settings').click();await f.card('provider-b').getByRole('button',{name:'Удалить',exact:true}).click();await p.locator('#confirm-action').click();await p.locator('#save-provider-deletions').click();await f.settled();assert.equal(f.saves[3].providers.length,1);assert.equal(f.saves[3].providers[0].name,'Provider A');assert(!f.saves[3].models[0].routes.some(x=>x.provider_id==='provider-b'));assert.equal(await f.name('provider-a').inputValue(),'untouched-draft');
  await p.locator('[data-settings=api-access]').click();assert(await p.locator('#config-actions').isVisible());f.clean();console.log('PASS scoped models/general/proxy/deletions: isolated CAS saves preserve other drafts; four topbars removed; IPsave stays');
 }finally{await f.context.close()}}
 {const f=await fixture(browser,{conflict:true});try{await f.page.locator('[data-settings=models]').click();const m=f.page.locator('[data-model-id]').first();await m.locator('.model-edit>summary').click();await m.getByLabel(/Имя модели \/ клиентский алиас/).fill('retained-on-conflict');await m.locator('[data-model-save]').click();await f.settled();assert.equal(f.saves.length,1);assert.equal(await m.getByLabel(/Имя модели \/ клиентский алиас/).inputValue(),'retained-on-conflict');assert.match(await m.locator('[data-model-error]').textContent(),/другой сессии/);f.clean();console.log('PASS scoped model409 retains draft; no retry')}finally{await f.context.close()}}
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
