import httpx
import pytest

from proxy.a6_client import A6Client


@pytest.mark.asyncio
async def test_immediately_fetched_exact_log_evidence_reused_only_for_same_token():
    calls=[]
    def serve(req):
        calls.append(req)
        return httpx.Response(200,json={'success':True,'data':{'total':1,'items':[{'request_id':'our-request','token_id':2,'type':2,'quota':5,'other':{'actual_channel_id':123}}]}})
    c=A6Client('fake-pat',23,httpx.MockTransport(serve))
    rows=await c.logs('our-request',2)
    assert c.cached_logs('our-request',2)==rows
    assert c.cached_logs('our-request',3) is None and c.cached_logs('other-request',2) is None
    cached=c.cached_logs('our-request',2);cached[0]['quota']=999
    assert c.cached_logs('our-request',2)[0]['quota']==5
    assert len(calls)==1
    await c.close()
