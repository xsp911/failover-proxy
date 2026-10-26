import httpx
import pytest
from proxy.a6_client import A6Client,A6Error


@pytest.mark.asyncio
async def test_quota_conversion_read_validated_and_cached():
    calls=[]
    def serve(req):
        calls.append(req)
        return httpx.Response(200,json={'success':True,'data':{'quota_per_unit':500000,'quota_display_type':'USD'}})
    c=A6Client('fake-pat',23,httpx.MockTransport(serve))
    assert await c.quota_per_unit()==500000
    assert await c.quota_per_unit()==500000 and len(calls)==1
    assert calls[0].url.path=='/api/status'
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('unit',[None,0,-1,True,'500000'])
async def test_invalid_billing_conversion_never_assumed(unit):
    c=A6Client('fake-pat',23,httpx.MockTransport(lambda req:httpx.Response(200,json={'success':True,'data':{'quota_per_unit':unit}})))
    with pytest.raises(A6Error,match='a6_billing_unit_unavailable'):await c.quota_per_unit()
    await c.close()
