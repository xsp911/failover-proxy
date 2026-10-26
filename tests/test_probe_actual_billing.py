import pytest
import httpx

from proxy.a6_probe import TargetProbe,ProbeError
from proxy.pool_billing import BillingStore
from proxy.pool_billing_sync import settle_request
from proxy.a6_client import A6Error
from test_pool_manager import policy


class Logs:
    def __init__(self):self.rows=[]
    async def logs(self,id,token):return self.rows
    async def quota_per_unit(self):return 500000


@pytest.mark.asyncio
async def test_error_only_quota_zero_stays_pending_until_consume_finality(tmp_path):
    db=BillingStore(tmp_path);rid=db.reserve('pool','job',2,123,.02,1)
    client=Logs();client.rows=[{'request_id':'failure-request','token_id':2,'type':5,'quota':0}]
    probe=TargetProbe(client,None,httpx.MockTransport(lambda req:httpx.Response(502,headers={'x-oneapi-request-id':'failure-request'})))
    async def settle(id,token):await settle_request(db,client,rid,id,token)
    probe.billing_settler=settle
    with pytest.raises(ProbeError,match='targeted_probe_http_502'):await probe.challenge(policy(),123,'fake-secret',{'prompt':'test'})
    stats=db.stats('pool');assert stats['actual_cost_usd']==0 and stats['pending_reserved_usd']==.02 and stats['settled_requests']==0
    client.rows=[{'request_id':'failure-request','token_id':2,'type':2,'quota':5000}]
    assert await settle_request(db,client,rid,'failure-request',2)
    assert db.stats('pool')['actual_cost_usd']==.01 and db.stats('pool')['pending_reserved_usd']==0


@pytest.mark.asyncio
async def test_billed_stream_parse_failure_counted_as_actual(tmp_path):
    db=BillingStore(tmp_path);rid=db.reserve('pool','job',2,123,.1,1)
    client=Logs();client.rows=[{'request_id':'billed-failure','token_id':2,'type':2,'quota':5000}]
    probe=TargetProbe(client,None,httpx.MockTransport(lambda req:httpx.Response(200,headers={'x-oneapi-request-id':'billed-failure','content-type':'text/event-stream'},content=b'data: {"error":{"message":"upstream failed"}}\n\n')))
    async def settle(id,token):await settle_request(db,client,rid,id,token)
    probe.billing_settler=settle
    with pytest.raises(ProbeError):await probe.challenge(policy(),123,'fake-secret',{'prompt':'test'})
    stats=db.stats('pool');assert stats['actual_cost_usd']==.01 and stats['pending_reserved_usd']==0


@pytest.mark.asyncio
async def test_unconfirmed_log_remains_pending_not_assumed_free(tmp_path):
    db=BillingStore(tmp_path);rid=db.reserve('pool','job',2,123,.05,1)
    client=Logs();client.rows=[{'request_id':'not-our-request','token_id':2,'type':2,'quota':0}]
    assert await settle_request(db,client,rid,'our-request',2) is False
    stats=db.stats('pool');assert stats['actual_cost_usd']==0 and stats['pending_reserved_usd']==.05 and stats['pending_requests']==1


@pytest.mark.asyncio
async def test_consume_log_preferred_to_error_without_double_counting(tmp_path):
    db=BillingStore(tmp_path);rid=db.reserve('pool','job',2,123,.05,1)
    client=Logs();client.rows=[{'request_id':'one-request','token_id':2,'type':5,'quota':0},{'request_id':'one-request','token_id':2,'type':2,'quota':50}]
    assert await settle_request(db,client,rid,'one-request',2)
    assert db.stats('pool')['actual_cost_usd']==.0001
