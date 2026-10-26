"""Thirty-day immutable membership changes; no prompts, keys or remote bodies."""
from .platform_io import private_mode
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from datetime import datetime,timezone


_ID=re.compile(r'[A-Za-z0-9_.:\-]{1,100}\Z')

def safe(value):
    if not isinstance(value,str) or not _ID.fullmatch(value):raise ValueError('invalid_membership_identifier')
    return value

def ids(values):
    if not isinstance(values,list) or len(values)>100 or any(type(v) is not int or not 0<v<2**63 for v in values):raise ValueError('invalid_membership_channels')
    return sorted(set(values))


class MembershipHistory:
    def __init__(self,data_dir):
        root=Path(data_dir)
        if root.is_symlink():raise ValueError('unsafe_history_directory')
        root.mkdir(parents=True,exist_ok=True,mode=0o700);root.chmod(0o700)
        self.path=root/'a6_pool_membership_history.db'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600);private_mode(fd,0o600);os.close(fd)
        with sqlite3.connect(self.path) as c:
            c.executescript('PRAGMA journal_mode=WAL;CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY,pool_id TEXT,ts REAL,payload TEXT);CREATE INDEX IF NOT EXISTS membership_pool_time ON events(pool_id,ts);')
    def record(self,pool_id,*,before,after,reason,source,job_id=None,subscription_id=None,config_version=None,now=None):
        safe(pool_id);safe(reason)
        if source not in ('manual','auto_refresh','auto_audit','delete','system'):raise ValueError('invalid_membership_source')
        before=ids(before);after=ids(after)
        if before==after:return None
        if job_id is not None:safe(job_id)
        for value in (subscription_id,config_version):
            if value is not None and (type(value) is not int or value<1):raise ValueError('invalid_membership_version')
        now=time.time() if now is None else now
        event={'id':'member_'+uuid.uuid4().hex,'pool_id':pool_id,'created_at':datetime.fromtimestamp(now,timezone.utc).isoformat(),'before':before,'after':after,'added':sorted(set(after)-set(before)),'removed':sorted(set(before)-set(after)),'reason':reason,'source':source,'job_id':job_id,'subscription_id':subscription_id,'config_version':config_version}
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO events VALUES(?,?,?,?)',(event['id'],pool_id,now,json.dumps(event)))
            c.execute('DELETE FROM events WHERE ts<?',(now-30*86400,))
        return event
    def prune(self,now=None):
        now=time.time() if now is None else now
        with sqlite3.connect(self.path) as c:return c.execute('DELETE FROM events WHERE ts<?',(now-30*86400,)).rowcount

    def events(self,pool_id,limit=50):
        safe(pool_id)
        if type(limit) is not int or not 1<=limit<=200:raise ValueError('invalid_membership_limit')
        with sqlite3.connect(self.path) as c:return [json.loads(row[0]) for row in c.execute('SELECT payload FROM events WHERE pool_id=? ORDER BY ts DESC,rowid DESC LIMIT ?',(pool_id,limit))]
