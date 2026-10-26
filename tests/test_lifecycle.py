"""Real ASGI receive/send lifecycle tests (not buffering ASGITransport)."""
import asyncio
import json
import time

import httpx
import pytest

from proxy.app import create_app, HeldJSONResponse, HeldStreamResponse, Replay
from proxy.engine import Completion, Engine, GatewayError
from proxy.store import Store
from test_engine import config, MODEL


@pytest.mark.asyncio
async def test_disconnect_cancels_upstream(tmp_path):
    app=create_app(tmp_path,transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    config(app.state.store)
    key=app.state.store.create_key('test',[])['token']
    stopped=asyncio.Event()
    finished=False
    async def slow(*args, **kwargs):
        nonlocal finished
        try:
            await asyncio.sleep(10)
            finished=True
            return {}
        except asyncio.CancelledError:
            stopped.set()
            raise
    app.state.engine.generate=slow
    body=json.dumps({'model':MODEL,'messages':[]}).encode()
    first=True
    async def receive():
        nonlocal first
        if first:
            first=False
            return {'type':'http.request','body':body,'more_body':False}
        return {'type':'http.disconnect'}
    sent=[]
    async def send(message):sent.append(message)
    scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'POST','scheme':'http','path':'/v1/chat/completions','raw_path':b'/v1/chat/completions','query_string':b'',
           'headers':[(b'host',b'test'),(b'authorization',('Bearer '+key).encode()),(b'content-length',str(len(body)).encode())],
           'server':('test',80),'client':('127.0.0.1',111)}
    async with asyncio.timeout(2):await app(scope,receive,send)
    assert stopped.is_set() and not finished
    assert app.state.inflight['n']==0
    await app.state.engine.close()


@pytest.mark.asyncio
async def test_nonstream_reservation_held_while_sending():
    released=False
    async def receive():await asyncio.sleep(60)
    entered=asyncio.Event();allow=asyncio.Event()
    async def send(message):
        if message['type']=='http.response.body':
            entered.set();await allow.wait()
    def release():
        nonlocal released
        released=True
    response=HeldJSONResponse({'answer':'value'},release)
    task=asyncio.create_task(response({'type':'http'},receive,send))
    await entered.wait();assert not released
    allow.set();await task;assert released


@pytest.mark.asyncio
async def test_replay_cancel_releases_reservation():
    released=[]
    replay=Replay([{'choices':[]}],lambda:released.append(True))
    entered=asyncio.Event()
    async def receive():await asyncio.sleep(60)
    async def send(message):
        if message['type']=='http.response.body':entered.set();await asyncio.sleep(60)
    response=HeldStreamResponse(replay,media_type='text/event-stream')
    scope={'type':'http','asgi':{'spec_version':'2.4'}}
    task=asyncio.create_task(response(scope,receive,send))
    await entered.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert released==[True] and not replay.events


@pytest.mark.parametrize('value',[
    {'choices':[None]},
    {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':'\ud800'}}]},
    {'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','tool_calls':1}}]},
])
def test_invalid_json_fails_before_success(value):
    with pytest.raises(GatewayError):Engine.validate_json(value,MODEL)


def test_invalid_sse_unicode():
    c=Completion(MODEL,10000,100)
    with pytest.raises(GatewayError):c.add({'choices':[{'index':0,'delta':{'content':'\ud800'},'finish_reason':'stop'}]})


def test_no_tls_keepalive_for_pinned_ip_hosts(tmp_path):
    engine=Engine(Store(tmp_path))
    # Persistent IP-origin connections must not skip a different hostname's TLS validation.
    assert engine.client._transport._pool._max_keepalive_connections==0
