import json
import sqlite3
import time

from proxy.pool_job_history import PoolJobHistory
from proxy.detector_store import DetectorStore
from test_detector_store import result


def test_more_than_two_hundred_jobs_remain_in_durable_thirty_day_history(tmp_path):
    history=PoolJobHistory(tmp_path)
    for i in range(205):history.save({'id':'job-'+str(i),'pool_id':'pool','kind':'verify','status':'done','progress':3})
    assert len(history.records())==205
    assert len(history.load())==200


def test_delayed_detector_result_retained_without_rolling_back_state(tmp_path):
    db=DetectorStore(tmp_path);now=time.time()
    db.record_result(result('new',status='verified',checked_at=now))
    db.record_result(result('late',status='suspect',checked_at=now-60))
    with db.connection() as c:
        assert c.execute('SELECT COUNT(*) FROM results').fetchone()[0]==2
    assert db.states()[0]['status']=='verified'
