#!/usr/bin/env bash
# Offline release gate. No installs, production credentials, servers, commits or pushes.
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON="${FP_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  printf '%s\n' 'Set FP_PYTHON to the project virtualenv Python; install requirements.lock first.' >&2
  exit 2
fi
contracts=(
  tests/browser-feedback.cjs
  tests/browser-proxy-access.cjs
  tests/browser-provider-editor.cjs
  tests/browser-provider-health.cjs
  tests/browser-scoped-settings.cjs
  tests/browser-detector.cjs
  tests/browser-a6.cjs
  tests/browser-market.cjs
  tests/browser-pools-fast.cjs
  tests/browser-pool-errors.cjs
  tests/browser-pool-billing.cjs
  tests/browser-pools.cjs
  tests/browser-pool-auto.cjs
  tests/browser-a6-usability.cjs
  tests/browser-a6-navigation.cjs
  tests/browser-pool-optional.cjs
  tests/browser-account.cjs
  tests/browser-pool-history-status.cjs
  tests/browser-project-observability.cjs
  tests/browser-pelican.cjs
  tests/browser-provider-policy.cjs
  tests/browser-capability-evidence.cjs
  tests/browser-pelican-motion.cjs
  tests/browser-saas-interactions.cjs
  tests/browser-console-visual.cjs
)
if [[ "${1:-}" == '--list-browser-contracts' ]]; then
  printf '%s\n' "${contracts[@]}"
  exit 0
fi
if [[ -n "${1:-}" && "${1:-}" != '--browser-contracts' ]]; then
  printf '%s\n' 'Usage: tools/check-release.sh [--list-browser-contracts|--browser-contracts]' >&2
  exit 2
fi
"$PYTHON" tools/check-secrets.py
if "$PYTHON" -m pytest -q; then
  "$PYTHON" tools/release-report.py --test-result passed
else
  "$PYTHON" tools/release-report.py --test-result failed
  exit 1
fi
printf '%s\n' 'Offline browser contracts (not production/live scripts):' "${contracts[@]}"
if [[ "${1:-}" == '--browser-contracts' ]]; then
  command -v node >/dev/null
  for contract in "${contracts[@]}"; do
    printf 'Running %s\n' "$contract"
    node "$contract"
  done
else
  printf '%s\n' 'Run with --browser-contracts to execute. Requires Node + Playwright + installed Chromium/system libraries.'
fi
