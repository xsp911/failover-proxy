// Offline account/key UX contract. All requests mocked; no credentials leave this process.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); }
catch { ({chromium} = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const origin = 'http://account.invalid';
const current = 'current-password-secret';
const replacement = 'replacement-password-secret';
const prefix = 'sk-prefix-' + 'abcdef0123456789'.repeat(14);
const token = 'sk-created-' + 'fedcba9876543210'.repeat(10);
(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [], messages = [], unexpected = [], writes = [];
    let accountError = null, keys = [{id: 'key-1', name: 'Application', prefix, enabled: true, models: [], requests: 1}];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => messages.push(message.text()));
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), method = request.method();
      assert.equal(url.origin, origin, 'no external requests');
      const reply = (json, status = 200) => route.fulfill({json, status});
      if (method === 'GET' && url.pathname === '/admin/observability') return reply({}, 404);
      if (url.pathname.startsWith('/admin/')) {
        if (method !== 'GET') {
          assert.equal(request.headers()['x-csrf-token'], 'csrf-account');
          writes.push({path: url.pathname, method, body: request.postDataJSON()});
        }
        if (url.pathname === '/admin/session') return reply({username: 'admin', csrf_token: 'csrf-account'});
        if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: []});
        if (url.pathname === '/admin/health') return reply({status: 'ok'});
        if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
        if (url.pathname === '/admin/account' && method === 'POST') {
          if (accountError) return reply({error: {code: accountError.code, message: current + replacement}, current_password: current, new_password: replacement}, accountError.status);
          return reply({ok: true, reauthenticate: true});
        }
        if (url.pathname === '/admin/keys' && method === 'GET') return reply({keys});
        if (url.pathname === '/admin/keys' && method === 'POST') return reply({token});
        if (url.pathname === '/admin/keys/key-1' && method === 'DELETE') { keys = []; return reply({ok: true}); }
        unexpected.push(method + ' ' + url.pathname); return reply({error: {code: 'unexpected_endpoint'}}, 500);
      }
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    const openAccount = async () => {
      await page.goto(origin + '/');
      await page.locator('#app').waitFor({state: 'visible'});
      await page.locator('#tab-settings').click();
      await page.locator('[data-settings="account"]').click();
      await page.locator('#settings-account').waitFor({state: 'visible'});
    };
    const fillAccount = async (password = replacement, confirmation = password, username = 'new-admin') => {
      await page.locator('#account-current-password').fill(current);
      await page.locator('#account-username').fill(username);
      await page.locator('#account-new-password').fill(password);
      await page.locator('#account-confirm-password').fill(confirmation);
    };
    const dispatch = () => page.locator('#account-form').evaluate(form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
    const assertPasswordsCleared = async () => {
      for (const id of ['account-current-password', 'account-new-password', 'account-confirm-password']) assert.equal(await page.locator('#' + id).inputValue(), '');
    };
    const assertNoSecrets = async () => {
      const visible = await page.locator('body').innerText();
      for (const secret of [current, replacement]) {
        assert.equal(visible.includes(secret), false, 'secret not rendered');
        assert.equal(messages.some(message => message.includes(secret)), false, 'secret not logged');
      }
      assert.deepEqual(await page.evaluate(() => ({local: Object.keys(localStorage), session: Object.keys(sessionStorage)})), {local: [], session: []});
    };
    await openAccount();
    assert.equal(await page.locator('#account-username').inputValue(), 'admin');
    for (const [id, type, autocomplete] of [['account-current-password', 'password', 'current-password'], ['account-username', 'text', 'username'], ['account-new-password', 'password', 'new-password'], ['account-confirm-password', 'password', 'new-password']]) {
      assert.equal(await page.locator('#' + id).evaluate(node => node.type), type);
      assert.equal(await page.locator('#' + id).getAttribute('autocomplete'), autocomplete);
    }
    assert.match(await page.locator('#settings-account').innerText(), /все сессии администратора будут завершены/);
    assert.match(await page.locator('#settings-account').innerText(), /ключи клиентского API не изменятся/);
    assert.match(await page.locator('#account-password-note').innerText(), /не менее 12 символов/);
    assert.equal(await page.locator('#config-form').isVisible(), false);
    // Client checks reject missing current password, blank username, short/mismatched passwords.
    await fillAccount(); await page.locator('#account-current-password').fill(''); await dispatch();
    assert.match(await page.locator('#account-error').innerText(), /текущий пароль/);
    await fillAccount(replacement, replacement, '   '); await dispatch();
    assert.match(await page.locator('#account-error').innerText(), /непустой логин/);
    await fillAccount('short', 'short'); await dispatch();
    assert.match(await page.locator('#account-error').innerText(), /не менее 12/);
    await fillAccount(replacement, replacement + '-mismatch'); await page.locator('#account-save').click();
    assert.match(await page.locator('#account-error').innerText(), /не совпадают/);
    assert.equal(writes.length, 0, 'invalid account form never posts');
    await assertNoSecrets();
    // Safe named errors. Wrong current password is not misreported as a CSRF/session error.
    for (const [code, status, expected] of [['invalid_current_password', 403, /Текущий пароль неверен/], ['invalid_admin_username', 400, /корректный непустой логин/], ['invalid_admin_password', 400, /не менее 12/], ['account_change_rate_limited', 429, /Слишком много попыток/]]) {
      accountError = {code, status}; await fillAccount(); await page.locator('#account-save').click();
      await page.waitForFunction(() => !document.querySelector('#account-save').disabled);
      assert.match(await page.locator('#account-error').innerText(), expected);
      assert.equal(await page.locator('#app').isVisible(), true);
      await assertPasswordsCleared(); await assertNoSecrets();
    }
    accountError = null;
    await fillAccount(); await page.locator('#account-save').click();
    await page.locator('#login-view').waitFor({state: 'visible'});
    assert.equal(await page.locator('#login-error').innerText(), 'Учётные данные обновлены. Войдите с новым логином и паролем.');
    assert.deepEqual(writes.at(-1).body, {current_password: current, new_username: 'new-admin', new_password: replacement});
    assert.equal(await page.locator('#account-username').inputValue(), '');
    await assertPasswordsCleared(); await assertNoSecrets();
    assert.equal(writes.some(write => write.path.startsWith('/admin/keys')), false, 'credential changes do not mutate API keys');
    // Login-only changes explicitly send blank new_password and force reauthentication too.
    await openAccount(); await fillAccount('', '', 'login-only'); await page.locator('#account-save').click();
    await page.locator('#login-view').waitFor({state: 'visible'});
    assert.deepEqual(writes.at(-1).body, {current_password: current, new_username: 'login-only', new_password: ''});
    await assertPasswordsCleared(); assert.equal(await page.locator('#account-username').inputValue(), '');
    // Full stored prefix remains readable at desktop/mobile; no pretend recoverable token.
    await openAccount(); await page.locator('[data-settings="keys"]').click(); await page.locator('.key-prefix').waitFor();
    assert.equal(await page.locator('.key-prefix').innerText(), prefix);
    assert.equal(await page.locator('.key-prefix-label').innerText(), 'Префикс ключа · полный ключ показывается только при создании');
    for (const width of [1440, 390]) {
      await page.setViewportSize({width, height: 1000});
      const dimensions = await page.locator('.key-prefix').evaluate(node => ({width: node.getBoundingClientRect().width, parent: node.parentElement.getBoundingClientRect().width, ellipsis: getComputedStyle(node).textOverflow, family: getComputedStyle(node).fontFamily, page: document.documentElement.scrollWidth, viewport: innerWidth}));
      assert.ok(dimensions.width >= dimensions.parent - 2, 'prefix fills its card');
      assert.notEqual(dimensions.ellipsis, 'ellipsis'); assert.match(dimensions.family, /monospace/);
      assert.ok(dimensions.page <= dimensions.viewport + 1, 'long prefix does not widen page');
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.getByRole('button', {name: 'Забыть', exact: true}).click();
    assert.match(await page.locator('#confirm-description').innerText(), /доступ по нему будет отозван/);
    assert.match(await page.locator('#confirm-description').innerText(), /не просто скрытие/);
    await page.locator('#confirm-dialog button[value="cancel"]').click();
    assert.equal(writes.some(write => write.method === 'DELETE'), false);
    await page.locator('#key-name').fill('New application'); await page.locator('#create-key').click();
    await page.locator('#token-dialog').waitFor({state: 'visible'});
    assert.equal(await page.locator('#new-token').innerText(), token);
    assert.ok(await page.locator('#token-dialog').evaluate(node => node.getBoundingClientRect().width) >= 750, 'new-token dialog expanded');
    assert.equal(await page.locator('#copy-token').isVisible(), true);
    await page.locator('#close-token').click(); await page.waitForFunction(() => document.querySelector('#new-token').textContent === '');
    await page.getByRole('button', {name: 'Забыть', exact: true}).click(); await page.locator('#confirm-action').click();
    await page.locator('.key-row').waitFor({state: 'detached'});
    assert.equal(writes.at(-1).method, 'DELETE'); assert.equal(writes.at(-1).path, '/admin/keys/key-1');
    await assertNoSecrets(); assert.deepEqual(errors, []); assert.deepEqual(unexpected, []);
    console.log('browser account/key UX: ok (validation, safe errors, logout/clear, login-only, readable prefixes, permanent delete, one-time token)');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
