// Offline navigation contract. Every request intercepted; no services/provider calls.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); }
catch { ({chromium} = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const origin = 'http://a6-navigation.invalid';
const sections = ['overview', 'composition', 'checks', 'automation', 'settings'];
const labels = ['Обзор', 'Состав', 'Проверки', 'Автоматизация', 'Настройки'];
(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const errors = [], external = [], requests = [], writes = [], unexpected = [];
    page.on('pageerror', error => errors.push(error.message));
    // Exercise the actual scheduled polling callback without a wall-clock delay.
    await page.addInitScript(() => {
      const schedule = window.setTimeout.bind(window), cancel = window.clearTimeout.bind(window);
      const polls = new Map();
      window.setTimeout = (callback, delay, ...args) => {
        const id = schedule(() => { polls.delete(id); callback(...args); }, delay);
        if ([5000, 15000].includes(delay)) polls.set(id, () => callback(...args));
        return id;
      };
      window.clearTimeout = id => { polls.delete(id); cancel(id); };
      window.__runScheduledPoolPoll = () => {
        const scheduled = [...polls.entries()].at(-1);
        if (!scheduled) throw new Error('No managed-pool poll scheduled');
        const [id, callback] = scheduled; polls.delete(id); cancel(id); callback();
      };
    });
    const candidate = {channel_id: 55, listing_id: 7, supplier: 'Seller <img src=x>', eligible: true, verification_status: 'verified', input_micros: 1000000, output_micros: 2000000, cache_read_micros: 10000, cache_hit_percent: 70, success_percent: 99};
    const base = {id: 'pool-a', name: 'Pool A', revision: 3, model: 'deepseek-v3.2', reference_model: 'deepseek-v3.2', protocol: 'chat', token_id: 11, test_token_id: 22, test_token_ids: [22], enabled: true, auto_publish: false, max_probe_requests: 60, max_probe_usd: 1, auto_daily_requests: 600, auto_daily_usd: 5, status: 'draft', candidates: [candidate], members: [candidate], auto_state: {}};
    let policies = [base, {...base, id: 'pool-b', name: 'Pool B', revision: 1}], jobs = [];
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), method = request.method();
      if (url.origin !== origin) { external.push(request.url()); return route.abort(); }
      const reply = (json, status = 200) => route.fulfill({json, status});
      if (method === 'GET' && url.pathname === '/admin/observability') return reply({}, 404);
      if (method === 'GET' && /^\/admin\/a6\/pools\/[^/]+\/events$/.test(url.pathname)) return reply({events: []});
      if (url.pathname.startsWith('/admin/')) {
        requests.push({path: url.pathname, method});
        if (method !== 'GET') {
          assert.equal(request.headers()['x-csrf-token'], 'csrf');
          writes.push({path: url.pathname, method, body: request.postDataJSON()});
        }
        if (url.pathname === '/admin/session') return reply({username: 'admin', csrf_token: 'csrf'});
        if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: []});
        if (url.pathname === '/admin/health') return reply({status: 'ok'});
        if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
        if (url.pathname === '/admin/a6/pools' && method === 'GET') return reply({pools: policies, jobs, tokens: [{id: 11, name: 'Working', status: 1}, {id: 22, name: 'Tests', status: 1}], catalog: [{id: 'deepseek-v3.2'}], capabilities: {}});
        if (url.pathname === '/admin/a6/pools/pool-a/discover' && method === 'POST') {
          assert.deepEqual(request.postDataJSON(), {});
          return reply({id: 'discover-a', pool_id: 'pool-a', kind: 'discover', status: 'done', progress: 1, total: 1}, 202);
        }
        if (url.pathname === '/admin/a6/pools/pool-a/restart' && method === 'POST') {
          assert.deepEqual(request.postDataJSON(), {confirm_cost: true});
          jobs = [{id: 'restart-a', pool_id: 'pool-a', kind: 'verify', status: 'running', phase: 'testing', progress: 0, total: 3, created_at: new Date().toISOString()}];
          return reply(jobs[0], 202);
        }
        if (url.pathname === '/admin/a6/pools/pool-a' && method === 'DELETE') {
          assert.deepEqual(request.postDataJSON(), {revision: 3, confirm_remote: true});
          policies = policies.filter(policy => policy.id !== 'pool-a'); jobs = [];
          return reply({ok: true});
        }
        unexpected.push(method + ' ' + url.pathname);
        return reply({error: 'Unexpected offline API'}, 500);
      }
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    const tab = section => page.locator('#pool-section-tab-' + section);
    const panelFor = async section => page.locator('#' + await tab(section).getAttribute('aria-controls'));
    const assertSection = async selected => {
      for (let i = 0; i < sections.length; i++) {
        const section = sections[i], button = tab(section), panel = await panelFor(section);
        assert.equal(await button.getAttribute('role'), 'tab');
        assert.equal((await button.textContent()).trim(), labels[i]);
        assert.equal(await button.getAttribute('aria-selected'), String(section === selected));
        assert.equal(await button.getAttribute('tabindex'), section === selected ? '0' : '-1');
        assert.equal(await panel.getAttribute('role'), 'tabpanel');
        assert.equal(await panel.getAttribute('aria-labelledby'), 'pool-section-tab-' + section);
        assert.equal(await panel.evaluate(node => node.hidden), section !== selected, section + ' hidden state');
      }
    };
    const openControl = async id => {
      const control = page.locator('#' + id);
      const panelId = await control.evaluate(node => node.closest('[role="tabpanel"]')?.id || null);
      if (panelId) await page.locator('[role="tab"][aria-controls="' + panelId + '"]').click();
      return control;
    };
    await page.goto(origin + '/');
    await page.locator('#app').waitFor({state: 'visible'});
    const loaded = page.waitForResponse(response => new URL(response.url()).pathname === '/admin/a6/pools');
    await page.locator('#tab-market').click(); await loaded;
    await page.locator('.pool-card').first().waitFor();
    assert.equal(requests.filter(request => request.path === '/admin/a6/pools').length, 1, 'main A6API tab loads pools directly');
    for (const id of ['market-sellers-tab', 'market-pools-tab', 'market-sellers-panel', 'market-form', 'market-token', 'market-rows', 'market-refresh']) assert.equal(await page.locator('#' + id).count(), 0, id + ' obsolete seller navigation removed');
    assert.equal(await page.locator('#pool-sections').isVisible(), false, 'no pool context before explicit selection');
    assert.equal(await page.locator('#pool-form').isVisible(), false, 'new pool form does not open automatically');
    await page.locator('#pools-refresh').click();
    assert.equal(await page.locator('#pool-form').isVisible(), false, 'refresh does not start creation');
    await page.locator('.pool-card').first().click();
    await assertSection('overview');
    assert.equal(writes.length, 0, 'opening pools and selecting a pool are free');
    assert.equal(await page.locator('[role="tablist"]').filter({has: tab('overview')}).count(), 1);
    for (const section of sections) { await tab(section).click(); await assertSection(section); }
    await tab('overview').click(); await tab('overview').focus(); await page.keyboard.press('ArrowRight');
    await assertSection('composition'); assert.equal(await tab('composition').evaluate(node => node === document.activeElement), true);
    await page.keyboard.press('End'); await assertSection('settings');
    await page.keyboard.press('Home'); await assertSection('overview');
    await page.keyboard.press('ArrowLeft'); await assertSection('settings');
    await tab('automation').click(); await page.locator('.pool-card').nth(1).click(); await assertSection('overview');
    await tab('checks').click(); await page.locator('.pool-card').first().click(); await assertSection('overview');
    const discover = await openControl('pool-discover');
    assert.equal((await discover.textContent()).trim(), 'Обновить список продавцов');
    await discover.click();
    await page.waitForFunction(() => document.querySelector('#pool-discover').disabled === false);
    assert.equal(await page.locator('#confirm-dialog').evaluate(node => node.open), false, 'seller list refresh has no paid consent');
    assert.equal(writes.filter(write => write.path.endsWith('/discover')).length, 1);
    assert.equal(writes.length, 1, 'refresh does not start verification');
    await tab('checks').click();
    assert.equal(await page.locator('#pool-restart').isVisible(), true, 'restart belongs to checks');
    assert.equal((await page.locator('#pool-restart').textContent()).trim(), 'Перезапустить проверку');
    await page.locator('#pool-restart').click();
    await page.locator('#confirm-dialog').waitFor({state: 'visible'});
    assert.equal(writes.filter(write => write.path.endsWith('/restart')).length, 0, 'restart awaits paid consent');
    assert.match(await page.locator('#confirm-description').textContent(), /Свежие успешные результаты сохраняются/);
    assert.match(await page.locator('#confirm-description').textContent(), /(?:24|суточной паузе).*не проверяются повторно/);
    await page.locator('#confirm-dialog button[value=cancel]').click();
    assert.equal(writes.filter(write => write.path.endsWith('/restart')).length, 0);
    await page.locator('#pool-restart').click(); await page.locator('#confirm-dialog').waitFor({state:'visible'});
    const restarted = page.waitForResponse(response => response.url().endsWith('/restart') && response.request().method() === 'POST');
    await page.locator('#confirm-action').click(); await restarted;
    await page.waitForFunction(() => document.querySelector('#pool-restart').disabled === false);
    assert.equal(writes.filter(write => write.path.endsWith('/restart')).length, 1);
    await assertSection('checks');
    assert.equal(await page.locator('#pool-restart').isDisabled(), false, 'restart remains available for active job');
    await tab('settings').click();
    if (!await page.locator('#pool-settings').evaluate(node => node.open)) await page.locator('#pool-settings > summary').click();
    await page.locator('#pool-name').fill('Unsaved navigation edit');
    await tab('composition').click(); await tab('automation').click(); await tab('settings').click();
    assert.equal(await page.locator('#pool-name').inputValue(), 'Unsaved navigation edit');
    await tab('checks').click();
    const polled = page.waitForResponse(response => new URL(response.url()).pathname === '/admin/a6/pools');
    await page.evaluate(() => window.__runScheduledPoolPoll()); await polled;
    await assertSection('checks');
    await tab('settings').click();
    assert.equal(await page.locator('#pool-name').inputValue(), 'Unsaved navigation edit', 'poll preserves dirty fields');
    await openControl('pool-delete'); await page.locator('#pool-delete').click();
    await page.locator('#confirm-dialog').waitFor({state: 'visible'});
    assert.match(await page.locator('#confirm-description').textContent(), /Подписки, каналы и токены A6API не удаляются/);
    await page.locator('#confirm-dialog button[value=cancel]').click();
    assert.equal(writes.filter(write => write.method === 'DELETE').length, 0);
    await page.locator('#pool-delete').click(); await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelectorAll('.pool-card').length === 1);
    await page.locator('#pool-new').click(); await assertSection('settings');
    assert.equal(await page.locator('#pool-settings').evaluate(node => node.open), true);
    assert.equal(await page.locator('#pool-name').inputValue(), '');
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    assert.equal(await page.locator('#market-view img').count(), 0, 'seller data rendered as text');
    assert.deepEqual(requests.filter(request => request.path === '/admin/a6/tokens' || /\/admin\/a6\/(?:market|sellers)/.test(request.path)), [], 'no legacy seller/token requests');
    assert.deepEqual(unexpected, []); assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log('PASS offline A6API navigation: direct pools/no seller API, accessible isolated tabs + keyboard, selection overview/new settings, free seller refresh, paid restart/cancel + active availability, dirty subsection/poll preservation, safe deletion, mobile');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
