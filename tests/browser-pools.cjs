// Offline browser contract test. All assets/API mocked; no provider or real network.
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
    const page = await browser.newPage({viewport: {width: 1440, height: 1100}});
    const errors = [], external = [], writes = [], lists = [];
    page.on('pageerror', error => errors.push(error.message));
    let policies = [], jobs = [], serial = 0, allowPublish = false;
    let conflict = false, saveError = null, previewReason = 'controlled_price_caps_pilot_required';
    const candidate = {channel_id: 55, listing_id: 7, supplier: 'Seller<img src=x onerror=alert(1)>', input_micros: 1000000, output_micros: 2000000, cache_read_micros: 19, cache_hit_percent: 61, success_percent: 98, eligible: true, reason: 'attribution unavailable', verification_status: 'unverified', probability: null, actual_channel_id: null};
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url()), method = req.method();
      if (url.origin !== 'http://pools.invalid') { external.push(req.url()); return route.abort(); }
      const reply = (json, status = 200) => route.fulfill({json, status});
      if (method === 'GET' && url.pathname === '/admin/observability') return reply({}, 404);
      if (method === 'GET' && /^\/admin\/a6\/pools\/[^/]+\/events$/.test(url.pathname)) return reply({events: []});
      if (url.pathname.startsWith('/admin/')) {
        if (method !== 'GET') { assert.equal(req.headers()['x-csrf-token'], 'csrf'); writes.push({path: url.pathname, method, body: req.postDataJSON()}); }
        if (url.pathname === '/admin/session') return reply({username: 'admin', csrf_token: 'csrf'});
        if (url.pathname === '/admin/stats') return reply({summary: {}, providers: [], timeline: [], recent_requests: [], recent_errors: []});
        if (url.pathname === '/admin/health') return reply({status: 'ok'});
        if (url.pathname === '/admin/config') return reply({revision: 1, settings: {}, providers: [], models: []});
        if (url.pathname === '/admin/a6/pools') {
          if (method === 'GET') { lists.push(Date.now()); return reply({pools: policies, catalog: [{id: 'reference-a', display_name: 'Exact reference A'}, {id: 'reference-b', display_name: 'Exact reference B'}], tokens: [{id: 11, name: 'Production', status: 1, key: 'SECRET_TOKEN_NEVER_RENDER'}, ...[22, 33, 44, 55].map(id => ({id, name: 'Tests ' + id, status: 1}))], jobs, capabilities: {targeted_probe_verified: false, route_caps_verified: false, warning: 'Capabilities unverified'}}); }
          const policy = {...req.postDataJSON(), id: 'pool-' + (++serial), revision: 1, status: 'draft', members: [], candidates: [], last_error: null};
          assert.equal(policy.auto_publish, false); assert.equal('candidates' in req.postDataJSON(), false);
          policies.push(policy); return reply(policy, 201);
        }
        const cancel = url.pathname.match(/^\/admin\/a6\/pools\/jobs\/([^/]+)\/cancel$/);
        if (cancel) { jobs = jobs.map(job => job.id === cancel[1] ? {...job, status: 'done'} : job); return reply({ok: true}); }
        const match = url.pathname.match(/^\/admin\/a6\/pools\/([^/]+)(?:\/(discover|verify|preview|publish))?$/);
        if (match) {
          const index = policies.findIndex(policy => policy.id === match[1]), policy = policies[index], action = match[2];
          if (!action && method === 'PUT') {
            if (saveError) { const code = saveError; saveError = null; return reply({error: {code}}, 409); }
            if (conflict) { conflict = false; policies[index] = {...policy, revision: policy.revision + 1, name: 'Remote change'}; return reply({error: 'revision conflict'}, 409); }
            assert.equal(req.postDataJSON().revision, policy.revision);
            policies[index] = {...policy, ...req.postDataJSON(), revision: policy.revision + 1}; return reply(policies[index]);
          }
          if (!action && method === 'DELETE') { assert.equal(req.postDataJSON().revision, policy.revision); policies.splice(index, 1); return reply({ok: true}); }
          if (action === 'discover' || action === 'verify') {
            assert.deepEqual(req.postDataJSON(), action === 'verify' ? {confirm_cost: true} : {});
            policies[index] = {...policy, candidates: [candidate]};
            const job = {id: 'job-' + (jobs.length + 1), pool_id: policy.id, kind: action, status: 'running', progress: action === 'verify' ? 0 : 1, total: 3, phase: action === 'verify' ? 'preflight' : 'discovery'}; jobs.push(job); return reply(job, 202);
          }
          if (action === 'preview') return reply({preview_id: 'preview-' + policy.id, expires_at: new Date(Date.now() + 60000).toISOString(), channels: [candidate], remote_before: {token_id: 11, channels: [], token: 'SECRET_REMOTE_NEVER_RENDER'}, draft: {channels: [candidate], caps: {input_price_micros: 2500000, output_price_micros: 3000000, cache_read_price_micros: 200000, cache_write_price_micros: 300000, per_request_price_micros: 50000, secret: 'SECRET_CAPS_NEVER_RENDER'}, fallback_enabled: false, token: 'SECRET_DRAFT_NEVER_RENDER'}, warnings: ['Explicit confirmation required'], can_publish: allowPublish, reason: previewReason});
          if (action === 'publish') { assert.deepEqual(req.postDataJSON(), {preview_id: 'preview-' + policy.id, confirm: true}); policies[index] = {...policy, status: 'published', revision: policy.revision + 1}; return reply({ok: true, status: 'published', pool: policies[index]}); }
        }
        if (url.pathname === '/admin/logout') return reply({ok: true});
        throw Error('Unexpected mocked API: ' + method + ' ' + url.pathname);
      }
      const asset = {'/': 'web/index.html', '/app.js': 'web/app.js', '/styles.css': 'web/styles.css'}[url.pathname];
      if (!asset) return route.abort();
      return route.fulfill({body: fs.readFileSync(path.join(root, asset)), contentType: url.pathname.endsWith('.js') ? 'application/javascript' : url.pathname.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    const openSection = section => page.locator('#pool-section-tab-' + section).click();
    const openSettings = async () => {
      await openSection('settings');
      if (!await page.locator('#pool-settings').evaluate(node => node.open)) await page.locator('#pool-settings > summary').click();
    };
    await page.goto('http://pools.invalid/');
    await page.locator('#app').waitFor({state: 'visible'});
    await page.locator('#tab-market').click();
    assert.equal(await page.locator('#pool-sections').isVisible(), false);
    assert.equal(await page.locator('#pool-form').isVisible(), false);
    await page.locator('#pool-new').click();
    await openSettings();
    await page.locator('#pool-name').waitFor();
    assert.equal(await page.locator('#pool-auto-publish').isDisabled(), false);
    assert.equal(await page.locator('#pool-enabled').isChecked(), false);
    for (let lane = 1; lane <= 4; lane++) {
      const selector = '#pool-test_token_id' + (lane === 1 ? '' : '_' + lane);
      assert.equal(await page.locator(selector).getAttribute('required') !== null, lane === 1);
      assert.match(await page.locator(selector).locator('..').textContent(), new RegExp('Тестовый токен' + lane));
    }
    assert.match(await page.locator('#pool-policy-fields').textContent(), /Бюджет запросов и USD общий/);
    assert.match(await page.locator('#pool-policy-fields').textContent(), /очистка тестовых маршрутов обязательна/);
    assert.match(await page.locator('#pools-capabilities').textContent(), /НЕ подтверждена/);
    const fillPolicy = async (name, model, reference) => {
      await openSettings();
      await page.locator('#pool-name').fill(name); await page.locator('#pool-model').fill(model);
      await page.locator('#pool-token_id').selectOption('11'); await page.locator('#pool-test_token_id').selectOption('22');
      await page.locator('#pool-reference_model').selectOption(reference);
      for (const cap of ['max_input_usd', 'max_output_usd', 'max_cache_read_usd', 'max_cache_write_usd']) await page.locator('#pool-' + cap).fill('2.5');
      await page.locator('#pool-save').click();
    };
    await fillPolicy('Pool A', 'model-a', 'reference-a');
    await page.waitForFunction(() => document.querySelectorAll('.pool-card').length === 1);
    await page.locator('#pool-new').click(); await fillPolicy('Pool B', 'model-b', 'reference-b');
    await page.waitForFunction(() => document.querySelectorAll('.pool-card').length === 2);
    assert.equal(policies.length, 2); assert.equal(policies[0].model, 'model-a'); assert.equal(policies[1].model, 'model-b');
    for (const write of writes.filter(write => write.path === '/admin/a6/pools' && write.method === 'POST')) {
      assert.deepEqual(write.body.test_token_ids, [22]); assert.equal(write.body.test_token_id, 22);
    }
    // Existing policies without a canonical list hydrate the first lane only.
    delete policies[0].test_token_ids;
    await page.locator('.pool-card').first().click();
    await openSettings();
    assert.equal(await page.locator('#pool-model').inputValue(), 'model-a');
    assert.equal(await page.locator('#pool-test_token_id').inputValue(), '22');
    for (const lane of [2, 3, 4]) assert.equal(await page.locator('#pool-test_token_id_' + lane).inputValue(), '');
    const beforeInvalid = writes.length;
    await page.locator('#pool-test_token_id_2').selectOption('22'); await page.locator('#pool-save').click();
    await page.waitForFunction(() => document.querySelector('#pools-error').textContent.includes('не должны повторяться'));
    assert.equal(writes.length, beforeInvalid, 'duplicate test tokens must not be submitted');
    await page.locator('#pool-test_token_id_2').selectOption('11'); await page.locator('#pool-save').click();
    await page.waitForFunction(() => document.querySelector('#pools-error').textContent.includes('отличные от рабочего'));
    assert.equal(writes.length, beforeInvalid, 'working token must not be used in any lane');
    for (const [lane, token] of [[2, '33'], [3, '44'], [4, '55']]) await page.locator('#pool-test_token_id_' + lane).selectOption(token);
    const original = {...policies[0]};
    await page.locator('#pool-name').fill('Pool A edited'); await page.locator('#pool-save').click();
    await page.waitForFunction(() => document.querySelector('#pool-form-state').textContent.includes('Ревизия 2'));
    assert.deepEqual(policies[0].test_token_ids, [22, 33, 44, 55]); assert.equal(policies[0].test_token_id, 22);
    assert.equal('test_token_id_2' in policies[0], false);
    for (const key of ['model', 'reference_model', 'protocol', 'max_input_usd', 'max_output_usd', 'max_cache_read_usd', 'max_cache_write_usd', 'min_cache_hit_percent', 'min_success_percent', 'threshold_percent', 'verification_ttl_hours', 'refresh_minutes', 'max_candidates', 'max_members', 'max_probe_requests', 'max_probe_usd', 'enabled', 'auto_publish']) assert.deepEqual(policies[0][key], original[key], key + ' must be preserved');
    await page.locator('.pool-card').nth(1).click(); await page.locator('.pool-card').first().click();
    for (const [lane, token] of [[1, '22'], [2, '33'], [3, '44'], [4, '55']]) assert.equal(await page.locator('#pool-test_token_id' + (lane === 1 ? '' : '_' + lane)).inputValue(), token);
    assert.equal(policies[1].name, 'Pool B');
    assert.equal(await page.locator('#pool-summary .pool-summary-card').count(), 5);
    assert.match(await page.locator('#pool-summary').textContent(), /Опубликовано участников.*Проверены хорошие.*Проверено кандидатов.*Кандидаты.*Следующий отбор/);
    await openSection('overview');
    await page.locator('#pool-discover').click();
    await openSection('checks');
    await page.locator('#pool-jobs button').waitFor();
    await page.waitForFunction(() => document.querySelector('#pools-status').textContent.includes('5 секунд'));
    assert.equal(await page.locator('#pool-candidates img').count(), 0); assert.match(await page.locator('#pool-candidates').textContent(), /Не проверен/);
    assert.match(await page.locator('#pool-jobs').textContent(), /Поиск кандидатов/);
    assert.match(await page.locator('#pool-candidates').textContent(), /\$0\.000019/);
    await page.locator('#pool-verify').click(); await page.locator('#confirm-dialog').waitFor({state: 'visible'});
    assert.equal(writes.filter(r => r.path.endsWith('/verify')).length, 0, 'verification must await paid confirmation');
    await page.locator('#confirm-dialog button[value=cancel]').click();
    await page.locator('#pool-verify').click(); await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelectorAll('#pool-jobs button').length === 2);
    assert.match(await page.locator('#pool-jobs').textContent(), /Предварительная проверка/);
    assert.match(await page.locator('#pool-jobs').textContent(), /Фактические списания пока не загружены/);
    assert.doesNotMatch(await page.locator('#pool-jobs').textContent(), /Фактически списано \(журнал A6API\): \$0\.000000/);
    assert.match(await page.locator('#pool-candidates').textContent(), /Не проверен/);
    const verifyJob = jobs.find(job => job.kind === 'verify');
    Object.assign(verifyJob, {phase: 'testing', current_channel_id: 55, current_supplier: candidate.supplier, search: {eligible_total: 30, attempted: 4, remaining: 26, target_members: 10, verified: 2, exhausted: false, cooldown_count: 3, cached_verified: 1}});
    // Register the response wait before clicking: every refresh is an offline mock.
    const refresh = async () => {
      const response = page.waitForResponse(r => new URL(r.url()).pathname === '/admin/a6/pools' && r.request().method() === 'GET');
      await page.locator('#pools-refresh').click(); await response;
    };
    policies[0].bans = Array.from({length: 10}, (_, i) => ({channel_id: 900 + i, failures: i === 0 ? 4 : 1, banned: i === 0, last_reason: 'Safe ban reason <img src=x>'}));
    await refresh();
    await openSection('automation');
    assert.equal(await page.locator('#pool-bans details').evaluate(node => node.open), false);
    assert.match(await page.locator('#pool-bans summary').textContent(), /Заблокировано каналов: 1 · каналов с ошибками: 10 · записей счётчиков: 10/);
    await page.locator('#pool-bans summary').click();
    assert.equal(await page.locator('.pool-ban-history-rows').evaluate(node => getComputedStyle(node).maxHeight), '240px');
    assert.equal(await page.locator('#pool-bans .pool-ban').count(), 10);
    assert.equal(await page.locator('#pool-bans img').count(), 0);
    await refresh();
    assert.equal(await page.locator('#pool-bans details').evaluate(node => node.open), true);
    await page.locator('#pool-bans summary').click();
    await page.waitForFunction(() => document.querySelector('#pool-candidates').textContent.includes('Проверяется'));
    assert.match(await page.locator('#pool-jobs').textContent(), /Сейчас: канал 55/);
    assert.match(await page.locator('.pool-search').textContent(), /Подходят: 30.*Проверено: 4.*Осталось: 26.*Цель участников: 10.*Проверены хорошие: 2.*Пауза 24 ч: 3.*Действующие проверки: 1/);
    assert.match(await page.locator('.pool-search').textContent(), /Поиск продолжается/);
    await openSection('composition');
    assert.equal(await page.locator('#pool-candidates th').first().evaluate(node => getComputedStyle(node).position), 'sticky');
    assert.equal(await page.locator('#pool-jobs img').count(), 0);
    assert.match(await page.locator('#pool-jobs').textContent(), /0 \/ 3/);
    verifyJob.current_channel_id = 999; await refresh();
    await page.waitForFunction(() => !document.querySelector('#pool-candidates').textContent.includes('Проверяется'));
    verifyJob.current_channel_id = 55;
    verifyJob.pool_id = policies[1].id; await refresh();
    assert.doesNotMatch(await page.locator('#pool-candidates').textContent(), /Проверяется/);
    verifyJob.pool_id = policies[0].id;
    // Canonical workers override stale legacy fields. Concurrent channels stay pool-local.
    policies[0].candidates = [candidate, {...candidate, channel_id: 56, supplier: 'Second seller'}];
    policies[1].candidates = [candidate, {...candidate, channel_id: 56}];
    verifyJob.workers = [
      {token_id: 22, phase: 'testing', current_channel_id: 55, current_supplier: candidate.supplier, status: 'running', error_kind: null, key: 'SECRET_WORKER_NEVER_RENDER'},
      {token_id: 33, phase: 'testing', current_channel_id: 56, current_supplier: 'Second seller', status: 'running', error_kind: null},
      {token_id: 44, phase: 'idle', current_channel_id: null, current_supplier: null, status: 'done', error_kind: null},
      {token_id: 55, phase: 'cleanup', current_channel_id: null, current_supplier: null, status: 'error', error_kind: 'Safe worker error <img src=x onerror=alert(1)>'}
    ];
    Object.assign(verifyJob, {phase: 'preflight', current_channel_id: 999});
    await refresh();
    await page.waitForFunction(() => [...document.querySelectorAll('#pool-candidates tbody tr')].filter(row => row.textContent.includes('Проверяется')).length === 2);
    assert.match(await page.locator('#pool-jobs').textContent(), /Параллельность: 2 \/ 4/);
    for (const token of [22, 33, 44, 55]) assert.match(await page.locator('#pool-jobs').textContent(), new RegExp('ID токена: ' + token));
    assert.match(await page.locator('#pool-jobs').textContent(), /Часть проверки завершилась с предупреждением/);
    assert.doesNotMatch(await page.locator('#pool-jobs').textContent(), /Safe worker error/);
    assert.equal(await page.locator('#pool-jobs img').count(), 0);
    await page.locator('.pool-card').nth(1).click();
    assert.doesNotMatch(await page.locator('#pool-candidates').textContent(), /Проверяется/, 'another pool must not inherit matching channel activity');
    await page.locator('.pool-card').first().click();
    verifyJob.workers[1].phase = 'saving'; await refresh();
    await page.waitForFunction(() => [...document.querySelectorAll('#pool-candidates tbody tr')].filter(row => row.textContent.includes('Проверяется')).length === 1);
    verifyJob.workers[0].status = 'done'; await refresh();
    await page.waitForFunction(() => !document.querySelector('#pool-candidates').textContent.includes('Проверяется'));
    delete verifyJob.workers;
    const statusCases = [['unverified', 'Не проверен'], ['verified', 'Проверен'], ['suspect', 'Ниже порога'], ['unknown', 'Не определено'], ['expired', 'Срок истёк'], ['skipped', 'Пропущен'], ['error', 'Ошибка']];
    policies[0].candidates = statusCases.map(([verification_status], i) => ({...candidate, channel_id: 70 + i, verification_status, reason: 'Safe reason <img src=x onerror=alert(1)>'}));
    Object.assign(verifyJob, {phase: 'saving', progress: 3}); await refresh();
    await page.waitForFunction(() => document.querySelectorAll('#pool-candidates tbody tr').length === 7);
    for (let i = 0; i < statusCases.length; i++) {
      const cell = page.locator('#pool-candidates tbody tr').nth(i).locator('td').nth(6);
      assert.match(await cell.textContent(), new RegExp(statusCases[i][1]));
      const tone = statusCases[i][0] === 'error' ? 'danger' : statusCases[i][0] === 'suspect' ? 'warning' : statusCases[i][0] === 'verified' ? 'success' : statusCases[i][0] === 'expired' ? 'warning' : 'neutral';
      assert.equal(await cell.locator('.pool-chip.' + tone).count(), 1);
      assert.equal(await cell.locator('p').count(), ['unknown', 'skipped', 'error'].includes(statusCases[i][0]) ? 1 : 0);
    }
    assert.equal(await page.locator('#pool-candidates img').count(), 0);
    assert.match(await page.locator('#pool-jobs').textContent(), /Сохранение результатов/);
    Object.assign(verifyJob, {phase: 'cleanup'}); await refresh();
    await page.waitForFunction(() => document.querySelector('#pool-jobs').textContent.includes('Завершение и очистка'));
    policies[0].candidates = [{...candidate, verification_status: 'checking'}];
    policies[0].last_error = 'a6_search_snapshot_changed';
    Object.assign(verifyJob, {phase: 'preflight', status: 'failed', progress: 0, error_kind: 'a6_search_snapshot_changed'}); await refresh();
    await page.waitForFunction(() => document.querySelector('#pool-last-error').textContent.includes('безопасно повторите запуск'));
    assert.match(await page.locator('#pool-last-error').textContent(), /Проверки кандидатов не выполнялись/);
    assert.doesNotMatch(await page.locator('#pool-candidates').textContent(), /Проверяется/);
    assert.doesNotMatch(await page.locator('#pool-jobs').textContent(), /Сейчас: канал/);
    assert.match(await page.locator('#pool-jobs').textContent(), /безопасно повторите запуск/);
    policies[0].last_error = null; policies[0].candidates = [candidate];
    Object.assign(verifyJob, {status: 'running', phase: 'testing', progress: 1, error_kind: null}); await refresh();
    await page.waitForFunction(() => document.querySelectorAll('#pool-jobs button').length === 2);
    await openSection('checks');
    await page.locator('#pool-jobs button').last().click(); // Newest-first list: cancel the older discovery job.
    await page.waitForFunction(() => document.querySelectorAll('#pool-jobs button').length === 1);
    // Partial completion preserves good results; warnings never become a generic job failure.
    policies[0].candidates = Array.from({length: 5}, (_, i) => ({...candidate, channel_id: 80 + i, verification_status: i < 2 ? 'verified' : i === 2 ? 'unknown' : i === 3 ? 'skipped' : 'unverified'}));
    Object.assign(verifyJob, {status: 'done', phase: 'idle', progress: 4, total: 5, error_kind: null, result: {candidates: 5, verified: 2, suspect: 0, unknown: 1, skipped: 1, unprocessed: 1, partial: true, requires_publication: true}, warnings: ['a6_http_500', {safe_kind: 'test_route_cleanup_uncertain', message: 'SECRET_WARNING_NEVER_RENDER'}, {error_kind: 'test_route_requires_reconciliation'}, {safe_kind: 'SECRET_UNKNOWN_WARNING_NEVER_RENDER'}], workers: [{token_id: 22, status: 'error', phase: 'cleanup', error_kind: 'test_route_cleanup_uncertain'}]});
    await refresh();
    await page.waitForFunction(() => document.querySelector('#pool-jobs').textContent.includes('Завершено с предупреждениями'));
    const partialText = await page.locator('#pool-jobs').textContent();
    assert.match(partialText, /Кандидатов: 5 · Проверено хороших \(включая прежние результаты\): 2 · Ниже порога: 0 · Не определено: 1 · Пропущено: 1 · Не обработано: 1/);
    assert.match(partialText, /Раздельные счётчики этого запуска недоступны; итог не равен числу новых проверок\./);
    assert.match(partialText, /Для создания рабочего маршрута нажмите Предпросмотр, затем Опубликовать; проверка сама не публикует\./);
    assert.doesNotMatch(await page.locator('#pool-jobs > .detector-job').last().locator('strong').first().textContent(), /Ошибка/);
    const warningText = await page.locator('.pool-job-warnings').textContent();
    assert.match(warningText, /Предупреждения/); assert.match(warningText, /A6API вернул HTTP 500/);
    assert.match(warningText, /Очистка тестового маршрута не подтверждена/); assert.match(warningText, /Тестовый маршрут требует сверки/);
    assert.doesNotMatch(partialText, /a6_http_500|test_route_cleanup_uncertain|test_route_requires_reconciliation|SECRET_/);
    assert.match(await page.locator('#pool-members').textContent(), /Кандидаты проверены, рабочий маршрут ещё не опубликован/);
    for (const [reason, expected] of [['target_reached', 'Цель достигнута'], ['budget_exhausted', 'Бюджет исчерпан'], ['catalogue_exhausted', 'Каталог исчерпан'], ['cooldown_wait', 'Повторная проверка после паузы 24 ч']]) {
      verifyJob.result.stop_reason = reason; await refresh();
      await page.waitForFunction(text => document.querySelector('.pool-search').textContent.includes(text), expected);
    }
    delete verifyJob.result.stop_reason; verifyJob.result.search_exhausted = true; await refresh();
    await page.waitForFunction(() => document.querySelector('.pool-search').textContent.includes('Каталог исчерпан'));
    assert.equal(await page.locator('#pool-publish').isDisabled(), true);
    assert.equal(writes.filter(r => r.path.endsWith('/publish')).length, 0);
    await openSection('overview');
    await page.locator('#pool-preview').click(); await page.locator('#pool-preview-panel').waitFor({state: 'visible'});
    assert.equal(await page.locator('#pool-publish').isDisabled(), true);
    assert.match(await page.locator('#pool-publish-reason').textContent(), /контролируемый пилот/);
    assert.match(await page.locator('#pool-preview-diff').textContent(), /Макс. вход: 2\.500000 USD \/ 1M/);
    assert.match(await page.locator('#pool-preview-diff').textContent(), /Макс. за запрос: 0\.050000 USD \/ запрос/);
    previewReason = 'pool_disabled'; await page.locator('#pool-preview').click();
    await page.waitForFunction(() => document.querySelector('#pool-publish-reason').textContent.includes('Пул выключен'));
    assert.match(await page.locator('#pool-publish-reason').textContent(), /включите обновление политики\/пул в форме и сохраните/);
    assert.equal(await page.locator('#pool-enabled').isChecked(), false, 'preview must not automatically enable the pool');
    assert.equal(writes.filter(r => r.path.endsWith('/publish')).length, 0);
    allowPublish = true; await page.locator('#pool-preview').click(); await page.locator('#pool-publish').waitFor({state: 'visible'});
    await page.waitForFunction(() => !document.querySelector('#pool-publish').disabled);
    assert.equal(writes.filter(r => r.path.endsWith('/publish')).length, 0);
    await page.locator('#pool-publish').click(); await page.locator('#confirm-dialog').waitFor({state: 'visible'});
    assert.equal(writes.filter(r => r.path.endsWith('/publish')).length, 0);
    await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelector('#pool-detail-status').textContent === 'Опубликован');
    assert.equal(writes.filter(r => r.path.endsWith('/publish')).length, 1);
    await openSettings();
    saveError = 'test_token_has_unmanaged_route'; await page.locator('#pool-name').fill('Unmanaged token conflict'); await page.locator('#pool-save').click();
    await page.waitForFunction(() => document.querySelector('#pools-error').textContent.includes('созданный вне этого пула'));
    assert.match(await page.locator('#pools-error').textContent(), /чужой маршрут автоматически не изменяется/);
    conflict = true; await page.locator('#pool-name').fill('Local conflicting edit'); await page.locator('#pool-save').click();
    await page.waitForFunction(() => document.querySelector('#pools-error').textContent.includes('другой сессии'));
    assert.equal(await page.locator('#pool-name').inputValue(), 'Local conflicting edit');
    await page.locator('#pool-reset').click(); await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelector('#pool-name').value === 'Remote change');
    await page.setViewportSize({width: 390, height: 844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, 'mobile must not overflow');
    const body = await page.locator('body').textContent();
    for (const secret of ['SECRET_TOKEN_NEVER_RENDER', 'SECRET_REMOTE_NEVER_RENDER', 'SECRET_DRAFT_NEVER_RENDER', 'SECRET_CAPS_NEVER_RENDER', 'SECRET_WORKER_NEVER_RENDER']) assert.equal(body.includes(secret), false);
    await openSection('overview');
    await page.locator('#pool-delete').click(); await page.locator('#confirm-action').click();
    await page.waitForFunction(() => document.querySelectorAll('.pool-card').length === 1);
    assert.equal(policies[0].name, 'Pool B');
    if (await page.locator('#nav-toggle').isVisible()) await page.locator('#nav-toggle').click();
    await page.locator('#tab-settings').click(); await page.locator('[data-settings=a6]').waitFor({state: 'visible'});
    const before = lists.length;
    await page.clock.install(); await page.clock.fastForward(16000);
    assert.equal(lists.length, before, 'inactive A6API section must not poll pools');
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
    console.log('PASS offline pools: multiple policies, CRUD/revisions, microprices, localized verification/reasons, runtime checking/phases, failed preflight/retry, paid consent, jobs/cancel, preview/publication, safe DOM, mobile, active polling');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
