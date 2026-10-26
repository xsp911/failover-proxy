import asyncio
import json

import httpx
import pytest

from proxy import diagnostics as d
from proxy.engine import Completion, Engine, GatewayError
from proxy.store import Store
from test_engine import config, MODEL


@pytest.mark.parametrize('code,message',[
    ('context_length_exceeded','ignored'),
    ('context_window_exceeded','ignored'),
    (None,'Maximum context length is 10000 tokens.'),
    (None,'Context window exceeded'),
    (None,'Too many input tokens'),
])
def test_precise_context_classification(code,message):
    kind,status,retry,detail=d.classify({'error':{'code':code,'message':message}},400)
    assert kind=='context_length_exceeded' and status==400 and not retry
    assert 'Reduce' in detail


def test_no_guessing_context_or_echoing_raw_data():
    for message in ('context setting is unsupported','TOPSECRET sk-test fp-other https://secret.invalid','Invalid context field'):
        kind,status,retry,detail=d.classify({'error':{'message':message}},400)
        assert kind=='upstream_http_400' and not retry
        assert message not in detail
    assert d.classify({'error':{'message':'Maximum context length exceeded'}},502)[0]=='upstream_http_502'


class Parts(httpx.AsyncByteStream):
    def __init__(self,parts,delay=0):self.parts=parts;self.delay=delay;self.reads=0
    async def __aiter__(self):
        for p in self.parts:
            self.reads+=1
            if self.delay:await asyncio.sleep(self.delay)
            yield p


@pytest.mark.asyncio
async def test_error_body_bounded_size_and_time(monkeypatch):
    stream=Parts([b'x'*10000,b'y'*10000])
    assert await d.read_error(httpx.Response(400,stream=stream)) is None
    assert stream.reads==1
    monkeypatch.setattr(d,'READ_TIMEOUT',0.01)
    assert await d.read_error(httpx.Response(400,stream=Parts([b'{}'],delay=1))) is None


@pytest.mark.asyncio
async def test_error_body_cancellation_not_swallowed():
    task=asyncio.create_task(d.read_error(httpx.Response(400,stream=Parts([b'{}'],delay=10))))
    await asyncio.sleep(0);task.cancel()
    with pytest.raises(asyncio.CancelledError):await task


@pytest.mark.asyncio
async def test_context_error_from_stream_stops_retry_without_cooldown(tmp_path):
    calls=[]
    def handler(req):
        calls.append(req.url.host)
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=b'data: {"error":{"code":"context_length_exceeded","message":"TOPSECRET"}}\n\n')
    store=Store(tmp_path);config(store)
    engine=Engine(store,httpx.MockTransport(handler));attribution={}
    with pytest.raises(GatewayError) as error:
        await engine.generate({'model':MODEL,'messages':[],'stream':True},store.get_config(True),'sse','key',attribution=attribution)
    assert error.value.kind=='context_length_exceeded' and not error.value.retryable
    assert not engine.cooldowns and not attribution and calls==['p1.invalid']
    await engine.close()


def test_header_name_safe_ascii_fallback():
    assert d.upstream_label({'id':'uuid','name':'Provider 1'})=='Provider 1'
    for name in ('Имя','bad\r\nX-Key: secret',''):
        assert d.upstream_label({'id':'uuid','name':name})=='uuid'


def test_nonstream_usage_is_not_normalized():
    x={'choices':[{'message':{'content':'okay'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':71}}
    Engine.validate_json(x,MODEL)
    assert x['usage']['total_tokens']==71
    assert 'reasoning_content' not in x['choices'][0]['message']
