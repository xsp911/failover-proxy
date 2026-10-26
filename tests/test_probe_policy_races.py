import httpx
import pytest

from proxy.model_monitor import ModelMonitor
from proxy.store import Store
from test_engine import config,MODEL,frame,wire
from test_model_monitor import analysis,settings,settle
from test_probe_confirmation import NUMBERS


@pytest.mark.asyncio
async def test_two_lows_invalidated_by_config_do_not_create_quarantine(tmp_path):
    def upstream(req):return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(NUMBERS),frame(reason='stop')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream));await settings(m)
    calls=0
    async def score(*_):
        nonlocal calls
        calls+=1
        if calls==2:
            cfg=s.get_config();cfg['providers'][0]['api_key']='new-key';s.save_config(cfg,cfg['revision'])
        return analysis(.1)
    m.score=score
    done=await settle(m,await m.enqueue('1',MODEL,MODEL))
    assert done['result']['status']=='unknown'
    assert done['result']['error_kind']=='configuration_changed'
    assert not m.db.journal() and not await m.blocked_providers()
    await m.close()


@pytest.mark.asyncio
async def test_scoring_confirmation_failure_inconclusive(tmp_path):
    def upstream(req):return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(NUMBERS),frame(reason='stop')]))
    s=Store(tmp_path);config(s);m=ModelMonitor(s,tmp_path,httpx.MockTransport(upstream));await settings(m)
    calls=0
    async def score(*_):
        nonlocal calls
        calls+=1
        if calls==2:raise ValueError('offline')
        return analysis(.1)
    m.score=score
    done=await settle(m,await m.enqueue('1',MODEL,MODEL))
    assert done['result']['status']=='unknown' and done['result']['error_kind']=='low_score_not_confirmed'
    assert done['result']['confirmation']['confirmed'] is False
    assert not await m.blocked_providers()
    await m.close()
