import pytest

from proxy.pool_billing import BillingStore
from proxy.pool_billing_sync import reconcile_pending


@pytest.mark.asyncio
async def test_reconciler_rotates_past_twenty_missing_logs(tmp_path,monkeypatch):
    store=BillingStore(tmp_path)
    ids=[]
    for i in range(21):
        rid=store.reserve('pool','job',2,123,.01,1,now=1);store.bind(rid,'req-'+str(i));ids.append(rid)
    requested=[]
    class Client:
        async def logs(self,request_id,token_id):
            requested.append(request_id)
            return [{'request_id':request_id,'token_id':2,'type':2,'quota':5}] if request_id=='req-20' else []
        async def quota_per_unit(self):return 500000
        async def close(self):pass
    class Manager:
        billing=store
        def client(self):return Client()
    async def fast_sleep(_):return None
    monkeypatch.setattr('proxy.pool_billing_sync.asyncio.sleep',fast_sleep)
    manager=Manager()
    await reconcile_pending(manager);await reconcile_pending(manager)
    assert 'req-20' in requested
    assert store.stats('pool')['settled_requests']==1
