import time

import pytest

from proxy.pool_billing import BillingStore
from proxy.pool_billing_sync import reconcile_pending
from proxy.pool_automation import apply_members
from proxy.a6_probe import TargetProbe
from proxy.pool_manager import iso,PoolError
from test_pool_parallel import setup


def test_reconcile_backoff_persists_one_five_fifteen_sixty_minutes(tmp_path):
    store=BillingStore(tmp_path);r=store.reserve('pool','job',2,123,.01,1,now=1000);store.bind(r,'req')
    assert store.due_reconcile(now=1059)==[]
    assert len(store.due_reconcile(now=1060))==1
    assert store.defer_reconcile(r,'error_log_only',now=1060)==1120
    assert store.due_reconcile(now=1119)==[]
    assert len(BillingStore(tmp_path).due_reconcile(now=1120))==1
    assert store.defer_reconcile(r,now=1120)==1420
    assert store.defer_reconcile(r,now=1420)==2320
    assert store.defer_reconcile(r,now=2320)==5920
    assert store.stats('pool')['pending_reserved_usd']==.01


@pytest.mark.asyncio
async def test_day_old_pending_one_lookup_then_six_hour_pause(tmp_path):
    store=BillingStore(tmp_path);r=store.reserve('pool','job',2,123,.01,1,now=1);store.bind(r,'req');lookups=[]
    class Client:
        async def logs(self,*args):lookups.append(1);return [{'request_id':'req','token_id':2,'type':5,'quota':0}]
        async def close(self):pass
    class Manager:
        billing=store
        def client(self):return Client()
    m=Manager();await reconcile_pending(m);await reconcile_pending(m)
    assert len(lookups)==1
    details=store.pending_diagnostics('pool');assert details['error_only_requests']==1
    assert details['next_reconcile_at']>time.time()+21590
    assert store.stats('pool')['settled_requests']==0


async def published(tmp_path,n=3):
    m,c,p=await setup(tmp_path,n);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    candidates=await m.discover(p,c)
    for row in candidates:m.db.save_verification(p['id'],p['revision'],{**row,'verification_status':'verified','probability':.99,'actual_channel_id':row['channel_id'],'verified_at':iso(time.time())})
    return m,c,p,await m.discover(p,c)


@pytest.mark.asyncio
async def test_unchanged_owned_route_updates_evidence_without_put(tmp_path):
    m,c,p,members=await published(tmp_path)
    await apply_members(m,p,c,members);writes=len(c.writes);version=m.auto.remote(p['id'])['config_version']
    await apply_members(m,p,c,list(reversed(members)))
    assert len(c.writes)==writes and m.auto.remote(p['id'])['config_version']==version
    remote=next(r for r in c.routes if r['token_id']==1);remote['caps']['output_price_micros']=99999;remote['config_version']+=1
    with pytest.raises(PoolError):await apply_members(m,p,c,members)
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_hour_audit_admits_only_fresh_verified_reserve_in_one_cycle(tmp_path,monkeypatch):
    m,c,p,allrows=await published(tmp_path,3)
    await apply_members(m,p,c,allrows[:1]);p=m.db.get(p['id']);calls=[]
    async def passed(self,pool,candidate,secret,reserve):
        calls.append(candidate['channel_id'])
        return {'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'request_ids':['req']}
    monkeypatch.setattr(TargetProbe,'run',passed)
    j=await m.enqueue(p['id'],'auto_audit',True);await m.tasks[j['id']]
    job=m.jobs[j['id']]
    assert job['status']=='done' and calls==[100]
    assert {c['channel_id'] for c in m.db.get(p['id'])['members']}=={100,101,102}
    assert set(job['result']['cached_replacements'])=={101,102}
    assert not job['result'].get('refill_scheduled')
    await m.close();await m.monitor.close()
