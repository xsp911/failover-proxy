"""Explicit provider-scoped routing, never leaked across failover attempts."""
import copy
import hashlib
import json
import re

SERVICE_TIERS=('default','flex','priority','fast','ultrafast')
PIN=re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z')


def routing_identity(provider):
    if provider.get('routing_mode','native')!='openrouter':return None
    tier=provider.get('service_tier') or None;pin=provider.get('provider_pin') or None
    return {'mode':'openrouter','service_tier':tier,'provider_pin':pin}


def routing_fingerprint(provider):
    value={'provider_id':provider.get('id'),'base_url':provider.get('base_url'),'credential':provider.get('api_key'),'policy':routing_identity(provider)}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def apply_provider_policy(payload,provider):
    out=copy.deepcopy(payload);policy=routing_identity(provider)
    if not policy:return out
    tier=policy['service_tier'];pin=policy['provider_pin']
    if tier:out['service_tier']=tier
    if pin:
        original=out.get('provider',{})
        if not isinstance(original,dict):raise ValueError('invalid_client_provider_policy')
        preferences=copy.deepcopy(original)
        # Administrative pin may narrow but must not expand client restrictions.
        allowed=preferences.get('only')
        if allowed is not None and (not isinstance(allowed,list) or pin not in allowed):raise ValueError('provider_pin_conflicts_with_client')
        ignored=preferences.get('ignore',[])
        if not isinstance(ignored,list) or any(not isinstance(item,str) or pin==item or pin.startswith(item+'/') for item in ignored):raise ValueError('provider_pin_conflicts_with_client')
        preferences.update(only=[pin],allow_fallbacks=False)
        out['provider']=preferences
    return out
