"""Read-only, local provider health enrichment for admin statistics.

The caller supplies ``await monitor.info()`` (already binding-validated), then
runs ``enrich`` in a worker thread. Traffic errors are not identity evidence;
HTTP 402 signals a billing/quota issue, never a measured account balance.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

WINDOW_SECONDS = 900
_KIND = re.compile(r"[A-Za-z0-9_:.\-]{1,80}\Z")
_ID = re.compile(r"[A-Za-z0-9_:.\-]{1,256}\Z")
_ATTEMPT_FIELDS = """json_extract(payload,'$.status') AS status,
    json_extract(payload,'$.error_kind') AS error_kind,
    json_extract(payload,'$.http_status') AS http_status, ts, request_id"""


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _kind(value):
    if value is None:
        return None
    return value if isinstance(value, str) and _KIND.fullmatch(value) else "upstream_error"


def _attempt(row):
    if row is None:
        return None
    status = row["status"]
    code = row["http_status"]
    request_id = row["request_id"]
    return {
        "status": status if status in ("success", "error", "cancelled") else "error",
        "error_kind": _kind(row["error_kind"]),
        "http_status": code if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599 else None,
        "ts": _iso(row["ts"]),
        "request_id": request_id if isinstance(request_id, str) and _ID.fullmatch(request_id) else None,
    }


def _models(info):
    """Keep only active routed models and their exact selected reference.

    ModelMonitor.info filters configuration keys. Its historical/manual states
    still need intersection with current targets, routing and stale_reference.
    Do not replace the retained conclusive status with latest_status=error.
    """
    providers = {p["id"]: p for p in info.get("providers", [])}
    targets = {(t["provider_id"], t["model"]): t["reference_model"]
               for t in info.get("settings", {}).get("targets", [])}
    states = {(s.get("provider_id"), s.get("model"), s.get("reference_model")): s
              for s in info.get("states", []) if not s.get("stale_reference")}
    result = {}
    for provider_id, provider in providers.items():
        models = []
        if not provider.get("enabled", True):
            result[provider_id] = models
            continue
        for model in sorted(set(provider.get("models", []))):
            reference = targets.get((provider_id, model))
            state = states.get((provider_id, model, reference)) if reference is not None else None
            latest = state.get("latest_result", {}) if state else {}
            status = state.get("status") if state else "unchecked"
            models.append({
                "model": model,
                "reference_model": reference,
                "status": status if status in ("verified", "suspect", "unknown") else "unchecked",
                "latest_status": state.get("latest_status", status) if state else None,
                "checked_at": state.get("checked_at") if state else None,
                "error_kind": _kind(latest.get("error_kind", state.get("error_kind"))) if state else None,
            })
        result[provider_id] = models
    return result


def enrich(store, monitor_info, result):
    """Add health to each result provider, preserving existing runtime fields.

    Latest observations are provider-local indexed ORDER BY/LIMIT queries, not
    a sample of the global recent errors. Recent counts stay exact in SQLite;
    only scalar aggregates and two whitelisted observation rows reach Python.
    Consecutive errors means the trailing error streak within the 900s window;
    both success and cancellation end that streak. No writes or network calls.
    """
    now = time.time()
    since = now - WINDOW_SECONDS
    models = _models(monitor_info)
    blocked = set(monitor_info.get("blocked_provider_ids", []))
    with store._connection() as conn:
        conn.execute("BEGIN")
        for provider in result.get("providers", []):
            provider_id = provider["id"]
            params = (provider_id, now)
            base = "FROM attempts WHERE provider_id=? AND ts<=?"
            latest = _attempt(conn.execute(
                f"SELECT {_ATTEMPT_FIELDS} {base} ORDER BY ts DESC,rowid DESC LIMIT 1", params).fetchone())
            last_error = _attempt(conn.execute(
                f"SELECT {_ATTEMPT_FIELDS} {base} AND json_extract(payload,'$.status')='error' "
                "ORDER BY ts DESC,rowid DESC LIMIT 1", params).fetchone())
            success = conn.execute(
                f"SELECT ts {base} AND json_extract(payload,'$.status')='success' "
                "ORDER BY ts DESC,rowid DESC LIMIT 1", params).fetchone()
            # A rowid tie-breaker makes streaks deterministic for equal timestamps.
            recent = dict(conn.execute("""
                WITH recent AS (
                    SELECT ts,rowid AS sequence,json_extract(payload,'$.status') AS status
                    FROM attempts WHERE provider_id=? AND ts>=? AND ts<=?
                ), boundary AS (
                    SELECT ts,sequence FROM recent WHERE status!='error'
                    ORDER BY ts DESC,sequence DESC LIMIT 1
                )
                SELECT COUNT(*) AS attempts,
                    COALESCE(SUM(status='error'),0) AS errors,
                    COALESCE(SUM(status='cancelled'),0) AS cancelled,
                    COALESCE(SUM(status='error' AND (NOT EXISTS(SELECT 1 FROM boundary)
                        OR (ts,sequence)>(SELECT ts,sequence FROM boundary))),0) AS consecutive_errors
                FROM recent
                """, (provider_id, since, now)).fetchone())
            payment_required = bool(latest and latest["status"] == "error" and latest["http_status"] == 402)
            provider["health"] = {
                "models": models.get(provider_id, []),
                "quarantined": provider_id in blocked,
                "last_attempt": latest,
                "last_error": last_error,
                "last_success_at": _iso(success["ts"]) if success else None,
                "recent": {"window_seconds": WINDOW_SECONDS, **recent},
                "billing": {
                    "status": "payment_required" if payment_required else "unknown",
                    "observed_at": latest["ts"] if payment_required else None,
                    "code": 402 if payment_required else None,
                },
            }
    return result
