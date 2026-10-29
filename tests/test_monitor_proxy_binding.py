import httpx
import pytest
from proxy.store import Store
from proxy.model_monitor import ModelMonitor
from test_engine import config,MODEL,frame,wire
from test_model_monitor import SAMPLE_NUMBERS,analysis,settle


@pytest.mark.asyncio
async def test_monitor_keeps_profile_in_probe_config_and_invalidates_rotation(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=wire([frame(SAMPLE_NUMBERS),frame(reason='stop')]))
    store=Store(tmp_path);config(store);cfg=store.get_config();cfg['outbound_proxies']=[{'id':'px','name':'test','scheme':'http','host':'127.0.0.1','port':3128,'username':'u','password':'one'}];cfg['providers'][0]['proxy_id']='px';store.save_config(cfg,cfg['revision'])
    monitor=ModelMonitor(store,tmp_path,httpx.MockTransport(handler))
    async def score(*args):return analysis(.99)
    monitor.score=score
    provider=store.get_config(True)['providers'][0];before=monitor.binding_key(provider,MODEL,MODEL)
    try:
        job=await monitor.enqueue('1',MODEL,MODEL);done=await settle(monitor,job)
        assert done['status']=='done' and done['result']['status']=='verified',done
        assert len(calls)==3
        cfg=store.get_config();cfg['outbound_proxies'][0]['password']='two';store.save_config(cfg,cfg['revision'])
        assert before!=monitor.binding_key(provider,MODEL,MODEL)
    finally:await monitor.close()
