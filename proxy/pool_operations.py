"""Durable remote test-route ownership and pilot evidence, no secrets."""
from .platform_io import private_mode
import json
import os
from pathlib import Path
import sqlite3
import time


class PoolOperations:
    def __init__(self,data_dir):
        self.path=Path(data_dir)/'a6_pool_operations.db'
        fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600);private_mode(fd,0o600);os.close(fd)
        with sqlite3.connect(self.path) as c:
            c.execute('CREATE TABLE IF NOT EXISTS routes (token_id INTEGER, model TEXT, subscription_id INTEGER, version INTEGER, draft TEXT, state TEXT, updated REAL, PRIMARY KEY(token_id,model))')

    def get(self,token_id,model):
        with sqlite3.connect(self.path) as c:
            c.row_factory=sqlite3.Row
            r=c.execute('SELECT * FROM routes WHERE token_id=? AND model=?',(token_id,model)).fetchone()
            return dict(r) if r else None

    def record(self,token_id,model,subscription_id,version,draft,state):
        with sqlite3.connect(self.path) as c:
            c.execute('INSERT INTO routes VALUES(?,?,?,?,?,?,?) ON CONFLICT(token_id,model) DO UPDATE SET subscription_id=excluded.subscription_id,version=excluded.version,draft=excluded.draft,state=excluded.state,updated=excluded.updated',
                (token_id,model,subscription_id,version,json.dumps(draft),state,time.time()))
