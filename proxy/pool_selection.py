"""Shared failed-seller cooldown classification for display and scheduling."""
import time


def next_check_epoch(history):
    status=history.get('last_status')
    reason=str(history.get('last_reason') or '')
    if status not in ('suspect','unknown','error'):return None
    if reason.startswith(('a6_','probe_attribution','probe_channel','test_route_')):return None
    if reason in ('probe_budget_exhausted','auto_daily_budget_exhausted','cancelled','pool_configuration_changed'):return None
    # A completed seller/protocol failure cools down regardless of whether the
    # ban classifier counts it as identity-related. Infrastructure doesn't.
    at=history.get('last_checked_at')
    return at+86400 if isinstance(at,(int,float)) else None


def cooling(history,now=None):
    at=next_check_epoch(history)
    return at is not None and at>(time.time() if now is None else now)
