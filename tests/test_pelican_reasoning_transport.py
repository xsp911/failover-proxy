import json
import re

import httpx
import pytest

from proxy.pelican import PelicanManager,MAX_TRANSPORT_BYTES
from proxy.store import Store
from test_engine import config,MODEL,frame,wire
from test_pelican_manager import response_svg,settle


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat','responses'])
async def test_large_reasoning_transport_small_svg_succeeds(tmp_path,protocol):
    store=Store(tmp_path);config(store);calls=[];sizes=[]
    def handle(req):
        data=json.loads(req.content);calls.append(data)
        prompt=data['messages'][0]['content'] if protocol=='chat' else data['input'][0]['content'][0]['text']
        nonce=re.search(r'code ([A-F0-9]{8})',prompt)[1];svg=response_svg(nonce)
        if protocol=='chat':
            reasoning={'choices':[{'index':0,'delta':{'reasoning_content':'r'*2048},'finish_reason':None}]}
            body=wire([reasoning]*600+[frame(svg),frame(reason='stop')])
        else:
            reasoning='data: '+json.dumps({'type':'response.reasoning_summary_text.delta','delta':'r'*2048})+'\n\n'
            completed={'type':'response.completed','response':{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':svg}]}]}}
            body=(reasoning*600+'data: '+json.dumps(completed)+'\n\n').encode()
        sizes.append(len(body));assert 1048576<len(body)<MAX_TRANSPORT_BYTES
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=body)
    manager=PelicanManager(store,tmp_path,httpx.MockTransport(handle));run=await manager.enqueue({'provider_id':'1','model':MODEL,'protocol':protocol,'effort':'high','confirm_cost':True})
    saved,svg=await settle(manager,run)
    assert saved['status']=='done' and all(saved['checks'].values()),saved
    assert svg and len(svg.encode())<262144 and len(calls)==1
    assert 'reasoning_content' not in json.dumps(saved)
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat','responses'])
async def test_transport_still_bounded_and_no_paid_retry(tmp_path,protocol,monkeypatch):
    store=Store(tmp_path);config(store);calls=[]
    monkeypatch.setattr('proxy.pelican.MAX_TRANSPORT_BYTES',1024)
    def handle(req):calls.append(req);return httpx.Response(200,headers={'content-type':'text/event-stream'},content=b':'+b'x'*1500+b'\n\n')
    manager=PelicanManager(store,tmp_path,httpx.MockTransport(handle));run=await manager.enqueue({'provider_id':'1','model':MODEL,'protocol':protocol,'confirm_cost':True})
    saved,svg=await settle(manager,run)
    assert saved['error_kind']=='response_size_limit' and svg is None and len(calls)==1
    await manager.close()
