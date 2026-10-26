# Failover Proxy — Infrastructure Console

## Architecture decision

Keep FastAPI + local static HTML/CSS/JavaScript. Existing state, auth/CSRF, paid-consent guards, polling, pool drafts and18browsercontracts are retained. No React/Tailwind/shadcn migration: this application doesn't need a client router, hydration or a heavy component runtime. Node is not needed to serve the frontend; separate numerical detector/optional renderer still need their existing Node runtime.

## Art direction

Operational console, not a marketing page. Persistent232px sidebar, restrained graphite layers, compact sticky header, dense real metrics and an interactive time series. Main workspace aligned left; readable Russian labels, task-centered navigation. Avoid decorative gradients/glow, huge slogans and identical oversized cards.

Palette primitives: background #0b0d12, sidebar #0f1219, surface #141922, elevated #1a2130, line #2c3443, text #edf1f8, muted #a0aabd; accents blue #91a8ff, success #70d5b1, amber #e4bc7d, danger #f29aaa.

Typography: local system sans with Cyrillic support (Segoe UI/Arial/system-ui), deliberately no external font requests. Tabular numerals for telemetry; monospace only keys/IDs, not body text. Scale12/13/14/16/20/28px. Four/eight pixel spacing; panel radius12, controls8, badges6. Component states use semantic CSS tokens.

## UX contracts

- Navigation order: dashboard, A6 pools, model checks, history, Pelican, settings; existing button IDs preserved.
- Dirty form confirmations, paid probes, route publication and destructive confirmations are unchanged.
- Logs and requests: local search/filter/sort, no extra backend requests. Detail dialog safe text only.
- Charts: hover + keyboard focus tooltip, no fake telemetry; no-data state remains explicit.
- Native controls, visible focus,44px mobile targets, reduced-motion, horizontal scrolling inside wide tables.
- Polling stays visible-tab scoped. Filters/search/focus and diagnostic panels survive refresh.
- Toasts keep errors persistent; no secrets are copied into markup, URL, localStorage or console.

## Design research/tools

Pinned local dev-only ui-ux-pro-max-cli2.15.0 and lucide-static1.61.0; install with npm ci --prefix tools/design --include=dev --ignore-scripts. UI UX skill installed project-locally using official CLI offline init. First generated recommendation was a FAQ landing page: rejected as unrelated to operations dashboard. Applied dense dark readable/keyboard recommendations, not generic landing layout orGSAP.

Reviewed sources: https://github.com/nextlevelbuilder/ui-ux-pro-max-skill; https://github.com/anthropics/skills/tree/main/skills/frontend-design; https://github.com/obra/superpowers; https://github.com/google-labs-code/stitch-skills. Frontend-design used as read-only guidance. Superpowers full plugin/hooks not installed; existing test/review process retained. Stitch requires authenticated MCP; not needed for local implementation. No remote shell installers, global packages or runtime CDNs.

Lucide SVG source is embedded at build/development time into trusted static markup. Production has no icon-package dependency. Tests cover actual API workflows, not screenshots alone.
