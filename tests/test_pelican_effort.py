import json

import httpx
import pytest

from proxy.pelican import PelicanManager,EFFORTS
from proxy.store import Store
from test_engine import config,MODEL


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat','responses'])
@pytest.mark.parametrize('effort',[None,'none','minimal','low','medium','high','xhigh'])
async def test_reasoning_effort_protocol_mapping_and_saved_history(tmp_path,protocol,effort):
    store=Store(tmp_path);config(store);calls=[]
    def upstream(req):calls.append(json.loads(req.content));return httpx.Response(400)
    manager=PelicanManager(store,tmp_path,httpx.MockTransport(upstream))
    body={'provider_id':'1','model':MODEL,'protocol':protocol,'confirm_cost':True}
    if effort is not None:body['effort']=effort
    run=await manager.enqueue(body);task=manager.tasks.get(run['id']);await task
    sent=calls[0]
    expected_budget=64000 if effort in ('high','xhigh') else 16000
    assert sent['max_output_tokens' if protocol=='responses' else 'max_tokens']==expected_budget
    assert manager.db.get(run['id'])[0]['max_output_tokens']==expected_budget
    if effort is None:assert 'reasoning_effort' not in sent and 'reasoning' not in sent
    elif protocol=='chat':assert sent['reasoning_effort']==effort and 'reasoning' not in sent
    else:assert sent['reasoning']=={'effort':effort} and 'reasoning_effort' not in sent
    saved,_=manager.db.get(run['id']);assert saved['effort']==effort and saved['timeout_seconds']==300
    assert len(calls)==1,'Unsupported effort must not trigger paid automatic retry'
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('effort',['ultra','HIGH',False,[],{},1])
async def test_invalid_effort_rejected_before_network(tmp_path,effort):
    store=Store(tmp_path);config(store);manager=PelicanManager(store,tmp_path)
    with pytest.raises(ValueError,match='invalid_effort'):await manager.enqueue({'provider_id':'1','model':MODEL,'confirm_cost':True,'effort':effort})
    assert manager.db.list()==[]
    await manager.close()
