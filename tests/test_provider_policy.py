import copy
import hashlib
import json

import httpx
import pytest

from proxy.provider_policy import apply_provider_policy,routing_fingerprint
from proxy.config import validate_config
from proxy.model_monitor import configuration_key
from proxy.engine import Engine
from proxy.store import Store
from test_engine import config,MODEL


def test_policy_preserves_privacy_and_exact_pin_no_mutation():
    payload={'provider':{'only':['deepinfra/fp8/flex'],'zdr':True,'data_collection':'deny','max_price':{'completion':2}},'service_tier':'priority'}
    original=copy.deepcopy(payload);p={'routing_mode':'openrouter','service_tier':'flex','provider_pin':'deepinfra/fp8/flex'}
    out=apply_provider_policy(payload,p)
    assert payload==original and out['provider']['zdr'] and out['provider']['data_collection']=='deny'
    assert out['provider']['only']==['deepinfra/fp8/flex'] and out['provider']['allow_fallbacks'] is False and out['service_tier']=='flex'


@pytest.mark.parametrize('preferences',[{'only':['other']},{'ignore':['deepinfra/fp8/flex']},'invalid'])
def test_client_routing_restrictions_not_broadened(preferences):
    with pytest.raises(ValueError):apply_provider_policy({'provider':preferences},{'routing_mode':'openrouter','provider_pin':'deepinfra/fp8/flex'})


def test_native_binding_unchanged_and_openrouter_changes_binding():
    provider={'id':'p','base_url':'https://example.invalid/v1','api_key':'secret'}
    expected=hashlib.sha256(json.dumps(['p',provider['base_url'],'secret','m','r'],separators=(',',':')).encode()).hexdigest()
    assert configuration_key(provider,'m','r')==expected
    changed={**provider,'routing_mode':'openrouter','provider_pin':'deepinfra/fp8/flex'}
    assert configuration_key(changed,'m','r')!=expected
    assert 'secret' not in routing_fingerprint(changed)


def test_config_routing_round_trip_and_native_validation(tmp_path):
    store=Store(tmp_path);config(store);c=store.get_config(True)
    c['providers'][0].update(routing_mode='openrouter',service_tier='flex',provider_pin='deepinfra/fp8/flex')
    c=validate_config(c);saved=store.save_config(c,c['revision'])
    assert saved['providers'][0]['provider_pin']=='deepinfra/fp8/flex'
    bad=copy.deepcopy(c);bad['providers'][0]['routing_mode']='native'
    with pytest.raises(ValueError,match='requires_openrouter'):validate_config(bad)


@pytest.mark.asyncio
async def test_pin_tier_do_not_leak_between_fallback_providers(tmp_path):
    store=Store(tmp_path);config(store);c=store.get_config(True);c['providers'][0].update(routing_mode='openrouter',service_tier='flex',provider_pin='deepinfra/fp8/flex');store.save_config(validate_config(c),c['revision']);seen=[]
    def handle(req):
        value=json.loads(req.content);seen.append(value)
        if len(seen)==1:return httpx.Response(503)
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'ok'}}]})
    e=Engine(store,httpx.MockTransport(handle))
    await e.generate({'model':MODEL,'messages':[{'role':'user','content':'hi'}]},store.get_config(True),'test','key')
    assert seen[0]['provider']['only']==['deepinfra/fp8/flex'] and seen[0]['service_tier']=='flex'
    assert 'provider' not in seen[1] and 'service_tier' not in seen[1]
    await e.close()
