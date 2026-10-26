import time

import pytest

from proxy.model_monitor import ModelMonitor
from proxy.store import Store
from test_detector_store import result


@pytest.mark.asyncio
async def test_restart_resumes_paid_monitor_due_time_not_immediate(tmp_path):
    store=Store(tmp_path);monitor=ModelMonitor(store,tmp_path)
    settings=monitor.db.settings();monitor.db.save_settings({'enabled':True,'interval_minutes':30,'threshold_percent':60,'targets':[]},settings['revision'])
    now=time.time();monitor.db.record_result(result('prior',status='verified',source='scheduled',checked_at=now-60,duration_ms=10000))
    called=[]
    async def enqueue(*args):called.append(args)
    monitor.enqueue=enqueue
    await monitor.start()
    try:
        assert monitor.next_run>=now-70+1800-1
        assert called==[]
        assert monitor.db.settings()['interval_minutes']==30
    finally:await monitor.close()
