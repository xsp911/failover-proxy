# Run locally, without touching production

## Scope and prerequisites

- Python **3.12+**. The project declares this minimum in [pyproject.toml](../pyproject.toml#L5); use the exact pins in [requirements.lock](../requirements.lock). The lock is shared by 3.12 and 3.14, not a platform-specific Linux wheel export. CI installs it on both OSes; Python 3.14 is not rejected by the launcher.
- No Docker, service installation, frontend build, React, npm or Node is required to serve the HTML/CSS/vanilla-JavaScript UI and core gateway. Uvicorn is installed without `[standard]`; **uvloop is not required**, particularly on Windows.
- **Node/npm are not launch prerequisites.** The launcher does not check for, install, or invoke them. Only the optional backend numerical-scoring feature requires Node >=22 plus the checked-in detector artifacts (CI uses Node 24). Without Node, numerical monitoring is unavailable; the static UI and core gateway still start normally. Optional SVG motion capture additionally needs Playwright and Chromium. Both optional features can be omitted; see below.
- Local default: `http://127.0.0.1:8843`, state in `~/.failover-proxy` (`Path.home()` on Windows). No default admin username or password is created.
- Never copy production secrets or production data into a local test. Keep local state outside the checkout and outside `/opt/failover-proxy/data`. The launcher explicitly refuses that legacy production directory; it does not inspect, stop, restart, or replace any service.

## Linux

From the checkout root (quote the path if it contains spaces):

```bash
python3 --version                       # 3.12 or newer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python tools/run-local.py
```

On first initialization, an interactive terminal asks for a new username and a password twice. Password length: 12–1024 characters. Password entry uses `getpass`; input is not echoed. No hard-coded secret, generated default account, or credential printout.

For a noninteractive launch, set **both** `FP_ADMIN_USERNAME` and `FP_ADMIN_PASSWORD` in the process environment using your secret-management mechanism. Do not put a real password in a command copied into shell history. Missing/partial/short credentials fail before any database is created. After successful first startup, unset the variables in the parent shell; they are unnecessary on later launches.

## Windows PowerShell

Native Windows support is implemented through [platform_io.py](../proxy/platform_io.py), rather than importing POSIX `fcntl` on Windows. **Native Windows has not been executed on the current Linux development host.** The checked-in matrix is a validation gate, not proof that a Windows runner has already passed. Use WSL2/Linux if native Windows validation is unavailable or any dependency pin lacks a wheel for your interpreter.

```powershell
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe tools\run-local.py
```

Python 3.14 alternative: replace `py -3.12` with `py -3.14`. Invoking the venv interpreter directly avoids PowerShell script execution-policy changes. Do not install uvloop.

First startup prompts for the account in a real terminal, as on Linux. For automation, supply both credential environment variables securely. The launcher never rotates an existing account even if those variables remain set.

Windows file security differs from Linux: `chmod` mode bits do **not** establish a private NTFS ACL. Use a per-user profile directory with inherited access limited to the intended account; review ACLs with `Get-Acl "$HOME\.failover-proxy"`. Avoid shared/public/network directories. Windows initialization uses a stable `.store.lock` byte-range lock; do not delete it while instances are running. Do not run POSIX and native Windows processes against the same database via WSL/shared mounts.

## Options and state safety

```text
python tools/run-local.py --host 127.0.0.1 --port 8843 --data-dir "path with spaces/local-state"
```

The frontend is checked-in static HTML/CSS/JavaScript served directly by FastAPI: no frontend build command, Node server, npm install, or asset-generation step belongs in the launch sequence. Optional design/icon tooling is development-only; its generated assets are already checked in and are not rebuilt at runtime.

The launcher resolves its own checkout path, inserts that application root into Python's import path, then starts `uvicorn.run("proxy.app:create_app", factory=True, ...)`. It works from a different current directory. `~` in the data argument expands to the user's home. Relative data paths resolve from the caller's current directory. No service changes, migration, dependency install, npm invocation, or paid request is performed by the launcher.

Before startup it opens an existing `proxy.db` **read-only** and checks for `admin(singleton=1)`. Existing admin: no prompt, no initialization request, no credential overwrite. Missing admin: require first-init credentials even if a config table already exists. A corrupt/unreadable database fails closed; do not delete it to make the launcher proceed. The application preserves credentials with `INSERT OR IGNORE` too. State includes the SQLite databases, journals and `secret.key`; losing/replacing the key loses access to encrypted upstream secrets. Back up the entire stopped-instance directory together.

The launcher overrides `FP_DATA_DIR` with the chosen local directory and chooses a port-specific local cookie name, avoiding the historical production cookie name. Do not bypass the launcher with a bare factory command: [create_app](../proxy/app.py#L142-L147) otherwise retains a historical production-directory fallback. Keep the default loopback bind. Binding `0.0.0.0`/a public address exposes the admin surface; use a trusted TLS reverse proxy and access controls if remote access is necessary.

Stop with Ctrl+C. Creating providers, running numerical checks or Pelican generation, and A6 pool verification/publication can incur upstream costs or remote mutations when configured. Use empty isolated local state or mocked tests; importing the UI is not permission to run paid checks.

## Optional backend runtimes

### Optional numerical scorer

Skip this entire section for ordinary local UI/gateway use. To enable numerical model checks, install a supported Node runtime (>=22; CI: 24) through your preferred installer. Keep [detector](../detector/catalog.json) artifacts together in the checkout. No npm packages or frontend build are needed by the numerical scorer.

```bash
node --version
.venv/bin/python -m pytest -q tests/test_detector_scoring.py
```

PowerShell: use `.\.venv\Scripts\python.exe` instead. Tests verify pinned artifact hashes, original numerical parity, malformed-input handling and execution from another directory. Confidence is closed-set evidence, not proof of model identity.

### Optional Pelican motion capture

Skip this entire section unless optional motion diagnostics are wanted. Static sanitized SVG generation/display and normal UI/gateway launch do not require Playwright or npm. Motion capture is a separate backend local renderer; it does not call a model, but its input SVG originates from an explicit generation that may cost money. Install these optional dependencies **outside the checkout** so no package manifest or frontend dependency changes are needed:

```bash
mkdir -p "$HOME/.failover-proxy-browser"
npm install --prefix "$HOME/.failover-proxy-browser" --no-save playwright
"$HOME/.failover-proxy-browser/node_modules/.bin/playwright" install chromium
export FP_PLAYWRIGHT_MODULE="$HOME/.failover-proxy-browser/node_modules/playwright"
```

PowerShell equivalent:

```powershell
$renderer = Join-Path $HOME '.failover-proxy-browser'
npm install --prefix $renderer --no-save playwright
& "$renderer\node_modules\.bin\playwright.cmd" install chromium
$env:FP_PLAYWRIGHT_MODULE = "$renderer\node_modules\playwright"
```

Chromium may require OS libraries on Linux; these commands intentionally do not install system packages. Native Windows motion capture deliberately returns `pelican_renderer_unavailable` until a tested Windows process-tree cleanup implementation exists; do not infer renderer support from passing gateway tests. The Linux renderer retains POSIX process-group cleanup. Normal SVG generation/display, the rest of the Python app and static UI remain separate. See [the audit](FRONTEND-AUDIT.md#platform-audit-and-validation-status).

## Offline validation and CI boundaries

```bash
.venv/bin/python -m compileall -q proxy tools/run-local.py
.venv/bin/python -m pytest -q tests/test_platform_launcher.py tests/test_platform_io.py tests/test_pelican_svg_security.py tests/test_pelican_smil_offsets.py
.venv/bin/python -m pytest -q tests/test_app.py tests/test_security.py tests/test_admin_account.py
```

These use temporary state, mock upstream transports and isolated launcher subprocesses; no live credentials or production data. The [compatibility workflow](../.github/workflows/compatibility.yml) runs these on Ubuntu/Windows and Python 3.12/3.14, with Node 24 numerical tests in a separate OS matrix. It does not install Playwright, run paid checks, or deploy anything.

**Explicit exclusions:** full Store permission/symlink/Unix-lock tests and operational service checks are not asserted portable; full pool/monitor integration and browser suites are not part of this small compatibility gate; optional Playwright motion is excluded. Historical [browser.cjs](../tests/browser.cjs#L1-L9) hard-codes a live 9097 URL, credential file and absolute Playwright path, then performs writes: **do not run it as a local smoke test**. Use only a dedicated isolated mock-backed browser harness reviewed for its target URL. A green selected-test matrix is not a claim that the complete historical suite passes on Windows.

Current local verification: Linux, Python 3.14, compilation passed; all selected launcher/platform/SVG/app/security/account/numerical tests passed (**95 passed, 1 upstream deprecation warning**, 27.94s). This run included 21 isolated launcher/platform tests; final credential-hardening refinements separately passed all 23 launcher/platform tests in 0.53s. Windows/3.12 runner results have not been observed here; current lock installation and native Windows behavior remain subject to CI.
