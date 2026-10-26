import asyncio
import json

import httpx
import pytest

from proxy import probe_rounds
from proxy.model_monitor import ModelMonitor
from proxy.store import Store
from test_engine import config,MODEL,frame,wire
from test_model_monitor import analysis,settle
from test_probe_confirmation import NUMBERS


@pytest.mark.asyncio
async def test_cancel_during_retry_delay_stops_further_requests(tmp_path,monkeypatch):
    monkeypatch.setattr(probe_rounds,'RETRY_DELAYS',(60,60))
    calls=[]
    def upstream(req):calls.append(req);return httpx.Response(500,json={'error':{'code':'upstream_timeout'}})
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream))
    j=await m.enqueue('1',MODEL,MODEL)
    async with asyncio.timeout(2):
        while m.jobs[j['id']].get('phase')!='retry_wait':await asyncio.sleep(.005)
    assert await m.cancel(j['id'])
    assert len(calls)==1 and not m.db.latest_results() and not m.db.journal()
    await m.close()


@pytest.mark.asyncio
async def test_insufficient_numeric_answer_retries_same_task(tmp_path,monkeypatch):
    monkeypatch.setattr(probe_rounds,'RETRY_DELAYS',(0,0))
    prompts=[]
    def upstream(req):
        prompts.append(json.loads(req.content)['messages'][0]['content'])
        text='1,2' if len(prompts)==2 else NUMBERS
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(text),frame(reason='stop')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream))
    async def score(*_):return analysis(.99)
    m.score=score
    done=await settle(m,await m.enqueue('1',MODEL,MODEL))
    assert prompts[1]==prompts[2] and len(prompts)==4
    assert done['result']['challenges'][1]['errors']==['insufficient_probe_numbers']
    assert done['result']['challenges'][1]['parsed_numbers']==250
    await m.close()


@pytest.mark.asyncio
async def test_length_finish_not_scored_as_complete(tmp_path,monkeypatch):
    monkeypatch.setattr(probe_rounds,'RETRY_DELAYS',(0,0))
    calls=[]
    def upstream(req):
        calls.append(req)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(NUMBERS),frame(reason='length')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream))
    async def unexpected(*_):raise AssertionError('Truncated output must not be scored')
    m.score=unexpected
    done=await settle(m,await m.enqueue('1',MODEL,MODEL))
    assert len(calls)==3 and done['result']['status']=='error'
    assert done['result']['challenges'][0]['finish_reason']=='length'
    assert not m.db.journal()
    await m.close()


def test_number_parser_fixtures_match_original_expectations():
    assert probe_rounds.parsed_number_count('1,2,3 字 4,5')==3
    assert probe_rounds.parsed_number_count('١٢٣ 1,355')==2
    assert probe_rounds.parsed_number_count('-3, 7, 999, 5')==3
