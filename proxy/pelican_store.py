"""Private durable single-scene generation results, never provider credentials."""
from .platform_io import private_mode
import json
import os
import sqlite3
import time
from pathlib import Path


class PelicanStore:
    def __init__(self,data_dir):
        self.path=Path(data_dir)/'pelican.db'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600);private_mode(fd,0o600);os.close(fd)
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY,payload TEXT NOT NULL,svg TEXT,created REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS motion (run_id TEXT PRIMARY KEY,payload TEXT NOT NULL,frames TEXT NOT NULL)')
            for id,payload in c.execute('SELECT id,payload FROM runs').fetchall():
                run=json.loads(payload)
                if run.get('status') in ('queued','running'):
                    run.update(status='error',error_kind='interrupted_by_restart',phase='finished')
                    c.execute('UPDATE runs SET payload=? WHERE id=?',(json.dumps(run),id))
    def save(self,run,svg=None):
        fields=('id','provider_id','provider_name','model','protocol','status','phase','created_at','finished_at','elapsed_ms','error_kind','checks','svg_available','usage','timeout_seconds','scene','generation_count','effort','max_output_tokens','diagnostics')
        value={k:run[k] for k in fields if k in run}
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO runs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,svg=COALESCE(excluded.svg,runs.svg)',(run['id'],json.dumps(value),svg,time.time()))
            c.execute('DELETE FROM runs WHERE created<?',(time.time()-30*86400,))
            c.execute('DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY created DESC LIMIT 100)')
            c.execute('DELETE FROM motion WHERE run_id NOT IN (SELECT id FROM runs)')
    def save_motion(self,id,result):
        result=dict(result);frames=result.pop('frames')
        with sqlite3.connect(self.path) as c:
            if not c.execute('SELECT 1 FROM runs WHERE id=?',(id,)).fetchone():raise ValueError('pelican_not_found')
            c.execute('INSERT INTO motion VALUES(?,?,?) ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload,frames=excluded.frames',(id,json.dumps(result),json.dumps(frames)))
        return result
    def motion(self,id,with_frames=False):
        with sqlite3.connect(self.path) as c:row=c.execute('SELECT payload,frames FROM motion WHERE run_id=?',(id,)).fetchone()
        if not row:return None
        value=json.loads(row[0])
        if with_frames:value['frames']=json.loads(row[1])
        return value
    def list(self):
        with sqlite3.connect(self.path) as c:return [json.loads(r[0]) for r in c.execute('SELECT payload FROM runs ORDER BY created DESC LIMIT 100')]
    def get(self,id):
        with sqlite3.connect(self.path) as c:
            row=c.execute('SELECT payload,svg FROM runs WHERE id=?',(id,)).fetchone()
            return (json.loads(row[0]),row[1]) if row else None
