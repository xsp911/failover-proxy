import asyncio
import json
import httpx
import pytest

from proxy.engine import Completion,GatewayError,parse_sse
from proxy.probe_stream import ProbeCompletion
from proxy.model_monitor import ModelMonitor
from proxy.store import Store
from test_engine import config,MODEL,frame,wire
from test_model_monitor import SAMPLE_NUMBERS, analysis,settle


@pytest.mark.asyncio
async def test_reasoning_over_one_megabyte_compacted_with_same_protocol_checks():
    reasoning={'choices':[{'index':0,'delta':{'reasoning_content':'x'*2048},'finish_reason':None}]}
    data=wire([reasoning]*600+[frame('1, 355, 2, 354'),frame(reason='stop')])
    assert len(data)>1048576
    with pytest.raises(GatewayError,match='response_size_limit'):
        await parse_sse(httpx.Response(200,content=data),Completion(MODEL,1048576,65536))
    result=await parse_sse(httpx.Response(200,content=data),ProbeCompletion(MODEL,16777216,65536))
    assert result.text=='1, 355, 2, 354' and not result.events and result.finished[0]=='stop'
    assert result.probe_event_count==602


@pytest.mark.asyncio
async def test_probe_preserves_eof_event_and_content_limits():
    for data,maximum,events,kind in [
        (wire([frame('numbers')],False),10000,100,'incomplete_upstream_stream'),
        (wire([frame('numbers'),frame(reason='stop')]),5,100,'response_size_limit'),
        (wire([frame('1'),frame('2'),frame(reason='stop')]),10000,1,'response_event_limit'),
        (wire([frame('x'*65537),frame(reason='stop')]),100000,100,'probe_content_limit')]:
        with pytest.raises(GatewayError,match=kind):await parse_sse(httpx.Response(200,content=data),ProbeCompletion(MODEL,maximum,events))


@pytest.mark.asyncio
async def test_three_distinct_provider_checks_overlap_but_scoring_serial(tmp_path):
    active=0;peak=0;hosts={};per_host=0;score_active=0;score_peak=0
    async def upstream(req):
        nonlocal active,peak,per_host
        host=req.url.host;hosts[host]=hosts.get(host,0)+1;per_host=max(per_host,hosts[host]);active+=1;peak=max(peak,active)
        try:
            await asyncio.sleep(.03)
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(SAMPLE_NUMBERS),frame(reason='stop')]))
        finally:active-=1;hosts[host]-=1
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream))
    async def score(*_):
        nonlocal score_active,score_peak
        score_active+=1;score_peak=max(score_peak,score_active)
        try:await asyncio.sleep(.02);return analysis(.99)
        finally:score_active-=1
    m.score=score
    jobs=[await m.enqueue(str(i),MODEL,MODEL) for i in (1,2,3)]
    await asyncio.gather(*(settle(m,j) for j in jobs))
    assert peak==3 and per_host==1 and score_peak==1
    assert all(m.jobs[j['id']]['status']=='done' for j in jobs)
    await m.close()


@pytest.mark.asyncio
async def test_same_provider_different_model_checks_are_serial(tmp_path):
    active=0;peak=0
    async def upstream(req):
        nonlocal active,peak
        active+=1;peak=max(peak,active)
        try:
            await asyncio.sleep(.01)
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(SAMPLE_NUMBERS),frame(reason='stop')]))
        finally:active-=1
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream))
    async def score(*_):return analysis(.99)
    m.score=score
    jobs=[await m.enqueue('1',model,MODEL) for model in (MODEL,'another-model')]
    await asyncio.gather(*(settle(m,j) for j in jobs))
    assert peak==1
    await m.close()
