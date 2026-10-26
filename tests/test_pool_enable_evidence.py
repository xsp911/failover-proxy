import time

import pytest

from proxy.pool_manager import iso
from test_pool_manager import manager,policy,row


@pytest.mark.asyncio
async def test_enable_checked_pool_retains_verified_candidates(manager):
    m,c=manager;p=await m.policy(policy());c.rows=[row()]
    candidate=(await m.discover(p,c))[0]
    m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'verified_at':iso(time.time()),'actual_channel_id':123,'reference_version':m.monitor.catalog['source_commit']})
    await m.discover(p,c)
    current=m.db.get(p['id'])
    saved=await m.policy({**current,'enabled':True},p['id'])
    assert saved['candidates'][0]['verification_status']=='verified'
    preview=await m.preview(p['id'])
    assert preview['can_publish'] is True and len(preview['channels'])==1
    assert not c.writes
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_price_policy_change_still_invalidates_checks(manager):
    m,c=manager;p=await m.policy(policy());c.rows=[row()]
    candidate=(await m.discover(p,c))[0]
    m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'verified_at':iso(time.time()),'actual_channel_id':123,'reference_version':m.monitor.catalog['source_commit']})
    current=m.db.get(p['id'])
    saved=await m.policy({**current,'max_output_usd':.01},p['id'])
    assert not saved['candidates']
    assert m.db.verification(p['id'],123) is None
    await m.close();await m.monitor.close()
