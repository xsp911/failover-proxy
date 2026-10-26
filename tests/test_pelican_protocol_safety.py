import asyncio
import json

import httpx
import pytest

from proxy.pelican import PelicanManager
from proxy.store import Store
from test_engine import config,MODEL


@pytest.mark.asyncio
async def test_slow_trickle_total_deadline_and_no_automatic_retry(tmp_path,monkeypatch):
    store=Store(tmp_path);config(store);calls=[]
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(.005)
                yield b': keepalive\n\n'
    def handle(req):calls.append(req);return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Slow())
    manager=PelicanManager(store,tmp_path,httpx.MockTransport(handle));monkeypatch.setattr('proxy.pelican.TIMEOUT_SECONDS',.03)
    run=await manager.enqueue({'provider_id':'1','model':MODEL,'confirm_cost':True});task=manager.tasks.get(run['id']);await task
    saved,svg=manager.db.get(run['id']);assert saved['error_kind']=='pelican_timeout' and not svg and len(calls)==1
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_before_task_started_is_durable(tmp_path):
    store=Store(tmp_path);config(store);manager=PelicanManager(store,tmp_path)
    run=await manager.enqueue({'provider_id':'1','model':MODEL,'confirm_cost':True})
    assert await manager.cancel(run['id'])
    saved,svg=manager.db.get(run['id']);assert saved['status']=='cancelled' and not svg
    await manager.close()


@pytest.mark.parametrize('body',[
 {'type':'response.incomplete','response':{'status':'incomplete'}},
 {'type':'response.failed','response':{'status':'failed'}},
 {'type':'response.output_text.delta','delta':'<svg/>'},
])
def test_responses_requires_completed_event_not_partial_svg(body):
    from proxy.engine import GatewayError
    with pytest.raises(GatewayError):PelicanManager._responses_sse(('data: '+json.dumps(body)+'\n\n').encode())
