# Frontend and platform audit

## Boundary and architectural decision

Audit scope: current static admin interface, its FastAPI contracts, persistence/runtime constraints, safe local launch and cross-platform validation. This is a source inventory, not a claim that every UI state or Windows runtime was exercised. No production state, paid upstream request, service, or remote deployment is needed for this work.

**Keep FastAPI + vanilla JavaScript intentionally.** [The factory](../proxy/app.py#L142-L179) already owns authentication, configuration and background managers; [static routes](../proxy/app.py#L218-L225) serve HTML, CSS and JavaScript directly. [The frontend](../web/app.js#L1-L14) is dependency-free, with explicit session/draft/job state. Replacing it with React would add a build/distribution boundary, retest DOM selectors, dialogs, form state, polling and asynchronous mutation semantics without a product requirement. A visual redesign can be substantial while retaining this lower-risk runtime and existing APIs. The frontend is checked-in static HTML/CSS/JavaScript served directly by FastAPI: **no frontend build, Node runtime, npm install, or asset-generation step is required to launch it**. Design/icon tooling is development-only; generated assets are checked in. Node >=22 is a dependency only of the **optional backend numerical-scoring feature**, not application startup; without Node that feature is unavailable while the UI/core gateway still starts. Optional Playwright motion capture is a separate backend capability.

The redesign boundary is layout/semantic markup/design tokens/styles and focused frontend interaction improvements. Preserve API URLs/methods/payloads, auth/CSRF, config revisions, provider routing and model behavior, explicit paid/remote-operation consent and buffered output. Backend platform adapters are compatibility changes, not permission to rewrite business logic.

## Current frontend feature inventory

| Area | Existing behavior to preserve | Source |
| --- | --- | --- |
| Session entry | Boot/session recovery, username/password login, HTTP-only cookie, logout, expiry recovery; no persistent browser secret storage | [markup](../web/index.html#L15-L35), [request wrapper](../web/app.js#L101-L131), [secret cleanup](../web/app.js#L149-L158) |
| Dashboard | Period filters; requests/errors/failovers/tokens; activity chart; latency/TTFT/throughput; provider and request tables; recent errors; missing usage stays unknown | [dashboard markup](../web/index.html#L39-L62), [stats endpoint](../proxy/app.py#L321-L331) |
| Activity and capability evidence | Separate client/monitor/pool accounting, passive capability/context observations; observations are not guaranteed model limits | [markup](../web/index.html#L50-L51), [API](../proxy/app.py#L287-L296), [capability persistence](../proxy/capability_store.py#L43-L87) |
| Providers and model aliases | Provider URL/priority/enabled/key; OpenRouter service tier/pin; model alias, route bindings, vision/context/output metadata; draft/save/reload | [editor markup](../web/index.html#L64-L74), [strict config validation](../proxy/config.py#L58-L137) |
| Gateway limits | Request/connect/read deadlines, cooldown/failure threshold, size/event/concurrency/attempt bounds; private upstream opt-in with SSRF warning | [validation bounds](../proxy/config.py#L6-L13), [UI](../web/index.html#L73-L73) |
| Client keys | Per-name/model scope, one-time full token, prefixes afterwards, enable/disable/revoke; not an admin password | [key UI](../web/index.html#L75-L75), [API](../proxy/app.py#L454-L486) |
| Admin account | Current-password verification; rename/password update; >=12-character new password; all sessions invalidated and reauthentication | [form](../web/index.html#L76-L89), [endpoint](../proxy/app.py#L267-L285) |
| Numerical checks | Detector state/catalog/settings, manual enqueue, cancel, journal; quarantine/runtime routing and configuration identity remain backend-owned | [API](../proxy/app.py#L418-L452), [monitor integration](../proxy/app.py#L528-L533) |
| A6 account/marketplace | Encrypted console PAT and user ID, verify/save/clear; token/seller reads; blank PAT retains stored secret | [UI](../web/index.html#L91-L103), [API](../proxy/app.py#L333-L400) |
| Managed A6 pools | Policy CRUD; discover, paid run/restart/verify; cancel; preview-before-publish; revision/identity guards; membership history and unban | [pool API](../proxy/pool_api.py#L15-L109) |
| Pelican | Fixed-scene manual SVG generation, cancel, sanitized output, sandboxed SVG response, optional motion/frames; not proof of identity or semantic quality | [Pelican routes](../proxy/pelican_api.py#L15-L64), [SVG safety tests](../tests/test_pelican_svg_security.py#L8-L42) |
| Async safety | AbortControllers, bounded request timeout, status/error normalization, epoch/sequence protection, polling stops off-tab/hidden; dirty state must survive recoverable conflicts | [state](../web/app.js#L9-L15), [API wrapper/polling](../web/app.js#L101-L147) |

Russian copy and `ru-RU` numeric/date formatting are existing product behavior, not accidental localization. Retain meaningful labels, error explanations and buffered-mode warnings across visual changes. Do not translate just the navigation while leaving ambiguous forms.

## API contract inventory

All `/admin/*` reads require a session except login; mutations additionally require `X-CSRF-Token`. Same-origin/Sec-Fetch-Site protections also apply. Public liveness is not an authorization probe. Route placeholders below are literal backend parameters.

| Methods | Path(s) | Contract/source |
| --- | --- | --- |
| GET | `/`, `/styles.css`, `/app.js`, `/health/liveliness` | Static delivery and basic liveness; [routes](../proxy/app.py#L214-L225) |
| POST / GET / POST | `/admin/login`, `/admin/session`, `/admin/logout` | Login returns CSRF token; session-bound cookie; [implementation](../proxy/app.py#L227-L265) |
| POST | `/admin/account` | `current_password`, `new_username`, `new_password`; reauthenticate on success; [implementation](../proxy/app.py#L267-L285) |
| GET / PUT | `/admin/config` | Redacted reads; complete validated config with `revision`; 409 on stale revision; [implementation](../proxy/app.py#L298-L319) |
| GET | `/admin/stats?period=hour|day|week|month`, `/admin/health`, `/admin/observability`, `/admin/capabilities` | Operational and passive evidence; [stats](../proxy/app.py#L321-L331), [health](../proxy/app.py#L402-L405), [evidence](../proxy/app.py#L287-L296) |
| POST | `/admin/providers/{provider_id}/test` | Saved provider `/models` availability, not proof of generation; [route](../proxy/app.py#L407-L416) |
| GET / POST | `/admin/keys` | List redacted key metadata; create returns token once; [routes](../proxy/app.py#L454-L469) |
| PATCH / DELETE | `/admin/keys/{key_id}` | Boolean enabled update or revoke; [routes](../proxy/app.py#L471-L486) |
| GET / PUT / DELETE | `/admin/a6` | Status, credentials save, clear; [routes](../proxy/app.py#L361-L400) |
| POST / GET / GET | `/admin/a6/verify`, `/admin/a6/tokens`, `/admin/a6/sellers` | Account verification and marketplace reads; [routes](../proxy/app.py#L333-L395) |
| GET / PUT / POST / GET | `/admin/detector`, `/admin/detector/settings`, `/admin/detector/check`, `/admin/detector/log` | Manual check returns 202; revision-aware settings; [routes](../proxy/app.py#L418-L452) |
| POST | `/admin/detector/jobs/{job_id}/cancel` | Cancels backend job; [route](../proxy/app.py#L444-L447) |
| GET / POST | `/admin/a6/pools` | Pool list/create policy; [routes](../proxy/pool_api.py#L15-L25) |
| PUT / DELETE | `/admin/a6/pools/{pool_id}` | Revisioned policy/delete; delete explicit `confirm_remote`; [routes](../proxy/pool_api.py#L27-L39) |
| POST | `/admin/a6/pools/{pool_id}/discover`, `/run`, `/restart`, `/verify` | Enqueue with cost confirmation where required; [routes](../proxy/pool_api.py#L41-L69) |
| POST | `/admin/a6/pools/jobs/{job_id}/cancel`, `/admin/a6/pools/{pool_id}/preview`, `/publish`, `/unban` | Preview/publish identity and explicit confirm remain intact; [routes](../proxy/pool_api.py#L71-L109) |
| GET | `/admin/a6/pools/{pool_id}/events` | Bounded membership history; [route](../proxy/pool_api.py#L90-L96) |
| GET / POST | `/admin/pelican` | Status/list or manual enqueue; [routes](../proxy/pelican_api.py#L15-L27) |
| POST / GET | `/admin/pelican/{id}/cancel`, `/admin/pelican/{id}/svg` | Cancel and sanitized SVG; [routes](../proxy/pelican_api.py#L28-L32), [SVG](../proxy/pelican_api.py#L59-L64) |
| POST / GET / GET | `/admin/pelican/{id}/motion`, `/admin/pelican/{id}/motion`, `/admin/pelican/{id}/frames/{index}` | Optional cached offline capture, bounded frames; [routes](../proxy/pelican_api.py#L33-L58) |
| GET / POST | `/v1/models`, `/v1/chat/completions` | Bearer client key, model-scoped access; [routes](../proxy/app.py#L488-L557) |

### Security and transport invariants

- [Security middleware](../proxy/app.py#L114-L139): no-store, nosniff, no-referrer, deny framing, same-origin CSP. Scripts remain self-hosted; no inline script, remote font/CDN dependency, `eval`, or unsafe rendered upstream HTML.
- [Frontend request helper](../web/app.js#L101-L131): same-origin credentials, no cache, redirect error, bounded timeout, CSRF on mutation, allowlisted error code rather than raw body display. Keep secrets out of local/session storage, query strings, screenshots and logs.
- [Config validation](../proxy/config.py#L33-L55): HTTPS public upstream default, no URL credentials/query/fragment, private destination opt-in; routing hints cannot silently weaken SSRF policy.
- [Chat endpoint](../proxy/app.py#L495-L570): bounded admission before body read, reject caller routing overrides, disconnect cancellation, fully buffer successful upstream output. SSE is **replay after completion**, not live tokens. Preserve `X-Request-ID`, `X-Buffered-Response` and upstream attribution semantics.
- [Persistence](../proxy/store.py#L108-L190): SQLite, encryption key, admin password hashing, revisioned configuration and independent metrics. Never regenerate a damaged key or reset credentials to bypass a local startup issue.
- A6/Pelican/numerical checks can issue real remote/paid calls. UI convenience controls must not add hidden checks on render, remove confirmations, retry ambiguous mutations automatically, or treat a timed-out mutation as definitely unapplied.

## Redesign acceptance and regression constraints

1. Keep stable control IDs and data attributes used by existing event wiring/browser contracts. Replace layout wrappers/styles before rewriting state machines. Native forms/dialogs remain keyboard-operable.
2. Sidebar/header hierarchy should distinguish overview, routing, keys/account, diagnostics, managed pools and visual probe. Show the active section, unsaved changes and running-job state without relying only on color.
3. Dense operational tables need local horizontal scrolling, stable headers, legible missing values and accessible captions; the page itself must not overflow narrow viewports. Filtering/sorting must not mutate backend configuration.
4. Preserve loading, empty, error, stale, saved, conflict and permission-expired states. A redesigned chart is not complete if its accessible table/text disappears. Announce important errors/status without flooding live regions during polling.
5. Preserve explicit cost and remote-write confirmations, one-time token reveal, sensitive-field cleanup, focus return on dialogs and account-change reauthentication. Do not silently discard dirty edits on tab navigation or polling.
6. Verify keyboard focus, reduced motion, 375–390px mobile width, desktop, no console exceptions, no mock/real endpoint confusion. Contrast and focus visibility require browser inspection, not a source-only claim.
7. Existing Python coverage is broad (roughly 900 baseline tests reported by the parent task); historical browser scripts are numerous and deployment-specific. Neither count is a verified result in this document. Record exact executed commands/results separately; do not call source inventory a passed test suite.

## Platform audit and validation status

### Findings before compatibility migration

- [Store](../proxy/store.py) originally imported `fcntl` unconditionally, opened directories with `os.O_DIRECTORY`, used `flock`, and called `os.fchmod`. Native Windows could not import/start it; this was an actual blocker, not merely a missing Windows smoke test.
- Auxiliary capability/detector/pool/history/Pelican stores also used descriptor `fchmod`. A launcher alone cannot make these Windows-compatible. The scoped [platform adapter](../proxy/platform_io.py) keeps POSIX directory locks and uses a stable Windows byte-range lock file; parent-owned persistence integrations replace direct POSIX calls. Windows `private_mode` is deliberately not a claim to enforce NTFS ACLs.
- Path portability: [the launcher](../tools/run-local.py) uses `Path.home`, `Path.resolve`, its own checkout root, argparse and Python's Uvicorn API; no shell activation, hard-coded venv, service manager or 9097 state. Read-only SQLite admin detection prevents reinitialization prompts/credential rotation.
- [Model monitor](../proxy/model_monitor.py) locates Node via `shutil.which` and launches the scorer as a subprocess. [Numerical tests](../tests/test_detector_scoring.py#L148-L178) check path independence and pinned artifact integrity. Node >=22 is required by the scorer; CI chooses 24.
- Optional [motion renderer](../proxy/pelican_motion.py#L46-L95) historically starts a POSIX session and kills a process group with `os.killpg`/`SIGKILL`. That is **not native Windows process-tree handling**. Without a separate adaptation, native Windows motion remains unsupported/unvalidated even when core app startup works. Playwright/browser availability is a second independent dependency.
- Historical [browser smoke](../tests/browser.cjs#L1-L9) hard-codes `/tmp` Playwright, production credential-file and live port 9097; later creates/revokes keys and saves configuration. It is neither portable nor safe as an unreviewed local test. Other legacy operational scripts may similarly assume systemd, POSIX permissions or absolute deployment paths.

### What is validated versus planned

[Compatibility CI](../.github/workflows/compatibility.yml) declares Ubuntu/Windows × Python 3.12/3.14, exact lock install, compilation, isolated launcher subprocess tests, concurrent platform-lock tests, pure config/security contracts, SVG safety and mocked native app auth/config/key/account tests. Node24 numerical parity/artifact tests run in a separate Ubuntu/Windows matrix. No Playwright install, browser live suite, production access, deployment or paid call is included.

Current host verification: Linux Python 3.14, compilation passed; **95 selected tests passed, 1 upstream deprecation warning, 27.94s** across launcher/platform/SVG/app/security/account/numerical tests (including 21 owned launcher/platform tests at that point); final credential-hardening refinements separately passed all 23 launcher/platform tests in 0.53s. Windows has **not** been run from this host; checked-in CI is not an observed passing CI run. Lock availability for other platforms/interpreters is gated by install, not assumed. Full POSIX file permission/symlink and system-service tests, complete integration suite, historical live browser suites and optional motion are explicitly outside this selected cross-platform gate. See [running instructions](RUNNING.md) for secure local setup and exact offline commands.
