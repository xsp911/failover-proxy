import copy
import time

import pytest

from proxy.pool_manager import PoolError,iso
from proxy.pool_automation import apply_members,remote_before
from test_pool_parallel import setup


async def published(tmp_path):
    m,c,p=await setup(tmp_path,5);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    candidates=await m.discover(p,c)
    for candidate in candidates[:3]:m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'actual_channel_id':candidate['channel_id'],'verified_at':iso(time.time())})
    candidates=await m.discover(p,c);await apply_members(m,p,c,candidates[:3]);return m,c,m.db.get(p['id'])


@pytest.mark.asyncio
async def test_member_resize_accepts_exact_remote_version_drift_keeps_proof(tmp_path):
    m,c,p=await published(tmp_path)
    work=next(r for r in c.routes if r['token_id']==1);work['config_version']+=3
    before=time.time();m.auto.claim_due(p['id'],'refresh',before);m.auto.finish(p['id'],'refresh',now=before)
    saved=await m.policy({**p,'max_members':6},p['id'])
    assert saved['max_members']==6 and len([r for r in saved['candidates'] if r.get('verification_status')=='verified'])==3
    assert m.auto.remote(p['id'])['config_version']==work['config_version']
    assert m.auto.state(p['id'])['next_refresh_at']<=before+65
    assert len([d for t,_,d in c.writes if t==1])==1
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_real_remote_cap_change_cannot_be_overwritten_on_resize(tmp_path):
    m,c,p=await published(tmp_path)
    work=next(r for r in c.routes if r['token_id']==1);work['config_version']+=1;work['caps']['output_price_micros']=999999
    with pytest.raises(PoolError,match='auto_remote_route_conflict'):await m.policy({**p,'max_members':6},p['id'])
    assert m.db.get(p['id'])['max_members']==3
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_auto_cycle_reconciles_only_identical_config_drift(tmp_path):
    m,c,p=await published(tmp_path);work=next(r for r in c.routes if r['token_id']==1);work['config_version']+=1
    r=await remote_before(m,p,c)
    assert r['config_version']==work['config_version'] and m.auto.remote(p['id'])['config_version']==work['config_version']
    await m.close();await m.monitor.close()
