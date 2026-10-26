import json
import httpx
import pytest
from proxy.app import create_app
from proxy.engine import Engine
from proxy.store import Store
from proxy.capability_store import CapabilityStore
from test_engine import config,MODEL


@pytest.mark.asyncio
async def test_gateway_attempts_passive_context_and_provider_observation(tmp_path):
    store=Store(tmp_path);config(store);c=store.get_config(True);c['models'][0].update(context_length=1000,context_preflight='observe');store.save_config(c,c['revision']);store.capabilities=CapabilityStore(tmp_path);sent=[]
    def handle(req):
        sent.append(req);return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'ok'}}],'usage':{'prompt_tokens':3000,'completion_tokens':12,'completion_tokens_details':{'reasoning_tokens':5}}})
    engine=Engine(store,httpx.MockTransport(handle))
    await engine.generate({'model':MODEL,'messages':[{'role':'user','content':'x'*12000}],'max_tokens':100},store.get_config(True),'req-observe','key')
    rows=store.capabilities.observations();assert len(rows)==1 and rows[0]['context']['over_limit'] and rows[0]['observed_output_tokens']==12 and rows[0]['reasoning_tokens']==5
    assert len(sent)==1,'Observe mode must not reject even over estimated context'
    await engine.close()


def test_trusted_instance_alias_resolved_before_registry_validation(tmp_path):
    root=tmp_path/'instance';root.mkdir();alias=tmp_path/'instance-alias';alias.symlink_to(root,target_is_directory=True)
    # Service may configure /opt/failover-proxy-v2/data via a parent alias.
    data=alias/'data';app=create_app(data)
    assert app.state.capabilities.path.parent==data.resolve()


@pytest.mark.asyncio
async def test_capability_api_authenticated_local_no_provider_call(tmp_path):
    app=create_app(tmp_path);app.state.store.initialize_admin('admin','password')
    app.state.capabilities.record(provider_id='p',model='model',protocol='chat',request_id='req',http_status=200,requested_output_tokens=64000,observed_output_tokens=10)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        assert (await c.get('/admin/capabilities')).status_code==401
        await c.post('/admin/login',json={'username':'admin','password':'password'})
        data=(await c.get('/admin/capabilities')).json();assert data['observations'][0]['parameter_accepted'] and data['summary'][0]['max_observed_output']==10
    await app.state.pelican.close();await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
