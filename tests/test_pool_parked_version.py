import copy

import pytest

from proxy.pool_cleanup import park_owned,reconcile_owned
from test_pool_parallel import setup


@pytest.mark.asyncio
async def test_exact_parked_configuration_may_reconcile_version_drift(tmp_path):
    m,c,p=await setup(tmp_path,1)
    route=await c.save_subscription(2,p['model'],m.route_draft(p,[100]))
    parked=await park_owned(m,c,p,2,route)
    owner=m.operations.get(2,p['model'])
    parked['config_version']+=1
    assert await reconcile_owned(m,c,p,2,owner,parked)
    assert m.operations.get(2,p['model'])['version']==parked['config_version']
    assert len(c.writes)==2
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_changed_active_caps_not_reconciled_as_harmless_drift(tmp_path):
    m,c,p=await setup(tmp_path,1)
    route=await c.save_subscription(2,p['model'],m.route_draft(p,[100]))
    parked=await park_owned(m,c,p,2,route);owner=m.operations.get(2,p['model'])
    changed=copy.deepcopy(parked);changed['config_version']+=1;changed['caps']['input_price_micros']=1000
    assert not await reconcile_owned(m,c,p,2,owner,changed)
    assert m.operations.get(2,p['model'])['version']==owner['version']
    await m.close();await m.monitor.close()
