import asyncio
import json

import httpx
import pytest

from proxy.model_monitor import ModelMonitor
from proxy.store import Store
from proxy import probe_rounds
from test_engine import config,MODEL,frame,wire
from test_model_monitor import analysis,settings,settle

NUMBERS=', '.join(str(1+(i*137)%355) for i in range(250))


async def run_case(tmp_path,scores,fail_calls=()):
    calls=[]
    def upstream(req):
        calls.append(json.loads(req.content)['messages'][0]['content'])
        if len(calls) in fail_calls:return httpx.Response(500,json={'error':{'code':'upstream_timeout','message':'raw secret'}})
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(NUMBERS),frame(reason='stop')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream));await settings(m)
    results=iter(scores)
    async def score(*_):return analysis(next(results))
    m.score=score
    done=await settle(m,await m.enqueue('1',MODEL,MODEL))
    return m,done,calls


@pytest.mark.asyncio
async def test_one_failure_retries_only_current_challenge(tmp_path,monkeypatch):
    monkeypatch.setattr(probe_rounds,'RETRY_DELAYS',(0,0))
    m,done,calls=await run_case(tmp_path,[.99],fail_calls=(2,))
    assert len(calls)==4 and calls[1]==calls[2] and calls[0]!=calls[1]
    assert done['result']['status']=='verified'
    tasks=done['result']['challenges']
    assert [t['attempts'] for t in tasks]==[1,2,1]
    assert tasks[1]['errors']==['upstream_timeout']
    assert all(t['parsed_numbers']==250 and t['finish_reason']=='stop' for t in tasks)
    assert done['result']['confirmation']['required'] is False
    await m.close()


@pytest.mark.asyncio
async def test_low_then_high_does_not_quarantine(tmp_path):
    m,done,calls=await run_case(tmp_path,[.69,.99])
    assert len(calls)==6 and done['round']==2
    assert done['result']['status']=='verified'
    assert done['result']['confirmation']['status']=='recovered'
    assert not await m.blocked_providers()
    assert len(done['result']['challenges'])==6
    await m.close()


@pytest.mark.asyncio
async def test_two_full_low_scores_quarantine(tmp_path):
    m,done,calls=await run_case(tmp_path,[.69,.70])
    assert len(calls)==6 and done['result']['status']=='suspect'
    assert done['result']['confirmation']['confirmed'] is True
    assert await m.blocked_providers()=={'1'}
    await m.close()


@pytest.mark.asyncio
async def test_confirmation_failure_is_unknown_not_new_quarantine(tmp_path,monkeypatch):
    monkeypatch.setattr(probe_rounds,'RETRY_DELAYS',(0,0))
    m,done,calls=await run_case(tmp_path,[.69],fail_calls=(4,5,6))
    assert len(calls)==6
    assert done['result']['status']=='unknown'
    assert done['result']['confirmation']['status']=='inconclusive'
    assert done['result']['error_kind']=='low_score_not_confirmed'
    assert done['result']['challenges'][-1]['attempts']==3
    assert not await m.blocked_providers() and not m.db.journal()
    await m.close()


@pytest.mark.asyncio
async def test_permanent_failure_not_retried(tmp_path):
    calls=[]
    def upstream(req):calls.append(req);return httpx.Response(400,json={'error':{'code':'context_length_exceeded'}})
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream))
    done=await settle(m,await m.enqueue('1',MODEL,MODEL))
    assert len(calls)==1 and done['result']['error_kind']=='context_length_exceeded'
    assert done['result']['challenges'][0]['errors']==['context_length_exceeded']
    await m.close()


def test_number_count_matches_original_longest_run():
    assert probe_rounds.parsed_number_count('words 1, 355, 4 letter 1, 2')==3
    assert probe_rounds.parsed_number_count('1, 356, 0, 2, 3')==3
    assert probe_rounds.parsed_number_count('0001 2')==2
