// Offline A6API entry regression. Removed seller UI must not issue dedicated seller/token requests.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); }
catch { ({chromium} = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');

(async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const page = await browser.newPage();
    const errors = [], requests = [], forbidden = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => requests.push({url:request.url(),method:request.method()}));
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== 'http://market.invalid' || req.method() !== 'GET') { forbidden.push(req.method() + ' ' + req.url()); return route.abort(); }
      if (req.method() === 'GET' && url.pathname === '/admin/observability') return route.fulfill({json: {}, status: 404});
      if (url.pathname.startsWith('/admin/')) {
        let value;
        if (url.pathname === '/admin/session') value = {username:'admin',csrf_token:'csrf'};
        else if (url.pathname === '/admin/health') value = {status:'ok',version:'0.5.1',active_requests:0};
        else if (url.pathname === '/admin/stats') value = {summary:{},providers:[],timeline:[],recent_requests:[],recent_errors:[]};
        else if (url.pathname === '/admin/config') value = {revision:1,settings:{},providers:[],models:[]};
        else if (url.pathname === '/admin/a6/pools') value = {pools:[],jobs:[],tokens:[{id:76809,name:'Production',status:1},{id:64469,name:'Test',status:1}],catalog:[{id:'model-a'}],capabilities:{}};
        else { forbidden.push(req.method() + ' ' + req.url()); return route.abort(); }
        return route.fulfill({json:value});
      }
      const asset = {'/':'web/index.html','/app.js':'web/app.js','/styles.css':'web/styles.css'}[url.pathname];
      if (!asset) { forbidden.push(req.url()); return route.abort(); }
      return route.fulfill({body:fs.readFileSync(path.join(root,asset)),contentType:url.pathname.endsWith('.js')?'application/javascript':url.pathname.endsWith('.css')?'text/css':'text/html'});
    });
    await page.goto('http://market.invalid/');
    await page.locator('#app').waitFor({state:'visible'});
    await page.locator('#tab-market').click();
    await page.locator('#market-pools-panel').waitFor({state:'visible'});
    await page.waitForFunction(() => document.querySelectorAll('#pool-token_id option').length >= 2);
    assert.equal(await page.locator('#pool-new').isVisible(), true, 'A6API opens managed pools directly');
    assert.equal(await page.locator('#pool-token_id option[value="76809"]').count(), 1, 'pool data supplies production tokens');
    assert.equal(await page.locator('#pool-test_token_id option[value="64469"]').count(), 1, 'pool data supplies test tokens');
    for (const selector of ['#market-sellers-tab','#market-sellers-panel','#market-pools-tab','#market-form','#market-token','#market-model','#market-search','#market-rows']) {
      assert.equal(await page.locator(selector).count(), 0, selector + ' must be physically removed, not merely hidden');
    }
    assert.ok(requests.some(request => new URL(request.url).pathname === '/admin/a6/pools'));
    for (const request of requests) {
      const pathname = new URL(request.url).pathname;
      assert.notEqual(pathname, '/admin/a6/sellers', 'entry must not search sellers');
      assert.notEqual(pathname, '/admin/a6/tokens', 'entry uses pool tokens, not the removed seller token loader');
      assert.equal(request.method, 'GET', 'entry must remain free of paid/API mutations');
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(forbidden, []);
    console.log('PASS A6API entry: managed pools direct, seller DOM physically absent, pool token data, no seller/token endpoint calls or mutations');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
