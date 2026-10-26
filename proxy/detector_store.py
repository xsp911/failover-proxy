"""Sanitized persistent fingerprint results and transition-only journal."""
from .platform_io import private_mode
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from .detector_evidence import sanitize_evidence, classify_confirmation

ID=re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')
HEX=re.compile(r'[a-f0-9]{64}\Z')


def iso(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()

def safe_text(value,maximum=200):
    if not isinstance(value,str) or not value or len(value)>maximum or any(ord(x)<32 for x in value):raise ValueError('invalid_detector_text')
    return value

def model_name(value):
    value=safe_text(value)
    if any(x.isspace() for x in value):raise ValueError('invalid_model')
    return value

def probability(value):
    if value is None:return None
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1:raise ValueError('invalid_probability')
    return value

def timestamp(value):
    if isinstance(value,str):value=datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('invalid_checked_at')
    return float(value)


class DetectorStore:
    def __init__(self,data_dir):
        root=Path(data_dir);root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=root/'detector.db'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600);private_mode(fd,0o600);os.close(fd)
        with self.connection() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, revision INTEGER, payload TEXT);
                CREATE TABLE IF NOT EXISTS results (id TEXT PRIMARY KEY,ts REAL,payload TEXT);
                CREATE INDEX IF NOT EXISTS results_ts ON results(ts);
                CREATE TABLE IF NOT EXISTS states (binding TEXT PRIMARY KEY,provider_id TEXT,model TEXT,reference_model TEXT,configuration_key TEXT,payload TEXT);
                CREATE INDEX IF NOT EXISTS states_provider ON states(provider_id);
                CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL,payload TEXT);
                CREATE INDEX IF NOT EXISTS journal_ts ON journal(ts);
            ''')
            c.execute('INSERT OR IGNORE INTO settings VALUES(1,0,?)',(json.dumps({'enabled':False,'interval_minutes':30,'threshold_percent':95,'targets':[]}),))

    @contextmanager
    def connection(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        try:
            c.execute('PRAGMA busy_timeout=10000')
            with c:yield c
        finally:c.close()

    @staticmethod
    def _settings(row):return {'revision':row['revision'],**json.loads(row['payload'])}

    def settings(self):
        with self.connection() as c:return self._settings(c.execute('SELECT * FROM settings WHERE id=1').fetchone())

    def save_settings(self,payload,expected_revision):
        if isinstance(expected_revision,bool) or not isinstance(expected_revision,int):raise ValueError('invalid_revision')
        if not isinstance(payload.get('enabled'),bool):raise ValueError('invalid_enabled')
        interval=payload.get('interval_minutes')
        if isinstance(interval,bool) or not isinstance(interval,int) or not 5<=interval<=1440:raise ValueError('invalid_interval_minutes')
        threshold=payload.get('threshold_percent')
        if isinstance(threshold,bool) or not isinstance(threshold,(int,float)) or not math.isfinite(threshold) or not 50<=threshold<=100:raise ValueError('invalid_threshold_percent')
        targets=payload.get('targets')
        if not isinstance(targets,list) or len(targets)>50:raise ValueError('invalid_targets')
        output=[];seen=set()
        for row in targets:
            if not isinstance(row,dict) or not isinstance(row.get('provider_id'),str) or not ID.fullmatch(row['provider_id']):raise ValueError('invalid_provider_id')
            item={'provider_id':row['provider_id'],'model':model_name(row.get('model')),'reference_model':model_name(row.get('reference_model'))}
            key=(item['provider_id'],item['model'])
            if key in seen:raise ValueError('duplicate_target')
            seen.add(key);output.append(item)
        value={'enabled':payload['enabled'],'interval_minutes':interval,'threshold_percent':threshold,'targets':output}
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT * FROM settings WHERE id=1').fetchone()
            if current['revision']!=expected_revision:raise ValueError('revision_conflict')
            old_threshold=json.loads(current['payload'])['threshold_percent']
            c.execute('UPDATE settings SET revision=revision+1,payload=? WHERE id=1',(json.dumps(value),))
            if threshold!=old_threshold:
                changed_at=iso(time.time())
                for record in c.execute('SELECT binding,payload FROM states').fetchall():
                    state=json.loads(record['payload']);conclusion=state.get('conclusive_result')
                    if not conclusion:continue
                    score=conclusion.get('target_probability')
                    if score is None:continue
                    updated={**conclusion,'threshold_percent':threshold}
                    if 'confirmation' in conclusion:
                        status,prob,error,confirmation=classify_confirmation(conclusion,threshold,conclusion['status'],conclusion.get('error_kind'))
                        updated.update(status=status,target_probability=prob,error_kind=error,confirmation=confirmation)
                    else:
                        status='verified' if score*100>=threshold else 'suspect'
                        updated['status']=status
                    previous=state['status']
                    state['status']=status
                    state['conclusive_result']=updated
                    state['policy_updated_at']=changed_at
                    c.execute('UPDATE states SET payload=? WHERE binding=?',(json.dumps(state),record['binding']))
                    if previous!=status:
                        change={**updated,'from_status':previous,'to_status':status,'checked_at':changed_at,'sample_checked_at':conclusion['checked_at'],'source':'threshold_change','threshold_percent':threshold,'previous_threshold_percent':old_threshold}
                        c.execute('INSERT INTO journal(ts,payload) VALUES(?,?)',(timestamp(changed_at),json.dumps(change)))
            return self._settings(c.execute('SELECT * FROM settings WHERE id=1').fetchone())

    @staticmethod
    def binding(provider_id,model,reference_model,configuration_key):
        return json.dumps([provider_id,model,reference_model,configuration_key],separators=(',',':'))

    def record_result(self,row):
        provider=row.get('provider_id');key=row.get('configuration_key')
        if not isinstance(provider,str) or not ID.fullmatch(provider) or not isinstance(key,str) or not HEX.fullmatch(key):raise ValueError('invalid_binding')
        status=row.get('status')
        if status not in ('verified','suspect','unknown','error'):raise ValueError('invalid_status')
        model=model_name(row.get('model'));reference=model_name(row.get('reference_model'))
        prob=probability(row.get('target_probability'))
        evidence=sanitize_evidence(row)
        if status in ('verified','suspect') and 'confirmation' not in evidence:
            if prob is None:raise ValueError('invalid_conclusion')
        source=row.get('source')
        if source not in ('manual','scheduled'):raise ValueError('invalid_source')
        ts=timestamp(row.get('checked_at'))
        duration=row.get('duration_ms')
        if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<0:raise ValueError('invalid_duration')
        candidates=[]
        raw=row.get('candidates',[])
        if not isinstance(raw,list):raise ValueError('invalid_candidates')
        for candidate in raw[:5]:
            candidates.append({'model':model_name(candidate.get('model')),'probability':probability(candidate.get('probability'))})
        error=row.get('error_kind')
        if error is not None and (not isinstance(error,str) or not re.fullmatch('[a-zA-Z0-9_:-]{1,80}',error)):raise ValueError('invalid_error_kind')
        result={'id':safe_text(row.get('id'),160),'provider_id':provider,'provider_name':safe_text(row.get('provider_name'),100),
            'model':model,'reference_model':reference,'configuration_key':key,'status':status,'target_probability':prob,
            'prediction':model_name(row['prediction']) if row.get('prediction') else None,'prediction_probability':probability(row.get('prediction_probability')),
            'checked_at':iso(ts),'source':source,'error_kind':error,'candidates':candidates,'duration_ms':duration,
            'reference_version':safe_text(row.get('reference_version'),160),**evidence}
        binding=self.binding(provider,model,reference,key)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT 1 FROM results WHERE id=?',(result['id'],)).fetchone():return result
            policy=json.loads(c.execute('SELECT payload FROM settings WHERE id=1').fetchone()[0])
            result['threshold_percent']=policy['threshold_percent']
            if 'confirmation' in evidence:
                if status!='error' and error not in ('configuration_changed','monitor_disabled_or_target_removed'):
                    status,prob,error,confirmation=classify_confirmation(evidence,policy['threshold_percent'],status,error)
                    result.update(status=status,target_probability=prob,error_kind=error,confirmation=confirmation)
            elif status in ('verified','suspect'):
                status='verified' if prob*100>=policy['threshold_percent'] else 'suspect'
                result['status']=status
            previous=c.execute('SELECT payload FROM states WHERE binding=?',(binding,)).fetchone()
            state=json.loads(previous[0]) if previous else {'provider_id':provider,'model':model,'reference_model':reference,'configuration_key':key,'status':'unknown','conclusive_result':None}
            # Delayed completion may not overwrite a newer observed state.
            if previous and timestamp(state['checked_at'])>ts:
                # Keep chronological evidence even when it is too old to change
                # current quarantine/state. Delayed results aren't lost history.
                c.execute('INSERT INTO results VALUES(?,?,?)',(result['id'],ts,json.dumps(result)))
                return result
            before=state['status']
            state.update(provider_name=result['provider_name'],checked_at=result['checked_at'],latest_status=status,latest_result=result)
            if status in ('verified','suspect'):
                state.update(status=status,conclusive_result=result)
                if before!=status:
                    change={**result,'from_status':before,'to_status':status}
                    c.execute('INSERT INTO journal(ts,payload) VALUES(?,?)',(ts,json.dumps(change)))
            c.execute('INSERT INTO states VALUES(?,?,?,?,?,?) ON CONFLICT(binding) DO UPDATE SET payload=excluded.payload',
                      (binding,provider,model,reference,key,json.dumps(state)))
            c.execute('INSERT INTO results VALUES(?,?,?)',(result['id'],ts,json.dumps(result)))
            cutoff=time.time()-30*86400
            for table in ('results','journal'):
                c.execute(f'DELETE FROM {table} WHERE ts<?',(cutoff,))
                c.execute(f'DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} ORDER BY ts DESC,rowid DESC LIMIT -1 OFFSET 10000)')
            # Keep at most1000 recent binding states; preserve conclusive suspects
            # even if old, while normal config hard-limits active targets50.
            c.execute("DELETE FROM states WHERE binding IN (SELECT binding FROM states WHERE json_extract(payload,'$.status')!='suspect' ORDER BY json_extract(payload,'$.checked_at') DESC LIMIT -1 OFFSET 1000)")
        return result

    def states(self):
        with self.connection() as c:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM states ORDER BY json_extract(payload,\'$.checked_at\') DESC LIMIT 2000')]

    def state(self,provider_id,model,reference_model,configuration_key):
        with self.connection() as c:
            row=c.execute('SELECT payload FROM states WHERE binding=?',(self.binding(provider_id,model,reference_model,configuration_key),)).fetchone()
            return json.loads(row[0]) if row else None

    def latest_results(self,limit=50):
        with self.connection() as c:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM results ORDER BY ts DESC LIMIT ?',(max(1,min(int(limit),200)),))]

    def journal(self,limit=100):
        with self.connection() as c:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM journal ORDER BY ts DESC,id DESC LIMIT ?',(max(1,min(int(limit),200)),))]

    def blocked_provider_ids(self,bindings):
        with self.connection() as c:
            c.execute('BEGIN')
            settings=self._settings(c.execute('SELECT * FROM settings WHERE id=1').fetchone())
            if not settings['enabled']:return set()
            selected={(t['provider_id'],t['model'],t['reference_model']) for t in settings['targets']}
            result=set()
            for b in bindings:
                if (b['provider_id'],b['model'],b['reference_model']) not in selected:continue
                row=c.execute('SELECT payload FROM states WHERE binding=?',(self.binding(b['provider_id'],b['model'],b['reference_model'],b['configuration_key']),)).fetchone()
                if row and json.loads(row[0])['status']=='suspect':result.add(b['provider_id'])
                elif not row or json.loads(row[0]).get('conclusive_result') is None:
                    for key in b.get('previous_configuration_keys',[]):
                        previous=c.execute('SELECT payload FROM states WHERE binding=?',(self.binding(b['provider_id'],b['model'],b['reference_model'],key),)).fetchone()
                        if previous and json.loads(previous[0]).get('status')=='suspect':result.add(b['provider_id']);break
            return result
