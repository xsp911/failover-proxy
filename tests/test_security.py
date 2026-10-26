import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from proxy.app import create_app
from proxy.engine import destination,GatewayError


@pytest.mark.asyncio
async def test_dns_pinning_and_sni():
    loop=asyncio.get_running_loop()
    records=[(2,1,6,'',('93.184.216.34',443))]
    async def resolve(*args,**kwargs):return records
    with patch.object(loop,'getaddrinfo',new=resolve):
        url,headers,extensions=await destination('https://api.example.com/v1/chat/completions',False)
    assert url=='https://93.184.216.34/v1/chat/completions'
    assert headers['Host']=='api.example.com'
    assert extensions['sni_hostname']=='api.example.com'


@pytest.mark.asyncio
async def test_dns_rebinding_private_answer_rejected():
    loop=asyncio.get_running_loop()
    async def resolve(*args,**kwargs):return [(2,1,6,'',('93.184.216.34',443)),(2,1,6,'',('127.0.0.1',443))]
    with patch.object(loop,'getaddrinfo',new=resolve):
        with pytest.raises(GatewayError,match='unsafe_upstream_address'):
            await destination('https://api.example.com/v1',False)


@pytest.mark.asyncio
async def test_concurrent_login_reserves_before_password_hash(tmp_path):
    app=create_app(tmp_path)
    calls=0
    def authenticate(*args):
        nonlocal calls
        calls+=1
        import time
        time.sleep(0.05)
        return False
    app.state.store.authenticate_admin=authenticate
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        responses=await asyncio.gather(*(c.post('/admin/login',json={'username':'admin','password':'bad'}) for _ in range(25)))
    assert calls==10
    assert sum(x.status_code==429 for x in responses)==15
    await app.state.engine.close()
