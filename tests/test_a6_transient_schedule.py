import time

import pytest

from proxy.a6_client import A6Error
from test_pool_parallel import setup


@pytest.mark.asyncio
async def test_auto_catalogue_failure_retries_later_not_six_hours(tmp_path):
    m,c,p=await setup(tmp_path,1);p=await m.policy({**p,'enabled':True,'auto_publish':True},p['id'])
    async def unstable(*args):raise A6Error('a6_search_snapshot_changed')
    c.search=unstable
    before=time.time();j=await m.enqueue(p['id'],'auto_refresh',True);await m.tasks[j['id']]
    assert m.jobs[j['id']]['status']=='error'
    state=m.auto.state(p['id']);assert before+299<=state['next_refresh_at']<=time.time()+301
    assert not c.writes
    await m.close();await m.monitor.close()
