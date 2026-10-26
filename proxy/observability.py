"""Read-only activity-source separation, never provider traffic or credentials."""
import json
import time
from datetime import datetime,timezone


def snapshot(store,monitor,pools):
    now=time.time();since=now-86400
    with store._connection() as c:
        requests=c.execute('SELECT COUNT(*),MAX(ts) FROM requests WHERE ts>=?',(since,)).fetchone()
        last=c.execute('SELECT MAX(ts) FROM requests').fetchone()[0]
        attempts=c.execute('SELECT COUNT(*) FROM attempts WHERE ts>=?',(since,)).fetchone()[0]
    with monitor.db.connection() as c:
        rows=[json.loads(r[0]) for r in c.execute('SELECT payload FROM results WHERE ts>=?',(since,))]
    provider={'results_24h':len(rows),'verified_24h':0,'suspect_24h':0,'unknown_24h':0,'error_24h':0,'upstream_attempts_24h':0,'interval_minutes':monitor.db.settings().get('interval_minutes')}
    for row in rows:
        key=row.get('status','unknown')+'_24h'
        if key in provider:provider[key]+=1
        provider['upstream_attempts_24h']+=sum(ch.get('attempts',0) or 0 for ch in row.get('challenges',[]) if isinstance(ch,dict))
    jobs=pools.job_history.records(since)
    today=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
    ids={p['id'] for p in pools.db.list()}
    bills=[pools.billing.stats(id,day=today) for id in ids]
    return {'generated_at':datetime.fromtimestamp(now,timezone.utc).isoformat(),'gateway':{'requests_24h':requests[0],'attempts_24h':attempts,'last_request_at':datetime.fromtimestamp(last,timezone.utc).isoformat() if last else None},'provider_checks':provider,'a6_checks':{'jobs_24h':len(jobs),'probes_24h':sum(j.get('probe_requests',0) or 0 for j in jobs),'errors_24h':sum(j.get('status')=='error' for j in jobs),'actual_cost_usd_utc_day':sum(b['actual_cost_usd'] for b in bills),'pending_reserved_usd':sum(b['pending_reserved_usd'] for b in bills)},'scope_note':'Клиентский трафик и синтетические проверки учитываются отдельно. Запросы к A6API напрямую, в обход этого прокси, в клиентскую статистику не входят. Активность — последние 24 часа; расходы — текущие UTC-сутки, резерв не списание.'}
