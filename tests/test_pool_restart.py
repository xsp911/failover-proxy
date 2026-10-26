import asyncio

import pytest

from proxy.pool_manager import PoolError
from test_pool_parallel import setup


@pytest.mark.asyncio
async def test_restart_requires_paid_confirmation_before_cancelling(tmp_path):
    m,c,p=await setup(tmp_path,1)
    with pytest.raises(PoolError,match='confirm_paid_verification_required'):await m.restart(p['id'],False)
    assert not m.jobs and not c.writes
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_restart_waits_for_cleanup_before_new_job(tmp_path):
    m,c,p=await setup(tmp_path,1)
    cleanup=asyncio.Event();started=asyncio.Event()
    async def running():
        try:started.set();await asyncio.Future()
        finally:await asyncio.sleep(.01);cleanup.set()
    task=asyncio.create_task(running());await started.wait()
    m.tasks['old']=task;m.jobs['old']={'id':'old','pool_id':p['id'],'status':'running','kind':'verify'}
    seen=[]
    async def run_now(id,confirm):
        assert cleanup.is_set() and id in m.restarting
        seen.append((id,confirm));m.jobs['new']={'id':'new','pool_id':id,'status':'queued'};return dict(m.jobs['new'])
    m.run_now=run_now
    result=await m.restart(p['id'],True)
    assert result['restarted'] is True and seen==[(p['id'],True)]
    assert m.jobs['old']['error_kind']=='cancelled' and not m.restarting
    await m.close();await m.monitor.close()


@pytest.mark.asyncio
async def test_restart_uses_existing_safe_mode_not_forced_retest(tmp_path):
    m,c,p=await setup(tmp_path,1)
    async def run_now(id,confirm):
        m.jobs['new']={'id':'new','pool_id':id,'kind':'verify','status':'queued'}
        return dict(m.jobs['new'])
    m.run_now=run_now
    before=m.db.get(p['id'])
    await m.restart(p['id'],True)
    after=m.db.get(p['id'])
    assert after==before and not c.writes
    await m.close();await m.monitor.close()
