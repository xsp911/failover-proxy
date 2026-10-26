// Fully intercepted browser test. No running server, external network, provider calls or real secrets.
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
    const errors = [], writes = [], requests = [], forbidden = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.clock.install();
    const settings = {revision:7,enabled:false,interval_minutes:30,threshold_percent:95,targets:[]};
    const providers = [{id:'p1',name:'Provider <img src=x onerror=alert(1)>',enabled:true,models:['ref-a','upstream-route']},{id:'p2',name:'Disabled provider',enabled:false,models:[]}];
    const data = {available:true,warning:'',catalog:{models:[{id:'ref-a',display_name:'Reference A',family:'A'},{id:'ref-b',display_name:'Reference B',family:'B'}],source_commit:'abc123'},settings,providers,states:[{provider_id:'p1',provider_name:providers[0].name,model:'ref-a',reference_model:'ref-a',status:'verified',latest_status:'error',checked_at:'2026-01-01T00:00:00Z',conclusive_result:{status:'verified',target_probability:0.97,prediction:'ref-a',prediction_probability:0.97},latest_result:{error_kind:'upstream_timeout'}}],jobs:[],blocked_provider_ids:['p1'],next_run_at:null};
    const config = {revision:1,settings:{request_timeout_s:180,connect_timeout_s:10,read_timeout_s:60,cooldown_s:60,failure_threshold:1,max_request_bytes:33554432,max_response_bytes:16777216,max_buffer_events:32768,max_concurrent_requests:8,max_attempts:3,trust_private_upstreams:false},providers:[],models:[]};
    const secret = 'sk-test-private-probe-never-render';
    const challenges = Array.from({length:6},(_,index) => ({round:index < 3 ? 1 : 2,id:'query-0' + (index % 3 + 1),expected_count:218,parsed_numbers:index === 5 ? 17 : 218,minimum_numbers:175,accepted:index !== 5,finish_reason:index === 5 ? 'length' : 'stop',attempts:index === 5 ? 3 : 1,errors:index === 5 ? ['insufficient_numbers','upstream_timeout','invalid_response'] : [],raw_text:secret,prompt:secret}));
    const evidence = {status:'unknown',error_kind:'low_score_not_confirmed',target_probability:null,prediction:null,challenges,rounds:[{round:1,status:'suspect',target_probability:0.12,prediction:'ref-b'},{round:2,status:'unknown',target_probability:null,prediction:null,error_kind:'upstream_timeout'}],confirmation:{required:true,confirmed:false,status:'inconclusive',first_probability:0.12,second_probability:null,threshold_percent:90.5},raw_response:secret,api_key:secret};
    data.states[0].latest_status = 'unknown'; data.states[0].latest_result = evidence;
    data.states.push({provider_id:'p2',model:'old-unknown',reference_model:'ref-a',status:'unknown',latest_status:'unknown',latest_result:{status:'unknown'}});
    let conflict = false;
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url());
      if (url.origin !== 'http://detector.invalid') { forbidden.push(req.url()); return route.abort(); }
      if (req.method() === 'GET' && url.pathname === '/admin/observability') return route.fulfill({json: {}, status: 404});
      if (url.pathname.startsWith('/admin/')) {
        requests.push(url.pathname + url.search);
        let value, status = 200;
        if (req.method() !== 'GET') {
          assert.equal(req.headers()['x-csrf-token'], 'test-csrf');
          writes.push({path:url.pathname,body:req.postData() ? req.postDataJSON() : undefined});
        }
        if (url.pathname === '/admin/session') value = {username:'test',csrf_token:'test-csrf'};
        else if (url.pathname === '/admin/config') value = config;
        else if (url.pathname === '/admin/stats') value = {summary:{},providers:[],timeline:[],recent_requests:[],recent_errors:[]};
        else if (url.pathname === '/admin/health') value = {status:'ok'};
        else if (url.pathname === '/admin/detector') value = data;
        else if (url.pathname === '/admin/detector/settings') {
          if (conflict) { status = 409; value = {error:'revision_conflict'}; }
          else { Object.assign(settings,req.postDataJSON(),{revision:settings.revision+1}); value = settings; }
        } else if (url.pathname === '/admin/detector/check') {
          value = {id:'job/1',...req.postDataJSON(),status:'queued',progress:0}; data.jobs = [value]; status = 202;
        } else if (url.pathname === '/admin/detector/jobs/job%2F1/cancel') { data.jobs = []; value = {ok:true}; }
        else if (url.pathname === '/admin/detector/log') value = {changes:[{id:'c1',provider_id:'p1',provider_name:providers[0].name,model:'upstream-route',reference_model:'ref-a',from_status:'verified',to_status:'suspect',checked_at:'2026-01-01T00:00:00Z',target_probability:0.12,prediction:'ref-b',source:'manual'}]};
        else if (url.pathname === '/admin/logout') value = {ok:true};
        else throw Error('Unexpected API: ' + req.method() + ' ' + url.pathname);
        return route.fulfill({status,json:value});
      }
      const asset = {'/':'web/index.html','/app.js':'web/app.js','/styles.css':'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body:fs.readFileSync(path.join(root,asset)),contentType:url.pathname.endsWith('.js')?'application/javascript':url.pathname.endsWith('.css')?'text/css':'text/html'});
    });
    await page.goto('http://detector.invalid/');
    await page.locator('#app').waitFor({state:'visible'});
    assert.equal(requests.filter(item => item === '/admin/detector').length,0);
    await page.locator('#tab-detector').click();
    await page.waitForFunction(() => !document.getElementById('detector-check-fieldset').disabled);
    assert.equal(await page.locator('#detector-enabled').isChecked(),false);
    assert.equal(await page.locator('#detector-targets tr').count(),2);
    assert.match(await page.locator('#detector-targets').textContent(),/Отключён вручную/);
    assert.match(await page.locator('#detector-targets').textContent(),/В карантине/);
    assert.match(await page.locator('#detector-states').textContent(),/Совпадает/);
    assert.match(await page.locator('#detector-states').textContent(),/97%/);
    assert.match(await page.locator('#detector-states').textContent(),/upstream_timeout/);
    const persisted = page.locator('#detector-states tr').first();
    await persisted.locator('details.detector-latest-result > summary').click();
    assert.equal(await persisted.locator('.detector-confirmation').isVisible(),true);
    assert.match(await persisted.locator('.detector-confirmation').textContent(),/Не подтверждено.*Первый раунд: 12%.*Второй раунд: —.*Порог: 90,5%/);
    assert.equal(await persisted.locator('.detector-round').count(),2);
    await persisted.locator('details.detector-challenges > summary').click();
    assert.equal(await persisted.locator('.detector-challenge').count(),6);
    assert.match(await persisted.locator('.detector-challenge').last().textContent(),/Раунд 2.*query-03.*Чисел: 17 \/ ожидается 218.*Минимум: 175.*Не принято.*finish_reason: length.*Попыток: 3/);
    assert.match(await persisted.locator('.detector-challenges').textContent(),/История ошибок: insufficient_numbers, upstream_timeout, invalid_response/);
    const legacy = page.locator('#detector-states tr').nth(1);
    assert.equal(await legacy.locator('.detector-confirmation,.detector-round,.detector-challenges').count(),0);
    assert.doesNotMatch(await legacy.textContent(),/unknown_error|Ошибка:/);
    assert.equal(await page.locator('#detector-view img').count(),0);
    assert.ok(!(await page.locator('body').textContent()).includes(secret));
    assert.equal(await page.locator('#detector-warning').isVisible(),false,'generic detector disclaimer removed');
    assert.doesNotMatch(await page.locator('#detector-view').textContent(),/Не доказательство подлинности\./);
    assert.equal(await page.locator('#detector-provider option').count(),2);
    let idleCount = requests.filter(item => item === '/admin/detector').length;
    await page.clock.runFor(3100);
    assert.equal(requests.filter(item => item === '/admin/detector').length,idleCount,'idle checks do not poll every 3s');
    await page.clock.runFor(12000);
    await page.waitForFunction(() => !document.querySelector('#refresh-detector').disabled);
    assert.ok(requests.filter(item => item === '/admin/detector').length > idleCount,'idle checks poll every 15s');

    // Arbitrary model accepted; exact catalog match autofills, nonmatch requires choice.
    await page.locator('#detector-model').fill('ref-a');
    assert.equal(await page.locator('#detector-reference').inputValue(),'ref-a');
    await page.locator('#detector-model').fill('arbitrary-private-model');
    assert.equal(await page.locator('#detector-reference').inputValue(),'');
    await page.locator('#detector-reference').selectOption('ref-b');
    await page.locator('#start-detector-check').click();
    await page.locator('#confirm-dialog').waitFor({state:'visible'});
    assert.match(await page.locator('#confirm-description').textContent(),/3 платных.*максимум 18.*2 и 5 секунд.*двух полных/);
    await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelector('#detector-jobs').textContent.includes('0/3'));
    assert.deepEqual(writes[0],{path:'/admin/detector/check',body:{provider_id:'p1',model:'arbitrary-private-model',reference_model:'ref-b'}});
    assert.equal(settings.enabled,false);
    assert.equal(writes.filter(item => item.path.endsWith('/settings')).length,0);
    Object.assign(data.jobs[0],{status:'running',progress:1,round:2,max_rounds:2,phase:'retry_wait',retry_attempt:3,max_attempts:3,challenges:evidence.challenges,rounds:evidence.rounds,confirmation:{...evidence.confirmation,status:'pending'},raw_response:secret});
    let count = requests.filter(item => item === '/admin/detector').length;
    await page.clock.runFor(3100);
    await page.waitForFunction(() => !document.querySelector('#refresh-detector').disabled);
    assert.ok(requests.filter(item => item === '/admin/detector').length > count,'pending jobs poll in 3s');
    const live = page.locator('#detector-jobs');
    assert.match(await live.textContent(),/1\/3.*Раунд 2\/2.*Прогресс текущего раунда.*Ожидание повтора.*Текущая попытка: 3\/3/);
    assert.match(await live.locator('.detector-confirmation').textContent(),/Ожидается второй раунд.*12%.*90,5%/);
    assert.equal(await live.locator('.detector-round').count(),2);
    assert.equal(await live.locator('.detector-challenge').count(),6);
    assert.ok(!(await page.locator('body').textContent()).includes(secret));
    // Invalid diagnostics stay non-secret; free-form finish/error strings never rendered.
    data.jobs[0].challenges = [{...challenges[5],id:'<img src=x onerror=alert(1)>',finish_reason:secret,errors:[secret,'<img src=x onerror=alert(1)>']}];
    data.jobs[0].result = {...evidence,error_kind:'upstream_timeout'}; data.jobs[0].error_kind = 'upstream_timeout';
    await page.locator('#refresh-detector').click();
    await page.waitForFunction(() => !document.querySelector('#refresh-detector').disabled);
    assert.equal((await live.textContent()).match(/Ошибка: upstream_timeout/g).length,2,'job error not repeated beside result; round error remains');
    delete data.jobs[0].result; delete data.jobs[0].error_kind;
    await page.locator('#refresh-detector').click();
    await page.waitForFunction(() => !document.querySelector('#refresh-detector').disabled);
    assert.ok(!(await live.textContent()).includes(secret));
    assert.equal(await live.locator('img').count(),0);
    assert.match(await live.locator('.detector-challenges').textContent(),/finish_reason: —.*История ошибок: unknown_error, unknown_error/);
    await page.getByRole('button',{name:'Отменить',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#detector-jobs').textContent.includes('Заданий пока нет'));
    assert.ok(writes.some(item => item.path === '/admin/detector/jobs/job%2F1/cancel'));
    await page.reload(); await page.locator('#app').waitFor({state:'visible'});
    await page.locator('#tab-detector').click();
    await page.waitForFunction(() => !document.getElementById('detector-check-fieldset').disabled);
    assert.equal(await page.locator('#detector-jobs .detector-job').count(),0);
    await page.locator('#detector-states tr').first().locator('details.detector-latest-result > summary').click();
    assert.match(await page.locator('#detector-states tr').first().locator('.detector-confirmation').textContent(),/Не подтверждено.*90,5%/);
    assert.equal(await page.locator('#detector-states tr').first().locator('.detector-challenge').count(),6,'persisted evidence survives reload without jobs');
    assert.match(await page.locator('#detector-states tr').first().textContent(),/Совпадает.*97%/,'inconclusive latest attempt preserves conclusive state');
    for (const [status,label,second] of [['confirmed','Низкая оценка подтверждена',0.15],['recovered','Совпадение восстановлено',0.98],['not_required','Не требуется',null]]) {
      data.states[0].latest_result = {...evidence,confirmation:{...evidence.confirmation,status,confirmed:status === 'confirmed',required:status !== 'not_required',second_probability:second}};
      await page.locator('#refresh-detector').click();
      await page.waitForFunction(() => !document.querySelector('#refresh-detector').disabled);
      const confirmation = await page.locator('#detector-states tr').first().locator('.detector-confirmation').textContent();
      assert.ok(confirmation.includes(label));
      assert.ok(confirmation.includes('Второй раунд: ' + (second == null ? '—' : second === 0.15 ? '15%' : '98%')));
    }
    data.states[0].latest_result = evidence;

    // Multiple targets and separate revisioned settings. Confirmation required for enable.
    const first = page.locator('#detector-targets tr').first();
    await first.locator('input').fill('ref-a');
    assert.equal(await first.locator('select').inputValue(),'ref-a');
    await first.getByRole('button',{name:'+ Цель',exact:true}).click();
    const second = page.locator('#detector-targets tr').nth(1);
    await second.locator('input').fill('custom-route'); await second.locator('select').selectOption('ref-b');
    await page.locator('#detector-enabled').check();
    await page.locator('#detector-interval').fill('15');
    await page.locator('#detector-threshold').fill('90.5');
    await page.locator('#save-detector-settings').click();
    await page.locator('#confirm-dialog').waitFor({state:'visible'});
    assert.match(await page.locator('#confirm-description').textContent(),/3 платных.*2 целей.*максимум 18.*2 и 5 секунд/);
    await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelector('#save-detector-settings').disabled && !document.querySelector('#detector-settings-fieldset').disabled);
    const saved = writes.find(item => item.path === '/admin/detector/settings').body;
    assert.deepEqual(saved,{revision:7,enabled:true,interval_minutes:15,threshold_percent:90.5,targets:[{provider_id:'p1',model:'ref-a',reference_model:'ref-a'},{provider_id:'p1',model:'custom-route',reference_model:'ref-b'}]});
    conflict = true;
    await page.locator('#detector-interval').fill('16'); await page.locator('#save-detector-settings').click(); await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelector('#detector-settings-error').textContent.includes('другой сессии'));
    assert.equal(await page.locator('#detector-interval').inputValue(),'16');
    assert.equal(settings.interval_minutes,15);
    const currentTargets = await page.locator('#detector-targets tr').count();
    await page.locator('#refresh-detector').click();
    await page.waitForFunction(() => !document.querySelector('#refresh-detector').disabled);
    assert.equal(await page.locator('#detector-targets tr').count(),currentTargets);
    assert.equal(await page.locator('#detector-interval').inputValue(),'16');

    // Tab/visibility polling pause; draft survives refresh and unrelated tabs.
    count = requests.filter(item => item === '/admin/detector').length;
    await page.evaluate(() => { Object.defineProperty(document,'hidden',{configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange')); });
    await page.clock.runFor(16000);
    assert.equal(requests.filter(item => item === '/admin/detector').length,count);
    await page.evaluate(() => { Object.defineProperty(document,'hidden',{configurable:true,value:false}); document.dispatchEvent(new Event('visibilitychange')); });
    await page.waitForFunction(() => !document.querySelector('#refresh-detector').disabled);
    assert.equal(await page.locator('#detector-interval').inputValue(),'16');
    await page.locator('#tab-detector-log').click();
    await page.waitForFunction(() => document.querySelector('#detector-log-rows').textContent.includes('Ниже порога'));
    assert.ok(requests.includes('/admin/detector/log?limit=100'));
    assert.match(await page.locator('#detector-log-rows').textContent(),/12%/);
    assert.match(await page.locator('#detector-log-view').textContent(),/Повторные проверки/);
    count = requests.filter(item => item === '/admin/detector').length;
    await page.clock.runFor(16000); assert.equal(requests.filter(item => item === '/admin/detector').length,count);
    await page.locator('#logout').click(); await page.locator('#confirm-action').click();
    await page.locator('#login-view').waitFor({state:'visible'});
    const total = requests.length; await page.clock.runFor(16000); assert.equal(requests.length,total);
    assert.deepEqual(errors,[]); assert.deepEqual(forbidden,[]);
    console.log('PASS detector: live round/retry evidence, six challenge counts/finish/error history, confirmation states/probabilities, persisted reload evidence, non-secret rendering, stable conclusive state, paid 3/18 warnings, tabs, CSRF, cancel, settings/conflicts, journal, polling/logout');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
