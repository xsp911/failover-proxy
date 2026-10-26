import asyncio
import copy
import json
import time

import httpx
import pytest

from proxy.store import Store
from proxy.engine import Engine, GatewayError, Completion, parse_sse

MODEL='deepseek-v4.1-flash-1017'


def config(store):
    c=store.get_config()
    c['settings']['trust_private_upstreams']=True
    c['providers']=[{'id':str(i),'name':'Provider '+str(i),'priority':i,'enabled':True,'base_url':'http://p'+str(i)+'.invalid/v1','api_key':'secret-'+str(i)} for i in (1,2,3)]
    c['models']=[{'id':'model','name':MODEL,'enabled':True,'supports_vision':True,'routes':[{'provider_id':str(i),'upstream_model':MODEL,'enabled':True} for i in (1,2,3)]}]
    return store.save_config(c,c['revision'])


def frame(text=None,reason=None,tool=None):
    delta={}
    if text is not None:delta['content']=text
    if tool is not None:delta['tool_calls']=[{'index':0,'id':'call_1','function':{'name':'bash','arguments':tool},'type':'function'}]
    return {'id':'test','object':'chat.completion.chunk','created':1,'model':MODEL,'choices':[{'index':0,'delta':delta,'finish_reason':reason}]}


def wire(events,done=True):
    return (''.join('data: '+json.dumps(x)+'\n\n' for x in events)+('data: [DONE]\n\n' if done else '')).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['eof','tool','banner','http','sse_error'])
async def test_failover_without_partial_leak(tmp_path,kind):
    store=Store(tmp_path);config(store)
    attempts=[];sent=[]
    def handle(req):
        attempts.append(req.url.host);sent.append(json.loads(req.content))
        if req.url.host=='p1.invalid':
            if kind=='http':return httpx.Response(503)
            if kind=='tool':data=wire([frame(tool='{"command":')],False)
            elif kind=='banner':data=wire([frame('AI provider temporarily unavailable\nRequest ID: req_x Ref: 35 UpstreamResponseCode: 503 Model: '+MODEL),frame(reason='stop')])
            elif kind=='sse_error':data=wire([frame('partial'),{'error':{'message':'secret upstream body'}}])
            else:data=wire([frame('partial')],False)
        else:data=wire([frame('success'),frame(reason='stop'),{'choices':[],'usage':{'prompt_tokens':100,'completion_tokens':10,'prompt_tokens_details':{'cached_tokens':40}}}])
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=data)
    engine=Engine(store,httpx.MockTransport(handle))
    payload={'model':MODEL,'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,abc'}}]}],'stream':True,'reasoning_effort':'high','thinking':{'type':'enabled','budget_tokens':2048},'tools':[{'type':'function','function':{'name':'bash','parameters':{}}}]}
    result=await engine.generate(payload,store.get_config(True),'req_test','key')
    assert attempts==['p1.invalid','p2.invalid']
    assert b'partial' not in b''.join(result.wire())
    assert b'success' in b''.join(result.wire())
    for p in sent:
        assert p['thinking']==payload['thinking'] and p['messages']==payload['messages'] and p['tools']==payload['tools']
    stats=store.stats('hour');assert stats['summary']['errors']==1 and stats['summary']['success']==1 and stats['summary']['switches']==1
    assert engine.cooldowns['1']>time.time()
    await engine.close()


@pytest.mark.asyncio
async def test_all_fail_and_cooldown(tmp_path):
    store=Store(tmp_path);config(store);hosts=[]
    def handle(req):hosts.append(req.url.host);return httpx.Response(503)
    engine=Engine(store,httpx.MockTransport(handle))
    with pytest.raises(GatewayError):await engine.generate({'model':MODEL,'messages':[],'stream':True},store.get_config(True),'one','key')
    assert hosts==['p1.invalid','p2.invalid','p3.invalid']
    with pytest.raises(GatewayError,match='no_available_provider'):await engine.generate({'model':MODEL,'messages':[]},store.get_config(True),'two','key')
    assert len(hosts)==3
    engine.cooldowns={p:time.time()-1 for p in engine.cooldowns}
    with pytest.raises(GatewayError):await engine.generate({'model':MODEL,'messages':[]},store.get_config(True),'three','key')
    assert len(hosts)==6
    await engine.close()


@pytest.mark.asyncio
async def test_json_and_client_error(tmp_path):
    store=Store(tmp_path);config(store);count=0
    def handle(req):
        nonlocal count
        count+=1
        if json.loads(req.content).get('bad'):return httpx.Response(400,json={'error':'bad request'})
        return httpx.Response(200,json={'model':'upstream','choices':[{'index':0,'message':{'role':'assistant','content':'okay'},'finish_reason':'stop'}],'usage':{'prompt_tokens':5,'completion_tokens':2}})
    engine=Engine(store,httpx.MockTransport(handle))
    result=await engine.generate({'model':MODEL,'messages':[]},store.get_config(True),'r1','key')
    assert result['model']==MODEL
    with pytest.raises(GatewayError) as e:await engine.generate({'model':MODEL,'messages':[],'bad':True},store.get_config(True),'r2','key')
    assert not e.value.retryable and count==2
    await engine.close()


class Parts(httpx.AsyncByteStream):
    def __init__(self,data):self.data=data
    async def __aiter__(self):
        for x in self.data:yield x


@pytest.mark.asyncio
async def test_split_unicode_crlf_and_usage():
    data=wire([frame('Привет'),frame(reason='stop'),{'choices':[],'usage':{'prompt_tokens':3,'completion_tokens':2}}]).replace(b'\n',b'\r\n')
    response=httpx.Response(200,stream=Parts([data[i:i+1] for i in range(len(data))]))
    result=await parse_sse(response,Completion(MODEL,100000,100))
    assert result.usage['completion_tokens']==2
    assert result.texts[0]=='Привет'


@pytest.mark.asyncio
@pytest.mark.parametrize('events,kind',[
    ([frame('part')],'incomplete_upstream_stream'),
    ([frame(tool='{"command":'),frame(reason='tool_calls')],'invalid_tool_arguments'),
    ([frame('first',reason='stop'),frame('more')],'content_after_finish'),
])
async def test_malformed_sse(events,kind):
    response=httpx.Response(200,content=wire(events))
    with pytest.raises(GatewayError,match=kind):await parse_sse(response,Completion(MODEL,100000,100))


@pytest.mark.asyncio
async def test_size_limit():
    with pytest.raises(GatewayError,match='response_size_limit'):
        await parse_sse(httpx.Response(200,content=wire([frame('long')])) ,Completion(MODEL,10,100))


@pytest.mark.asyncio
async def test_successful_tools():
    result=await parse_sse(httpx.Response(200,content=wire([frame(tool='{"command":"echo okay"}'),frame(reason='tool_calls')])),Completion(MODEL,100000,100))
    assert result.tools[(0,0)]['name']=='bash'
