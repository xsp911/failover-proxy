import pytest

from proxy.pool_gateway import guarded_config
from proxy.pool_automation import apply_members
from proxy.pool_manager import PoolError,iso
from test_pool_parallel import setup
import time


@pytest.mark.asyncio
async def test_gateway_rejects_external_remote_whitelist_edit(tmp_path):
    m,c,p=await setup(tmp_path,1);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    candidate=(await m.discover(p,c))[0]
    m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'actual_channel_id':100,'verified_at':iso(time.time())})
    members=await m.discover(p,c);await apply_members(m,p,c,members)
    config={'providers':[{'id':'a6','base_url':'https://api.a6api.com/v1','api_key':'sk-secret-for-1'}],'models':[{'name':'alias','routes':[{'provider_id':'a6','upstream_model':p['model']}]}]}
    assert await guarded_config(m,config,'alias') is config
    remote=next(r for r in c.routes if r['token_id']==1);remote['channels']=[{'channel_id':999}];remote['config_version']+=1
    with pytest.raises(PoolError,match='managed_pool_remote_changed'):await guarded_config(m,config,'alias')
    await m.close();await m.monitor.close()
