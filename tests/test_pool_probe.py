import asyncio
import copy
import json
import time

import httpx
import pytest

from proxy.a6_probe import TargetProbe,ProbeError,responses_text
from proxy.pool_manager import PoolManager,PoolError,iso
from proxy.pool_search import seller_result
from proxy.a6_settings import A6Settings
from proxy.store import Store
from proxy.model_monitor import ModelMonitor
from test_pool_manager import FakeClient,policy,row
from test_engine import frame,wire,MODEL


@pytest.mark.asyncio
async def test_probe_ordinary_secret_with_exact_log_attribution(tmp_path):
    s=Store(tmp_path);monitor=ModelMonitor(s,tmp_path);client=FakeClient()
    async def logs(id,token):return [{'request_id':id,'token_id':token,'actual_channel_id':123,'fallback_used':False}]
    client.logs=logs
    requests=[]
    def handler(req):
        requests.append(req)
        return httpx.Response(200,headers={'content-type':'text/event-stream','x-oneapi-request-id':'request-test'},content=wire([frame('1, 2, 3'),frame(reason='stop')]))
    probe=TargetProbe(client,monitor,httpx.MockTransport(handler));p=policy()
    answer,ev=await probe.challenge(p,123,'secret-ordinary',{'prompt':'numbers','expected_count':3,'id':'query-01'})
    assert requests[0].headers['authorization']=='Bearer sk-secret-ordinary'
    assert not requests[0].headers['authorization'].endswith('-123')
    assert answer['text']=='1, 2, 3' and ev['actual_channel_id']==123
    async def wrong(id,token):return [{'request_id':id,'token_id':token,'actual_channel_id':456,'fallback_used':False}]
    client.logs=wrong
    with pytest.raises(ProbeError,match='channel_mismatch'):await probe.challenge(p,123,'secret',{'prompt':'x','expected_count':3,'id':'query-01'})
    await monitor.close()


@pytest.mark.asyncio
async def test_responses_protocol_lifecycle():
    data=''.join('data: '+json.dumps(x)+'\n\n' for x in [
        {'type':'response.output_text.delta','delta':'1,2,3'},
        {'type':'response.completed','response':{'status':'completed','output':[]}}])
    text,finish=await responses_text(httpx.Response(200,content=data.encode()),MODEL)
    assert text=='1,2,3' and finish=='stop'
    with pytest.raises(ProbeError):await responses_text(httpx.Response(200,content=b'data: {"type":"response.failed"}\n\n'),MODEL)


@pytest.mark.asyncio
async def test_pat_preserved_and_not_in_routing_config(tmp_path):
    store=Store(tmp_path);a6=A6Settings(store);a6.save('fake-pat-only-for-tests',23984)
    before=store.get_config()
    assert '_a6_pat_encrypted' not in before and 'a6_user_id' not in before
    store.save_config(before,before['revision'])
    assert a6.secret()==('fake-pat-only-for-tests',23984)
    assert '_a6_pat_encrypted' not in store.get_config(True)


def test_real_cache_hit_not_cache_price_discount():
    one=row(cache_hit_rate_24h=3500);two=row(channel=2,cache_hit_rate_24h=7000)
    result=seller_result([one,two],MODEL,1,40,None,None,50)
    assert len(result['sellers'])==1 and result['sellers'][0]['cache_hit_percent']==70
    assert 'cache_discount_percent' not in result['sellers'][0]
