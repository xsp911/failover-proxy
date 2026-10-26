from proxy.pool_job_history import PoolJobHistory


def test_job_history_survives_restart_and_marks_interruption(tmp_path):
    store=PoolJobHistory(tmp_path)
    store.save({'id':'done','pool_id':'pool','kind':'verify','status':'done','progress':3,'result':{'verified':1},'raw_response':'must not persist','secret':'must not persist'})
    store.save({'id':'running','pool_id':'pool','kind':'verify','status':'running','progress':1})
    reopened=PoolJobHistory(tmp_path);jobs=reopened.load()
    assert jobs[0]['status']=='done' and jobs[0]['result']['verified']==1
    assert 'raw_response' not in jobs[0] and 'secret' not in jobs[0]
    assert jobs[1]['status']=='error' and jobs[1]['error_kind']=='interrupted_by_restart'
