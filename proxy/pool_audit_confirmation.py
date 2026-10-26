"""Confirm technical failures of an already-published member before removal."""
import asyncio

_INFRA_PREFIX=('a6_','test_route_','probe_attribution','probe_channel')
_INFRA={'probe_budget_exhausted','auto_daily_budget_exhausted','cancelled','pool_configuration_changed'}
_TECH={'upstream_sse_error','content_after_finish','invalid_finish_reason','incomplete_upstream_stream','probe_incomplete_stream','probe_incomplete_finish','probe_timeout_or_network','probe_insufficient_numbers','probe_upstream_response_failed','upstream_timeout','upstream_connection'}


def needs_confirmation(result):
    reason=result.get('reason') or ''
    return result.get('verification_status')=='unknown' and (reason in _TECH or reason.startswith('targeted_probe_http_')) and reason not in _INFRA and not reason.startswith(_INFRA_PREFIX)


async def confirm_member(probe,pool,candidate,secret,reserve,first):
    if not needs_confirmation(first):return first
    activity=getattr(probe,'activity',None)
    if activity:await activity('confirming_failure')
    await asyncio.sleep(2)
    second=await probe.run(pool,candidate,secret,reserve)
    second={**second,'technical_confirmation':{'first_reason':first.get('reason'),'second_status':second.get('verification_status'),'confirmed':needs_confirmation(second)}}
    return second
