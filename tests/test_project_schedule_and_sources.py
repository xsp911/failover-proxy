import httpx
import pytest

from proxy.pool_auto_store import PoolAutoStore
from proxy.app import create_app


def test_explicit_not_before_can_postpone_already_earlier_refresh(tmp_path):
    state=PoolAutoStore(tmp_path);state.configure('pool',True,1000)
    state.request_refill('pool',1000,5000,'seller_cooldown')
    current=state.state('pool');assert current['next_refresh_at']==5000 and current['refresh_not_before']==5000
    assert not state.claim_due('pool','refresh',4999)
    assert state.claim_due('pool','audit',4600),'hourly audits must not be reduced by refill pause'
    state.finish('pool','audit',now=4601)
    assert state.claim_due('pool','refresh',5000)


def test_manual_automatic_run_respects_original_regular_intervals(tmp_path):
    state=PoolAutoStore(tmp_path);state.configure('pool',True,1000)
    assert state.start_manual('pool','refresh',1100)
    assert state.state('pool')['next_refresh_at']==1100+21600
    state.finish('pool','refresh',now=1101)
    assert state.start_manual('pool','audit',1200)
    assert state.state('pool')['next_audit_at']==1200+3600


@pytest.mark.asyncio
async def test_local_observability_has_separate_empty_sources_no_upstream(tmp_path):
    app=create_app(tmp_path);app.state.store.initialize_admin('admin','password')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        assert (await c.get('/admin/observability')).status_code==401
        await c.post('/admin/login',json={'username':'admin','password':'password'})
        r=await c.get('/admin/observability');assert r.status_code==200,r.text
        data=r.json();assert data['gateway']['requests_24h']==0 and data['provider_checks']['results_24h']==0 and data['a6_checks']['probes_24h']==0
        assert data['provider_checks']['interval_minutes']==30
    await app.state.pools.close();await app.state.monitor.close();await app.state.engine.close()
