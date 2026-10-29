/* Failover Proxy admin. No dependencies, persistent browser storage, or HTML injection. */
'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const nf = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });
  const short = new Intl.NumberFormat('ru-RU', { notation: 'compact', maximumFractionDigits: 1 });
  const dateFormat = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const timeFormat = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const state = { session: null, epoch: 0, tab: 'stats', settingsTab: 'providers', period: 'day', config: null, draft: null, dirty: false, saving: false, configLoading: false, keys: [], keysLoading: false, stats: null, statsSequence: 0, statsController: null, poll: null };
  const pools = { tab: 'pools', section: 'overview', data: null, selected: null, dirty: false, busy: false, loading: false, preview: null, timer: null, controller: null, sequence: 0 };
  const poolEvents = { id: null, data: null, loading: false, controller: null, sequence: 0 };
  const detector = { data: null, draft: null, dirty: false, saving: false, checking: false, timer: null, controller: null, sequence: 0, logController: null, logSequence: 0, cancelling: new Set() };
  const pelican = { data: null, timer: null, controller: null, sequence: 0, busy: false, mutationController: null, signingOut: false, actionError: '', cancelling: new Set(), motionNodes: new Map() };
  const journal = { rows: [], search: '', filter: 'all', sort: 'newest', signature: '' };
  const requestsView = { search: '', filter: 'all', sort: 'newest', signature: '' };
  const pageLabels = {
    stats: ['Обзор шлюза', 'Запросы, переключения и фактический расход ресурсов.'],
    settings: ['Шлюз', 'Поставщики, модели и ограничения одного API.'],
    access: ['Доступ', 'Ключи клиентского API и учётная запись администратора.'],
    detector: ['Проверки моделей', 'Ручная проверка и отдельный мониторинг upstream-маршрутов.'],
    'detector-log': ['Журнал проверок', 'Только изменения определённого статуса; последние 100 записей.'],
    market: ['A6API', 'Независимая политика для каждого пула, модели и токена. PAT — в подвкладке «Учётные данные».'],
    pelican: ['Пеликан', 'Одна сцена, одна генерация, ручная оценка результата. Отдельно от проверок моделей и A6API.']
  };
  const settingDefs = [
    ['request_timeout_s', 'Общий таймаут запроса', 'Целые секунды · весь клиентский запрос, включая переключения.', 1, 1],
    ['connect_timeout_s', 'Подключение к поставщику', 'Целые секунды · установка upstream-соединения.', 1, 1],
    ['read_timeout_s', 'Ожидание чтения', 'Целые секунды · ожидание очередных данных upstream.', 1, 1],
    ['cooldown_s', 'Пауза после сбоев', 'Целые секунды · поставщик временно исключается из маршрутизации.', 0, 1],
    ['failure_threshold', 'Порог последовательных сбоев', 'Количество ошибок до включения паузы.', 1, 1],
    ['max_request_bytes', 'Максимум тела запроса', 'Байты · включая изображения и сообщения.', 1, 1],
    ['max_response_bytes', 'Максимум буферизованного ответа', 'Байты · ограничивает расход памяти на запрос.', 1, 1],
    ['max_buffer_events', 'Максимум событий в буфере', 'Количество upstream-событий до полного ответа.', 1, 1],
    ['max_concurrent_requests', 'Одновременные запросы', 'Количество · общий предел активных запросов.', 1, 1],
    ['max_attempts', 'Максимум попыток на запрос', 'Количество · обращения к поставщикам, не число клиентских запросов.', 1, 1]
  ];

  function el(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text != null) element.textContent = String(text);
    return element;
  }
  function text(id, value) { $(id).textContent = value == null ? '—' : String(value); }
  function number(value, compact = false) { return value == null || !Number.isFinite(Number(value)) ? '—' : (compact ? short : nf).format(Number(value)); }
  function percent(value) { return value == null || !Number.isFinite(Number(value)) ? '—' : number(value) + '%'; }
  function date(value, onlyTime = false) {
    if (value == null || value === '') return '—';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? '—' : (onlyTime ? timeFormat : dateFormat).format(parsed);
  }
  function badge(value, style = '') { return el('span', 'badge' + (style ? ' ' + style : ''), value); }
  function showNotice(message, kind = '') {
    text('global-message', message);
    $('global-notice').className = 'notice' + (kind ? ' ' + kind : '');
    $('global-notice').setAttribute('role', ['warning', 'danger'].includes(kind) ? 'alert' : 'status');
    $('global-notice').hidden = false;
  }
  function areaError(id, message) { text(id, message); $(id).hidden = !message; }
  function errorMessage(error) {
    const messages = {
      a6_http_502: 'A6API временно вернул 502 при чтении данных. Изменения не опубликованы; повторите предпросмотр.',
      a6_http_504: 'A6API не успел ответить при чтении данных. Изменения не опубликованы; повторите предпросмотр.',
      a6_search_snapshot_changed: 'Каталог A6API изменился во время чтения. Повторите предпросмотр; рабочий маршрут не менялся.',
      a6_connection_failed: 'Не удалось дождаться ответа A6API. Повторите чтение данных; если выполнялась запись, сначала проверьте состояние маршрута.',
      invalid_current_password: 'Текущий пароль неверен. Проверьте пароль и повторите попытку.',
      invalid_admin_username: 'Укажите корректный непустой логин администратора длиной не более 100 символов без пробелов и управляющих символов.',
      admin_credentials_unchanged: 'Логин не изменён, новый пароль не задан. Измените логин или укажите новый пароль.',
      invalid_admin_password: 'Новый пароль должен содержать не менее 12 символов. Чтобы сохранить текущий пароль, оставьте оба поля нового пароля пустыми.',
      account_change_rate_limited: 'Слишком много попыток изменения учётных данных. Подождите и повторите попытку.',
      test_token_in_use: 'Один из тестовых токенов уже занят другим пулом как рабочий или тестовый. Выберите отдельные свободные токены для этого пула.',
      pool_token_unavailable: 'Один из выбранных токенов отключён, удалён или недоступен в A6API. Обновите список токенов и выберите активные.',
      separate_test_token_required: 'Рабочий токен нельзя использовать для проверки. Выберите другие тестовые токены.',
      invalid_test_token_ids: 'Выберите от 1 до 4 разных активных тестовых токенов.',
      conflicting_test_token_id: 'Список тестовых токенов не совпал с первым ключом. Перезагрузите форму и выберите ключи заново.',
      test_token_has_unmanaged_route: 'У тестового токена уже есть маршрут, созданный вне этого пула. Выберите свободный тестовый токен или проверьте и удалите маршрут вручную; чужой маршрут автоматически не изменяется.',
      test_token_has_conflicting_pin: 'У одного из тестовых токенов уже закреплён продавец для этой модели. Выберите другой свободный токен; существующий пин автоматически не снимается.',
      zero_price_cap_is_not_enforceable_use_positive_limit: 'Укажите положительное ценовое ограничение или оставьте поле пустым: без ограничения. A6API может трактовать нулевой потолок как отсутствие ограничения.',
      invalid_price_cap: 'Укажите положительное ценовое ограничение или оставьте поле пустым: без ограничения.',
      auto_remote_route_conflict: 'Конфигурация рабочего маршрута A6API отличается от сохранённой. Автоперезапись остановлена; проверьте Предпросмотр публикации. Это не конфликт другой сессии формы.',
      published_pool_identity_is_immutable: 'У опубликованного пула нельзя менять рабочий токен, модель или первый тестовый токен без отдельного отключения маршрута.',
      pool_operation_in_progress: 'В этом пуле выполняется операция. Дождитесь завершения или отмените проверку перед изменением ключей.',
      reference_model_not_in_catalog: 'Выберите существующий эталон модели из каталога.',
      duplicate_token_model: 'Для этого рабочего токена и модели пул уже существует.',
      revision_conflict: 'Политика изменена в другой сессии. Правки сохранены в форме; обновите данные и загрузите новую ревизию.'
    };
    if (messages[error.code]) return messages[error.code] + ' [' + error.code + ']';
    if (error.status === 400 || error.status === 422) {
      const suffix = typeof error.code === 'string' && /^[a-z][a-z0-9_]{0,100}$/.test(error.code) ? ' [' + error.code + ']' : '';
      return 'Сервер отклонил значения. Проверьте поля, ограничения и маршруты.' + suffix;
    }
    if (error.status === 401) return 'Сессия истекла. Войдите снова.';
    if (error.status === 403) return 'Доступ отклонён. Обновите сессию: выйдите и войдите снова.';
    if (error.status === 409 || error.status === 412) return 'Конфигурация уже изменена в другой сессии. Ваши изменения не сохранены. Перезагрузите конфигурацию и повторите правки; автоматической перезаписи нет.';
    if (error.status === 404) return 'Объект больше не существует. Обновите данные.';
    if (error.status === 413) return 'Размер данных превышает допустимый предел сервера.';
    if (error.status === 429) return 'Слишком много запросов. Подождите и повторите действие.';
    if (error.status >= 500) return 'Сервер временно недоступен (HTTP ' + error.status + ').';
    if (error.name === 'AbortError') return 'Время ожидания истекло. Для изменений результат неизвестен: перед повтором проверьте данные на сервере.';
    return 'Не удалось получить ответ. Проверьте соединение. Перед повторной отправкой изменений обновите данные.';
  }
  async function api(path, { method = 'GET', body, signal, timeout = 20000, auth = true } = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal) { if (signal.aborted) controller.abort(); else signal.addEventListener('abort', abort, { once: true }); }
    const timer = setTimeout(abort, timeout);
    const epoch = state.epoch;
    try {
      const headers = { Accept: 'application/json' };
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (method !== 'GET' && auth) {
        if (!state.session?.csrf_token) throw new Error('Missing CSRF token');
        headers['X-CSRF-Token'] = state.session.csrf_token;
      }
      const response = await fetch(path, { method, headers, credentials: 'same-origin', cache: 'no-store', redirect: 'error', body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal });
      if (!response.ok) {
        const error = new Error('HTTP ' + response.status);
        error.status = response.status;
        try {
          const payload = await response.json();
          const code = payload?.error?.code;
          if (typeof code === 'string' && /^[a-z][a-z0-9_]{0,100}$/.test(code)) error.code = code;
        } catch { /* Do not display raw server bodies or exception text. */ }
        if (response.status === 401 && auth && epoch === state.epoch) showLogin('Сессия истекла. Войдите снова.');
        throw error;
      }
      if (response.status === 204) return null;
      return await response.json();
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', abort);
    }
  }
  function stopPolling() {
    clearInterval(state.poll);
    state.poll = null;
    if (state.statsController) state.statsController.abort();
    state.statsController = null;
    state.statsSequence++;
    $('stats-content').setAttribute('aria-busy', 'false');
    $('refresh-stats').disabled = false;
    $('refresh-stats').classList.remove('is-spinning');
  }
  function updatePolling() {
    stopPolling();
    const active = !!state.session && state.tab === 'stats' && !document.hidden;
    text('polling-status', active ? 'Обновление каждые 15 секунд' : 'Автообновление приостановлено');
    if (active) { void loadStats(); state.poll = setInterval(() => void loadStats(), 15000); }
  }
  function clearSecrets() {
    $('password').value = '';
    $('account-form').reset();
    areaError('account-error', '');
    for (const provider of state.draft?.providers || []) delete provider.api_key;
    for (const input of document.querySelectorAll('input[data-provider-secret]')) input.value = '';
    text('new-token', '');
    text('copy-status', '');
    if ($('token-dialog').open) $('token-dialog').close();
    if ($('confirm-dialog').open) $('confirm-dialog').close('cancel');
  }
  function showLogin(message = '') {
    state.epoch++;
    stopPolling();
    stopDetectorPolling();
    stopPoolsPolling();
    stopPelicanPolling();
    if (pelican.mutationController) pelican.mutationController.abort();
    pelican.mutationController = null; pelican.data = null; pelican.busy = false; pelican.actionError = ''; pelican.cancelling.clear(); pelican.motionNodes.clear();
    $('pelican-form').reset(); $('pelican-fields').disabled = true; $('pelican-start').disabled = true;
    for (const id of ['pelican-provider', 'pelican-model', 'pelican-runs']) $(id).replaceChildren();
    areaError('pelican-error', ''); text('pelican-status', 'Войдите для просмотра генераций.');
    pools.data = pools.selected = pools.preview = null; pools.dirty = pools.busy = pools.loading = false;
    resetPoolEvents(); renderActivitySources(null);
    $('capability-results').replaceChildren(); $('capability-evidence').open=false; $('capability-refresh').disabled=false;
    for (const id of ['pools-list', 'pool-policy-fields', 'pool-jobs', 'pool-members', 'pool-candidates', 'pool-preview-diff', 'pool-auto-state', 'pool-bans']) $(id).replaceChildren();
    $('pool-details').hidden = $('pool-preview-panel').hidden = true;
    pools.tab = 'pools'; pools.section = 'overview';
    $('a6-pat').value = '';
    clearSecrets();
    detector.data = detector.draft = null;
    detector.dirty = detector.saving = detector.checking = false;
    detector.cancelling.clear();
    $('detector-settings-form').reset(); $('detector-check-form').reset();
    for (const id of ['detector-targets', 'detector-provider', 'detector-reference', 'detector-jobs', 'detector-states', 'detector-log-rows', 'detector-manual-models']) $(id).replaceChildren();
    $('detector-settings-fieldset').disabled = $('detector-check-fieldset').disabled = true;
    $('detector-dirty-dot').hidden = true;
    for (const id of ['detector-error', 'detector-settings-error', 'detector-check-error', 'detector-log-error', 'detector-warning']) areaError(id, '');
    if ($('journal-detail')?.open) $('journal-detail').close();
    $('journal-detail-body')?.replaceChildren();
    journal.rows = []; journal.signature = requestsView.signature = '';
    journal.search = requestsView.search = ''; journal.filter = requestsView.filter = 'all'; journal.sort = requestsView.sort = 'newest';
    for (const id of ['journal-search', 'request-search']) if ($(id)) $(id).value = '';
    for (const id of ['journal-filter', 'request-filter']) if ($(id)) $(id).value = 'all';
    for (const id of ['journal-sort', 'request-sort']) if ($(id)) $(id).value = 'newest';
    for (const id of ['journal-count', 'request-count']) if ($(id)) text(id, '');
    setNavigationOpen(false);
    state.session = null;
    state.config = state.draft = state.stats = null;
    state.keys = [];
    state.dirty = state.saving = state.configLoading = state.keysLoading = false;
    $('config-fieldset').disabled = true;
    $('save-config').disabled = true;
    $('provider-editor').replaceChildren();
    $('model-editor').replaceChildren();
    $('general-fields').replaceChildren();
    $('key-models').replaceChildren();
    $('keys-list').replaceChildren(el('div', 'panel empty', 'Войдите для просмотра ключей.'));
    $('key-name').value = '';
    $('create-key').disabled = false;
    $('refresh-keys').disabled = false;
    $('boot').hidden = true;
    $('app').hidden = true;
    $('login-view').hidden = false;
    areaError('login-error', message);
    $('session-retry').hidden = !message;
  }
  function showApp(session) {
    state.epoch++;
    state.session = session;
    pelican.signingOut = false;
    $('boot').hidden = true;
    $('login-view').hidden = true;
    $('app').hidden = false;
    $('password').value = '';
    text('session-user', session.username || 'Администратор');
    $('account-username').value = session.username || '';
    state.tab = 'stats';
    state.settingsTab = 'providers';
    activateSettings('providers', false);
    switchTab('stats');
    void loadConfig();
    $('global-notice').hidden = true;
  }
  async function restoreSession() {
    $('session-retry').disabled = true;
    try {
      const session = await api('/admin/session', { auth: false });
      if (!session?.csrf_token) throw new Error('Invalid session response');
      showApp(session);
    } catch (error) { showLogin(error.status === 401 ? '' : 'Проверка сессии недоступна. Повторите проверку или войдите, когда сервер станет доступен.'); }
    finally { $('session-retry').disabled = false; }
  }
  // Six sidebar entries. «Журнал проверок» and the A6 credentials are subsurfaces of
  // «Проверки» and «A6API»: they keep their own state but are not top-level sections.
  const navEntries = ['stats', 'settings', 'access', 'detector', 'market', 'pelican'];
  const viewOfTab = { stats: 'stats', settings: 'settings', access: 'settings', detector: 'detector', 'detector-log': 'detector', market: 'market', pelican: 'pelican' };
  const navEntryOf = tab => (tab === 'detector-log' ? 'detector' : tab);
  const settingsGroups = { providers: 'gateway', models: 'gateway', general: 'gateway', keys: 'access', account: 'access', a6: 'market' };
  function markNavigation(tab) {
    const entry = navEntryOf(tab);
    for (const name of navEntries) {
      const button = $('tab-' + name), active = name === entry;
      button.classList.toggle('active', active);
      button.removeAttribute('aria-current');
      if (active) button.setAttribute('aria-current', 'page');
    }
    const journal = $('tab-detector-log');
    journal.classList.toggle('active', tab === 'detector-log');
    journal.removeAttribute('aria-current');
    if (tab === 'detector-log') { journal.setAttribute('aria-current', 'page'); $('tab-detector').removeAttribute('aria-current'); }
  }
  function activateDetectorSection(section) {
    const log = section === 'log';
    if ($('detector-heading')) $('detector-heading').hidden = log;
    $('detector-monitor-panel').hidden = log;
    $('detector-log-view').hidden = !log;
  }
  function activateMarketSection(section, syncPolling = true) {
    const creds = section === 'creds';
    pools.tab = creds ? 'creds' : 'pools';
    $('market-pools-panel').hidden = creds;
    $('market-creds-panel').hidden = !creds;
    for (const [id, selected] of [['a6-tab-pools', !creds], ['a6-tab-creds', creds]]) {
      $(id).setAttribute('aria-selected', String(selected)); $(id).tabIndex = selected ? 0 : -1;
    }
    if (creds) void loadA6();
    if (syncPolling) updatePoolsPolling();
  }
  function switchTab(tab) {
    state.tab = tab;
    setNavigationOpen(false);
    if ($('journal-detail')?.open && tab !== 'detector-log') $('journal-detail').close();
    text('active-page-title', pageLabels[tab]?.[0] || 'Failover Proxy');
    text('active-page-description', pageLabels[tab]?.[1] || '');
    // The description is clamped to one line so the header height never jumps between pages.
    $('active-page-description').title = pageLabels[tab]?.[1] || '';
    const view = viewOfTab[tab];
    for (const name of ['stats', 'settings', 'detector', 'market', 'pelican']) $(name + '-view').hidden = name !== view;
    markNavigation(tab);
    activateDetectorSection(tab === 'detector-log' ? 'log' : 'monitor');
    // Both «Шлюз» and «Доступ» open the same configuration surface, on their own group.
    if (tab === 'settings') activateSettings(settingsGroups[state.settingsTab] === 'gateway' ? state.settingsTab : 'providers', false);
    if (tab === 'access') activateSettings(settingsGroups[state.settingsTab] === 'access' ? state.settingsTab : 'keys', false);
    if (tab === 'market') activateMarketSection('pools', false);
    updatePolling();
    updateDetectorPolling();
    if (tab === 'settings' && !state.config && !state.configLoading) void loadConfig();
    // «Доступ» is a first-class entry point to the keys list (brief §6.4), so it loads
    // the list and the model checkboxes exactly like the «Ключи доступа» subtab does.
    if (tab === 'access') {
      if (!state.config && !state.configLoading) void loadConfig();
      renderKeyModels();
      void loadKeys();
    }
    updatePoolsPolling();
    updatePelicanPolling();
  }
  function activateSettings(tab, refresh = true) {
    state.settingsTab = tab;
    const group = settingsGroups[tab] || 'gateway';
    for (const button of document.querySelectorAll('[data-settings]')) {
      if (button.dataset.settings === tab) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
    }
    for (const name of ['providers', 'models', 'general', 'keys', 'a6', 'account']) $('settings-' + name).hidden = name !== tab;
    $('config-form').hidden = group !== 'gateway';
    // Save/reload belong to the configuration form only, never to keys or the account.
    if ($('config-actions')) $('config-actions').hidden = group !== 'gateway';
    const access = group === 'access';
    text('settings-title', access ? 'Доступ' : 'Шлюз');
    text('settings-subtitle', access ? 'Ключи клиентского API и учётная запись администратора.' : 'Поставщики, модели и ограничения одного API.');
    if (group === 'market') {
      if (state.tab !== 'market') {
        state.tab = 'market';
        for (const name of ['stats', 'settings', 'detector', 'market', 'pelican']) $(name + '-view').hidden = name !== 'market';
        markNavigation('market');
        text('active-page-title', pageLabels.market[0]);
        text('active-page-description', pageLabels.market[1]);
        $('active-page-description').title = pageLabels.market[1];
        updatePolling(); updateDetectorPolling(); updatePelicanPolling();
      }
      activateMarketSection('creds');
    } else if (state.tab === 'settings' || state.tab === 'access') {
      state.tab = access ? 'access' : 'settings';
      text('active-page-title', pageLabels[state.tab]?.[0] || '');
      text('active-page-description', pageLabels[state.tab]?.[1] || '');
      $('active-page-description').title = pageLabels[state.tab]?.[1] || '';
      markNavigation(state.tab);
    }
    if (refresh && tab === 'models' && state.draft) renderModels();
    if (refresh && tab === 'keys') { renderKeyModels(); void loadKeys(); }
  }

  // Statistics. All server strings are text nodes; SVG uses DOM namespaces only.
  async function loadStats() {
    if (!state.session || state.tab !== 'stats' || document.hidden) return;
    const sequence = ++state.statsSequence;
    const epoch = state.epoch;
    if (state.statsController) state.statsController.abort();
    state.statsController = new AbortController();
    const signal = state.statsController.signal;
    $('refresh-stats').disabled = true;
    $('refresh-stats').classList.add('is-spinning');
    $('stats-content').setAttribute('aria-busy', 'true');
    if (!state.stats) text('stats-status', 'Загрузка статистики…');
    try {
      const [stats, health, activity] = await Promise.allSettled([
        api('/admin/stats?period=' + state.period, { signal, timeout: 12000 }),
        api('/admin/health', { signal, timeout: 12000 }),
        api('/admin/observability', { signal, timeout: 12000 })
      ]);
      if (sequence !== state.statsSequence || epoch !== state.epoch) return;
      if (stats.status === 'fulfilled') {
        state.stats = stats.value;
        renderStats(stats.value);
        areaError('stats-error', '');
        text('stats-status', 'Обновлено ' + date(stats.value.generated_at) + ' · время вашего браузера');
      } else {
        areaError('stats-error', 'Статистика недоступна. ' + (state.stats ? 'Показаны последние загруженные значения; они могли устареть. ' : '') + errorMessage(stats.reason));
        text('stats-status', 'Нет свежих данных · повтор через 15 секунд');
      }
      renderActivitySources(activity.status === 'fulfilled' ? activity.value : null);
      if (health.status === 'fulfilled') renderHealth(health.value);
      else { text('health-badge', 'Нет связи'); $('health-badge').className = 'badge warning'; }
    } finally {
      if (sequence === state.statsSequence && epoch === state.epoch) {
        $('refresh-stats').disabled = false;
        $('refresh-stats').classList.remove('is-spinning');
        $('stats-content').setAttribute('aria-busy', 'false');
      }
    }
  }
  function renderActivitySources(data) {
    const gateway = data?.gateway || {}, provider = data?.provider_checks || {}, a6 = data?.a6_checks || {};
    const rows = [
      ['Шлюз · клиенты', number(gateway.requests_24h) + ' запросов / 24 ч', ['Попыток: ' + number(gateway.attempts_24h), 'Последний запрос: ' + date(gateway.last_request_at)]],
      ['Проверки поставщиков', number(provider.upstream_attempts_24h) + ' upstream-проб / 24 ч', ['Результатов: ' + number(provider.results_24h) + ' · подтверждено: ' + number(provider.verified_24h), 'Ниже порога: ' + number(provider.suspect_24h) + ' · неизвестно: ' + number(provider.unknown_24h) + ' · ошибок: ' + number(provider.error_24h), 'Настроенный интервал: ' + number(provider.interval_minutes) + ' мин']],
      ['Проверки A6API', number(a6.probes_24h) + ' проб / 24 ч', ['Заданий: ' + number(a6.jobs_24h) + ' · ошибок: ' + number(a6.errors_24h), 'Списано за сутки UTC: ' + poolBillingAmount(a6.actual_cost_usd_utc_day), 'Ожидающий резерв: ' + poolBillingAmount(a6.pending_reserved_usd) + ' · не списание']]
    ];
    $('activity-sources').replaceChildren(...rows.map(([label, value, notes]) => {
      const card = el('article', 'activity-source'); card.append(el('h3', '', label), el('strong', '', value));
      for (const note of notes) card.append(el('p', 'small', note));
      return card;
    }));
    text('activity-sources-status', data ? 'Обновлено ' + date(data.generated_at) : 'Раздельный учёт временно недоступен');
    text('activity-sources-note', 'Отсутствие клиентского трафика не означает, что мониторинг простаивает. Окна учёта источников могут различаться; показатели не складываются. Списания A6API — за сутки UTC, резерв — текущий.' + (typeof data?.scope_note === 'string' ? ' ' + data.scope_note : ''));
  }
  function renderHealth(health) {
    const healthy = ['ok', 'healthy', 'ready', 'up'].includes(String(health.status).toLowerCase());
    text('health-badge', healthy ? 'Система работает' : 'Статус: ' + String(health.status || 'неизвестен'));
    $('health-badge').className = 'badge' + (healthy ? '' : ' warning');
    text('version', health.version ? 'v' + String(health.version).replace(/^v/, '') : '');
    text('footer-health', 'Активных запросов: ' + number(health.active_requests) + (health.queued_requests == null ? '' : ' · В очереди: ' + number(health.queued_requests)) + ' · Полная буферизация');
  }
  function renderStats(data) {
    const summary = data.summary || {};
    text('metric-requests', number(summary.final_requests, true));
    $('metric-requests').title = number(summary.final_requests) + ' клиентских · ' + number(summary.requests) + ' попыток';
    text('metric-errors', number(summary.final_errors));
    text('metric-switches', number(summary.switches));
    text('metric-tokens', number(summary.total_tokens, true));
    $('metric-tokens').title = number(summary.total_tokens);
    text('metric-success', percent(summary.final_success_rate) + ' успешных · ' + number(summary.final_success) + ' ответов');
    renderChart(Array.isArray(data.timeline) ? data.timeline : []);
    renderTokens(summary);
    const performance = [
      ['Скорость upstream', number(summary.avg_tps), 'ток/с · по фактическим данным'],
      ['Первый токен upstream', number(summary.avg_ttft_ms), 'мс · TTFT поставщика'],
      ['Первый ответ клиенту', number(summary.avg_client_ttft_ms), 'мс · после полной буферизации'],
      ['Полная задержка', number(summary.avg_latency_ms), 'мс · среднее время'],
      ['p50 / p95', number(summary.p50_ms) + ' / ' + number(summary.p95_ms), 'мс · перцентили задержки']
    ];
    $('performance').replaceChildren(...performance.map(([label, value, note]) => {
      const item = el('div', 'performance-item');
      item.append(el('span', '', label), el('strong', '', value), el('small', '', note));
      return item;
    }));
    const providers = Array.isArray(data.providers) ? [...data.providers].sort((a, b) => Number(a.priority) - Number(b.priority)) : [];
    $('provider-stats').replaceChildren(...providers.map(provider => {
      const card = el('article', 'panel provider-card');
      const head = el('div', 'provider-head');
      const name = el('div');
      name.append(el('h3', '', provider.name), el('p', 'small', 'Приоритет ' + number(provider.priority)));
      const cooldown = provider.cooldown_until != null && new Date(provider.cooldown_until).getTime() > Date.now();
      head.append(name, badge(!provider.enabled ? 'ВЫКЛЮЧЕН' : cooldown ? 'ПАУЗА' : 'ГОТОВ', !provider.enabled ? 'neutral' : cooldown ? 'warning' : ''));
      const counters = el('div', 'provider-counters');
      for (const [label, value] of [['Попытки', provider.requests], ['Ошибки', provider.errors], ['Переходы', provider.switches]]) {
        const item = el('div'); item.append(el('strong', '', number(value)), el('span', '', label)); counters.append(item);
      }
      const progress = el('div', 'provider-progress');
      const title = el('div'); title.append(el('span', '', 'Успешность попыток'), el('span', '', percent(provider.success_rate)));
      const track = el('div', 'track'), bar = el('span');
      bar.style.width = Math.min(100, Math.max(0, Number(provider.success_rate) || 0)) + '%';
      track.append(bar); progress.append(title, track);
      card.append(head, counters, progress);
      if (cooldown) card.append(el('p', 'small', 'Пауза до ' + date(provider.cooldown_until)));
      return card;
    }));
    if (!providers.length) $('provider-stats').append(el('div', 'panel empty', 'Поставщиков пока нет. Добавьте первый в настройках.'));
    renderTable('provider-table', providers.map(p => [p.name, number(p.requests), number(p.success), number(p.errors), number(p.switches), number(p.input_tokens) + ' / ' + number(p.output_tokens), number(p.cache_read_tokens) + ' / ' + number(p.cache_write_tokens), percent(p.success_rate), number(p.avg_latency_ms), number(p.avg_ttft_ms), number(p.avg_client_ttft_ms), number(p.avg_tps), number(p.p50_ms) + ' / ' + number(p.p95_ms)]), 13, 'За этот период попыток пока нет.');
    renderRequests();
    $('recent-errors').replaceChildren(...(data.recent_errors || []).map(error => {
      const row = el('div', 'error-row');
      const main = el('div', 'error-main');
      main.append(el('strong', '', error.provider || 'Неизвестный поставщик'), el('p', '', String(error.kind || 'Ошибка') + ' · HTTP ' + (error.status ?? '—') + ' · ' + (error.request_id || 'Без ID')));
      row.append(el('span', 'error-marker'), main, el('span', 'error-time', date(error.time, true)));
      return row;
    }));
    if (!(data.recent_errors || []).length) $('recent-errors').append(el('div', 'empty', 'Ошибок за этот период нет.'));
    text('coverage-note', data.coverage?.note || 'Покрытие статистики сервером не указано. Отсутствующий usage не восстанавливается и не оценивается.');
  }
  async function loadCapabilityEvidence() {
    const epoch=state.epoch;
    $('capability-refresh').disabled=true;
    try {
      const data=await api('/admin/capabilities');
      if(epoch!==state.epoch)return;
      const nodes=(data.observations||[]).map(row=>{
        const item=el('div','pool-membership-event');
        item.append(el('strong','',String(row.provider_id)+' · '+String(row.model)+' · '+String(row.source)));
        item.append(el('p','small','Запрошено: '+number(row.requested_output_tokens)+' · Реально OUT: '+number(row.observed_output_tokens)+' · Рассуждения: '+number(row.reasoning_tokens)));
        item.append(el('p','small','HTTP: '+number(row.http_status)+' · '+(row.parameter_accepted?'Параметр принят, потолок не измерен':'Принятие параметра не подтверждено')+' · '+date(row.observed_at)));
        if(row.context?.reliability==='heuristic') item.append(el('p','small','Оценка контекста: ~'+number(row.context.estimated_total_tokens)+' / '+number(row.context.context_length)+(row.context.over_limit?' · возможное превышение':'')+' · приблизительно, без отказа'));
        return item;
      });
      $('capability-results').replaceChildren(...(nodes.length?nodes:[el('p','small','Наблюдений пока нет. Проверки автоматически не запускаются.')]));
    } catch(error){if(epoch===state.epoch)text('capability-results',errorMessage(error));}
    finally{if(epoch===state.epoch)$('capability-refresh').disabled=false;}
  }
  $('capability-refresh').addEventListener('click',loadCapabilityEvidence);

  function renderTable(id, rows, columns, empty) {
    const nodes = rows.map(values => {
      const row = el('tr');
      for (const value of values) { const cell = el('td', '', value == null ? '—' : value); cell.title = cell.textContent; row.append(cell); }
      return row;
    });
    if (!nodes.length) { const row = el('tr'), cell = el('td', 'empty', empty); cell.colSpan = columns; row.append(cell); nodes.push(row); }
    $(id).replaceChildren(...nodes);
  }
  function normalized(value) { return String(value ?? '').toLocaleLowerCase('ru-RU').trim(); }
  function renderRequests() {
    const source = Array.isArray(state.stats?.recent_requests) ? state.stats.recent_requests : [];
    const query = normalized(requestsView.search);
    const rows = source.filter(row => {
      const status = Number(row.status), failed = Number.isFinite(status) && status >= 400;
      return (requestsView.filter === 'all' || (requestsView.filter === 'error' ? failed : status >= 200 && status < 400)) &&
        (!query || [row.id, row.model, row.provider, row.status].some(value => normalized(value).includes(query)));
    }).slice().sort((a, b) => requestsView.sort === 'provider' ? String(a.provider || '').localeCompare(String(b.provider || ''), 'ru') : requestsView.sort === 'latency' ? (Number(b.total_ms) || 0) - (Number(a.total_ms) || 0) : ((Date.parse(a.time) || 0) - (Date.parse(b.time) || 0)) * (requestsView.sort === 'oldest' ? 1 : -1));
    if ($('request-count')) text('request-count', 'Показано ' + rows.length + ' из ' + source.length);
    const values = rows.map(r => [date(r.time), r.model || '—', r.status == null ? '—' : String(r.status), number(r.attempts), r.provider || '—', number(r.total_ms), number(r.input_tokens) + ' / ' + number(r.output_tokens), r.id || '—']);
    const signature = JSON.stringify([values, query, requestsView.filter]);
    if (signature === requestsView.signature) return;
    requestsView.signature = signature;
    renderTable('requests-table', values, 8, source.length ? 'Ничего не найдено. Измените поиск или фильтр.' : 'За этот период запросов пока нет.');
    [...$('requests-table').rows].forEach((row, index) => {
      if (rows[index]) { row.dataset.status = Number(rows[index].status) >= 400 ? 'error' : 'success'; row.cells[2].classList.add('request-status'); }
    });
  }
  function renderTokens(summary) {
    const definitions = [['Вход · IN', 'input_tokens', 'var(--chart-requests)'], ['Выход · OUT', 'output_tokens', 'var(--blue)'], ['Кеш · чтение', 'cache_read_tokens', 'var(--amber)'], ['Кеш · запись', 'cache_write_tokens', 'var(--chart-cache-write)']];
    const max = Math.max(1, ...definitions.map(([, key]) => Number(summary[key]) || 0));
    $('token-breakdown').replaceChildren(...definitions.map(([label, key, color]) => {
      const item = el('div'), heading = el('div', 'token-heading');
      heading.append(el('span', '', label), el('b', '', number(summary[key])));
      const track = el('div', 'track'), bar = el('span');
      bar.style.setProperty('--bar', color);
      bar.style.width = Math.min(100, Math.max(0, Number(summary[key]) || 0) / max * 100) + '%';
      track.append(bar); item.append(heading, track); return item;
    }));
  }
  function svgEl(tag, attributes = {}, label) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (label != null) node.textContent = String(label);
    return node;
  }
  // Chart colours come from the component token layer, so the stylesheet stays the single source.
  const cssToken = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  function renderChart(timeline) {
    const color = {
      requests: cssToken('--chart-requests'), errors: cssToken('--chart-errors'), switches: cssToken('--chart-switches'),
      grid: cssToken('--grid-line'), axis: cssToken('--axis-label'), cursor: cssToken('--axis-label'), surface: cssToken('--panel')
    };
    const chart = $('activity-chart');
    chart.setAttribute('role', 'group');
    if (chart.contains(document.activeElement) || chart.matches(':hover') && chart.querySelector('.chart-tooltip:not([hidden])')) return;
    $('chart-axis').replaceChildren();
    if (!timeline.length) { $('activity-chart').replaceChildren(el('div', 'empty', 'Здесь появится активность запросов. Пока данных нет.')); return; }
    const w = 900, h = 220, left = 38, right = 12, top = 13, bottom = 15;
    const max = Math.max(1, ...timeline.flatMap(row => ['requests', 'errors', 'switches'].map(key => Math.max(0, Number(row[key]) || 0))));
    const x = i => timeline.length === 1 ? (w + left - right) / 2 : left + i * (w - left - right) / (timeline.length - 1);
    const y = v => h - bottom - Math.max(0, Number(v) || 0) / max * (h - top - bottom);
    const svg = svgEl('svg', { viewBox: '0 0 ' + w + ' ' + h, preserveAspectRatio: 'none', role: 'group', 'aria-label': 'Активность запросов. Точки доступны с клавиатуры.' });
    const defs = svgEl('defs'), gradient = svgEl('linearGradient', { id: 'activity-fill', x1: 0, x2: 0, y1: 0, y2: 1 });
    gradient.append(svgEl('stop', { offset: '0%', 'stop-color': color.requests, 'stop-opacity': '.22' }), svgEl('stop', { offset: '100%', 'stop-color': color.requests, 'stop-opacity': '0' }));
    defs.append(gradient); svg.append(defs);
    for (let index = 0; index <= 3; index++) {
      const value = max * index / 3;
      svg.append(svgEl('line', { x1: left, x2: w, y1: y(value), y2: y(value), stroke: color.grid, 'stroke-dasharray': '3 6' }), svgEl('text', { x: left - 10, y: y(value) + 3, fill: color.axis, 'font-size': 12, 'text-anchor': 'end' }, number(value, true)));
    }
    const path = key => timeline.map((row, i) => (i ? 'L' : 'M') + x(i).toFixed(2) + ' ' + y(row[key]).toFixed(2)).join(' ');
    svg.append(svgEl('path', { d: path('requests') + ' L' + x(timeline.length - 1) + ' ' + (h - bottom) + ' L' + x(0) + ' ' + (h - bottom) + ' Z', fill: 'url(#activity-fill)' }));
    // Three series differ by colour and by stroke pattern, so the trend survives colour-blind viewing.
    for (const [key, dash] of [['requests', null], ['errors', '5 4'], ['switches', '1 3 6 3']]) svg.append(svgEl('path', { d: path(key), stroke: color[key], fill: 'none', 'stroke-width': key === 'requests' ? 2 : 1.5, 'vector-effect': 'non-scaling-stroke', 'stroke-linejoin': 'round', ...(dash ? { 'stroke-dasharray': dash } : {}) }));
    const cursor = svgEl('line', { class: 'chart-cursor', y1: top, y2: h - bottom, stroke: color.cursor, 'stroke-dasharray': '4 4', visibility: 'hidden', 'pointer-events': 'none' });
    const marker = svgEl('circle', { class: 'chart-point', r: 5, fill: color.requests, stroke: color.surface, 'stroke-width': 2, visibility: 'hidden', 'pointer-events': 'none' });
    const tooltip = el('div', 'chart-tooltip'); tooltip.id = 'activity-tooltip'; tooltip.hidden = true; tooltip.setAttribute('role', 'tooltip');
    const hide = () => { tooltip.hidden = true; cursor.setAttribute('visibility', 'hidden'); marker.setAttribute('visibility', 'hidden'); };
    const show = (row, i) => {
      tooltip.replaceChildren(el('strong', '', date(row.time)), el('span', '', 'Запросы: ' + number(row.requests)), el('span', '', 'Ошибки: ' + number(row.errors)), el('span', '', 'Переключения: ' + number(row.switches)));
      tooltip.hidden = false;
      const availableWidth = chart.clientWidth;
      tooltip.style.left = Math.max(8, Math.min(availableWidth - tooltip.offsetWidth - 8, x(i) / w * availableWidth + 10)) + 'px';
      cursor.setAttribute('x1', x(i)); cursor.setAttribute('x2', x(i)); cursor.setAttribute('visibility', 'visible');
      marker.setAttribute('cx', x(i)); marker.setAttribute('cy', y(row.requests)); marker.setAttribute('visibility', 'visible');
    };
    const hits = timeline.map((row, i) => {
      const start = i === 0 ? left : (x(i - 1) + x(i)) / 2;
      const end = i === timeline.length - 1 ? w - right : (x(i) + x(i + 1)) / 2;
      const hit = svgEl('rect', { class: 'chart-hit', x: start, y: top, width: Math.max(1, end - start), height: h - top - bottom, fill: 'transparent', tabindex: i === 0 ? 0 : -1, role: 'img', 'aria-label': date(row.time) + ': ' + number(row.requests) + ' запросов, ' + number(row.errors) + ' ошибок, ' + number(row.switches) + ' переключений', 'aria-describedby': tooltip.id });
      hit.addEventListener('pointerenter', () => show(row, i));
      hit.addEventListener('pointerleave', () => { if (!svg.contains(document.activeElement)) hide(); });
      hit.addEventListener('focus', () => show(row, i)); hit.addEventListener('blur', hide);
      hit.addEventListener('keydown', event => {
        const next = event.key === 'ArrowRight' ? Math.min(timeline.length - 1, i + 1) : event.key === 'ArrowLeft' ? Math.max(0, i - 1) : event.key === 'Home' ? 0 : event.key === 'End' ? timeline.length - 1 : null;
        if (event.key === 'Escape') { hide(); return; }
        if (next == null) return;
        event.preventDefault(); hits.forEach((node, index) => node.setAttribute('tabindex', index === next ? '0' : '-1')); hits[next].focus();
      });
      return hit;
    });
    svg.append(cursor, marker, ...hits);
    chart.replaceChildren(svg, tooltip);
    $('activity-chart').setAttribute('aria-label', 'График запросов, ошибок и переключений. Точек: ' + timeline.length + '.');
    const indices = [...new Set([0, Math.floor((timeline.length - 1) / 2), timeline.length - 1])];
    $('chart-axis').replaceChildren(...indices.map(index => el('span', '', date(timeline[index].time, state.period === 'hour' || state.period === 'day'))));
  }

  // Config is a draft. Secrets are write-only and never hydrated from the server.
  function optionalFields(value, keys) {
    const result = {};
    for (const key of keys) if (Object.prototype.hasOwnProperty.call(value, key)) result[key] = value[key];
    return result;
  }
  function cleanConfig(config) {
    if (!config || config.revision == null || !config.settings || !Array.isArray(config.providers) || !Array.isArray(config.models)) throw new Error('Invalid configuration');
    const settings = {};
    for (const [key] of settingDefs) settings[key] = config.settings[key];
    settings.trust_private_upstreams = config.settings.trust_private_upstreams === true;
    return {
      revision: config.revision,
      settings,
      providers: config.providers.map(p => ({ id: p.id, name: p.name, base_url: p.base_url, enabled: p.enabled === true, priority: p.priority, has_api_key: p.has_api_key === true, ...optionalFields(p, ['routing_mode', 'service_tier', 'provider_pin']) })),
      models: config.models.map(m => ({ id: m.id, name: m.name, supports_vision: m.supports_vision === true, enabled: m.enabled === true, context_length: m.context_length ?? null, max_output_tokens: m.max_output_tokens ?? null, ...optionalFields(m, ['context_preflight']), routes: (m.routes || []).map(r => ({ provider_id: r.provider_id, upstream_model: r.upstream_model, enabled: r.enabled === true })) }))
    };
  }
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function markDirty() {
    if (!state.draft) return;
    state.dirty = true;
    updateConfigState();
  }
  function updateConfigState() {
    $('dirty-dot').hidden = !state.dirty;
    $('save-config').disabled = !state.dirty || state.saving || state.configLoading;
    $('reload-config').disabled = state.saving || state.configLoading;
    text('save-config', state.saving ? 'Сохранение…' : 'Сохранить изменения');
    text('config-state', state.configLoading ? 'Загрузка конфигурации…' : state.saving ? 'Сохраняем с проверкой ревизии…' : state.draft ? 'Ревизия ' + state.draft.revision + (state.dirty ? ' · Есть несохранённые изменения' : ' · Все изменения сохранены') : 'Конфигурация не загружена');
    for (const button of document.querySelectorAll('[data-provider-test]')) button.disabled = state.dirty || state.saving || !state.config?.providers.some(provider => provider.id === button.dataset.providerTest);
    text('provider-count', state.draft?.providers.length || 0);
    text('model-count', state.draft?.models.length || 0);
  }
  async function loadConfig() {
    if (!state.session || state.configLoading || state.saving) return;
    const epoch = state.epoch;
    state.configLoading = true;
    $('config-fieldset').disabled = true;
    updateConfigState();
    areaError('config-error', '');
    try {
      const config = cleanConfig(await api('/admin/config'));
      if (epoch !== state.epoch) return;
      clearProviderSecrets();
      state.config = config;
      state.draft = clone(config);
      state.dirty = false;
      renderConfig();
    } catch (error) { if (epoch === state.epoch) areaError('config-error', errorMessage(error)); }
    finally {
      if (epoch === state.epoch) { state.configLoading = false; $('config-fieldset').disabled = !state.draft; updateConfigState(); }
    }
  }
  function clearProviderSecrets() {
    for (const provider of state.draft?.providers || []) delete provider.api_key;
    for (const input of document.querySelectorAll('input[data-provider-secret]')) input.value = '';
  }
  function renderConfig() {
    renderProviders(); renderModels(); renderGeneral(); renderKeyModels(); updateConfigState();
  }
  function inputField(labelText, value, { type = 'text', placeholder = '', required = false, min, step, hint = '', maxLength = 500, onInput } = {}) {
    const label = el('label', '', labelText), input = el('input');
    input.type = type;
    input.value = value == null ? '' : String(value);
    input.placeholder = placeholder;
    input.required = required;
    if (type !== 'number') input.maxLength = maxLength;
    if (min != null) input.min = min;
    if (step != null) input.step = step;
    if (type === 'number') input.max = Number.MAX_SAFE_INTEGER;
    input.autocomplete = type === 'password' ? 'new-password' : 'off';
    input.spellcheck = false;
    if (onInput) input.addEventListener('input', () => { onInput(type === 'number' ? input.value === '' ? '' : Number(input.value) : input.value); markDirty(); });
    label.append(input);
    if (hint) label.append(el('span', 'small', hint));
    return { label, input };
  }
  function toggle(labelText, checked, onChange) {
    const label = el('label', 'toggle-label'), input = el('input'); input.type = 'checkbox'; input.checked = checked;
    input.addEventListener('change', () => { onChange(input.checked); markDirty(); });
    label.append(input, el('span', '', labelText)); return label;
  }
  function selectField(labelText, value, choices, onChange) {
    const label = el('label', '', labelText), select = el('select');
    for (const [key, title] of choices) {
      const option = el('option', '', title); option.value = key; select.append(option);
    }
    select.value = value;
    select.addEventListener('change', () => { onChange(select.value); markDirty(); });
    label.append(select); return { label, select };
  }
  function action(label, style, handler) {
    const button = el('button', 'button ' + style, label); button.type = 'button'; button.addEventListener('click', handler); return button;
  }
  function uuid() {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    // randomUUID requires HTTPS in some browsers; retain cryptographic UUIDv4 on HTTP.
    const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    return [...bytes].map((value, i) => ([4, 6, 8, 10].includes(i) ? '-' : '') + value.toString(16).padStart(2, '0')).join('');
  }
  async function confirmAction(title, description, label = 'Удалить') {
    if ($('confirm-dialog').open) return false;
    text('confirm-title', title); text('confirm-description', description); text('confirm-action', label);
    $('confirm-dialog').returnValue = 'cancel';
    return new Promise(resolve => {
      $('confirm-dialog').addEventListener('close', () => resolve($('confirm-dialog').returnValue === 'confirm'), { once: true });
      $('confirm-dialog').showModal();
    });
  }
  function renderProviders() {
    if (!state.draft) return;
    $('provider-editor').replaceChildren(...state.draft.providers.map((provider, index) => {
      const card = el('article', 'panel editor-card'), heading = el('div', 'editor-heading'), left = el('div', 'editor-heading-left');
      const titles = el('div'), title = el('h3', '', provider.name || 'Новый поставщик');
      titles.append(title, el('p', 'small', 'OpenAI-compatible · Chat Completions'));
      left.append(el('span', 'order-marker', String(index + 1).padStart(2, '0')), titles);
      const controls = el('div', 'editor-actions');
      controls.append(toggle('Включён', provider.enabled, value => { provider.enabled = value; }), action('Удалить', 'subtle small-button', async () => {
        const routeCount = state.draft.models.reduce((count, model) => count + model.routes.filter(route => route.provider_id === provider.id).length, 0);
        if (!await confirmAction('Удалить поставщика?', '«' + (provider.name || 'Новый поставщик') + '» будет удалён из черновика. Связанных маршрутов: ' + routeCount + '; они тоже будут удалены. Изменения вступят в силу после сохранения.')) return;
        if (!state.draft) return;
        state.draft.providers = state.draft.providers.filter(item => item.id !== provider.id);
        for (const model of state.draft.models) model.routes = model.routes.filter(route => route.provider_id !== provider.id);
        markDirty(); renderProviders(); renderModels();
      }));
      heading.append(left, controls);
      const fields = el('div', 'fields-grid');
      const name = inputField('Название', provider.name, { required: true, maxLength: 160, placeholder: 'Например, основной upstream', onInput: value => { provider.name = value; title.textContent = value || 'Новый поставщик'; } });
      const priority = inputField('Приоритет', provider.priority, { type: 'number', required: true, min: 1, step: 1, hint: 'От 1 до 1000. Меньшее число — более ранняя попытка.', onInput: value => { provider.priority = value; } });
      priority.input.max = 1000;
      const url = inputField('Базовый URL', provider.base_url, { type: 'url', required: true, maxLength: 2048, placeholder: 'https://api.example.com/v1', hint: 'База OpenAI API, без /chat/completions. Без логина, пароля и токенов в URL.', onInput: value => { provider.base_url = value; } });
      url.label.classList.add('field-full');
      const secret = inputField('API-ключ поставщика', provider.api_key || '', { type: 'password', maxLength: 8192, placeholder: provider.has_api_key ? 'Ключ сохранён · пустое поле оставляет прежний' : 'Введите ключ, если требуется поставщиком', hint: provider.has_api_key ? 'Сервер не раскрывает существующий ключ. Новое значение заменит его.' : 'Пустое поле не устанавливает и не удаляет ключ.', onInput: value => { provider.api_key = value; } });
      secret.input.dataset.providerSecret = provider.id;
      secret.label.classList.add('field-full');
      fields.append(name.label, priority.label, url.label, secret.label);
      const advanced = el('details', 'provider-routing'), advancedFields = el('div', 'fields-grid');
      advanced.append(el('summary', '', 'Маршрутизация OpenRouter'));
      const mode = selectField('Режим маршрутизации', provider.routing_mode || 'native', [['native', 'native · напрямую'], ['openrouter', 'openrouter · явная политика']], value => {
        provider.routing_mode = value;
        if (value === 'native') { provider.service_tier = null; provider.provider_pin = null; tier.select.value = ''; pin.input.value = ''; }
      });
      mode.select.dataset.providerRoutingMode = provider.id;
      const useOpenRouter = () => { provider.routing_mode = 'openrouter'; mode.select.value = 'openrouter'; };
      const tier = selectField('Service tier', provider.service_tier || '', [['', 'Не задан · без автоматического выбора'], ...['default', 'flex', 'priority', 'fast', 'ultrafast'].map(value => [value, value])], value => {
        provider.service_tier = value || null; if (value) useOpenRouter();
      });
      tier.select.dataset.providerServiceTier = provider.id;
      const pin = inputField('Точный endpoint slug', provider.provider_pin, { maxLength: 300, placeholder: 'Например, deepinfra/turbo', hint: 'Точное имя endpoint OpenRouter, не отображаемое имя поставщика.', onInput: value => { provider.provider_pin = value || null; if (value) useOpenRouter(); } });
      pin.input.dataset.providerPin = provider.id;
      advancedFields.append(mode.label, tier.label, pin.label);
      advanced.append(advancedFields, el('p', 'small', 'Только для OpenRouter. Закрепление строгое: provider.only и allow_fallbacks: false; fallback внутри OpenRouter выключен. Tier автоматически не выбирается. Переход в native очищает tier и endpoint перед сохранением.'));
      const bottom = el('div', 'provider-bottom'), result = el('span', 'test-result', 'Проверка /models без генерации');
      const test = action('Проверить подключение', 'secondary small-button', async () => {
        if (state.dirty || !state.config?.providers.some(item => item.id === provider.id)) { result.textContent = 'Сначала сохраните изменения.'; return; }
        const epoch = state.epoch; test.disabled = true; result.className = 'test-result'; result.textContent = 'Проверяем upstream /models…';
        try {
          const response = await api('/admin/providers/' + encodeURIComponent(provider.id) + '/test', { method: 'POST', timeout: 30000 });
          if (epoch !== state.epoch) return;
          result.className = 'test-result ' + (response.ok ? 'success' : 'error');
          result.textContent = (response.ok ? 'Доступен' : 'Проверка не пройдена') + ' · HTTP ' + (response.status ?? '—') + ' · ' + number(response.latency_ms) + ' мс' + (!response.ok && response.error_kind ? ' · ' + response.error_kind : '');
        } catch (error) { if (epoch === state.epoch) { result.className = 'test-result error'; result.textContent = errorMessage(error); } }
        finally { if (epoch === state.epoch) test.disabled = state.dirty || state.saving; }
      });
      test.dataset.providerTest = provider.id; test.disabled = state.dirty || !state.config?.providers.some(item => item.id === provider.id);
      bottom.append(test, result); card.append(heading, fields, advanced, bottom); return card;
    }));
    if (!state.draft.providers.length) $('provider-editor').append(el('div', 'panel empty', 'Первый шаг — добавьте поставщика. Затем создайте модель и её маршрут.'));
    updateConfigState();
  }
  function renderModels() {
    if (!state.draft) return;
    $('model-editor').replaceChildren(...state.draft.models.map(model => {
      const card = el('article', 'panel editor-card'), heading = el('div', 'editor-heading');
      const title = el('h3', '', model.name || 'Новая модель'), controls = el('div', 'editor-actions');
      controls.append(action('Удалить модель', 'subtle small-button', async () => {
        if (!await confirmAction('Удалить модель?', 'Алиас «' + (model.name || 'Новая модель') + '» и его маршруты будут удалены после сохранения конфигурации. Клиенты больше не смогут вызывать этот алиас.')) return;
        if (!state.draft) return;
        state.draft.models = state.draft.models.filter(item => item.id !== model.id); markDirty(); renderModels();
      }));
      heading.append(title, controls);
      const name = inputField('Имя модели / клиентский алиас', model.name, { required: true, maxLength: 200, placeholder: 'Например, assistant-fast', hint: 'Это имя приложение передаёт в поле model. Должно быть уникальным.', onInput: value => { model.name = value; title.textContent = value || 'Новая модель'; } });
      const options = el('div', 'model-options');
      options.append(toggle('Модель включена', model.enabled, value => { model.enabled = value; }), toggle('Поддержка изображений', model.supports_vision, value => { model.supports_vision = value; }));
      const metadata = el('div', 'fields-grid');
      for (const [key, label, min] of [['context_length', 'Размер контекста, токенов', 1000], ['max_output_tokens', 'Максимум выходных токенов', 1]]) {
        const field = inputField(label, model[key], { type: 'number', min, step: 1, placeholder: 'Не указан', hint: 'Целое число от ' + min + ' до 10000000; пустое поле — неизвестно.', onInput: value => { model[key] = value === '' ? null : value; } });
        field.input.max = 10000000;
        metadata.append(field.label);
      }
      const preflight = selectField('Предварительная оценка контекста', model.context_preflight || 'off', [['off', 'off · выключена'], ['observe', 'observe · только наблюдение']], value => { model.context_preflight = value; });
      preflight.select.dataset.modelContextPreflight = model.id;
      metadata.append(preflight.label);
      const metadataNote = el('p', 'small', 'Оценка не точный токенизатор: используется консервативная оценка по байтам. observe не отклоняет запросы. По умолчанию off; строгая блокировка по этой оценке недоступна. Размер контекста и максимум выхода — справочные метаданные, не гарантии upstream.');
      const routeHeading = el('div', 'route-heading');
      routeHeading.append(el('h3', '', 'Маршруты'), action('+ Маршрут', 'secondary small-button', () => {
        if (!state.draft.providers.length) { showNotice('Сначала добавьте поставщика на соответствующей вкладке.', 'warning'); return; }
        model.routes.push({ provider_id: state.draft.providers[0].id, upstream_model: '', enabled: true }); markDirty(); renderModels();
      }));
      const routes = el('div', 'route-list');
      model.routes.forEach((route, index) => {
        const row = el('div', 'route-row'), label = el('label', '', 'Поставщик'), select = el('select'); select.required = true;
        const empty = el('option', '', 'Выберите поставщика'); empty.value = ''; select.append(empty);
        for (const provider of state.draft.providers) {
          const option = el('option', '', (provider.name || 'Без названия') + ' · приоритет ' + provider.priority + (provider.enabled ? '' : ' · выключен'));
          option.value = provider.id; select.append(option);
        }
        select.value = route.provider_id;
        select.addEventListener('change', () => { route.provider_id = select.value; markDirty(); }); label.append(select);
        const upstream = inputField('Имя модели у поставщика', route.upstream_model, { required: true, maxLength: 300, placeholder: 'Например, gpt-4.1', onInput: value => { route.upstream_model = value; } });
        const remove = action('Удалить', 'subtle small-button', async () => {
          if (!await confirmAction('Удалить маршрут?', 'Маршрут модели «' + (model.name || 'Новая модель') + '» будет удалён из черновика. Применение — после сохранения.')) return;
          if (!state.draft) return;
          model.routes.splice(index, 1); markDirty(); renderModels();
        });
        row.append(label, upstream.label, toggle('Включён', route.enabled, value => { route.enabled = value; }), remove); routes.append(row);
      });
      if (!model.routes.length) routes.append(el('div', 'empty', 'Маршрутов нет. Добавьте хотя бы один для доступности модели.'));
      card.append(heading, name.label, options, metadata, metadataNote, routeHeading, routes, el('p', 'small', 'Порядок попыток определяется приоритетами поставщиков. Vision — декларация возможностей; upstream должен поддерживать изображения.'));
      return card;
    }));
    if (!state.draft.models.length) $('model-editor').append(el('div', 'panel empty', 'Моделей пока нет. Создайте клиентский алиас и добавьте маршруты к поставщикам.'));
    updateConfigState();
  }
  function renderGeneral() {
    if (!state.draft) return;
    $('general-fields').replaceChildren(...settingDefs.map(([key, label, hint, min, step]) => inputField(label, state.draft.settings[key], { type: 'number', required: true, min, step, hint, onInput: value => { state.draft.settings[key] = value; } }).label));
    $('trust-private').checked = state.draft.settings.trust_private_upstreams;
  }
  function configPayload() {
    return {
      revision: state.draft.revision,
      settings: { ...state.draft.settings },
      providers: state.draft.providers.map(provider => {
        const result = { id: provider.id, name: provider.name.trim(), base_url: provider.base_url.trim(), enabled: provider.enabled, priority: provider.priority, has_api_key: provider.has_api_key, ...optionalFields(provider, ['routing_mode', 'service_tier', 'provider_pin']) };
        if (provider.api_key?.trim()) result.api_key = provider.api_key;
        return result;
      }),
      models: state.draft.models.map(model => ({ id: model.id, name: model.name.trim(), supports_vision: model.supports_vision, enabled: model.enabled, context_length: model.context_length ?? null, max_output_tokens: model.max_output_tokens ?? null, ...optionalFields(model, ['context_preflight']), routes: model.routes.map(route => ({ provider_id: route.provider_id, upstream_model: route.upstream_model.trim(), enabled: route.enabled })) }))
    };
  }
  function validateConfig() {
    for (const input of $('config-form').querySelectorAll('input,select')) {
      if (!input.checkValidity()) {
        const section = input.closest('.settings-section');
        if (section) activateSettings(section.id.replace('settings-', ''), false);
        input.reportValidity(); return false;
      }
    }
    const names = new Set();
    for (const provider of state.draft.providers) {
      if (!provider.name.trim()) { activateSettings('providers', false); areaError('config-error', 'Название поставщика не может быть пустым.'); return false; }
      if ((provider.service_tier || provider.provider_pin) && provider.routing_mode !== 'openrouter') {
        activateSettings('providers', false); areaError('config-error', 'Service tier и endpoint требуют режима openrouter. Выберите openrouter или очистите эти поля.'); return false;
      }
      let url;
      try { url = new URL(provider.base_url.trim()); } catch { /* Native validation can accept protocols unsupported by the proxy. */ }
      if (!url || !['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
        activateSettings('providers', false); areaError('config-error', 'URL поставщика должен начинаться с http:// или https://, без логина, пароля, query-параметров и фрагмента.'); return false;
      }
    }
    for (const model of state.draft.models) {
      const name = model.name.trim();
      if (!name || names.has(name)) { activateSettings('models', false); areaError('config-error', 'Алиасы моделей должны быть непустыми и уникальными.'); return false; }
      names.add(name);
      if (model.enabled && !model.routes.some(route => route.enabled && state.draft.providers.some(provider => provider.id === route.provider_id && provider.enabled))) {
        activateSettings('models', false); areaError('config-error', 'У включённой модели «' + name + '» должен быть включённый маршрут к включённому поставщику.'); return false;
      }
      if (model.routes.some(route => !route.upstream_model.trim() || !state.draft.providers.some(provider => provider.id === route.provider_id))) {
        activateSettings('models', false); areaError('config-error', 'Проверьте имя upstream-модели и поставщика в каждом маршруте.'); return false;
      }
    }
    return true;
  }
  async function saveConfig(event) {
    event.preventDefault();
    if (!state.draft || !state.dirty || state.saving || state.configLoading) return;
    areaError('config-error', '');
    if (!validateConfig()) return;
    if (state.draft.settings.trust_private_upstreams && !state.config.settings.trust_private_upstreams) {
      if (!await confirmAction('Разрешить приватные upstream?', 'Это ослабляет защиту от SSRF и разрешает подключения к внутренним адресам. Включайте только для доверенной инфраструктуры.', 'Разрешить')) return;
    }
    const epoch = state.epoch;
    state.saving = true; $('config-fieldset').disabled = true; updateConfigState();
    try {
      const config = cleanConfig(await api('/admin/config', { method: 'PUT', body: configPayload() }));
      if (epoch !== state.epoch) return;
      clearProviderSecrets(); state.config = config; state.draft = clone(config); state.dirty = false; renderConfig();
      showNotice('Конфигурация сохранена. Секретные поля очищены; существующие ключи не раскрываются.');
    } catch (error) { if (epoch === state.epoch) areaError('config-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) { state.saving = false; $('config-fieldset').disabled = false; updateConfigState(); } }
  }

  // Client API keys are independent of the config revision. Tokens are shown once.
  function renderKeyModels() {
    const selected = new Set([...$('key-models').querySelectorAll('input:checked')].map(input => input.value));
    $('key-models').replaceChildren(...(state.config?.models || []).map(model => {
      const label = el('label'), input = el('input'); input.type = 'checkbox'; input.value = model.name; input.checked = selected.has(model.name);
      label.append(input, el('span', '', model.name)); return label;
    }));
    if (!state.config?.models.length) $('key-models').append(el('p', 'small', 'Сохранённых моделей пока нет. Пустой список разрешает все модели.'));
  }
  async function loadKeys() {
    if (!state.session || state.keysLoading) return;
    const epoch = state.epoch; state.keysLoading = true; $('refresh-keys').disabled = true; areaError('keys-error', '');
    try {
      const result = await api('/admin/keys');
      if (epoch !== state.epoch) return;
      state.keys = Array.isArray(result?.keys) ? result.keys : []; renderKeys();
    } catch (error) { if (epoch === state.epoch) areaError('keys-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) { state.keysLoading = false; $('refresh-keys').disabled = false; } }
  }
  function renderKeys() {
    $('keys-list').replaceChildren(...state.keys.map(key => {
      const card = el('article', 'panel editor-card key-row'), info = el('div', 'key-info'), meta = el('div', 'key-meta');
      meta.append(el('h3', '', key.name || 'Без названия'), badge(key.enabled ? 'АКТИВЕН' : 'ОТКЛЮЧЁН', key.enabled ? '' : 'neutral'));
      info.append(meta, el('p', 'key-prefix-label', 'Префикс ключа · полный ключ показывается только при создании'), el('code', 'key-prefix', key.prefix || '—'), el('p', '', 'Создан ' + date(key.created_at) + ' · Запросов: ' + number(key.requests)), el('p', '', 'Модели: ' + (key.models?.length ? key.models.join(', ') : 'все')));
      const actions = el('div', 'editor-actions');
      const toggleButton = action(key.enabled ? 'Отключить' : 'Включить', 'secondary small-button', async () => {
        if (key.enabled && !await confirmAction('Отключить ключ?', 'Приложения с ключом «' + (key.name || key.prefix) + '» потеряют доступ. Позже ключ можно включить.', 'Отключить')) return;
        const epoch = state.epoch; toggleButton.disabled = deleteButton.disabled = true;
        try {
          await api('/admin/keys/' + encodeURIComponent(key.id), { method: 'PATCH', body: { enabled: !key.enabled } });
          if (epoch !== state.epoch) return;
          showNotice(key.enabled ? 'Ключ отключён.' : 'Ключ включён.'); await loadKeys();
        } catch (error) { if (epoch === state.epoch) areaError('keys-error', errorMessage(error)); }
        finally { toggleButton.disabled = deleteButton.disabled = false; }
      });
      const deleteButton = action('Забыть', 'destructive small-button', async () => {
        if (!await confirmAction('Забыть ключ навсегда?', 'Ключ «' + (key.name || key.prefix) + '» будет удалён навсегда, доступ по нему будет отозван для всех приложений. Это не просто скрытие ключа из списка. Восстановить ключ и отменить действие нельзя.', 'Забыть навсегда')) return;
        const epoch = state.epoch; toggleButton.disabled = deleteButton.disabled = true;
        try {
          await api('/admin/keys/' + encodeURIComponent(key.id), { method: 'DELETE' });
          if (epoch !== state.epoch) return;
          showNotice('Ключ отозван без возможности восстановления.'); await loadKeys();
        } catch (error) { if (epoch === state.epoch) areaError('keys-error', errorMessage(error)); }
        finally { toggleButton.disabled = deleteButton.disabled = false; }
      });
      actions.append(toggleButton, deleteButton); card.append(info, actions); return card;
    }));
    if (!state.keys.length) $('keys-list').append(el('div', 'panel empty', 'Ключей пока нет. Создайте первый ключ для своего приложения.'));
  }
  // A6API marketplace: token choice, model text, cheapest sellers with cache filter.
  function usd(micros) { return typeof micros === 'number' && Number.isFinite(micros) ? '$' + (micros / 1e6).toFixed(6) : '—'; }
  // Managed pools: policy-only writes, explicit paid work, preview-bound publication.
  const poolFields = [
    ['name', 'Название пула', 'text', ''], ['token_id', 'Рабочий API-токен', 'token', ''],
    ['test_token_id', 'Тестовый токен1', 'token', ''],
    ['test_token_id_2', 'Тестовый токен2', 'token', ''],
    ['test_token_id_3', 'Тестовый токен3', 'token', ''],
    ['test_token_id_4', 'Тестовый токен4', 'token', ''],
    ['model', 'Модель A6API (точное имя)', 'text', ''],
    ['reference_model', 'Точный ожидаемый эталон', 'reference', ''],
    ['protocol', 'Протокол', 'protocol', 'chat'],
    ['max_input_usd', 'Макс. вход, USD / 1M', 'price', ''],
    ['max_output_usd', 'Макс. выход, USD / 1M', 'price', ''],
    ['max_cache_read_usd', 'Макс. чтение кеша, USD / 1M', 'price', ''],
    ['max_cache_write_usd', 'Макс. запись кеша, USD / 1M', 'price', ''],
    ['min_cache_hit_percent', 'Мин. cache hit за 24 ч, % (не цена)', 'percent', 40],
    ['min_success_percent', 'Мин. успешность, %', 'percent', 90],
    ['threshold_percent', 'Порог fingerprint, % (не доказательство)', 'percent', 90],
    ['verification_ttl_hours', 'Срок проверки, часы', 'integer', 24],
    ['refresh_minutes', 'Обновление кандидатов, минуты', 'integer', 10],
    ['max_candidates', 'Максимум кандидатов', 'integer', 10],
    ['max_members', 'Максимум участников', 'integer', 3],
    ['max_probe_requests', 'Бюджет платных запросов', 'integer', 60],
    ['max_probe_usd', 'Бюджет проверки, USD всего (не USD / 1M)', 'budget', 1],
    ['auto_daily_usd', 'Автоматический режим: USD / сутки', 'budget', 5],
    ['auto_daily_requests', 'Автоматический режим: запросов / сутки', 'integer', 600]
  ];
  const poolPath = id => '/admin/a6/pools/' + encodeURIComponent(id);
  const activePools = () => !!state.session && state.tab === 'market' && pools.tab === 'pools' && !document.hidden;
  function stopPoolsPolling() {
    clearTimeout(pools.timer); pools.timer = null;
    if (pools.controller) pools.controller.abort();
    pools.controller = null; pools.loading = false; pools.sequence++;
  }
  function schedulePoolsPolling() {
    clearTimeout(pools.timer);
    if (!activePools()) return;
    const running = (pools.data?.jobs || []).some(job => ['queued', 'running'].includes(job.status));
    text('pools-status', 'Обновление каждые ' + (running ? '5' : '15') + ' секунд · только открытая вкладка');
    pools.timer = setTimeout(() => void loadPools(), running ? 5000 : 15000);
  }
  function updatePoolsPolling() { stopPoolsPolling(); if (activePools()) void loadPools(); }
  function activateMarket() { pools.tab = 'pools'; updatePoolsPolling(); }
  const poolSections = ['overview', 'composition', 'checks', 'automation', 'settings'];
  function setPoolSection(section, focus = false) {
    if (!poolSections.includes(section)) return;
    pools.section = section;
    for (const key of poolSections) {
      const selected = key === section;
      $('pool-section-' + key).hidden = !selected;
      const button = $('pool-section-tab-' + key);
      button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1;
    }
    if (section === 'settings') $('pool-settings').open = true;
    if (section === 'checks' && $('pool-membership-history').open) void loadPoolEvents();
    if (focus) $('pool-section-tab-' + section).focus();
  }
  async function loadPools(clearError = false) {
    if (!activePools() || pools.loading) return;
    const epoch = state.epoch, sequence = ++pools.sequence;
    pools.loading = true; pools.controller = new AbortController();
    try {
      const data = await api('/admin/a6/pools', { signal: pools.controller.signal });
      if (epoch !== state.epoch || sequence !== pools.sequence) return;
      pools.data = data; if (clearError) areaError('pools-error', '');
      renderPools();
    } catch (error) {
      if (epoch === state.epoch && sequence === pools.sequence && error.name !== 'AbortError') areaError('pools-error', errorMessage(error));
    } finally {
      if (epoch === state.epoch && sequence === pools.sequence) { pools.loading = false; pools.controller = null; schedulePoolsPolling(); }
    }
  }
  function resetPoolEvents() {
    if (poolEvents.controller) poolEvents.controller.abort();
    poolEvents.sequence++; poolEvents.controller = null; poolEvents.id = null; poolEvents.data = null; poolEvents.loading = false;
    $('pool-events').replaceChildren(); text('pool-events-status', 'Выберите пул и откройте историю.');
    $('pool-events-refresh').disabled = !pools.selected;
  }
  function poolEventIds(values) {
    if (!Array.isArray(values)) return '—';
    const ids = values.map(value => value && typeof value === 'object' ? value.channel_id ?? value.id : value)
      .filter(value => typeof value === 'number' && Number.isSafeInteger(value) || typeof value === 'string' && /^[a-zA-Z0-9_.:-]{1,128}$/.test(value));
    return ids.length ? ids.join(', ') : 'нет';
  }
  function renderPoolEvents(events) {
    $('pool-events').replaceChildren(...events.map(event => {
      const row = el('article', 'pool-membership-event');
      row.append(el('strong', '', date(event.created_at)), el('p', 'small', 'Добавлены ID: ' + poolEventIds(event.added) + ' · удалены ID: ' + poolEventIds(event.removed)));
      if (typeof event.reason === 'string') row.append(el('p', 'small', 'Причина: ' + poolReason(event.reason)));
      if (typeof event.source === 'string') row.append(el('p', 'small', 'Источник: ' + event.source));
      if (poolEventIds([event.job_id]) !== 'нет') row.append(el('p', 'small', 'Задание ID: ' + poolEventIds([event.job_id])));
      return row;
    }));
    if (!events.length) $('pool-events').append(el('p', 'empty', 'Изменений состава в журнале пока нет.'));
  }
  async function loadPoolEvents(force = false) {
    if (!activePools() || pools.section !== 'checks' || !pools.selected) return;
    const id = pools.selected.id;
    if (poolEvents.id !== id) resetPoolEvents();
    if (poolEvents.loading || !force && poolEvents.id === id && poolEvents.data !== null) return;
    const epoch = state.epoch, sequence = ++poolEvents.sequence;
    poolEvents.id = id; poolEvents.loading = true; poolEvents.controller = new AbortController();
    $('pool-events-refresh').disabled = true; text('pool-events-status', 'Загрузка локальной истории…');
    try {
      const result = await api(poolPath(id) + '/events?limit=50', { signal: poolEvents.controller.signal });
      if (epoch !== state.epoch || sequence !== poolEvents.sequence || pools.selected?.id !== id) return;
      poolEvents.data = Array.isArray(result?.events) ? result.events.filter(event => event && typeof event === 'object').slice(0, 50) : [];
      renderPoolEvents(poolEvents.data); text('pool-events-status', 'Последние ' + poolEvents.data.length + ' событий · обновлено ' + date(new Date().toISOString()));
    } catch (error) {
      if (epoch === state.epoch && sequence === poolEvents.sequence && error.name !== 'AbortError') text('pool-events-status', 'История недоступна. ' + errorMessage(error));
    } finally {
      if (epoch === state.epoch && sequence === poolEvents.sequence) { poolEvents.loading = false; poolEvents.controller = null; $('pool-events-refresh').disabled = !pools.selected; }
    }
  }
  const poolTestTokenFields = ['test_token_id', 'test_token_id_2', 'test_token_id_3', 'test_token_id_4'];
  const poolTestTokenIds = policy => Array.isArray(policy.test_token_ids) ? policy.test_token_ids : policy.test_token_id != null ? [policy.test_token_id] : [];
  function renderPoolForm(policy = {}) {
    const testTokenIds = poolTestTokenIds(policy);
    $('pool-policy-fields').replaceChildren(...poolFields.map(([key, label, kind, fallback]) => {
      const lane = poolTestTokenFields.indexOf(key);
      const wrapper = el('label', '', label), value = lane >= 0 ? testTokenIds[lane] ?? '' : policy[key] ?? fallback;
      let input;
      if (['token', 'reference', 'protocol'].includes(kind)) {
        input = el('select');
        const options = kind === 'token' ? (pools.data?.tokens || []).map(t => [t.id, t.name || 'Без названия'])
          : kind === 'reference' ? [...(pools.data?.catalog || [])].sort((a, b) => String(a.display_name || a.id).localeCompare(String(b.display_name || b.id), ['ru', 'en'], { numeric: true, sensitivity: 'base' }) || String(a.id).localeCompare(String(b.id))).map(m => [m.id, (m.display_name || m.id) + ' · ' + m.id])
          : [['chat', 'Chat Completions'], ['responses', 'Responses (нужен готовый адаптер)']];
        if (kind !== 'protocol') { const empty = el('option', '', lane > 0 ? 'Не используется' : 'Выберите…'); empty.value = ''; input.append(empty); }
        for (const [id, name] of options) {
          const option = el('option', '', name); option.value = String(id);
          if (kind === 'token') {
            const token = (pools.data?.tokens || []).find(t => String(t.id) === String(id));
            const others = (pools.data?.pools || []).filter(p => p.id !== pools.selected?.id);
            const conflict = others.find(p => lane >= 0 ? String(p.token_id) === String(id) || poolTestTokenIds(p).some(t => String(t) === String(id)) : poolTestTokenIds(p).some(t => String(t) === String(id)));
            if (token?.status !== 1) { option.disabled = true; option.textContent += ' · отключён'; }
            else if (conflict) { option.disabled = true; option.textContent += ' · занят пулом «' + conflict.name + '»'; }
            else if (lane >= 0 && String(policy.token_id) === String(id)) { option.textContent += ' · рабочий ключ этого пула (нельзя для теста)'; }
          }
          input.append(option);
        }
        if (value !== '' && !options.some(([id]) => String(id) === String(value))) { const missing = el('option', '', 'Недоступен: ' + value); missing.value = String(value); input.append(missing); }
      } else {
        input = el('input'); input.type = ['text'].includes(kind) ? 'text' : 'number';
        if (kind === 'text') input.maxLength = key === 'name' ? 160 : 512;
        else { input.min = kind === 'integer' ? '1' : '0'; input.step = kind === 'integer' ? '1' : 'any'; if (kind === 'percent') input.max = '100'; }
      }
      input.id = 'pool-' + key; input.value = String(value); input.required = kind !== 'price' && lane <= 0;
      if (kind === 'price') input.placeholder = 'Без ограничения';
      wrapper.append(input); return wrapper;
    }));
    const note = el('p', 'small field-full', 'Выбирайте точный эталон из каталога. Если gpt-6.1 отсутствует, другой эталон (например gpt6-astra) не подтверждает gpt-6.1; совпадение названий/семейства недостаточно. От 1 до 4 тестовых токенов: до 4 кандидатов параллельно. Первый обязателен, остальные можно оставить пустыми. Для каждой линии нужен свой токен, отличный от рабочего и других тестовых. Бюджет запросов и USD общий для всех линий. Ценовые поля можно оставить пустыми — «Без ограничения». Бюджет проверки — расчётный предел, не гарантия фактических списаний; сервер ограничивает каждую пробу по цене снимка кандидата. У каждой линии отдельные маршрут и ценовые caps своего токена; очистка тестовых маршрутов обязательна.');
    const help = el('details', 'pool-form-help field-full');
    help.append(el('summary', '', 'Пояснения к эталону, ключам и ценам'), note);
    $('pool-policy-fields').append(help);
    $('pool-enabled').checked = policy.enabled === true;
    $('pool-auto-publish').checked = policy.auto_publish === true;
    text('pool-form-title', pools.selected ? 'Изменить политику' : 'Новый пул');
    text('pool-save', pools.selected ? 'Сохранить политику' : 'Создать пул');
    if (!pools.selected) setPoolSection('settings');
    pools.dirty = false; updatePoolControls();
  }
  function invalidatePoolPreview() { pools.preview = null; $('pool-preview-panel').hidden = true; $('pool-publish').disabled = true; }
  function updatePoolControls() {
    $('pool-fields').disabled = pools.busy;
    $('pool-new').disabled = pools.busy;
    const active = selectedPoolJobs().find(job => ['queued', 'running'].includes(job.status));
    for (const id of ['pool-discover', 'pool-verify', 'pool-preview']) $(id).disabled = pools.busy || pools.dirty || !pools.selected;
    $('pool-run').disabled = pools.busy || pools.dirty || !pools.selected || !!active;
    $('pool-restart').disabled = pools.busy || pools.dirty || !pools.selected;
    $('pool-delete').disabled = pools.busy || !pools.selected;
    $('pool-cancel').hidden = !active; $('pool-cancel').disabled = pools.busy;
    text('pool-run', active ? 'Проверка выполняется' : selectedPoolJobs().length ? 'Продолжить проверку' : 'Запустить проверку');
    $('pool-publish').disabled = pools.busy || pools.dirty || !pools.preview?.can_publish || Date.parse(pools.preview.expires_at) <= Date.now();
    for (const button of $('pool-bans').querySelectorAll('button')) button.disabled = pools.busy || pools.dirty;
    text('pool-form-state', pools.dirty ? 'Есть несохранённые правки. Сохраните политику перед действиями.' : pools.selected ? 'Ревизия ' + pools.selected.revision + ' · независимая политика' : 'Новый независимый пул · по умолчанию выключен');
  }
  async function selectPool(policy) {
    if (pools.busy) return;
    if (pools.dirty && !await confirmAction('Отбросить правки пула?', 'Несохранённые правки этой политики будут потеряны.', 'Отбросить')) return;
    pools.selected = policy; resetPoolEvents(); setPoolSection(policy ? 'overview' : 'settings'); $('pool-settings').open = !policy; invalidatePoolPreview(); renderPoolForm(policy || {}); renderPools();
  }
  function renderPools() {
    const data = pools.data || {}, policies = data.pools || [], caps = data.capabilities || {};
    text('pools-capabilities', 'Адресная атрибуция: ' + (caps.targeted_probe_verified === true ? 'подтверждена' : 'НЕ подтверждена') + ' · Ценовые caps маршрута: ' + (caps.route_caps_verified === true ? 'подтверждены' : 'НЕ подтверждены') + (caps.warning ? ' · ' + caps.warning : ''));
    $('pools-list').replaceChildren(...policies.map(policy => {
      const button = el('button', 'pool-card' + (policy.id === pools.selected?.id ? ' active' : ''));
      button.type = 'button'; button.disabled = pools.busy;
      button.append(el('strong', '', policy.name), el('span', 'small', policy.model + ' · ' + policy.protocol + ' · ' + poolStatusLabel(policy.status)), el('span', 'small', 'Участников: ' + (policy.members || []).length + ' · ' + (policy.enabled ? 'включён' : 'выключен') + ' · автоматический режим ' + (policy.auto_publish === true ? 'включён' : 'выключен')));
      button.addEventListener('click', () => void selectPool(policy)); return button;
    }));
    if (!policies.length) $('pools-list').append(el('p', 'empty', 'Пулов нет. Создайте независимую политику.'));
    if (!$('pool-policy-fields').children.length) renderPoolForm();
    if (pools.selected) {
      const latest = policies.find(p => p.id === pools.selected.id);
      if (latest && !pools.dirty && !pools.busy) {
        if (latest.revision !== pools.selected.revision) { pools.selected = latest; invalidatePoolPreview(); renderPoolForm(latest); }
        else pools.selected = latest;
      }
      if (!latest && !pools.busy) { pools.selected = null; invalidatePoolPreview(); if (!pools.dirty) renderPoolForm(); }
    }
    renderPoolDetails(); renderPoolJobs(); updatePoolControls();
    if (poolEvents.id !== null && poolEvents.id !== pools.selected?.id) resetPoolEvents();
    $('pool-events-refresh').disabled = !pools.selected || poolEvents.loading;
  }
  const poolStatusLabels = { draft: 'Черновик', published: 'Опубликован', active: 'Активен', ready: 'Кандидаты найдены', blocked: 'Маршрут заблокирован', syncing: 'Публикация состава', disabled: 'Выключен', error: 'Ошибка', failed: 'Ошибка', degraded: 'Требует внимания', empty: 'Нет участников', paused: 'Приостановлен' };
  const poolStatusLabel = status => poolStatusLabels[status] || 'Состояние не определено';
  function poolTone(status) {
    return ['verified', 'published', 'active', 'done', 'completed'].includes(status) ? 'success' : ['error', 'failed'].includes(status) ? 'danger' : ['suspect', 'expired', 'degraded'].includes(status) ? 'warning' : ['checking', 'running', 'queued'].includes(status) ? 'info' : 'neutral';
  }
  function poolChip(label, status) { return badge(label, 'pool-chip ' + poolTone(status)); }
  const poolVerificationLabels = { unverified: 'Не проверен', checking: 'Проверяется', verified: 'Проверен', suspect: 'Ниже порога', unknown: 'Не определено', expired: 'Срок истёк', skipped: 'Пропущен', error: 'Ошибка' };
  const poolPhaseLabels = { discovery: 'Поиск кандидатов', preflight: 'Предварительная проверка', testing: 'Проверка кандидатов', saving: 'Сохранение результатов', cleanup: 'Завершение и очистка', idle: 'Ожидание' };
  const poolJobWorkers = job => Array.isArray(job.workers) ? job.workers : [job];
  const poolJobStatusLabels = { queued: 'В очереди', running: 'Выполняется', done: 'Завершено', completed: 'Завершено', failed: 'Ошибка', error: 'Ошибка', cancelled: 'Отменено' };
  const poolJobKindLabels = { verify: 'Проверка', discover: 'Поиск', auto_refresh: 'Автоматический отбор и публикация (6ч)', auto_audit: 'Проверка участников (1ч)' };
  const poolWarningLabels = {
    a6_http_500: 'A6API вернул HTTP 500. Часть проверок могла не завершиться.',
    test_route_cleanup_uncertain: 'Очистка тестового маршрута не подтверждена. Требуется проверка его состояния.',
    test_route_requires_reconciliation: 'Тестовый маршрут требует сверки состояния перед повторной проверкой.',
    test_token_has_unmanaged_route: 'У тестового токена уже есть чужой маршрут. Выберите свободный токен; существующий маршрут автоматически не изменяется.'
  };
  function poolWarningLabel(value) {
    const kind = typeof value === 'string' ? value : value?.safe_kind || value?.error_kind;
    return poolWarningLabels[kind] || 'Часть проверки завершилась с предупреждением. Проверьте состояние тестовых маршрутов перед повторным запуском.';
  }
  function poolReason(value) {
    if (typeof value !== 'string') return '';
    if (poolWarningLabels[value]) return poolWarningLabels[value];
    if (value === 'detector_reference_updated') return 'База детектора обновлена. Результат получен прежней версией; требуется очередная штатная проверка.';
    if (value === 'pool_disabled') return 'Пул выключен: включите обновление политики/пул в форме и сохраните. Затем повторите Предпросмотр; публикация остаётся отдельным действием.';
    if (value === 'a6_search_snapshot_changed') return 'Каталог A6API изменился во время поиска. Проверка не началась; безопасно повторите запуск.';
    if (value === 'auto_remote_route_conflict') return 'Удалённый маршрут рабочего токена для этой модели конфликтует с автоматическим режимом. Автоматическая перезапись заблокирована. Один раз получите ручной Предпросмотр, проверьте изменения и подтвердите публикацию; затем автоматический режим сможет продолжить работу.';
    return value;
  }
  function poolCandidateStatus(row, policyId) {
    // Current work is transient: never persist a checking state in the policy.
    const checking = (pools.data?.jobs || []).some(job => job.pool_id === policyId && ['verify', 'auto_refresh', 'auto_audit'].includes(job.kind) && job.status === 'running' && poolJobWorkers(job).some(worker => worker.status === 'running' && worker.phase === 'testing' && worker.current_channel_id != null && String(worker.current_channel_id) === String(row.channel_id)));
    if (checking) return 'checking';
    // An interrupted/finished job must not leave a stale persisted checking label.
    return row.verification_status === 'checking' ? 'unverified' : row.verification_status || 'unverified';
  }
  function poolPrediction(row, status) {
    if (!['verified', 'suspect'].includes(status)) return '—';
    if (!Object.prototype.hasOwnProperty.call(row, 'predicted_model')) return 'Нет данных';
    if (typeof row.predicted_model !== 'string' || !row.predicted_model.trim()) return '—';
    const model = (pools.data?.catalog || []).find(item => item.id === row.predicted_model);
    const label = typeof model?.display_name === 'string' && model.display_name ? model.display_name : row.predicted_model;
    const probability = detectorProbability(row.predicted_probability);
    return (status === 'suspect' ? 'Возможное несоответствие: ' : '') + label + (probability === '—' ? '' : ' · ' + probability);
  }
  function poolTable(rows, policyId) {
    const table = el('table', 'pool-table'), head = el('thead'), headings = el('tr');
    for (const title of ['Канал / листинг', 'Продавец', 'Вход / выход USD / 1M', 'Кеш USD / 1M', 'Cache hit / успех', 'Допуск / причина', 'Проверка / время', 'Вероятность', 'Похож на модель']) { const th = el('th', '', title); th.scope = 'col'; headings.append(th); }
    head.append(headings); table.append(head); const body = el('tbody');
    for (const row of rows || []) {
      const tr = el('tr');
      for (const value of [String(row.channel_id ?? '—') + ' / ' + (row.listing_id ?? '—'), row.supplier ?? '—', usd(row.input_micros) + ' / ' + usd(row.output_micros), usd(row.cache_read_micros), percent(row.cache_hit_percent) + ' / ' + percent(row.success_percent), (row.eligible === true ? 'Да' : 'Нет') + (poolReason(row.reason) ? ' · ' + poolReason(row.reason) : '')]) tr.append(el('td', '', value));
      if (['string', 'number'].includes(typeof row.actual_channel_id)) tr.firstElementChild.title = 'Запрос направлен в канал: ' + row.actual_channel_id;
      const status = poolCandidateStatus(row, policyId), verification = el('td');
      tr.dataset.verification = status;
      const historical = ['unverified', 'expired'].includes(status) && typeof row.last_check_status === 'string' && row.last_check_status !== '';
      const waiting = historical && ['suspect', 'unknown', 'error'].includes(row.last_check_status);
      const nextCheck = typeof row.next_check_at === 'string' ? Date.parse(row.next_check_at) : NaN;
      const paused = waiting && Number.isFinite(nextCheck) && nextCheck > Date.now();
      const label = row.reason === 'detector_reference_updated' ? 'База обновлена' : paused ? 'Пауза до ' + date(row.next_check_at) : waiting && status === 'unverified' ? 'Ожидает повторной проверки' : poolVerificationLabels[status] || poolVerificationLabels.unknown;
      verification.append(poolChip(label, paused ? 'unverified' : status), el('span', 'pool-verified-at small', date(status === 'unverified' ? null : row.verified_at)));
      if (historical) {
        const previous = poolVerificationLabels[row.last_check_status] || poolVerificationLabels.unknown;
        const reason = poolReason(row.last_check_reason);
        verification.append(el('p', 'pool-check-history small', 'Прежняя проверка: ' + previous + ' · ' + date(row.last_checked_at) + (reason ? ' · ' + reason : '')),
          el('p', 'small', status === 'unverified' ? 'Прежний результат сброшен после изменения политики.' : 'Срок прежнего результата истёк; требуется повторная проверка.'));
      }
      if (row.technical_confirmation && typeof row.technical_confirmation.confirmed === 'boolean') verification.append(el('p', 'small', row.technical_confirmation.confirmed ? 'Технический сбой подтверждён повторной проверкой.' : 'Технический сбой перепроверен; повтор не подтвердил прежнюю ошибку.'));
      if (status === 'suspect') verification.title = 'Результат ниже порога качества. Это не ошибка запроса.';
      if (status === 'unknown') verification.title = 'Недостаточно данных для оценки качества.';
      if (['unknown', 'skipped', 'error'].includes(status)) {
        const reason = poolReason(row.verification_reason) || poolReason(row.error_kind) || poolReason(row.reason);
        if (reason) verification.append(el('p', 'small', reason));
      }
      const prediction = el('td', '', poolPrediction(row, status));
      prediction.title = 'На какой эталон похож ответ: вероятность (%) относится к закрытому каталогу и не доказывает подлинность модели.';
      tr.append(verification, el('td', '', ['unverified', 'expired'].includes(status) || row.probability == null ? '—' : number(row.probability)), prediction);
      body.append(tr);
    }
    if (!(rows || []).length) { const tr = el('tr'), td = el('td', 'empty', 'Пусто · маршрутизация закрыта, fallback выключен'); td.colSpan = 9; tr.append(td); body.append(tr); }
    table.append(body); return table;
  }
  function poolEpochDate(value) {
    if (value == null || value === '') return '—';
    const epoch = Number(value);
    return Number.isFinite(epoch) ? date(epoch * 1000) : '—';
  }
  function poolBillingAmount(value) {
    return value != null && Number.isFinite(Number(value)) && Number(value) >= 0 ? '$' + Number(value).toFixed(6) : '—';
  }
  function renderPoolBilling(billing, legacyReserve) {
    const area = el('section', 'pool-billing');
    area.setAttribute('aria-label', 'Фактические списания');
    if (!billing || typeof billing !== 'object' || Array.isArray(billing)) {
      area.append(el('p', 'small', 'Фактические списания пока не загружены'));
      if (legacyReserve != null) area.append(el('p', 'small', 'Старый расчётный резерв, не списание: ' + poolBillingAmount(legacyReserve)));
      return area;
    }
    area.append(el('p', 'small', 'Фактически списано (журнал A6API): ' + poolBillingAmount(billing.actual_cost_usd)),
      el('p', 'small', 'Ожидает подтверждения: ' + number(billing.pending_requests) + ' запросов · защитный резерв ' + poolBillingAmount(billing.pending_reserved_usd)),
      el('p', 'small', 'Подтверждено запросов: ' + number(billing.settled_requests) + ' / ' + number(billing.total_requests)));
    const reconcile = billing.reconciliation;
    if (Number(billing.pending_requests) > 0 && reconcile && typeof reconcile === 'object') {
      const details = el('details', 'pool-reconcile-diagnostics');
      details.append(el('summary', '', 'Диагностика неподтверждённых списаний'));
      const missing = Number(reconcile.missing_request_id), errors = Number(reconcile.error_only_requests), age = Number(reconcile.oldest_pending_age_s), next = Number(reconcile.next_reconcile_at);
      if (Number.isFinite(missing) && missing > 0) details.append(el('p', 'small', 'Без ID запроса: ' + number(missing) + ' — автоматическая сверка невозможна.'));
      if (Number.isFinite(errors) && errors > 0) details.append(el('p', 'small', 'Есть только журнал ошибки: ' + number(errors) + ' — списание не подтверждено.'));
      if (Number.isFinite(age) && age > 0) details.append(el('p', 'small', 'Самое старое ожидание: ' + number(Math.ceil(age / 60)) + ' мин.'));
      if (Number.isFinite(next) && next > 0) details.append(el('p', 'small', 'Следующая сверка: ' + poolEpochDate(next)));
      details.append(el('p', 'small', 'Паузы сверки: 1 → 5 → 15 → 60 мин; для записей старше суток — 6 ч. Частота проверок моделей не меняется. Резерв не является списанием и остаётся до подтверждения A6API.'));
      area.append(details);
    }
    if (billing.history_complete !== true) area.append(el('p', 'small', 'Учёт с обновления; прошлые проверки учтены не полностью. Прошлые проверки автоматически не добавлены; это не означает нулевые списания за всю историю.'));
    return area;
  }
  function poolRefreshWait(auto = {}) {
    if (!auto.refresh_not_before && !auto.refresh_reason) return '';
    const reasons = { cooldown_wait: 'пауза повторной проверки кандидатов', candidate_cooldown: 'пауза повторной проверки кандидатов', budget_exhausted: 'исчерпан бюджет', daily_budget_exhausted: 'исчерпан суточный бюджет', refill_scheduled: 'ожидание добора участников', target_reached: 'целевой состав набран', worker_error: 'ошибка линии проверки' };
    const reason = typeof auto.refresh_reason === 'string' ? reasons[auto.refresh_reason] || poolReason(auto.refresh_reason) : 'ограничение расписания';
    const notBefore = typeof auto.refresh_not_before === 'string' && !Number.isFinite(Number(auto.refresh_not_before)) ? date(auto.refresh_not_before) : poolEpochDate(auto.refresh_not_before);
    return 'Ожидание отбора: ' + reason + (auto.refresh_not_before ? ' · не раньше ' + notBefore : '') + '. Настроенная частота не изменена.';
  }
  function renderPoolAutoState(policy) {
    const auto = policy.auto_state || {}, area = $('pool-auto-state');
    area.replaceChildren(el('p', 'small', 'Автоматический режим: ' + (policy.auto_publish === true && policy.enabled === true ? 'включён' : 'выключен')),
      el('p', 'small', 'Следующий отбор / проверка / публикация: ' + poolEpochDate(auto.next_refresh_at) + ' · следующий аудит участников: ' + poolEpochDate(auto.next_audit_at)),
      el('p', 'small', 'Сутки: ' + (typeof policy.billing?.day === 'string' ? policy.billing.day : typeof auto.day === 'string' ? auto.day : '—') + ' · запросов использовано: ' + number(auto.daily_requests ?? 0) + ' / ' + number(policy.auto_daily_requests ?? 600) + ' · дневной лимит: ' + poolBillingAmount(policy.auto_daily_usd ?? 5)),
      renderPoolBilling(policy.billing, auto.daily_reserved_usd));
    if (poolRefreshWait(auto)) area.append(el('p', 'notice info', poolRefreshWait(auto)));
    if (policy.billing && typeof policy.billing === 'object') area.append(el('p', 'small', 'Бюджет: фактические списания ' + poolBillingAmount(policy.billing.actual_cost_usd) + ' + ожидающий защитный резерв ' + poolBillingAmount(policy.billing.pending_reserved_usd) + ' · лимит ' + poolBillingAmount(policy.auto_daily_usd ?? 5)));
    if (typeof auto.last_error === 'string' && auto.last_error) area.append(el('p', 'notice danger', 'Последняя ошибка автоматического режима: ' + poolReason(auto.last_error)));
    // Render only known safe remote fields, never arbitrary backend objects or credentials.
    if (auto.remote_state != null) {
      const remote = el('section'); remote.append(el('h4', '', 'Удалённое состояние'));
      if (['string', 'boolean', 'number'].includes(typeof auto.remote_state)) remote.append(el('p', 'small', auto.remote_state));
      else remote.append(previewSummary(auto.remote_state));
      area.append(remote);
    }
    const bans = Array.isArray(policy.bans) ? policy.bans : Array.isArray(auto.bans) ? auto.bans : [];
    const list = $('pool-bans'), expanded = list.querySelector('details')?.open === true;
    const history = el('details', 'pool-ban-history'); history.open = expanded;
    history.append(el('summary', '', 'Заблокировано каналов: ' + bans.filter(row => row.banned === true).length + ' · каналов с ошибками: ' + bans.filter(row => Number(row.failures) > 0).length + ' · записей счётчиков: ' + bans.length));
    const historyRows = el('div', 'pool-ban-history-rows');
    history.append(historyRows); list.replaceChildren(history);
    for (const row of bans) {
      const item = el('div', 'pool-ban');
      const id = ['string', 'number'].includes(typeof row.channel_id) ? row.channel_id : null;
      item.append(el('p', 'small', 'Канал ID ' + (id ?? '—') + ' · ошибок: ' + number(row.failures ?? 0) + ' / 4 · ' + (row.banned === true ? 'заблокирован' : 'не заблокирован')));
      const status = row.laststatus ?? row.last_status, reason = row.lastreason ?? row.last_reason;
      if (['string', 'number'].includes(typeof status)) item.append(el('p', 'small', 'Последний статус: ' + status));
      if (typeof reason === 'string' && reason) item.append(el('p', 'small', 'Причина: ' + poolReason(reason)));
      if (row.banned === true && id != null) {
        const unban = el('button', 'button secondary', 'Разблокировать канал ' + id); unban.type = 'button'; unban.disabled = pools.busy || pools.dirty;
        unban.addEventListener('click', () => void unbanPoolChannel(policy, id)); item.append(unban);
      }
      historyRows.append(item);
    }
  }
  async function unbanPoolChannel(policy, channelId) {
    if (pools.busy || pools.dirty || pools.selected?.id !== policy.id) return;
    const epoch = state.epoch;
    if (!await confirmAction('Разблокировать канал?', 'Канал ID ' + channelId + ' в пуле «' + policy.name + '»: блокировка и счётчик ошибок будут сброшены. В автоматическом режиме канал снова сможет участвовать в платных проверках и автоматической публикации в пределах бюджетов.', 'Разблокировать')) return;
    if (epoch !== state.epoch || pools.busy || pools.dirty || pools.selected?.id !== policy.id) return;
    await poolMutation(() => api(poolPath(policy.id) + '/unban', { method: 'POST', body: { channel_id: channelId } }), () => showNotice('Канал разблокирован. Состояние обновлено.'));
  }
  function renderPoolSummary(policy) {
    let summary = $('pool-summary');
    if (!summary) {
      summary = el('div', 'pool-summary'); summary.id = 'pool-summary';
      summary.setAttribute('aria-label', 'Сводка пула');
      $('pool-details').insertBefore(summary, $('pool-last-error'));
    }
    const candidates = policy.candidates || [], auto = policy.auto_state || {};
    const checked = candidates.filter(row => ['verified', 'suspect', 'unknown', 'error'].includes(poolCandidateStatus(row, policy.id))).length;
    const stats = [
      ['Опубликовано участников', number((policy.members || []).length), 'Рабочий маршрут'],
      ['Проверены хорошие', number(candidates.filter(row => row.verification_status === 'verified').length), 'В каталоге, не только за последний запуск'],
      ['Проверено кандидатов', number(checked), 'Каталог: включая прежние результаты и ошибки'],
      ['Кандидаты', number(policy.discovery_summary?.eligible ?? candidates.filter(row => row.eligible === true).length), 'Подходят под ограничения'],
      ['Следующий отбор', policy.enabled === true && policy.auto_publish === true ? poolEpochDate(auto.next_refresh_at) : 'Выключен', 'Аудит: ' + poolEpochDate(auto.next_audit_at)]
    ];
    summary.replaceChildren(...stats.map(([label, value, hint]) => {
      const card = el('div', 'pool-summary-card');
      card.append(el('span', 'small', label), el('strong', '', value), el('span', 'small', hint)); return card;
    }));
  }
  function renderPoolSearch(job) {
    const search = job.search && typeof job.search === 'object' ? job.search : {};
    if (!Object.keys(search).length && !job.result?.stop_reason && job.result?.search_exhausted !== true) return null;
    const area = el('section', 'pool-search'); area.setAttribute('aria-label', 'Прогрессивный поиск');
    area.append(el('strong', '', 'Поиск до достижения цели'));
    const metrics = el('div', 'pool-search-metrics');
    for (const [key, label] of [['eligible_total', 'Подходят'], ['attempted', 'Проверено'], ['remaining', 'Осталось'], ['target_members', 'Цель участников'], ['verified', 'Проверены хорошие'], ['cooldown_count', 'Пауза 24 ч'], ['cached_verified', 'Действующие проверки']]) {
      const item = el('span'); item.append(el('span', 'small', label + ': '), el('strong', '', number(search[key]))); metrics.append(item);
    }
    area.append(metrics);
    const reasons = { worker_error: 'Поиск остановлен из-за ошибки линии проверки. Это не означает, что весь каталог проверен. Проверьте причину ошибки; затем продолжите проверку.', cooldown_wait: 'Новые кандидаты закончились. Повторная проверка после паузы 24 ч; блокировка после 4 последовательных ошибок сохраняется.', target_reached: 'Цель достигнута: достаточно проверенных участников.', budget_exhausted: 'Бюджет исчерпан: поиск остановлен до проверки всего каталога.', catalogue_exhausted: 'Каталог исчерпан: все подходящие кандидаты рассмотрены.' };
    const reason = reasons[job.result?.stop_reason];
    if (reason) area.append(el('p', 'small', reason));
    else if (typeof job.result?.stop_reason === 'string' && job.result.stop_reason) area.append(el('p', 'small', 'Причина остановки: ' + poolReason(job.result.stop_reason)));
    else if (search.exhausted === true || job.result?.search_exhausted === true) area.append(el('p', 'small', reasons.catalogue_exhausted));
    else if (['queued', 'running'].includes(job.status)) area.append(el('p', 'small', 'Поиск продолжается; результаты появляются по мере проверки.'));
    return area;
  }
  function selectedPoolJobs() {
    return (pools.data?.jobs || []).filter(job => job.pool_id === pools.selected?.id).slice().reverse().sort((a, b) => (Date.parse(b.created_at || b.started_at || '') || 0) - (Date.parse(a.created_at || a.started_at || '') || 0));
  }
  function renderPoolWorkflow(policy) {
    const active = selectedPoolJobs().find(job => ['queued', 'running'].includes(job.status));
    const auto = policy.enabled === true && policy.auto_publish === true, next = policy.auto_state?.next_refresh_at;
    const configured = poolTestTokenIds(policy).length;
    const workers = active ? poolJobWorkers(active) : [];
    const working = workers.filter(worker => worker.status === 'running' && worker.phase === 'testing').length;
    text('pool-workflow-publish', auto ? 'Автоматически после успешной проверки; только в пределах бюджетов.' : 'Вручную: предпросмотр, затем подтверждение. Проверка сама не публикует.');
    const noVerified = !(policy.candidates || []).some(row => row.verification_status === 'verified') && !(policy.members || []).length;
    text('pool-current-state', active ? 'Сейчас: ' + (poolJobStatusLabels[active.status] || 'Проверка') + ' · ' + (poolPhaseLabels[active.phase] || 'Подготовка') + '. Прогресс: ' + number(active.progress ?? 0) + ' / ' + number(active.total) + '.' : 'Сейчас проверка не выполняется. ' + (auto ? 'Следующий автоматический отбор: ' + poolEpochDate(next) + '.' : 'Следующий запуск — вручную кнопкой ниже.') + (noVerified ? ' Нет проверенных участников: рабочий маршрут закрыт, пока не опубликован безопасный состав.' : '') + (policy.auto_state?.last_error || policy.last_error ? ' Последний запуск требует внимания; причина ниже.' : ''));
    if (!active && auto && poolRefreshWait(policy.auto_state)) text('pool-current-state', $('pool-current-state').textContent + ' ' + poolRefreshWait(policy.auto_state));
    text('pool-worker-state', 'Выбрано тестовых ключей: ' + configured + ' / 4. Сейчас проверяют кандидатов: ' + working + ' / ' + configured + '. Выбранные ключи не означают активные запросы.');
  }
  function renderPoolDetails() {
    const policy = pools.selected; $('pool-details').hidden = !policy; if (!policy) return;
    text('pool-detail-name', policy.name); text('pool-detail-status', poolStatusLabel(policy.status));
    $('pool-detail-status').className = 'badge pool-chip ' + poolTone(policy.status);
    renderPoolSummary(policy);
    renderPoolWorkflow(policy);
    renderPoolAutoState(policy);
    text('pool-detail-meta', policy.model + ' · эталон ' + policy.reference_model + ' · ' + policy.protocol + ' · обнаружено ' + date(policy.last_discovered_at) + ' · синхронизация ' + date(policy.last_sync_at));
    const latestJob = selectedPoolJobs()[0];
    const failedBeforeChecks = latestJob && ['failed', 'error'].includes(latestJob.status) && (latestJob.progress ?? 0) === 0;
    const lastError = poolReason(policy.last_error) || (failedBeforeChecks ? poolReason(latestJob.error_kind) || 'Предварительная проверка не пройдена.' : '');
    areaError('pool-last-error', lastError + (failedBeforeChecks ? ' Проверки кандидатов не выполнялись; задание остановлено.' : ''));
    const eligibleCandidates = (policy.candidates || []).filter(row => row.eligible === true);
    const summary = policy.discovery_summary;
    const candidatesArea = $('pool-candidates');
    const note = el('p', 'small', summary ? 'Всего: ' + number(summary.total) + ' · подходят: ' + number(summary.eligible) + ' · отсеяно: ' + number(summary.excluded) + ' · показано: ' + number(eligibleCandidates.length) : 'Показаны только продавцы, подходящие под ограничения пула.');
    $('pool-members').replaceChildren(poolTable(policy.members, policy.id));
    if (!(policy.members || []).length && eligibleCandidates.some(row => row.verification_status === 'verified')) $('pool-members').prepend(el('p', 'small', 'Кандидаты проверены, рабочий маршрут ещё не опубликован'));
    candidatesArea.replaceChildren(note, el('p', 'small', '«Похож на модель» — оценка сходства ответа с эталоном. Вероятность (%) относится к закрытому каталогу и не доказывает подлинность модели; «Нет данных» — старый результат без предсказания.'), poolTable(eligibleCandidates, policy.id));
  }
  function renderPoolJobs() {
    const expanded = $('pool-jobs').querySelector('details')?.open === true;
    const jobs = selectedPoolJobs();
    const cards = jobs.map(job => {
      const card = el('div', 'detector-job pool-job');
      const active = ['queued', 'running'].includes(job.status);
      const warnings = Array.isArray(job.warnings) ? job.warnings : [];
      const done = ['done', 'completed'].includes(job.status);
      const statusLabel = done && (job.result?.partial || warnings.length) ? 'Завершено с предупреждениями' : poolJobStatusLabels[job.status] || 'Не определено';
      card.append(el('strong', '', (poolJobKindLabels[job.kind] || 'Задание') + ' · ' + statusLabel), el('p', 'small', 'Пул ' + job.pool_id + ' · ' + (job.progress ?? 0) + ' / ' + (job.total ?? '—')));
      const jobProgress = el('progress', 'pool-progress');
      jobProgress.setAttribute('aria-label', 'Прогресс задания');
      if (Number(job.total) > 0) { jobProgress.max = Number(job.total); jobProgress.value = Math.max(0, Number(job.progress) || 0); }
      card.append(jobProgress, renderPoolBilling(job.billing, job.reserved_cost_usd ?? job.result?.reserved_cost_usd));
      if (job.updated_at || job.last_activity_at) card.append(el('p', 'small', 'Обновлено: ' + date(job.updated_at) + ' · последняя активность: ' + date(job.last_activity_at)));
      const diagnostics = job.api_diagnostics || pools.data?.api_diagnostics;
      if (diagnostics && typeof diagnostics === 'object') card.append(el('p', 'small', 'A6API с начала процесса: запросов ' + number(diagnostics.requests) + ' · ограничения/недоступность 429/503: ' + number(diagnostics.rate_limited) + ' · последний HTTP ' + number(diagnostics.last_status) + ' · активных ' + number(diagnostics.inflight) + ' / ' + number(diagnostics.max_inflight) + (diagnostics.cooldown_until ? ' · пауза до ' + poolEpochDate(diagnostics.cooldown_until) : '')));
      if (diagnostics?.endpoints && typeof diagnostics.endpoints === 'object') {
        const details = el('details', 'pool-endpoint-diagnostics'); details.append(el('summary', '', 'Обращения к A6API по назначению'));
        const labels = {tokens:'Список токенов',marketplace:'Каталог продавцов',subscriptions:'Маршруты',pins:'Закрепления',logs:'Журналы списаний',status:'Параметры аккаунта',other:'Прочее'};
        for (const [key, label] of Object.entries(labels)) {
          const metric = diagnostics.endpoints[key]; if (!metric || typeof metric !== 'object') continue;
          const count = Number(metric.requests), duration = Number(metric.total_duration_s);
          details.append(el('p', 'small', label + ': ' + number(count) + ' запросов · ошибок ' + number(metric.errors) + ' · HTTP 5xx ' + number(metric.http_5xx) + ' · среднее ' + (Number.isFinite(count) && count > 0 && Number.isFinite(duration) ? number(duration / count) : '—') + ' с'));
        }
        card.append(details);
      }
      const search = renderPoolSearch(job); if (search) card.append(search);
      if (job.result?.refill_scheduled === true) card.append(el('p', 'small', 'Недостаточно участников: запланирован добор. Расписание следующего отбора — в состоянии пула.'));
      if (job.result && ['verify', 'auto_refresh', 'auto_audit'].includes(job.kind)) {
        const result = job.result, count = value => Array.isArray(value) ? number(value.length) : number(value);
        const runFields = [['checked_this_run', 'Проверено в этом запуске'], ['passed_this_run', 'Прошли в этом запуске'], ['cached_verified', 'Свежие прежние проверки'], ['published_members', 'Опубликовано участников'], ['removed_members', 'Удалено участников']];
        if (runFields.some(([key]) => result[key] != null)) {
          card.append(el('p', 'small pool-run-result', runFields.map(([key, label]) => label + ': ' + count(result[key])).join(' · ')));
          card.append(el('p', 'small', 'Прежние действующие проверки не являются новыми пробами. Состав рабочего маршрута — отдельно от результатов этого запуска.'));
        } else {
          card.append(el('p', 'small', [['candidates', 'Кандидатов'], ['verified', 'Проверено хороших (включая прежние результаты)'], ['suspect', 'Ниже порога'], ['unknown', 'Не определено'], ['skipped', 'Пропущено'], ['unprocessed', 'Не обработано']].map(([key, label]) => label + ': ' + number(result[key])).join(' · ')));
          card.append(el('p', 'small', 'Раздельные счётчики этого запуска недоступны; итог не равен числу новых проверок.'));
        }
        if (done) card.append(el('p', 'small', (pools.data?.pools || []).find(policy => policy.id === job.pool_id)?.auto_publish === true ? 'Автоматический режим включён; результат публикации и расписание см. в состоянии пула.' : 'Для создания рабочего маршрута нажмите Предпросмотр, затем Опубликовать; проверка сама не публикует.'));
      }
      if (warnings.length) {
        const warningArea = el('section', 'pool-job-warnings'); warningArea.append(el('strong', '', 'Предупреждения'));
        for (const warning of warnings) warningArea.append(el('p', 'small', poolWarningLabel(warning)));
        card.append(warningArea);
      }
      if (active && poolPhaseLabels[job.phase]) card.append(el('p', 'small', poolPhaseLabels[job.phase]));
      const workers = poolJobWorkers(job);
      if (job.kind === 'verify') {
        const testing = active ? workers.filter(worker => worker.status === 'running' && worker.phase === 'testing').length : 0;
        card.append(el('p', 'small', 'Параллельность: ' + testing + ' / ' + workers.length + ' · общий прогресс и бюджет'));
      }
      const workerGrid = el('div', 'pool-worker-grid');
      for (const [index, worker] of workers.entries()) {
        const details = el('div', 'pool-worker');
        if (Array.isArray(job.workers)) {
          const tokenId = ['number', 'string'].includes(typeof worker.token_id) ? worker.token_id : '—';
          details.append(el('p', 'small', 'Линия ' + (index + 1) + ' · ID токена: ' + tokenId + ' · ' + (poolJobStatusLabels[worker.status] || '—') + ' · ' + (poolPhaseLabels[worker.phase] || '—')));
        }
        if (active && worker.status === 'running' && worker.phase === 'testing' && worker.current_channel_id != null) details.append(el('p', 'small', 'Сейчас: канал ' + worker.current_channel_id + (typeof worker.current_supplier === 'string' && worker.current_supplier ? ' · ' + worker.current_supplier : '')));
        const stages = {confirming_failure: 'Подтверждающая проверка технического сбоя', catalogue: 'Обновление каталога', connecting: 'Соединение', streaming: 'Получение ответа', billing: 'Сверка списания', attribution: 'Проверка канала', scoring: 'Оценка результата', reserving: 'Резерв бюджета'};
        if (active && worker.request_stage) details.append(el('p', 'small', 'Этап запроса: ' + (stages[worker.request_stage] || String(worker.request_stage)) + (worker.challenge_id != null ? ' · проба ' + String(worker.challenge_id) : '') + ' · активность ' + date(worker.last_activity_at)));
        if (worker !== job && worker.error_kind) details.append(el('p', 'small', poolWarningLabel(worker.error_kind)));
        if (worker !== job) details.append(poolChip(poolJobStatusLabels[worker.status] || 'Не определено', worker.status));
        if (Number(worker.total) > 0) {
          const progress = el('progress', 'pool-progress'); progress.max = Number(worker.total); progress.value = Math.max(0, Number(worker.progress) || 0); progress.setAttribute('aria-label', 'Прогресс линии ' + (index + 1)); details.append(progress);
        }
        if (details.children.length) workerGrid.append(details);
      }
      if (workerGrid.children.length) card.append(workerGrid);
      if (poolReason(job.error_kind)) card.append(el('p', 'small', poolReason(job.error_kind)));
      if (!active && ['failed', 'error'].includes(job.status) && (job.progress ?? 0) === 0) card.append(el('p', 'small', 'Проверки кандидатов не выполнялись; задание остановлено.'));
      if (['queued', 'running'].includes(job.status)) {
        const cancel = el('button', 'button secondary', 'Отменить задание'); cancel.type = 'button'; cancel.disabled = pools.busy;
        cancel.addEventListener('click', () => void poolMutation(() => api('/admin/a6/pools/jobs/' + encodeURIComponent(job.id) + '/cancel', { method: 'POST', body: {} })));
        card.append(cancel);
      }
      return card;
    });
    const recent = cards.slice(0, 3), older = cards.slice(3);
    $('pool-jobs').replaceChildren(...recent);
    if (older.length) {
      const history = el('details', 'pool-job-history'); history.open = expanded;
      history.append(el('summary', '', 'Вся история выбранного пула · ещё ' + older.length), ...older);
      $('pool-jobs').append(history);
    }
    if (!jobs.length) $('pool-jobs').append(el('p', 'empty', pools.selected ? 'У выбранного пула заданий пока нет.' : 'Выберите пул для просмотра заданий.'));
  }
  function readPoolPolicy() {
    const policy = {};
    for (const [key, , kind] of poolFields) {
      if (poolTestTokenFields.includes(key)) continue;
      const value = $('pool-' + key).value.trim();
      if (kind === 'price' && !value) { policy[key] = null; continue; }
      if (!value) throw new Error('Заполните обязательные поля политики, включая отдельный тестовый токен и бюджеты. Ценовые ограничения можно оставить пустыми.');
      if (['price', 'budget', 'integer', 'percent'].includes(kind)) {
        policy[key] = Number(value);
        if (kind === 'price' && (!Number.isFinite(policy[key]) || policy[key] <= 0)) throw new Error('Укажите положительное ценовое ограничение или оставьте поле пустым: без ограничения.');
        if (!Number.isFinite(policy[key]) || policy[key] < 0 || (kind === 'integer' && (!Number.isInteger(policy[key]) || policy[key] < 1)) || (kind === 'percent' && policy[key] > 100)) throw new Error('Проверьте числовые ограничения политики.');
      } else if (kind === 'token') policy[key] = /^\d+$/.test(value) ? Number(value) : value;
      else policy[key] = value;
    }
    const testTokenValues = poolTestTokenFields.map(key => $('pool-' + key).value.trim());
    if (!testTokenValues[0]) throw new Error('Выберите первый тестовый токен.');
    const selectedTokens = testTokenValues.filter(Boolean);
    if (new Set(selectedTokens).size !== selectedTokens.length) throw new Error('Тестовые токены не должны повторяться: выберите разный токен для каждой линии.');
    if (selectedTokens.some(value => String(policy.token_id) === value)) throw new Error('Выберите отдельные тестовые токены, отличные от рабочего.');
    policy.test_token_ids = selectedTokens.map(value => /^\d+$/.test(value) ? Number(value) : value);
    policy.test_token_id = policy.test_token_ids[0];
    if (policy.max_members > policy.max_candidates) throw new Error('Участников не может быть больше кандидатов.');
    policy.enabled = $('pool-enabled').checked; policy.auto_publish = $('pool-auto-publish').checked;
    if (policy.auto_publish && !policy.enabled) throw new Error('Для автоматического режима сначала отметьте «Включить обновление этой политики», затем сохраните. Политика не включается автоматически.');
    return policy;
  }
  async function poolMutation(work, success) {
    if (!state.session || pools.busy) return;
    const epoch = state.epoch; stopPoolsPolling(); pools.busy = true; areaError('pools-error', ''); updatePoolControls(); renderPoolJobs();
    try {
      const result = await work(); if (epoch !== state.epoch) return;
      if (success) success(result); invalidatePoolPreview();
    } catch (error) {
      if (epoch === state.epoch) areaError('pools-error', (error.status === 409 || error.status === 412) && (!error.code || error.code === 'revision_conflict') ? 'Политика изменена в другой сессии. Правки сохранены в форме; обновите данные и сбросьте правки для загрузки новой ревизии. Автоперезаписи нет.' : errorMessage(error));
    } finally {
      if (epoch === state.epoch) { pools.busy = false; updatePoolControls(); await loadPools(); }
    }
  }
  function poolBudgetRisk(policy) {
    return ['max_input_usd', 'max_output_usd', 'max_cache_read_usd', 'max_cache_write_usd'].some(key => policy[key] == null || policy[key] === 0)
      ? ' Часть ценовых ограничений не задана: без ограничения. Бюджет — расчётный предел по снимку цен, не гарантия фактического списания. Сервер задаёт отдельный ценовой предел каждой пробы по фактическому кандидату; при неизвестной цене безопасный платный запуск невозможен.' : '';
  }
  async function savePool(event) {
    event.preventDefault(); if (!state.session || pools.busy) return;
    let policy; try { policy = readPoolPolicy(); } catch (error) { areaError('pools-error', error.message); return; }
    const selected = pools.selected, epoch = state.epoch;
    const budgetIncreased = [['auto_daily_usd', 5], ['auto_daily_requests', 600], ['max_probe_usd', 1], ['max_probe_requests', 60]].some(([key, fallback]) => policy[key] > Number(selected?.[key] ?? fallback));
    if (policy.auto_publish && (selected?.auto_publish !== true || selected?.enabled !== true || budgetIncreased)) {
      if (!await confirmAction('Разрешить автоматический режим и расходы?', 'Пул «' + policy.name + '»: постоянные автоматические платные запросы — отбор, проверка и публикация каждые 6 часов; аудит участников каждый час. Суточный предел: ' + policy.auto_daily_requests + ' запросов и ' + policy.auto_daily_usd + ' USD. Разовый предел проверки: ' + policy.max_probe_requests + ' запросов и ' + policy.max_probe_usd + ' USD. Режим автоматически изменяет удалённый маршрут рабочего токена ID ' + policy.token_id + ' для модели «' + policy.model + '» без отдельного подтверждения каждой публикации. Закрепления (pins) не используются; fallback выключен, пустой пул закрыт. Запросы продолжаются до отключения режима в пределах обоих бюджетов.' + poolBudgetRisk(policy), 'Разрешить режим и расходы')) return;
      if (epoch !== state.epoch || pools.selected?.id !== selected?.id || pools.selected?.revision !== selected?.revision || pools.busy) return;
      try { if (JSON.stringify(readPoolPolicy()) !== JSON.stringify(policy)) { areaError('pools-error', 'Поля изменились во время подтверждения. Проверьте значения и сохраните ещё раз.'); return; } } catch (error) { areaError('pools-error', error.message); return; }
    }
    await poolMutation(() => api(selected ? poolPath(selected.id) : '/admin/a6/pools', { method: selected ? 'PUT' : 'POST', body: selected ? { ...policy, revision: selected.revision } : policy }), result => {
      pools.selected = result; renderPoolForm(result); showNotice(policy.auto_publish ? 'Политика сохранена. Автоматический режим включён: дальнейшие проверки платные, публикации автоматические в пределах бюджетов.' : 'Политика сохранена. Публикация не выполнялась.');
    });
  }
  async function poolAction(kind) {
    if (!pools.selected || (pools.dirty && kind !== 'delete') || pools.busy) return;
    const selected = pools.selected, epoch = state.epoch;
    if (kind === 'restart' && !await confirmAction('Перезапустить платную проверку?', 'Текущее задание пула «' + selected.name + '» будет отменено, тестовые маршруты безопасно закрыты. Затем запускается новый цикл в прежнем ручном или автоматическом режиме. Свежие успешные результаты сохраняются, продавцы на паузе 24 часа не проверяются повторно. Лимиты: ' + selected.max_probe_requests + ' запросов и ' + selected.max_probe_usd + ' USD за запуск; в авторежиме действуют и суточные лимиты. Возможна автоматическая публикация, если она включена.' + poolBudgetRisk(selected), 'Перезапустить и подтвердить расходы')) return;
    if (kind === 'run' && !await confirmAction('Запустить платную проверку?', 'Пул «' + selected.name + '»: до ' + selected.max_probe_requests + ' запросов и ' + selected.max_probe_usd + ' USD за запуск, бюджет общий для ' + poolTestTokenIds(selected).length + ' линий. ' + (selected.auto_publish === true && selected.enabled === true ? 'Автоматический отбор и публикация включены: запуск учитывает также суточные лимиты ' + selected.auto_daily_requests + ' запросов и ' + selected.auto_daily_usd + ' USD, может изменить рабочий маршрут без отдельной публикации.' : 'Ручная проверка не публикует маршрут. После проверки нужен предпросмотр и подтверждение публикации.') + ' Фактические списания показываются отдельно от защитного резерва.' + poolBudgetRisk(selected), 'Подтвердить расходы')) return;
    if (kind === 'verify' && !await confirmAction('Запустить платную проверку пула?', 'Пул «' + selected.name + '»: максимум ' + selected.max_probe_requests + ' запросов, бюджет ' + selected.max_probe_usd + ' USD общий для всех линий. До ' + poolTestTokenIds(selected).length + ' кандидатов параллельно, ID тестовых токенов: ' + poolTestTokenIds(selected).join(', ') + '. Каждая линия использует отдельный токен, свой маршрут и ценовые caps; очистка тестовых маршрутов обязательна. Этот запуск не публикует рабочий маршрут; публикация — отдельным действием или следующим автоматическим запуском. Fingerprint не доказывает подлинность; адресная атрибуция может быть недоступна.' + poolBudgetRisk(selected), 'Подтвердить расходы')) return;
    if (kind === 'delete' && !await confirmAction('Удалить пул?', 'Пул «' + selected.name + '»: автоматические проверки остановятся, активные задания будут отменены. Рабочий маршрут будет безопасно закрыт: без участников, fallback выключен. Только после подтверждения безопасного состояния удаляется локальная политика. Подписки, каналы и токены A6API не удаляются. Несохранённые правки будут потеряны.', 'Удалить пул')) return;
    if (epoch !== state.epoch || pools.selected?.id !== selected.id || pools.selected?.revision !== selected.revision || (pools.dirty && kind !== 'delete')) return;
    await poolMutation(() => api(poolPath(selected.id) + (kind === 'delete' ? '' : '/' + kind), { method: kind === 'delete' ? 'DELETE' : 'POST', body: kind === 'delete' ? { revision: selected.revision, confirm_remote: true } : ['verify', 'run', 'restart'].includes(kind) ? { confirm_cost: true } : {} }), () => {
      if (kind === 'delete') { pools.selected = null; renderPoolForm(); }
      showNotice(kind === 'delete' ? 'Пул удалён. Автоматические проверки остановлены, рабочий маршрут безопасно закрыт. Подписки и токены не удалялись.' : kind === 'run' && selected.auto_publish === true && selected.enabled === true ? 'Автоматический запуск принят: проверка и публикация в пределах бюджетов.' : 'Задание принято. Публикация не выполнялась.');
    });
  }
  function previewSummary(value) {
    // Never stringify backend objects: remote state may contain credentials.
    const summary = el('div');
    for (const [key, label] of [['token_id', 'ID токена'], ['model_name', 'Модель'], ['model', 'Модель'], ['fallback_enabled', 'Fallback'], ['auto_publish', 'Автопубликация'], ['config_version', 'Версия']]) {
      if (['string', 'number', 'boolean'].includes(typeof value?.[key])) summary.append(el('p', 'small', label + ': ' + value[key]));
    }
    for (const [usdKey, microsKey, label] of [['max_input_usd', 'input_price_micros', 'Макс. вход'], ['max_output_usd', 'output_price_micros', 'Макс. выход'], ['max_cache_read_usd', 'cache_read_price_micros', 'Макс. чтение кеша'], ['max_cache_write_usd', 'cache_write_price_micros', 'Макс. запись кеша']]) {
      const caps = value?.caps;
      const hasMicros = caps != null && Object.prototype.hasOwnProperty.call(caps, microsKey);
      const hasCapsUsd = caps != null && Object.prototype.hasOwnProperty.call(caps, usdKey);
      const hasUsd = value != null && Object.prototype.hasOwnProperty.call(value, usdKey);
      const amount = hasMicros ? caps[microsKey] : hasCapsUsd ? caps[usdKey] : value?.[usdKey];
      if (!(hasMicros || hasCapsUsd || hasUsd)) continue;
      if (amount === null || amount === 0) summary.append(el('p', 'small', label + ': Без ограничения'));
      else if (typeof amount === 'number' && Number.isFinite(amount) && amount > 0) summary.append(el('p', 'small', label + ': ' + (hasMicros ? (amount / 1e6).toFixed(6) : amount) + ' USD / 1M'));
    }
    const perRequest = value?.caps?.per_request_price_micros;
    if (value?.caps && Object.prototype.hasOwnProperty.call(value.caps, 'per_request_price_micros')) {
      if (perRequest === null || perRequest === 0) summary.append(el('p', 'small', 'Макс. за запрос: Без ограничения'));
      else if (typeof perRequest === 'number' && Number.isFinite(perRequest) && perRequest > 0) summary.append(el('p', 'small', 'Макс. за запрос: ' + (perRequest / 1e6).toFixed(6) + ' USD / запрос'));
    }
    if (Array.isArray(value?.channels)) for (const channel of value.channels) summary.append(el('p', 'small', 'Канал: ' + (typeof channel === 'object' ? channel?.channel_id ?? '—' : typeof channel === 'number' ? channel : '—')));
    if (!summary.children.length) summary.append(el('p', 'small', 'Нет доступных безопасных полей.'));
    return summary;
  }
  async function previewPool() {
    if (!pools.selected || pools.dirty || pools.busy) return;
    const selected = pools.selected, epoch = state.epoch; pools.busy = true; updatePoolControls(); invalidatePoolPreview(); areaError('pools-error', '');
    try {
      const preview = await api(poolPath(selected.id) + '/preview', { method: 'POST', body: {}, timeout: 90000 });
      if (epoch !== state.epoch || pools.selected?.id !== selected.id) return;
      pools.preview = { ...preview, pool_id: selected.id };
      text('pool-preview-meta', 'Действует до ' + date(preview.expires_at) + ' · только ручная публикация');
      const before = el('section'), after = el('section'); before.append(el('h3', '', 'Удалённая конфигурация сейчас'), previewSummary(preview.remote_before)); after.append(el('h3', '', 'Предлагаемые изменения'), previewSummary(preview.draft));
      for (const channel of preview.channels || []) after.append(el('p', 'small', 'Канал ' + channel.channel_id + ' · ' + (channel.supplier || '—')));
      $('pool-preview-diff').replaceChildren(before, after);
      areaError('pool-preview-warnings', (preview.warnings || []).filter(w => typeof w === 'string').join(' · '));
      const reason = preview.reason === 'controlled_price_caps_pilot_required' ? 'Требуется контролируемый пилот: адресная атрибуция и соблюдение ценовых ограничений маршрутом ещё не подтверждены. До успешного пилота удалённая публикация отключена.' : poolReason(preview.reason);
      text('pool-publish-reason', preview.can_publish === true ? 'Сервер разрешает публикацию. Проверьте состав и изменения перед подтверждением.' : 'Публикация заблокирована: ' + (reason || 'не выполнены требования безопасности.'));
      setPoolSection('overview');
      $('pool-preview-panel').hidden = false;
    } catch (error) { if (epoch === state.epoch) areaError('pools-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) { pools.busy = false; updatePoolControls(); } }
  }
  async function publishPool() {
    const preview = pools.preview, selected = pools.selected, epoch = state.epoch;
    if (!preview?.can_publish || !selected || pools.dirty || pools.busy || preview.pool_id !== selected.id) return;
    if (!Number.isFinite(Date.parse(preview.expires_at)) || Date.parse(preview.expires_at) <= Date.now()) { invalidatePoolPreview(); areaError('pools-error', 'Предпросмотр истёк. Получите новый.'); return; }
    if (!await confirmAction('Опубликовать пул в A6API?', 'Будет изменена удалённая конфигурация пула «' + selected.name + '» по показанному предпросмотру. Проверьте каналы и ценовые ограничения. Это отдельная ручная операция, не сохранение политики.', 'Подтвердить публикацию')) return;
    if (epoch !== state.epoch || pools.preview !== preview || pools.selected?.id !== selected.id || pools.dirty) return;
    await poolMutation(() => api(poolPath(selected.id) + '/publish', { method: 'POST', body: { preview_id: preview.preview_id, confirm: true } }), result => {
      if (result.pool) { pools.selected = result.pool; renderPoolForm(result.pool); }
      showNotice('Публикация завершена.');
    });
  }
  // A6API console credentials: PAT is write-only, only a mask comes back.
  function renderA6(data) {
    const configured = data?.configured === true;
    text('a6-badge', configured ? 'Настроено' : 'Не настроено');
    $('a6-badge').className = 'badge ' + (configured ? '' : 'neutral');
    text('a6-pat-state', data?.has_pat ? 'Сохранённый PAT: ' + (data.pat_masked || '—') + ' · оставьте поле пустым, чтобы не менять' : 'PAT ещё не сохранён');
    if (data?.user_id != null) $('a6-user-id').value = String(data.user_id);
    $('a6-clear').disabled = !data?.has_pat;
  }
  async function loadA6() {
    if (!state.session) return;
    const epoch = state.epoch; areaError('a6-error', ''); $('a6-reload').disabled = true;
    try {
      const data = await api('/admin/a6');
      if (epoch !== state.epoch) return;
      renderA6(data);
    } catch (error) { if (epoch === state.epoch) areaError('a6-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) $('a6-reload').disabled = false; }
  }
  function a6Payload() {
    const userId = Number($('a6-user-id').value);
    return { pat: $('a6-pat').value.trim(), user_id: Number.isInteger(userId) ? userId : $('a6-user-id').value };
  }
  async function saveA6(event) {
    event.preventDefault();
    if (!state.session) return;
    const epoch = state.epoch; const payload = a6Payload();
    if (payload.pat && payload.pat.length < 16) { areaError('a6-error', 'PAT слишком короткий.'); return; }
    $('a6-save').disabled = true; areaError('a6-error', ''); text('a6-verify-result', '');
    try {
      const data = await api('/admin/a6', { method: 'PUT', body: payload });
      if (epoch !== state.epoch) return;
      $('a6-pat').value = ''; renderA6(data);
      showNotice('Учётные данные A6API сохранены.');
    } catch (error) { if (epoch === state.epoch) areaError('a6-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) $('a6-save').disabled = false; }
  }
  async function verifyA6() {
    if (!state.session) return;
    const epoch = state.epoch; const payload = a6Payload();
    $('a6-verify').disabled = true; areaError('a6-error', ''); text('a6-verify-result', 'Проверяем доступ…');
    try {
      const result = await api('/admin/a6/verify', { method: 'POST', body: payload, timeout: 30000 });
      if (epoch !== state.epoch) return;
      const labels = { a6_auth_rejected: 'PAT отклонён', a6_user_id_mismatch: 'ID не совпадает с владельцем PAT',
        a6_connection_failed: 'Нет связи с A6API', a6_unexpected_status: 'Неожиданный ответ A6API', a6_invalid_response: 'Некорректный ответ A6API' };
      text('a6-verify-result', result.ok
        ? 'Доступ подтверждён · ' + (result.display_name || 'без имени') + ' · ID ' + number(result.user_id) + ' · токенов: ' + number(result.tokens_total)
        : 'Проверка не пройдена: ' + (labels[result.error_kind] || 'неизвестная ошибка'));
    } catch (error) { if (epoch === state.epoch) areaError('a6-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) $('a6-verify').disabled = false; }
  }
  async function clearA6() {
    if (!state.session) return;
    if (!await confirmAction('Удалить учётные данные A6API?', 'Сохранённый PAT и ID будут стёрты. Marketplace и закрепление каналов перестанут работать, пока вы не введёте их снова.', 'Удалить')) return;
    const epoch = state.epoch; $('a6-clear').disabled = true; areaError('a6-error', '');
    try {
      const data = await api('/admin/a6', { method: 'DELETE' });
      if (epoch !== state.epoch) return;
      $('a6-pat').value = ''; $('a6-user-id').value = ''; renderA6(data); text('a6-verify-result', '');
      showNotice('Учётные данные A6API удалены.');
    } catch (error) { if (epoch === state.epoch) areaError('a6-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) $('a6-clear').disabled = false; }
  }
  async function createKey(event) {
    event.preventDefault();
    const name = $('key-name').value.trim();
    if (!name) { $('key-name').focus(); areaError('keys-error', 'Укажите непустое название ключа.'); return; }
    const epoch = state.epoch; $('create-key').disabled = true; areaError('keys-error', '');
    try {
      const result = await api('/admin/keys', { method: 'POST', body: { name, models: [...$('key-models').querySelectorAll('input:checked')].map(input => input.value) } });
      if (epoch !== state.epoch) return;
      if (typeof result?.token !== 'string' || !result.token) throw new Error('Missing token');
      text('new-token', result.token); text('copy-status', ''); $('token-dialog').showModal();
      $('key-form').reset(); renderKeyModels(); await loadKeys();
    } catch (error) { if (epoch === state.epoch) areaError('keys-error', errorMessage(error) + ' Не создавайте дубликат, пока не проверите список ключей.'); }
    finally { if (epoch === state.epoch) $('create-key').disabled = false; }
  }

  async function changeAccount(event) {
    event.preventDefault();
    if (!state.session || $('account-save').disabled) return;
    areaError('account-error', '');
    const username = $('account-username').value.trim();
    const currentPassword = $('account-current-password').value;
    const newPassword = $('account-new-password').value;
    if (!currentPassword) { areaError('account-error', 'Введите текущий пароль.'); $('account-current-password').focus(); return; }
    if (!username) { areaError('account-error', 'Укажите непустой логин администратора.'); $('account-username').focus(); return; }
    if (newPassword && [...newPassword].length < 12) { areaError('account-error', 'Новый пароль должен содержать не менее 12 символов.'); $('account-new-password').focus(); return; }
    if (newPassword !== $('account-confirm-password').value) { areaError('account-error', 'Новые пароли не совпадают.'); $('account-confirm-password').focus(); return; }
    const epoch = state.epoch;
    $('account-save').disabled = true;
    try {
      const result = await api('/admin/account', { method: 'POST', body: { current_password: currentPassword, new_username: username, new_password: newPassword } });
      if (epoch !== state.epoch) return;
      if (result?.ok !== true || result?.reauthenticate !== true) throw new Error('Invalid account response');
      showLogin('Учётные данные обновлены. Войдите с новым логином и паролем.');
    } catch (error) { if (epoch === state.epoch) areaError('account-error', errorMessage(error)); }
    finally {
      $('account-current-password').value = '';
      $('account-new-password').value = '';
      $('account-confirm-password').value = '';
      $('account-save').disabled = false;
    }
  }

  // Model checks: independent settings revision, no vendor/browser dependencies.
  function detectorProbability(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? percent(value * 100) : '—';
  }
  function detectorStatus(value) {
    const labels = { verified: ['Совпадает', ''], suspect: ['Ниже порога', 'warning'], unknown: ['Не определено', 'neutral'], error: ['Ошибка', 'warning'] };
    return badge(...(labels[value] || labels.unknown));
  }
  function detectorErrorKind(value) { return typeof value === 'string' && /^[a-z][a-z0-9_]{0,63}$/i.test(value) ? value : 'unknown_error'; }
  function detectorSettingsCopy(settings = {}) {
    return { revision: settings.revision, enabled: settings.enabled === true, interval_minutes: settings.interval_minutes ?? 30, threshold_percent: settings.threshold_percent ?? 95, targets: (settings.targets || []).map(target => ({ provider_id: target.provider_id, model: target.model || '', reference_model: target.reference_model || '' })) };
  }
  function detectorPending() { return (detector.data?.jobs || []).some(job => ['queued', 'running'].includes(job.status)); }
  function stopDetectorPolling() {
    clearTimeout(detector.timer); detector.timer = null;
    detector.controller?.abort(); detector.controller = null; detector.sequence++;
    detector.logController?.abort(); detector.logController = null; detector.logSequence++;
    $('detector-log-rows').setAttribute('aria-busy', 'false');
    $('refresh-detector-log').disabled = false;
  }
  function scheduleDetectorPoll() {
    clearTimeout(detector.timer); detector.timer = null;
    if (!state.session || state.tab !== 'detector' || document.hidden) return;
    const delay = detectorPending() ? 3000 : 15000;
    text('detector-polling', 'Обновление каждые ' + delay / 1000 + ' секунд');
    detector.timer = setTimeout(() => void loadDetector(), delay);
  }
  function updateDetectorPolling() {
    stopDetectorPolling();
    text('detector-polling', 'Автообновление приостановлено');
    if (!state.session || document.hidden) return;
    if (state.tab === 'detector') void loadDetector();
    if (state.tab === 'detector-log') void loadDetectorLog();
  }
  async function loadDetector() {
    if (!state.session || state.tab !== 'detector' || document.hidden) return;
    clearTimeout(detector.timer); detector.timer = null;
    detector.controller?.abort();
    const controller = detector.controller = new AbortController(), sequence = ++detector.sequence, epoch = state.epoch;
    $('refresh-detector').disabled = true;
    try {
      const data = await api('/admin/detector', { signal: controller.signal, timeout: 12000 });
      if (epoch !== state.epoch || sequence !== detector.sequence) return;
      detector.data = data;
      if (!detector.draft || (!detector.dirty && !detector.saving)) detector.draft = detectorSettingsCopy(data.settings);
      renderDetector(); areaError('detector-error', '');
      text('detector-status', 'Обновлено ' + date(new Date().toISOString()) + ' · время вашего браузера');
    } catch (error) {
      if (epoch === state.epoch && sequence === detector.sequence) areaError('detector-error', 'Проверки недоступны. ' + (detector.data ? 'Показаны последние загруженные данные. ' : '') + errorMessage(error));
    } finally {
      if (epoch === state.epoch && sequence === detector.sequence) { detector.controller = null; $('refresh-detector').disabled = false; scheduleDetectorPoll(); }
    }
  }
  function referenceOptions(select, value = '') {
    select.replaceChildren();
    const placeholder = el('option', '', 'Выберите эталон'); placeholder.value = ''; select.append(placeholder);
    for (const model of detector.data?.catalog?.models || []) {
      const option = el('option', '', (model.display_name || model.id) + (model.family ? ' · ' + model.family : ''));
      option.value = model.id; select.append(option);
    }
    if (value && ![...select.options].some(option => option.value === value)) {
      const missing = el('option', '', value + ' · отсутствует в каталоге'); missing.value = value; missing.disabled = true; select.append(missing);
    }
    select.value = value;
  }
  function exactDetectorReference(model) { return (detector.data?.catalog?.models || []).some(item => item.id === model.trim()) ? model.trim() : ''; }
  function markDetectorDirty() {
    detector.dirty = true; updateDetectorSettingsState();
  }
  function updateDetectorSettingsState() {
    $('detector-dirty-dot').hidden = !detector.dirty;
    $('save-detector-settings').disabled = !detector.dirty || detector.saving || !detector.data?.available;
    text('detector-settings-state', detector.saving ? 'Сохранение…' : detector.dirty ? 'Есть несохранённые изменения' : 'Настройки сохранены · ревизия ' + (detector.draft?.revision ?? '—'));
  }
  function detectorProviderInfo(provider) {
    const wrapper = el('div', 'detector-provider-info'); wrapper.append(el('strong', '', provider.name || provider.id));
    wrapper.append(badge(provider.enabled ? 'Включён' : 'Отключён вручную', provider.enabled ? '' : 'neutral'));
    const blocked = (detector.data?.blocked_provider_ids || []).includes(provider.id);
    wrapper.append(badge(blocked ? 'В карантине' : 'Не в карантине', blocked ? 'warning' : 'neutral'));
    return wrapper;
  }
  function renderDetectorTargets() {
    const rows = [];
    for (const [providerIndex, provider] of (detector.data?.providers || []).entries()) {
      const targets = detector.draft.targets.filter(target => target.provider_id === provider.id);
      if (!targets.length) targets.push({ provider_id: provider.id, model: '', reference_model: '' });
      const list = el('datalist'); list.id = 'detector-models-' + providerIndex;
      for (const model of provider.models || []) { const option = el('option'); option.value = model; list.append(option); }
      targets.forEach((target, index) => {
        const row = el('tr'), providerCell = el('td'), modelCell = el('td'), referenceCell = el('td'), actions = el('td');
        if (!index) providerCell.append(detectorProviderInfo(provider));
        const model = el('input'); model.type = 'text'; model.value = target.model; model.maxLength = 512; model.setAttribute('list', list.id); model.setAttribute('aria-label', 'Upstream-модель · ' + provider.name); model.placeholder = 'Введите upstream-модель';
        const reference = el('select'); reference.setAttribute('aria-label', 'Ожидаемый эталон · ' + provider.name); referenceOptions(reference, target.reference_model);
        const retain = () => { if (!detector.draft.targets.includes(target)) detector.draft.targets.push(target); markDetectorDirty(); };
        model.addEventListener('input', () => { target.model = model.value; target.reference_model = exactDetectorReference(model.value); reference.value = target.reference_model; retain(); });
        reference.addEventListener('change', () => { target.reference_model = reference.value; retain(); });
        modelCell.append(model); if (!index) modelCell.append(list); referenceCell.append(reference);
        if (!index) actions.append(action('+ Цель', 'secondary small-button', () => {
          if (!detector.draft.targets.includes(target)) detector.draft.targets.push(target);
          detector.draft.targets.push({ provider_id: provider.id, model: '', reference_model: '' }); markDetectorDirty(); renderDetectorTargets();
        }));
        actions.append(action('Убрать', 'subtle small-button', () => { detector.draft.targets = detector.draft.targets.filter(item => item !== target); markDetectorDirty(); renderDetectorTargets(); }));
        row.append(providerCell, modelCell, referenceCell, actions); rows.push(row);
      });
    }
    $('detector-targets').replaceChildren(...rows);
    if (!rows.length) { const row = el('tr'), cell = el('td', 'empty', 'Поставщиков пока нет. Добавьте их в настройках.'); cell.colSpan = 4; row.append(cell); $('detector-targets').append(row); }
  }
  function updateManualModels() {
    const provider = (detector.data?.providers || []).find(item => item.id === $('detector-provider').value);
    $('detector-manual-models').replaceChildren(...(provider?.models || []).map(model => { const option = el('option'); option.value = model; return option; }));
  }
  function renderDetectorManual() {
    const providers = detector.data?.providers || [], providerSelect = $('detector-provider');
    const selectedProvider = providerSelect.value, selectedReference = $('detector-reference').value;
    providerSelect.replaceChildren(...providers.map(provider => {
      const option = el('option', '', (provider.name || provider.id) + (!provider.enabled ? ' · отключён вручную' : '') + ((detector.data.blocked_provider_ids || []).includes(provider.id) ? ' · в карантине' : ''));
      option.value = provider.id; return option;
    }));
    if (providers.some(provider => provider.id === selectedProvider)) providerSelect.value = selectedProvider;
    referenceOptions($('detector-reference'), selectedReference || exactDetectorReference($('detector-model').value)); updateManualModels();
    $('detector-check-fieldset').disabled = !detector.data?.available || !providers.length || detector.checking;
  }
  function appendDetectorResult(container, result) {
    if (!result) return;
    if (result.error_kind) container.append(el('p', 'detector-error-detail', 'Ошибка: ' + detectorErrorKind(result.error_kind)));
    if (result.status) container.append(detectorStatus(result.status));
    container.append(el('p', 'small', 'Вероятность эталона: ' + detectorProbability(result.target_probability) + ' · Предсказание: ' + (result.prediction || '—') + ' (' + detectorProbability(result.prediction_probability) + ')'));
    if (result.duration_ms != null) container.append(el('p', 'small', 'Время: ' + number(result.duration_ms) + ' мс'));
    if (result.confirmation && typeof result.confirmation === 'object') {
      const confirmation = result.confirmation;
      const labels = { not_required: 'Не требуется', pending: 'Ожидается второй раунд', confirmed: 'Низкая оценка подтверждена', recovered: 'Совпадение восстановлено', inconclusive: 'Не подтверждено' };
      container.append(el('p', 'small detector-confirmation', 'Подтверждение: ' + (labels[confirmation.status] || 'Не определено') + ' · Первый раунд: ' + detectorProbability(confirmation.first_probability) + ' · Второй раунд: ' + detectorProbability(confirmation.second_probability) + ' · Порог: ' + percent(confirmation.threshold_percent)));
    }
    if (Array.isArray(result.rounds)) {
      for (const round of result.rounds.slice(0, 2)) {
        const line = el('p', 'small detector-round', 'Раунд ' + number(round.round) + ' · ');
        line.append(detectorStatus(round.status), document.createTextNode(' · Вероятность эталона: ' + detectorProbability(round.target_probability) + ' · Предсказание: ' + (round.prediction || '—')));
        if (round.error_kind) line.append(document.createTextNode(' · Ошибка: ' + detectorErrorKind(round.error_kind)));
        container.append(line);
      }
    }
    if (Array.isArray(result.challenges) && result.challenges.length) {
      const details = el('details', 'detector-challenges'); details.append(el('summary', '', 'Диагностика заданий (' + Math.min(6, result.challenges.length) + ')'));
      for (const challenge of result.challenges.slice(0, 6)) {
        const id = typeof challenge.id === 'string' && /^query-\d{2}$/.test(challenge.id) ? challenge.id : '—';
        const accepted = challenge.accepted === true ? 'Принято' : challenge.accepted === false ? 'Не принято' : '—';
        const finish = typeof challenge.finish_reason === 'string' && /^[a-z][a-z0-9_]{0,63}$/i.test(challenge.finish_reason) ? challenge.finish_reason : '—';
        details.append(el('p', 'small detector-challenge', 'Раунд ' + number(challenge.round) + ' · ' + id + ' · Чисел: ' + number(challenge.parsed_numbers) + ' / ожидается ' + number(challenge.expected_count) + ' · Минимум: ' + number(challenge.minimum_numbers) + ' · ' + accepted + ' · finish_reason: ' + finish + ' · Попыток: ' + number(challenge.attempts)));
        if (Array.isArray(challenge.errors) && challenge.errors.length) details.append(el('p', 'detector-error-detail', 'История ошибок: ' + challenge.errors.slice(0, 3).map(detectorErrorKind).join(', ')));
      }
      container.append(details);
    }
    if (Array.isArray(result.candidates) && result.candidates.length) {
      const details = el('details', 'detector-candidates'); details.append(el('summary', '', 'Кандидаты каталога'));
      for (const candidate of result.candidates) details.append(el('p', 'small', candidate.model + ': ' + detectorProbability(candidate.probability)));
      container.append(details);
    }
  }
  function renderDetector() {
    const data = detector.data, available = data.available === true;
    areaError('detector-warning', available ? '' : 'Проверки недоступны.');
    $('detector-settings-fieldset').disabled = !available || detector.saving;
    if (!detector.dirty && !detector.saving) {
      $('detector-enabled').checked = detector.draft.enabled;
      $('detector-interval').value = detector.draft.interval_minutes;
      $('detector-threshold').value = detector.draft.threshold_percent;
      renderDetectorTargets();
    } else {
      // Preserve in-progress edits and focus while refreshing live provider quarantine.
      for (const [index, provider] of (data.providers || []).entries()) {
        const first = [...$('detector-targets').rows].filter(row => row.cells[0].childElementCount)[index];
        if (first) first.cells[0].replaceChildren(detectorProviderInfo(provider));
      }
    }
    text('detector-threshold-badge', 'Порог ' + percent(data.settings?.threshold_percent));
    text('detector-next-run', 'Следующий запуск: ' + (data.settings?.enabled ? date(data.next_run_at) : 'мониторинг выключен'));
    updateDetectorSettingsState(); renderDetectorManual();
    text('detector-catalog', 'Каталог: ' + (data.catalog?.models || []).length + ' моделей · source_commit: ' + (data.catalog?.source_commit || '—'));
    const providerNames = new Map((data.providers || []).map(provider => [provider.id, provider.name]));
    $('detector-jobs').replaceChildren(...(data.jobs || []).map(job => {
      const item = el('article', 'detector-job'), head = el('div', 'detector-job-heading'), info = el('div');
      const jobLabels = { queued: 'В очереди', running: 'Выполняется', done: 'Завершено', error: 'Ошибка' };
      info.append(el('h3', '', (providerNames.get(job.provider_id) || job.provider_id) + ' · ' + job.model), el('p', 'small', 'Эталон: ' + job.reference_model + ' · ' + (jobLabels[job.status] || 'Не определено') + ' · ' + number(Math.max(0, Math.min(3, Number(job.progress) || 0))) + '/3'));
      if (job.round === 1 || job.round === 2) info.append(el('p', 'small detector-job-round', 'Раунд ' + job.round + '/' + ([1, 2].includes(job.max_rounds) ? job.max_rounds : '—') + ' · Прогресс текущего раунда'));
      const phases = { sampling: 'Сбор ответов', retry_wait: 'Ожидание повтора', scoring: 'Расчёт вероятности', confirmation: 'Подтверждение низкой оценки' };
      if (phases[job.phase]) info.append(el('p', 'small detector-job-phase', phases[job.phase]));
      if ([1, 2, 3].includes(job.retry_attempt)) info.append(el('p', 'small detector-job-retry', 'Текущая попытка: ' + job.retry_attempt + '/' + ([1, 2, 3].includes(job.max_attempts) ? job.max_attempts : '—')));
      head.append(info);
      if (['queued', 'running'].includes(job.status)) {
        const cancel = action('Отменить', 'secondary small-button', () => void cancelDetectorJob(job.id)); cancel.disabled = detector.cancelling.has(job.id); head.append(cancel);
      }
      item.append(head);
      if (job.error_kind && job.error_kind !== job.result?.error_kind) item.append(el('p', 'detector-error-detail', 'Ошибка: ' + detectorErrorKind(job.error_kind)));
      appendDetectorResult(item, job.result || { challenges: job.challenges, rounds: job.rounds, confirmation: job.confirmation }); return item;
    }));
    if (!data.jobs?.length) $('detector-jobs').append(el('div', 'empty', 'Заданий пока нет.'));
    $('detector-states').replaceChildren(...(data.states || []).map(item => {
      const row = el('tr'), result = item.conclusive_result || {}, latest = item.latest_result || {};
      const providerCell = el('td', '', item.provider_name || providerNames.get(item.provider_id) || item.provider_id);
      if ((data.blocked_provider_ids || []).includes(item.provider_id)) providerCell.append(el('br'), badge('В карантине', 'warning'));
      const statusCell = el('td'); statusCell.append(detectorStatus(item.status));
      const latestCell = el('td');
      latestCell.append(detectorStatus(item.latest_status || latest.status || 'unknown'));
      if (Object.keys(latest).length) {
        const details = el('details', 'detector-latest-result'); details.append(el('summary', '', 'Подробности последней попытки'));
        appendDetectorResult(details, latest); latestCell.append(details);
      }
      row.append(providerCell, el('td', '', item.model + ' / ' + item.reference_model), statusCell, el('td', '', detectorProbability(result.target_probability)), el('td', '', (result.prediction || '—') + ' / ' + detectorProbability(result.prediction_probability)), latestCell, el('td', '', date(item.checked_at))); return row;
    }));
    if (!data.states?.length) { const row = el('tr'), cell = el('td', 'empty', 'Определённых результатов пока нет.'); cell.colSpan = 7; row.append(cell); $('detector-states').append(row); }
  }
  async function saveDetectorSettings(event) {
    event.preventDefault();
    if (!detector.draft || !detector.dirty || detector.saving || !detector.data?.available) return;
    if (!$('detector-settings-form').reportValidity()) return;
    const payload = detectorSettingsCopy(detector.draft);
    payload.interval_minutes = Number($('detector-interval').value);
    payload.threshold_percent = Number($('detector-threshold').value);
    payload.enabled = $('detector-enabled').checked;
    payload.targets = payload.targets.map(target => ({ ...target, model: target.model.trim() })).filter(target => target.model || target.reference_model);
    const references = new Set((detector.data.catalog?.models || []).map(model => model.id));
    if (!Number.isInteger(payload.interval_minutes) || payload.interval_minutes < 5 || payload.interval_minutes > 1440 || payload.targets.some(target => !target.model || !references.has(target.reference_model))) {
      areaError('detector-error', 'Укажите upstream-модель и эталон для каждой цели. Интервал — целое число от 5 до 1440 минут.'); return;
    }
    if (!Number.isFinite(payload.threshold_percent) || payload.threshold_percent < 50 || payload.threshold_percent > 100) { areaError('detector-settings-error', 'Порог должен быть числом от 50 до 100%.'); return; }
    if (payload.enabled && !payload.targets.length) { areaError('detector-error', 'Для мониторинга добавьте хотя бы одну цель.'); return; }
    const epoch = state.epoch;
    if (payload.enabled && !await confirmAction('Включить платный мониторинг?', 'Каждые ' + payload.interval_minutes + ' мин. обычно выполняются 3 платных запроса для каждой из ' + payload.targets.length + ' целей; максимум 18 на цель (2 раунда × 3 задания × 3 попытки). Паузы повторов: 2 и 5 секунд. Карантин — только после двух полных калиброванных раундов ниже одного порога. Вероятность относится к закрытому каталогу и не доказывает подлинность. Отключённые вручную поставщики автоматически не включаются.', 'Сохранить мониторинг')) return;
    if (epoch !== state.epoch || !state.session) return;
    detector.saving = true; $('detector-settings-fieldset').disabled = true; updateDetectorSettingsState(); areaError('detector-settings-error', '');
    try {
      const settings = await api('/admin/detector/settings', { method: 'PUT', body: payload });
      if (epoch !== state.epoch) return;
      detector.draft = detectorSettingsCopy(settings); detector.data.settings = settings; detector.dirty = false; renderDetector();
      showNotice('Настройки мониторинга сохранены. Ручные проверки независимы от мониторинга.');
    } catch (error) { if (epoch === state.epoch) areaError('detector-settings-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) { detector.saving = false; renderDetector(); if (state.tab === 'detector') void loadDetector(); } }
  }
  async function startDetectorCheck(event) {
    event.preventDefault();
    if (detector.checking || !detector.data?.available) return;
    const payload = { provider_id: $('detector-provider').value, model: $('detector-model').value.trim(), reference_model: $('detector-reference').value };
    if (!payload.model || !(detector.data.providers || []).some(provider => provider.id === payload.provider_id) || !(detector.data.catalog?.models || []).some(model => model.id === payload.reference_model)) {
      areaError('detector-error', 'Выберите поставщика, укажите upstream-модель и ожидаемый эталон.'); return;
    }
    const epoch = state.epoch;
    if (!await confirmAction('Запустить платную проверку?', 'Обычно выполняются 3 платных запроса к выбранному поставщику; максимум 18 (2 раунда × 3 задания × 3 попытки). Паузы повторов: 2 и 5 секунд. Карантин — только после двух полных калиброванных раундов ниже одного порога. Настройки мониторинга не изменятся. Вероятность закрытого набора не доказывает подлинность.', 'Проверить')) return;
    if (epoch !== state.epoch || !state.session) return;
    detector.checking = true; $('detector-check-fieldset').disabled = true; areaError('detector-check-error', '');
    try {
      const job = await api('/admin/detector/check', { method: 'POST', body: payload });
      if (epoch !== state.epoch) return;
      detector.data.jobs = [job, ...(detector.data.jobs || []).filter(item => item.id !== job.id)];
      renderDetector(); showNotice('Проверка поставлена в очередь. Мониторинг не изменён.');
    } catch (error) { if (epoch === state.epoch) areaError('detector-check-error', errorMessage(error) + ' Перед повтором обновите задания.'); }
    finally { if (epoch === state.epoch) { detector.checking = false; renderDetectorManual(); scheduleDetectorPoll(); } }
  }
  async function cancelDetectorJob(id) {
    if (detector.cancelling.has(id)) return;
    const epoch = state.epoch; detector.cancelling.add(id); renderDetector();
    try {
      await api('/admin/detector/jobs/' + encodeURIComponent(id) + '/cancel', { method: 'POST' });
      if (epoch === state.epoch) { showNotice('Отмена задания запрошена. Уже выполненные платные запросы не отменяются.'); await loadDetector(); }
    } catch (error) { if (epoch === state.epoch) areaError('detector-error', errorMessage(error)); }
    finally { if (epoch === state.epoch) { detector.cancelling.delete(id); renderDetector(); } }
  }
  function journalSource(value) { return ({ manual: 'Ручная проверка', scheduled: 'Мониторинг', monitor: 'Мониторинг', scheduler: 'Мониторинг', threshold_change: 'Изменение порога' })[value] || 'Проверка'; }
  function showJournalDetail(change) {
    const dialog = $('journal-detail'), body = $('journal-detail-body');
    if (!dialog || !body) return;
    text('journal-detail-title', 'Событие · ' + (change.provider_name || change.provider_id || 'Поставщик'));
    const fields = [['Время', date(change.checked_at)], ['Поставщик', change.provider_name || change.provider_id], ['Upstream-модель', change.model], ['Ожидаемый эталон', change.reference_model], ['Прежний статус', detectorStatus(change.from_status).textContent], ['Новый статус', detectorStatus(change.to_status).textContent], ['Вероятность эталона', detectorProbability(change.target_probability)], ['Предсказание', change.prediction], ['Источник', journalSource(change.source)]];
    const list = el('dl', 'journal-detail-list');
    for (const [label, value] of fields) list.append(el('dt', '', label), el('dd', '', value == null || value === '' ? '—' : value));
    body.replaceChildren(list, el('p', 'small', 'Вероятность относится к закрытому каталогу и не доказывает подлинность модели. Показан снимок события; обновление журнала не изменяет открытые подробности.'));
    if (!dialog.open) dialog.showModal();
  }
  function renderJournal() {
    const query = normalized(journal.search);
    const rows = journal.rows.filter(change => (journal.filter === 'all' || change.to_status === journal.filter) && (!query || [change.provider_name, change.provider_id, change.model, change.reference_model, change.prediction, journalSource(change.source)].some(value => normalized(value).includes(query))))
      .slice().sort((a, b) => journal.sort === 'provider' ? String(a.provider_name || a.provider_id || '').localeCompare(String(b.provider_name || b.provider_id || ''), 'ru') : ((Date.parse(a.checked_at) || 0) - (Date.parse(b.checked_at) || 0)) * (journal.sort === 'oldest' ? 1 : -1));
    if ($('journal-count')) text('journal-count', 'Показано ' + rows.length + ' из ' + journal.rows.length);
    // Unchanged polling must not remove a focused details action.
    const signature = JSON.stringify([rows.map(change => [change.id, change.checked_at, change.provider_id, change.provider_name, change.model, change.reference_model, change.from_status, change.to_status, change.target_probability, change.prediction, change.source]), query, journal.filter]);
    if (signature === journal.signature) return;
    if ($('detector-log-rows').contains(document.activeElement) || $('journal-detail')?.open) return;
    journal.signature = signature;
    $('detector-log-rows').replaceChildren(...rows.map(change => {
      const row = el('tr'), from = el('td'), to = el('td'); from.append(detectorStatus(change.from_status)); to.append(detectorStatus(change.to_status));
      row.dataset.status = change.to_status || 'unknown';
      row.append(el('td', '', date(change.checked_at)), el('td', '', change.provider_name || change.provider_id), el('td', '', change.model + ' / ' + change.reference_model), from, to, el('td', '', detectorProbability(change.target_probability)), el('td', '', change.prediction || '—'), el('td', '', journalSource(change.source)));
      if ($('journal-detail')) { const actions = el('td'); const button = action('Подробнее', 'subtle small-button journal-detail-action', () => showJournalDetail(change)); button.setAttribute('aria-label', 'Подробнее: ' + (change.provider_name || change.provider_id) + ' · ' + date(change.checked_at)); actions.append(button); row.append(actions); }
      return row;
    }));
    if (!rows.length) { const row = el('tr'), cell = el('td', 'empty', journal.rows.length ? 'События не найдены. Измените поиск или фильтр.' : 'Изменений определённого статуса пока нет.'); cell.colSpan = $('journal-detail') ? 9 : 8; row.append(cell); $('detector-log-rows').append(row); }
  }
  async function loadDetectorLog() {
    if (!state.session || state.tab !== 'detector-log' || document.hidden) return;
    detector.logController?.abort();
    const controller = detector.logController = new AbortController(), sequence = ++detector.logSequence, epoch = state.epoch;
    $('refresh-detector-log').disabled = true; $('detector-log-rows').setAttribute('aria-busy', 'true'); text('detector-log-status', 'Загрузка журнала…');
    try {
      const data = await api('/admin/detector/log?limit=100', { signal: controller.signal, timeout: 12000 });
      if (epoch !== state.epoch || sequence !== detector.logSequence) return;
      journal.rows = Array.isArray(data.changes) ? data.changes : [];
      renderJournal();
      areaError('detector-log-error', ''); text('detector-log-status', 'Обновлено ' + date(new Date().toISOString()) + ' · время вашего браузера');
    } catch (error) { if (epoch === state.epoch && sequence === detector.logSequence) { areaError('detector-log-error', errorMessage(error)); text('detector-log-status', 'Нет свежих данных'); } }
    finally { if (epoch === state.epoch && sequence === detector.logSequence) { detector.logController = null; $('refresh-detector-log').disabled = false; $('detector-log-rows').setAttribute('aria-busy', 'false'); } }
  }

  // Pelican is independent of fingerprint/A6 checks. Never insert generated SVG into the DOM.
  function pelicanVisible() { return !!state.session && !pelican.signingOut && state.tab === 'pelican' && !document.hidden; }
  function pelicanActive(run) { return ['queued', 'running'].includes(run.status); }
  function stopPelicanPolling() {
    clearTimeout(pelican.timer); pelican.timer = null;
    if (pelican.controller) pelican.controller.abort();
    pelican.controller = null; pelican.sequence++;
    text('pelican-polling', 'Автообновление приостановлено');
    $('pelican-refresh').disabled = pelican.busy || pelican.signingOut;
  }
  function schedulePelicanPoll() {
    clearTimeout(pelican.timer); pelican.timer = null;
    if (!pelicanVisible() || pelican.busy || pelican.controller) return;
    const active = (pelican.data?.runs || []).some(pelicanActive);
    text('pelican-polling', active ? 'Обновление каждые 5 секунд' : 'Обновление каждые 15 секунд');
    pelican.timer = setTimeout(() => { pelican.timer = null; void loadPelican(); }, active ? 5000 : 15000);
  }
  function updatePelicanPolling() {
    stopPelicanPolling();
    if (pelicanVisible() && !pelican.busy) void loadPelican();
  }
  function updatePelicanControls() {
    const ready = !!state.session && !pelican.signingOut && !!pelican.data && !pelican.busy;
    $('pelican-fields').disabled = !ready;
    $('pelican-start').disabled = !ready || !$('pelican-provider').value || !$('pelican-model').value || (pelican.data?.runs || []).some(pelicanActive);
    $('pelican-refresh').disabled = !!pelican.controller || pelican.busy || pelican.signingOut;
  }
  function pelicanModels() {
    const provider = (pelican.data?.providers || []).find(item => item.id === $('pelican-provider').value);
    const selected = $('pelican-model').value;
    $('pelican-model').replaceChildren(...(provider?.models || []).filter(model => typeof model === 'string').map(model => {
      const option = el('option', '', model); option.value = model; return option;
    }));
    if ([...$('pelican-model').options].some(option => option.value === selected)) $('pelican-model').value = selected;
    if (!$('pelican-model').options.length) { const option = el('option', '', 'Нет настроенных upstream-моделей'); option.value = ''; $('pelican-model').append(option); }
    updatePelicanControls();
  }
  function pelicanError(error) {
    const labels = {
      pelican_busy: 'Уже выполняется генерация. Дождитесь завершения или отмените её.',
      pelican_run_in_progress: 'Уже выполняется генерация. Дождитесь завершения или отмените её.',
      generation_in_progress: 'Уже выполняется генерация. Дождитесь завершения или отмените её.',
      confirm_cost_required: 'Для одной платной генерации требуется явное подтверждение.',
      pelican_cost_confirmation_required: 'Для одной платной генерации требуется явное подтверждение.',
      pelican_provider_unavailable: 'Поставщик недоступен. Обновите список и проверьте настройки.',
      pelican_model_unavailable: 'Модель недоступна. Обновите список и выберите upstream-модель.',
      pelican_invalid_protocol: 'Выберите протокол Chat Completions или Responses.',
      pelican_invalid_effort: 'Выберите допустимый уровень reasoning effort.',
      provider_unavailable: 'Поставщик недоступен. Обновите список и проверьте настройки.',
      invalid_provider: 'Поставщик недоступен. Обновите список и проверьте настройки.',
      model_not_available: 'Модель недоступна. Обновите список и выберите upstream-модель.',
      pelican_timeout: 'Истекли 5 минут ожидания. Автоматического повтора нет.',
      pelican_connection_failed: 'Не удалось подключиться к поставщику. Автоматического повтора нет.',
      pelican_incomplete_generation: 'Поставщик вернул незавершённый ответ. Автоматического повтора нет.',
      pelican_generation_failed: 'Поставщик завершил генерацию с ошибкой. Автоматического повтора нет.',
      pelican_sensitive_output: 'Ответ скрыт: обнаружены чувствительные данные. Предпросмотр недоступен.'
    };
    return labels[error.code] || errorMessage(error);
  }
  function pelicanRunError(kind) {
    const structural = { pelican_missing_animation: 'Безопасный SVG получен, но объявление анимации не найдено.', pelican_missing_nonce: 'Безопасный SVG получен, но код этого запуска в SVG text не найден.', pelican_invalid_svg: 'Ответ не содержит одного корректного SVG.', pelican_unsafe_svg: 'SVG отклонён проверкой безопасности.', pelican_external_resource: 'SVG отклонён: обнаружена внешняя ссылка.', pelican_unsafe_style: 'SVG отклонён: небезопасный стиль.', pelican_svg_size_limit: 'SVG превысил допустимый размер.', pelican_svg_complexity_limit: 'SVG превысил допустимую сложность.', pelican_invalid_svg_dimensions: 'SVG содержит недопустимый размер сцены.', pelican_cancelled: 'Генерация отменена.', interrupted_by_restart: 'Генерация прервана перезапуском сервиса.' };
    if (kind === 'pelican_output_limit') return 'Исчерпан лимит выходных токенов, включая рассуждения. Рисунок не завершён. Автоматического повтора нет.';
    if (kind === 'pelican_content_filter') return 'Поставщик остановил ответ фильтром содержимого. Автоматического повтора нет.';
    if (kind === 'response_size_limit') return 'Поток ответа превысил транспортный лимит. Он включает reasoning и служебные SSE-данные, а не только SVG. Автоматического повтора нет.';
    if (kind === 'response_event_limit') return 'Поставщик прислал слишком много событий потока. Автоматического повтора нет.';
    if (structural[kind]) return structural[kind];
    if (typeof kind === 'string' && /^upstream_http_\d{3}$/.test(kind)) return 'Поставщик отклонил запрос (HTTP ' + kind.slice(-3) + '). Автоматического повтора нет.';
    if (['pelican_timeout', 'pelican_connection_failed', 'pelican_incomplete_generation', 'pelican_generation_failed', 'pelican_sensitive_output'].includes(kind)) return pelicanError({ code: kind });
    return ({ timeout: 'Истекли 5 минут ожидания.', cancelled: 'Генерация отменена.', upstream_timeout: 'Поставщик не ответил за 5 минут.', upstream_error: 'Поставщик завершил запрос с ошибкой.', upstream_http: 'Поставщик отклонил запрос.', connection_error: 'Не удалось подключиться к поставщику.', invalid_svg: 'Ответ не содержит допустимого SVG.', unsafe_svg: 'SVG отклонён проверкой безопасности.', response_too_large: 'Ответ превысил допустимый размер.', empty_response: 'Поставщик вернул пустой ответ.', interrupted: 'Генерация прервана.' })[kind] || 'Генерация не завершена. Проверьте поставщика; повтор возможен только вручную с новым подтверждением оплаты.';
  }
  function pelicanCheck(value) {
    if (value === true) return 'пройдена';
    if (value === false) return 'не пройдена';
    return 'нет результата';
  }
  function renderPelicanRuns() {
    const images = new Map([...$('pelican-runs').querySelectorAll('img[data-run-id]')].map(image => [image.dataset.runId, image]));
    const runs = pelican.data?.runs || [];
    $('pelican-runs').replaceChildren(...runs.map(run => {
      const card = el('article', 'pelican-run'); card.dataset.runId = String(run.id);
      const heading = el('div', 'detector-job-heading'), title = el('div');
      title.append(el('h3', '', run.provider_name || run.provider_id), el('p', 'small', run.model + ' · ' + (run.protocol === 'responses' ? 'Responses' : 'Chat Completions') + ' · effort: ' + (['none', 'minimal', 'low', 'medium', 'high', 'xhigh'].includes(run.effort) ? run.effort : 'по умолчанию')));
      const statuses = { queued: 'В очереди', running: 'Генерация', done: 'Завершено', error: 'Ошибка', cancelled: 'Отменено' };
      heading.append(title, badge(statuses[run.status] || 'Статус неизвестен', run.status === 'done' ? 'success' : run.status === 'error' ? 'danger' : 'neutral'));
      card.append(heading);
      const phases = { queued: 'Ожидание запуска', starting: 'Подготовка запроса', generating: 'Генерация SVG', requesting: 'Запрос поставщику', waiting: 'Ожидание поставщика', validating: 'Проверка SVG', checking: 'Структурные проверки', complete: 'Завершено', done: 'Завершено', finished: 'Завершено', cancelled: 'Отменено', error: 'Остановлено' };
      const measuredElapsed = run.elapsed_ms == null ? null : Math.max(0, Number(run.elapsed_ms));
      const sinceCreated = Date.now() - new Date(run.created_at).getTime();
      const elapsed = pelicanActive(run) && !measuredElapsed && Number.isFinite(sinceCreated) ? Math.max(0, sinceCreated) : measuredElapsed;
      card.append(el('p', 'small pelican-progress', 'Этап: ' + (phases[run.phase] || (pelicanActive(run) ? 'Ожидание результата' : 'Завершён')) + ' · Прошло: ' + (Number.isFinite(elapsed) && elapsed !== null ? number(elapsed / 1000) + ' с' : '—') + ' · Таймаут: 5 минут'));
      card.append(el('p', 'small', 'Создано: ' + date(run.created_at) + (run.finished_at ? ' · Завершено: ' + date(run.finished_at) : '')));
      const checks = el('div', 'pelican-checks');
      for (const [key, label] of [['safe_svg', 'Безопасный SVG'], ['animation', 'Анимация'], ['nonce', 'Маркер запроса']]) checks.append(el('span', 'pool-chip neutral', label + ': ' + pelicanCheck(run.checks?.[key])));
      card.append(checks);
      if (run.status === 'error') card.append(el('p', 'detector-error-detail', pelicanRunError(run.error_kind)));
      if (Number.isInteger(run.max_output_tokens)) card.append(el('p', 'small', 'Предел выхода, включая рассуждения: ' + number(run.max_output_tokens) + ' токенов'));
      if (run.diagnostics && ['length', 'max_output_tokens', 'content_filter', 'stop', 'unknown'].includes(run.diagnostics.finish_reason)) card.append(el('p', 'small', 'Причина остановки поставщика: ' + run.diagnostics.finish_reason));
      if (run.usage && typeof run.usage === 'object') {
        const input = run.usage.input_tokens ?? run.usage.prompt_tokens, output = run.usage.output_tokens ?? run.usage.completion_tokens;
        card.append(el('p', 'small', 'Токены IN / OUT: ' + number(input) + ' / ' + number(output) + ' · Только фактический usage'));
        if (Number.isInteger(run.usage.reasoning_tokens)) card.append(el('p', 'small', 'Из них рассуждения: ' + number(run.usage.reasoning_tokens)));
      }
      if (pelicanActive(run)) {
        const cancel = el('button', 'button secondary', pelican.cancelling.has(run.id) ? 'Отмена…' : 'Отменить генерацию'); cancel.type = 'button'; cancel.disabled = pelican.busy;
        cancel.addEventListener('click', () => void cancelPelican(run.id)); card.append(cancel);
      }
      if (run.svg_available === true) {
        const figure = el('figure', 'pelican-preview');
        let image = images.get(String(run.id));
        if (!image || image.hidden) {
          image = el('img'); image.dataset.runId = String(run.id); image.alt = 'Сгенерированная сцена: пеликан на велосипеде на морской набережной'; image.referrerPolicy = 'no-referrer';
          image.addEventListener('error', () => { image.hidden = true; image.parentElement?.append(el('p', 'notice danger', 'Предпросмотр недоступен. Обновите данные или проверьте сессию.')); }, { once: true });
          image.src = '/admin/pelican/' + encodeURIComponent(run.id) + '/svg';
        }
        figure.append(image, el('figcaption', 'small', 'Недоверенный SVG показан только как изображение. Проверьте вручную: пеликан, велосипед, морская набережная, движение. Структурные проверки не определяют качество или подлинность модели.'));
        card.append(figure);
        if(pelican.motionNodes.has(String(run.id))){card.append(pelican.motionNodes.get(String(run.id)));return card;}
        const motion=el('details','pool-form-help');motion.append(el('summary','','Кадры и движение · локальная диагностика'));
        const motionButton=el('button','button secondary','Проверить движение · без запроса модели');motionButton.type='button';
        const output=el('div','small');
        motionButton.addEventListener('click',async()=>{
          const epoch=state.epoch;motionButton.disabled=true;
          try{
            const result=await api('/admin/pelican/'+encodeURIComponent(run.id)+'/motion',{method:'POST',body:{}});
            if(epoch!==state.epoch)return;
            output.replaceChildren(el('p','small','Фигур: '+number(result.shape_count)+' · Движение: '+(result.motion_detected?'обнаружено':'не обнаружено')+'. Это не оценка качества пеликана.'));
            const frames=el('div','pelican-motion-frames');
            for(let i=0;i<4;i++){const img=el('img');img.alt='Кадр '+(i+1);img.src='/admin/pelican/'+encodeURIComponent(run.id)+'/frames/'+i;img.loading='lazy';frames.append(img);}
            output.append(frames);
          }catch(error){if(epoch===state.epoch)output.replaceChildren(el('p','small','Не удалось выполнить локальную диагностику. Изображение сохранено; повтор не обращается к модели.'));}
          finally{if(epoch===state.epoch)motionButton.disabled=false;}
        });motion.append(motionButton,output);pelican.motionNodes.set(String(run.id),motion);card.append(motion);
      }
      return card;
    }));
    if (!runs.length) $('pelican-runs').append(el('p', 'empty', 'Генераций пока нет. Выбор поставщика не запускает запрос.'));
  }
  async function loadPelican() {
    if (!pelicanVisible() || pelican.controller || pelican.busy) return;
    clearTimeout(pelican.timer); pelican.timer = null;
    const controller = new AbortController(), epoch = state.epoch, sequence = ++pelican.sequence;
    pelican.controller = controller; updatePelicanControls();
    try {
      const data = await api('/admin/pelican', { signal: controller.signal });
      if (epoch !== state.epoch || sequence !== pelican.sequence || !pelicanVisible()) return;
      pelican.data = data;
      const selected = $('pelican-provider').value;
      $('pelican-provider').replaceChildren(...(data.providers || []).map(provider => { const option = el('option', '', provider.name || provider.id); option.value = provider.id; return option; }));
      if ([...$('pelican-provider').options].some(option => option.value === selected)) $('pelican-provider').value = selected;
      if (!$('pelican-provider').options.length) { const option = el('option', '', 'Нет доступных поставщиков'); option.value = ''; $('pelican-provider').append(option); }
      pelicanModels(); renderPelicanRuns(); areaError('pelican-error', pelican.actionError);
      text('pelican-status', 'Обновлено ' + date(new Date().toISOString()) + ' · Генераций: ' + number((data.runs || []).length));
    } catch (error) {
      if (epoch === state.epoch && sequence === pelican.sequence && !controller.signal.aborted) { areaError('pelican-error', pelicanError(error)); text('pelican-status', 'Нет свежих данных'); }
    } finally {
      if (epoch === state.epoch && sequence === pelican.sequence) { pelican.controller = null; updatePelicanControls(); schedulePelicanPoll(); }
    }
  }
  async function startPelican(event) {
    event.preventDefault();
    if (!pelicanVisible() || pelican.busy || $('pelican-start').disabled) return;
    const epoch = state.epoch;
    const body = { provider_id: $('pelican-provider').value, model: $('pelican-model').value, protocol: $('pelican-protocol').value, confirm_cost: true };
    if ($('pelican-effort').value) body.effort = $('pelican-effort').value;
    pelican.busy = true; updatePelicanControls(); stopPelicanPolling();
    const confirmed = await confirmAction('Одна платная генерация?', 'Будет отправлен ровно один платный запрос выбранному поставщику: ' + $('pelican-provider').selectedOptions[0].textContent + ', модель ' + body.model + ', протокол ' + body.protocol + ', effort ' + (body.effort || 'по умолчанию поставщика') + '. Фиксированная сцена: пеликан на велосипеде на морской набережной. Таймаут 5 минут, до ' + (['high', 'xhigh'].includes(body.effort) ? '64 000' : '16 000') + ' выходных токенов, включая рассуждения. Это верхний лимит, не фактическое списание. Автоматических повторов нет. Отмена не гарантирует отсутствие списания у поставщика.', 'Подтвердить оплату · 1 генерация');
    if (epoch !== state.epoch) return;
    try {
      if (!confirmed || !pelicanVisible()) return;
      pelican.mutationController = new AbortController();
      await api('/admin/pelican', { method: 'POST', body, signal: pelican.mutationController.signal });
      if (epoch === state.epoch) { pelican.actionError = ''; areaError('pelican-error', ''); }
    } catch (error) { if (epoch === state.epoch) { pelican.actionError = pelicanError(error); areaError('pelican-error', pelican.actionError); } }
    finally {
      if (epoch === state.epoch) { pelican.busy = false; pelican.mutationController = null; updatePelicanControls(); if (confirmed && pelicanVisible()) void loadPelican(); else schedulePelicanPoll(); }
    }
  }
  async function cancelPelican(id) {
    if (!pelicanVisible() || pelican.busy) return;
    const epoch = state.epoch;
    pelican.busy = true; pelican.cancelling.add(id); stopPelicanPolling(); updatePelicanControls(); renderPelicanRuns();
    try {
      pelican.mutationController = new AbortController();
      await api('/admin/pelican/' + encodeURIComponent(id) + '/cancel', { method: 'POST', body: {}, signal: pelican.mutationController.signal });
      if (epoch === state.epoch) { pelican.actionError = ''; areaError('pelican-error', ''); }
    } catch (error) { if (epoch === state.epoch) { pelican.actionError = pelicanError(error); areaError('pelican-error', pelican.actionError); } }
    finally {
      if (epoch === state.epoch) { pelican.busy = false; pelican.mutationController = null; pelican.cancelling.delete(id); updatePelicanControls(); renderPelicanRuns(); if (pelicanVisible()) void loadPelican(); }
    }
  }

  // Optional SaaS shell controls stay independent of API and mutation flows.
  function setNavigationOpen(open) {
    const wasOpen=document.body.classList.contains('nav-open');
    document.body.classList.toggle('nav-open', open);
    $('nav-toggle')?.setAttribute('aria-expanded', String(open));
    $('nav-toggle')?.setAttribute('aria-label', open ? 'Закрыть навигацию' : 'Открыть навигацию');
    if ($('nav-overlay')) $('nav-overlay').hidden = !open;
    if(open) $('workspace-nav')?.querySelector('.nav-tab.active')?.focus();
    else if(wasOpen && $('workspace-nav')?.contains(document.activeElement)) $('nav-toggle')?.focus();
  }
  $('nav-toggle')?.addEventListener('click', () => setNavigationOpen(!document.body.classList.contains('nav-open')));
  $('nav-overlay')?.addEventListener('click', () => setNavigationOpen(false));
  for (const [id, view, key, event] of [
    ['journal-search', journal, 'search', 'input'], ['journal-filter', journal, 'filter', 'change'], ['journal-sort', journal, 'sort', 'change'],
    ['request-search', requestsView, 'search', 'input'], ['request-filter', requestsView, 'filter', 'change'], ['request-sort', requestsView, 'sort', 'change']
  ]) $(id)?.addEventListener(event, () => { view[key] = $(id).value; if (view === journal) renderJournal(); else renderRequests(); });
  $('journal-detail-close')?.addEventListener('click', () => $('journal-detail').close());
  $('journal-detail')?.addEventListener('close', () => { if (state.session) renderJournal(); });
  $('detector-log-rows').addEventListener('focusout', () => queueMicrotask(() => { if (state.session && !$('journal-detail')?.open) renderJournal(); }));
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || !state.session || event.isComposing) return;
    // Native modal Escape/cancel semantics preserve one-time token safeguards.
    if (document.querySelector('dialog[open]')) return;
    if (event.key === 'Escape') { setNavigationOpen(false); return; }
    if(event.key==='Tab' && document.body.classList.contains('nav-open') && matchMedia('(max-width:760px)').matches){
      const nodes=[...$('workspace-nav').querySelectorAll('a[href],button:not(:disabled)')];
      if(event.shiftKey && document.activeElement===nodes[0]){event.preventDefault();nodes.at(-1)?.focus();return;}
      if(!event.shiftKey && document.activeElement===nodes.at(-1)){event.preventDefault();nodes[0]?.focus();return;}
    }
    const editing = event.target instanceof Element && !!event.target.closest('input,textarea,select,[contenteditable="true"]');
    if (event.key === '/' && !editing && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const search = state.tab === 'detector-log' ? $('journal-search') : state.tab === 'stats' ? $('request-search') : null;
      if (search) { event.preventDefault(); search.focus(); }
    }
    if (event.altKey && !event.ctrlKey && !event.metaKey && /^[1-7]$/.test(event.key)) {
      const tabs = ['stats', 'settings', 'detector', 'detector-log', 'market', 'pelican', 'access'];
      event.preventDefault(); $('tab-' + tabs[Number(event.key) - 1])?.click();
    }
  });

  // Event wiring.
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    $('login-button').disabled = true; text('login-button', 'Вход…'); areaError('login-error', '');
    try {
      const username = $('username').value.trim();
      const session = await api('/admin/login', { method: 'POST', auth: false, body: { username, password: $('password').value } });
      if (!session?.csrf_token) throw new Error('Invalid login response');
      showApp({ username, csrf_token: session.csrf_token });
    } catch (error) { areaError('login-error', error.status === 401 ? 'Неверное имя пользователя или пароль.' : errorMessage(error)); }
    finally { $('password').value = ''; $('login-button').disabled = false; text('login-button', 'Войти в панель ↗'); }
  });
  $('session-retry').addEventListener('click', restoreSession);
  $('logout').addEventListener('click', async () => {
    if ((state.dirty || detector.dirty || pools.dirty) && !await confirmAction('Выйти без сохранения?', 'Несохранённые изменения и введённые ключи поставщиков будут потеряны.', 'Выйти')) return;
    $('logout').disabled = true;
    pelican.signingOut = true; stopPelicanPolling(); updatePelicanControls();
    if (pelican.mutationController) pelican.mutationController.abort();
    try { await api('/admin/logout', { method: 'POST' }); showLogin(); }
    catch (error) { if (state.session) showNotice('Выход не подтверждён сервером. ' + errorMessage(error), 'danger'); }
    finally { $('logout').disabled = false; if (state.session) { pelican.signingOut = false; updatePelicanControls(); updatePelicanPolling(); } }
  });
  $('tab-stats').addEventListener('click', () => switchTab('stats'));
  $('tab-settings').addEventListener('click', () => switchTab('settings'));
  $('tab-access').addEventListener('click', () => switchTab('access'));
  $('a6-tab-pools').addEventListener('click', () => activateMarketSection('pools'));
  $('a6-tab-creds').addEventListener('click', () => activateMarketSection('creds'));
  // Roving focus for the A6API tablist, matching the pool sections pattern below.
  const marketTabs = ['pools', 'creds'];
  for (const [index, section] of marketTabs.entries()) {
    $('a6-tab-' + section).addEventListener('keydown', event => {
      const step = { ArrowRight: 1, ArrowLeft: marketTabs.length - 1 }[event.key];
      let next;
      if (step) next = (index + step) % marketTabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = marketTabs.length - 1;
      else return;
      event.preventDefault();
      activateMarketSection(marketTabs[next]);
      $('a6-tab-' + marketTabs[next]).focus();
    });
  }
  $('tab-detector').addEventListener('click', () => switchTab('detector'));
  $('tab-detector-log').addEventListener('click', () => switchTab('detector-log'));
  $('tab-market').addEventListener('click', () => switchTab('market'));
  $('tab-pelican').addEventListener('click', () => switchTab('pelican'));
  $('pelican-refresh').addEventListener('click', () => void loadPelican());
  $('pelican-provider').addEventListener('change', pelicanModels);
  $('pelican-model').addEventListener('change', updatePelicanControls);
  $('pelican-form').addEventListener('submit', startPelican);




  $('pools-refresh').addEventListener('click', () => void loadPools(true));
  $('pool-events-refresh').addEventListener('click', () => void loadPoolEvents(true));
  $('pool-membership-history').addEventListener('toggle', () => { if ($('pool-membership-history').open) void loadPoolEvents(); });
  $('pool-new').addEventListener('click', () => void selectPool(null));
  $('pool-reset').addEventListener('click', async () => {
    if (pools.dirty && !await confirmAction('Сбросить правки пула?', 'Будет загружена последняя полученная ревизия политики.', 'Сбросить')) return;
    pools.selected = (pools.data?.pools || []).find(p => p.id === pools.selected?.id) || null;
    invalidatePoolPreview(); renderPoolForm(pools.selected || {}); renderPools();
  });
  $('pool-form').noValidate = true;
  $('pool-form').addEventListener('submit', savePool);
  for (const event of ['input', 'change']) $('pool-form').addEventListener(event, () => { pools.dirty = true; invalidatePoolPreview(); updatePoolControls(); });
  $('pool-discover').addEventListener('click', () => void poolAction('discover'));
  $('pool-run').addEventListener('click', () => void poolAction('run'));
  $('pool-restart').addEventListener('click', () => void poolAction('restart'));
  for (const [index, section] of poolSections.entries()) {
    const button = $('pool-section-tab-' + section);
    button.addEventListener('click', () => setPoolSection(section));
    button.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % poolSections.length;
      else if (event.key === 'ArrowLeft') next = (index + poolSections.length - 1) % poolSections.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = poolSections.length - 1;
      else return;
      event.preventDefault(); setPoolSection(poolSections[next], true);
    });
  }
  $('pool-cancel').addEventListener('click', () => {
    const job = selectedPoolJobs().find(item => ['queued', 'running'].includes(item.status));
    if (job) void poolMutation(() => api('/admin/a6/pools/jobs/' + encodeURIComponent(job.id) + '/cancel', { method: 'POST', body: {} }));
  });
  $('pool-verify').addEventListener('click', () => void poolAction('verify'));
  $('pool-delete').addEventListener('click', () => void poolAction('delete'));
  $('pool-preview').addEventListener('click', () => void previewPool());
  $('pool-publish').addEventListener('click', () => void publishPool());
  $('a6-form').addEventListener('submit', saveA6);
  $('a6-verify').addEventListener('click', verifyA6);
  $('a6-clear').addEventListener('click', clearA6);
  $('a6-reload').addEventListener('click', loadA6);
  $('refresh-detector').addEventListener('click', loadDetector);
  $('refresh-detector-log').addEventListener('click', loadDetectorLog);
  $('detector-settings-form').addEventListener('submit', saveDetectorSettings);
  $('detector-check-form').addEventListener('submit', startDetectorCheck);
  $('detector-enabled').addEventListener('change', () => { if (detector.draft) { detector.draft.enabled = $('detector-enabled').checked; markDetectorDirty(); } });
  $('detector-interval').addEventListener('input', () => { if (detector.draft) { detector.draft.interval_minutes = Number($('detector-interval').value); markDetectorDirty(); } });
  $('detector-threshold').addEventListener('input', () => { if (detector.draft) { detector.draft.threshold_percent = Number($('detector-threshold').value); markDetectorDirty(); } });
  $('detector-provider').addEventListener('change', updateManualModels);
  $('detector-model').addEventListener('input', () => { $('detector-reference').value = exactDetectorReference($('detector-model').value); });
  $('reload-detector-settings').addEventListener('click', async () => {
    const epoch = state.epoch;
    if (detector.dirty && !await confirmAction('Отбросить настройки проверок?', 'Несохранённые цели и настройки мониторинга будут потеряны.', 'Перезагрузить')) return;
    if (epoch !== state.epoch) return;
    detector.dirty = false; areaError('detector-settings-error', ''); void loadDetector();
  });
  $('brand-link').addEventListener('click', event => { event.preventDefault(); switchTab('stats'); });
  $('refresh-stats').addEventListener('click', loadStats);
  $('dismiss-notice').addEventListener('click', () => { $('global-notice').hidden = true; });
  for (const button of document.querySelectorAll('[data-period]')) button.addEventListener('click', () => {
    if (state.period === button.dataset.period) return;
    state.period = button.dataset.period;
    for (const item of document.querySelectorAll('[data-period]')) item.setAttribute('aria-pressed', String(item === button));
    // Old-period numbers must never look like newly selected-period data.
    state.stats = null; requestsView.signature = '';
    if ($('request-count')) text('request-count', 'Загрузка…');
    for (const id of ['metric-requests', 'metric-errors', 'metric-switches', 'metric-tokens']) text(id, '—');
    text('metric-success', 'Загрузка периода…');
    $('activity-chart').replaceChildren(el('div', 'empty', 'Загрузка выбранного периода…'));
    $('chart-axis').replaceChildren();
    for (const id of ['token-breakdown', 'performance', 'provider-stats', 'recent-errors']) $(id).replaceChildren(el('div', 'empty', 'Загрузка…'));
    renderTable('provider-table', [], 13, 'Загрузка…'); renderTable('requests-table', [], 8, 'Загрузка…');
    text('coverage-note', 'Информация о покрытии загружается…');
    void loadStats();
  });
  for (const button of document.querySelectorAll('[data-settings]')) button.addEventListener('click', () => activateSettings(button.dataset.settings));
  $('reload-config').addEventListener('click', async () => {
    if (state.dirty && !await confirmAction('Отбросить несохранённые изменения?', 'Будет загружена актуальная серверная конфигурация. Правки и введённые ключи поставщиков будут потеряны.', 'Отбросить')) return;
    void loadConfig();
  });
  $('add-provider').addEventListener('click', () => {
    try {
      const priority = state.draft.providers.length ? Math.max(...state.draft.providers.map(p => Number(p.priority) || 0)) + 1 : 1;
      state.draft.providers.push({ id: uuid(), name: '', base_url: '', enabled: true, priority, has_api_key: false }); markDirty(); renderProviders();
      const cards = $('provider-editor').children; cards[cards.length - 1]?.querySelector('input:not([type=checkbox])')?.focus();
    } catch { showNotice('Браузер не поддерживает безопасное создание ID. Откройте панель по HTTPS в современном браузере.', 'danger'); }
  });
  $('add-model').addEventListener('click', () => {
    try {
      state.draft.models.push({ id: uuid(), name: '', supports_vision: false, enabled: true, context_length: null, max_output_tokens: null, routes: [] }); markDirty(); renderModels();
      const cards = $('model-editor').children; cards[cards.length - 1]?.querySelector('input:not([type=checkbox])')?.focus();
    } catch { showNotice('Браузер не поддерживает безопасное создание ID. Используйте HTTPS и современный браузер.', 'danger'); }
  });
  $('config-form').noValidate = true;
  $('config-form').addEventListener('submit', saveConfig);
  $('trust-private').addEventListener('change', () => { if (state.draft) { state.draft.settings.trust_private_upstreams = $('trust-private').checked; markDirty(); } });
  $('key-form').addEventListener('submit', createKey);
  $('account-form').addEventListener('submit', changeAccount);
  $('refresh-keys').addEventListener('click', loadKeys);
  $('close-token').addEventListener('click', () => $('token-dialog').close());
  $('token-dialog').addEventListener('cancel', event => { event.preventDefault(); text('copy-status', 'Сначала сохраните токен. Для закрытия нажмите «Сохранён, закрыть».'); });
  $('token-dialog').addEventListener('close', () => { text('new-token', ''); text('copy-status', ''); });
  $('copy-token').addEventListener('click', async () => {
    let copied = false;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText($('new-token').textContent);
      copied = true;
    } catch {
      try {
        const range = document.createRange(); range.selectNodeContents($('new-token'));
        const selection = window.getSelection();
        if (selection) {
          selection.removeAllRanges(); selection.addRange(range);
          copied = document.execCommand('copy') === true;
        }
      } catch { /* Keep the selected token available for manual copying. */ }
    }
    text('copy-status', copied
      ? 'Токен скопирован. Буфер обмена доступен другим приложениям; очистите его после использования.'
      : 'Автокопирование не удалось. Выделите токен, если он не выделен, и нажмите Ctrl+C (на Mac — Cmd+C).');
  });
  document.addEventListener('visibilitychange', () => { updatePolling(); updateDetectorPolling(); updatePoolsPolling(); updatePelicanPolling(); });
  window.addEventListener('beforeunload', event => {
    if (state.dirty || detector.dirty || pools.dirty || $('token-dialog').open) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => { clearSecrets(); stopPolling(); stopDetectorPolling(); stopPoolsPolling(); stopPelicanPolling(); if (pelican.mutationController) pelican.mutationController.abort(); });
  window.addEventListener('pageshow', event => { if (event.persisted) { showLogin(); void restoreSession(); } });
  void restoreSession();
})();
