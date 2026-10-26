// Offline browser test for A6API settings tab. No server, provider or real PAT.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); }
catch { ({chromium} = require('/tmp/failover-browser-check/node_modules/playwright')); }
const root = path.resolve(__dirname, '..');
const PAT = 'SYNTHETIC_TEST_PAT_0000000000000';

(async () => {
  const browser = await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const page = await browser.newPage();
    const errors = [], writes = [], forbidden = [];
    page.on('pageerror', error => errors.push(error.message));
    let state = {configured:false,has_pat:false,pat_masked:'',user_id:null};
    let verifyCalls = 0;
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== 'http://a6.invalid') { forbidden.push(req.url()); return route.abort(); }
      if (req.method() === 'GET' && url.pathname === '/admin/observability') return route.fulfill({json: {}, status: 404});
      if (url.pathname.startsWith('/admin/')) {
        let value, status = 200;
        if (req.method() !== 'GET') {
          assert.equal(req.headers()['x-csrf-token'], 'csrf');
          writes.push({path:url.pathname,body:req.postData() ? req.postDataJSON() : undefined});
        }
        if (url.pathname === '/admin/session') value = {username:'admin',csrf_token:'csrf'};
        else if (url.pathname === '/admin/health') value = {status:'ok',version:'0.5.0',active_requests:0};
        else if (url.pathname === '/admin/stats') value = {summary:{},providers:[],timeline:[],recent_requests:[],recent_errors:[]};
        else if (url.pathname === '/admin/config') value = {revision:1,settings:{},providers:[],models:[]};
        else if (url.pathname === '/admin/a6' && req.method() === 'GET') value = state;
        else if (url.pathname === '/admin/a6' && req.method() === 'PUT') {
          const body = req.postDataJSON();
          if (body.pat && body.pat.length < 16) { status = 400; value = {error:{message:'invalid_a6_pat',code:'invalid_a6_pat'}}; }
          else { state = {configured:true,has_pat:true,pat_masked:'SYNT********00',user_id:body.user_id}; value = state; }
        } else if (url.pathname === '/admin/a6' && req.method() === 'DELETE') {
          state = {configured:false,has_pat:false,pat_masked:'',user_id:null}; value = state;
        } else if (url.pathname === '/admin/a6/verify') {
          verifyCalls += 1;
          const body = req.postDataJSON();
          value = body.user_id === 23984
            ? {ok:true,status:200,user_id:23984,display_name:'xspider',tokens_total:20}
            : {ok:false,status:200,error_kind:'a6_user_id_mismatch'};
        } else if (url.pathname === '/admin/logout') value = {ok:true};
        else throw Error('Unexpected API ' + req.method() + ' ' + url.pathname);
        return route.fulfill({status,json:value});
      }
      const asset = {'/':'web/index.html','/app.js':'web/app.js','/styles.css':'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body:fs.readFileSync(path.join(root,asset)),contentType:url.pathname.endsWith('.js')?'application/javascript':url.pathname.endsWith('.css')?'text/css':'text/html'});
    });
    await page.goto('http://a6.invalid/');
    await page.locator('#app').waitFor({state:'visible'});
    assert.equal(await page.locator('#tab-a6').count(), 0, 'A6 must not be a top-level tab');
    await page.locator('#tab-settings').click();
    await page.locator('[data-settings="a6"]').click();
    await page.waitForFunction(() => document.getElementById('a6-badge').textContent === 'Не настроено');
    assert.equal(await page.locator('#a6-pat').inputValue(), '');
    assert.equal(await page.locator('#a6-clear').isDisabled(), true);
    assert.match(await page.locator('#a6-pat-state').textContent(), /не сохранён/);

    // Short PAT is rejected before any request.
    await page.locator('#a6-pat').fill('short');
    await page.locator('#a6-user-id').fill('23984');
    await page.locator('#a6-save').click();
    assert.match(await page.locator('#a6-error').textContent(), /короткий/);
    assert.equal(writes.length, 0);

    await page.locator('#a6-pat').fill(PAT);
    await page.locator('#a6-save').click();
    await page.waitForFunction(() => document.getElementById('a6-badge').textContent === 'Настроено');
    assert.deepEqual(writes[0], {path:'/admin/a6',body:{pat:PAT,user_id:23984}});
    assert.equal(await page.locator('#a6-pat').inputValue(), '', 'PAT field must be cleared after save');
    assert.match(await page.locator('#a6-pat-state').textContent(), /SYNT\*+00/);
    assert.equal(await page.locator('#a6-user-id').inputValue(), '23984');
    assert.equal(await page.locator('#a6-clear').isDisabled(), false);

    // Verify uses stored PAT when field is blank.
    await page.locator('#a6-verify').click();
    await page.waitForFunction(() => document.getElementById('a6-verify-result').textContent.includes('Доступ подтверждён'));
    const verified = await page.locator('#a6-verify-result').textContent();
    assert.match(verified, /xspider/); assert.match(verified, /токенов: 20/);
    assert.match(verified.replace(/\D/g, ''), /23984/);
    assert.deepEqual(writes[1], {path:'/admin/a6/verify',body:{pat:'',user_id:23984}});

    await page.locator('#a6-user-id').fill('999');
    await page.locator('#a6-verify').click();
    await page.waitForFunction(() => document.getElementById('a6-verify-result').textContent.includes('не пройдена'));
    assert.match(await page.locator('#a6-verify-result').textContent(), /ID не совпадает/);
    assert.equal(verifyCalls, 2);

    // Reload keeps mask and user id; PAT never rendered.
    await page.locator('#a6-reload').click();
    await page.waitForFunction(() => document.getElementById('a6-pat-state').textContent.includes('SYNT') && document.getElementById('a6-user-id').value === '23984');
    assert.equal(await page.locator('#a6-pat').inputValue(), '');
    assert.equal(await page.locator('#a6-user-id').inputValue(), '23984', 'server value wins after reload; failed verification never saves the edited user id');

    await page.locator('#a6-clear').click();
    await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.getElementById('a6-badge').textContent === 'Не настроено');
    assert.deepEqual(writes.at(-1), {path:'/admin/a6',body:undefined});
    assert.equal(await page.locator('#a6-user-id').inputValue(), '');

    assert.equal((await page.content()).includes(PAT), false, 'PAT must never appear in DOM');
    assert.deepEqual(errors, []);
    assert.deepEqual(forbidden, []);
    console.log('PASS a6 tab: masked storage, validation, save/verify/mismatch/reload/clear, PAT never rendered');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
