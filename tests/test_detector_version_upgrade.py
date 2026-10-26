import asyncio
import time

import pytest

from proxy.pool_manager import PoolManager,iso
from proxy.detector_store import DetectorStore
from proxy.model_monitor import ModelMonitor
from test_pool_parallel import setup
from test_detector_store import result,settings,binding


@pytest.mark.asyncio
async def test_pool_old_reference_cannot_be_laundered_by_operational_edit(tmp_path):
    m,c,p=await setup(tmp_path,1);candidate=(await m.discover(p,c))[0]
    m.db.save_verification(p['id'],p['revision'],{**candidate,'verification_status':'verified','probability':.99,'actual_channel_id':100,'verified_at':iso(time.time()),'reference_version':m.monitor.catalog['source_commit']})
    await m.discover(p,c);old=m.monitor.catalog['source_commit'];m.monitor.catalog={**m.monitor.catalog,'source_commit':'new-reference-version'}
    visible=await m.get(p['id']);assert visible['candidates'][0]['verification_status']=='expired'
    saved=await m.policy({**visible,'name':'changed-name'},p['id'])
    assert saved['candidates'][0]['verification_status']=='expired'
    record=m.db.verification(p['id'],100)
    assert record is None or record.get('reference_version')!=m.monitor.catalog['source_commit']
    await m.close();await m.monitor.close()


def test_prior_reference_quarantine_survives_inconclusive_new_reference(tmp_path):
    store=DetectorStore(tmp_path);store.save_settings(settings(),store.settings()['revision'])
    store.record_result(result('old',status='suspect'))
    current={**binding(),'configuration_key':'b'*64,'previous_configuration_keys':['a'*64]}
    assert store.blocked_provider_ids([current])=={'p1'}
    store.record_result(result('new-unknown',status='unknown',configuration_key='b'*64))
    assert store.blocked_provider_ids([current])=={'p1'}
    store.record_result(result('new-verified',status='verified',configuration_key='b'*64))
    assert store.blocked_provider_ids([current])==set()
