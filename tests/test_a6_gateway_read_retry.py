import httpx
import pytest

from proxy.a6_client import A6Client,A6Error


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[502,504])
async def test_gateway_read_retry_recovers_without_repeating_write(status):
    calls=[]
    def handler(request):
        calls.append(request)
        if len(calls)<2:return httpx.Response(status)
        return httpx.Response(200,json={'success':True,'data':{'ok':True}})
    c=A6Client('fake-pat',23,httpx.MockTransport(handler))
    assert await c.request('GET','/api/subscriptions')=={'ok':True}
    assert len(calls)==2
    await c.close()


@pytest.mark.asyncio
async def test_gateway_write_error_never_blind_retried():
    calls=[]
    def handler(request):calls.append(request);return httpx.Response(502)
    c=A6Client('fake-pat',23,httpx.MockTransport(handler))
    with pytest.raises(A6Error,match='a6_route_write_uncertain'):await c.request('PUT','/api/subscriptions/1/config',body={},write=True)
    assert len(calls)==1
    await c.close()
