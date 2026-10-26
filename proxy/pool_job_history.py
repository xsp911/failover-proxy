"""Bounded durable job summaries; no prompts, model output or credentials."""
from .platform_io import private_mode
import json
import os
import sqlite3
import time
from pathlib import Path


class PoolJobHistory:
    def __init__(self,data_dir):
        self.path=Path(data_dir)/'a6_pool_jobs.db'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600);private_mode(fd,0o600);os.close(fd)
        with sqlite3.connect(self.path) as c:c.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,payload TEXT,updated REAL)')
    def save(self,job):
        allowed=('id','pool_id','kind','status','created_at','updated_at','last_activity_at','progress','total','error_kind','automatic','phase','parallelism','workers','search','warnings','result','billing','probe_requests','removed_channels')
        value={k:job[k] for k in allowed if k in job}
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated=excluded.updated',(job['id'],json.dumps(value),time.time()))
            c.execute('DELETE FROM jobs WHERE updated<?',(time.time()-30*86400,))
    def records(self,since=None):
        with sqlite3.connect(self.path) as c:
            rows=c.execute('SELECT payload FROM jobs WHERE updated>=? ORDER BY updated',(since if since is not None else 0,)).fetchall()
        return [json.loads(row[0]) for row in rows]
    def load(self):
        with sqlite3.connect(self.path) as c:rows=c.execute('SELECT payload FROM (SELECT payload,updated FROM jobs ORDER BY updated DESC LIMIT 200) ORDER BY updated').fetchall()
        result=[]
        for row in rows:
            job=json.loads(row[0])
            if job.get('status') in ('queued','running'):
                job.update(status='error',error_kind='interrupted_by_restart')
            result.append(job)
        return result
