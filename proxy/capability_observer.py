"""Best-effort local observations; failures never trigger paid request retries."""
import logging
import sqlite3

LOG=logging.getLogger('failover.capabilities')
CONTEXT_KEYS={'estimated_input_tokens','estimated_total_tokens','context_length','method','over_limit','reliability'}


def observe(store,*,provider,model,protocol,request_id,source,payload,outcome,http_status=None,usage=None,finish_reason=None,error_kind=None,context=None):
    if store is None:return
    from .provider_policy import routing_fingerprint
    usage=usage if isinstance(usage,dict) else {}
    requested=payload.get('max_completion_tokens',payload.get('max_output_tokens',payload.get('max_tokens')))
    if type(requested) is not int or requested<0:requested=None
    output=usage.get('output_tokens',usage.get('completion_tokens'))
    reasoning=usage.get('reasoning_tokens')
    for key in ('completion_tokens_details','output_tokens_details'):
        details=usage.get(key)
        if isinstance(details,dict):reasoning=details.get('reasoning_tokens',reasoning)
    try:
        store.record(provider_id=provider['id'],model=model,protocol=protocol,request_id=request_id,requested_output_tokens=requested,observed_output_tokens=output if type(output) is int and output>=0 else None,reasoning_tokens=reasoning if type(reasoning) is int and reasoning>=0 else None,http_status=http_status,finish_reason=finish_reason,outcome=outcome,source=source,error_kind=error_kind,context={k:v for k,v in context.items() if k in CONTEXT_KEYS} if context else None,routing_fingerprint=routing_fingerprint(provider))
    except (ValueError,OSError,sqlite3.Error):
        LOG.warning('capability observation not saved; no request content logged')
