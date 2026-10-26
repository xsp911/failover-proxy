"""Durable request-linked actual USD charges; pending estimates are not spend."""
from .platform_io import private_mode
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from decimal import Decimal,ROUND_CEILING,InvalidOperation
from datetime import datetime,timezone


_ID=re.compile(r'[A-Za-z0-9_.:\-]{1,160}\Z')

def identity(value):
    if not isinstance(value,str) or not _ID.fullmatch(value):raise ValueError('invalid_billing_id')
    return value

def nanos(value):
    try:
        n=Decimal(str(value))
        if not n.is_finite() or n<0 or n>1000 or isinstance(value,bool):raise ValueError()
        return int((n*1000000000).to_integral_value(rounding=ROUND_CEILING))
    except (ValueError,InvalidOperation,OverflowError):raise ValueError('invalid_billing_amount') from None

def positive(value):
    if type(value) is not int or value<=0:raise ValueError('invalid_billing_integer')
    return value

def utcday(now):return datetime.fromtimestamp(now,timezone.utc).date().isoformat()


class BillingStore:
    def __init__(self,data_dir):
        root=Path(data_dir);root.mkdir(parents=True,exist_ok=True)
        self.path=root/'a6_pool_billing.db'
        fd=os.open(self.path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600);private_mode(fd,0o600);os.close(fd)
        with self.connection() as c:
            c.executescript('''PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS billing (reservation_id TEXT PRIMARY KEY,pool_id TEXT NOT NULL,job_id TEXT NOT NULL,token_id INTEGER NOT NULL,channel_id INTEGER NOT NULL,request_id TEXT UNIQUE,reserved_nanos INTEGER NOT NULL,actual_nanos INTEGER,quota INTEGER,quota_per_unit INTEGER,status TEXT NOT NULL,created REAL NOT NULL,settled REAL,day TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS billing_pool_day ON billing(pool_id,day);
                CREATE INDEX IF NOT EXISTS billing_job ON billing(pool_id,job_id);
                CREATE TABLE IF NOT EXISTS billing_reconcile (reservation_id TEXT PRIMARY KEY,attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL,last_attempt REAL,last_reason TEXT,lease_until REAL);
                CREATE INDEX IF NOT EXISTS billing_reconcile_due ON billing_reconcile(next_attempt);
            ''')
    @contextmanager
    def connection(self):
        c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def reserve(self,pool_id,job_id,token_id,channel_id,amount_usd,run_limit_usd,daily_limit_usd=None,now=None):
        identity(pool_id);identity(job_id);positive(token_id);positive(channel_id)
        amount=nanos(amount_usd);run=nanos(run_limit_usd);daily=None if daily_limit_usd is None else nanos(daily_limit_usd)
        now=time.time() if now is None else now;day=utcday(now)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            used=c.execute("SELECT COALESCE(SUM(CASE WHEN status='settled' THEN actual_nanos WHEN status='pending' THEN reserved_nanos ELSE 0 END),0) FROM billing WHERE pool_id=? AND job_id=?",(pool_id,job_id)).fetchone()[0]
            if used+amount>run:return None
            if daily is not None:
                used=c.execute("SELECT COALESCE(SUM(CASE WHEN status='settled' THEN actual_nanos WHEN status='pending' THEN reserved_nanos ELSE 0 END),0) FROM billing WHERE pool_id=? AND (day=? OR status='pending')",(pool_id,day)).fetchone()[0]
                if used+amount>daily:return None
            id='bill_'+uuid.uuid4().hex
            c.execute('INSERT INTO billing VALUES(?,?,?,?,?,NULL,?,NULL,NULL,NULL,?,?,NULL,?)',(id,pool_id,job_id,token_id,channel_id,amount,'pending',now,day))
            return id
    def bind(self,id,request_id):
        identity(id);identity(request_id)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM billing WHERE reservation_id=?',(id,)).fetchone()
            if r is None:raise ValueError('billing_reservation_not_found')
            if r['status']=='cancelled':raise ValueError('billing_reservation_cancelled')
            if r['request_id'] not in (None,request_id):raise ValueError('billing_request_mismatch')
            try:c.execute('UPDATE billing SET request_id=? WHERE reservation_id=?',(request_id,id))
            except sqlite3.IntegrityError:raise ValueError('billing_duplicate_request') from None
    def settle(self,id,request_id,token_id,quota,quota_per_unit,now=None):
        identity(id);identity(request_id);positive(token_id);positive(quota_per_unit)
        if type(quota) is not int or quota<0:raise ValueError('invalid_billing_quota')
        actual=int((Decimal(quota)*1000000000/Decimal(quota_per_unit)).to_integral_value(rounding=ROUND_CEILING))
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT * FROM billing WHERE reservation_id=?',(id,)).fetchone()
            if r is None:raise ValueError('billing_reservation_not_found')
            if r['token_id']!=token_id or r['request_id']!=request_id:raise ValueError('billing_attribution_mismatch')
            if r['status']=='settled':
                if r['quota']!=quota or r['quota_per_unit']!=quota_per_unit:raise ValueError('billing_settlement_changed')
                return dict(r)
            if r['status']!='pending':raise ValueError('billing_reservation_not_pending')
            c.execute("UPDATE billing SET status='settled',actual_nanos=?,quota=?,quota_per_unit=?,settled=? WHERE reservation_id=?",(actual,quota,quota_per_unit,time.time() if now is None else now,id))
            c.execute('DELETE FROM billing_reconcile WHERE reservation_id=?',(id,))
            return dict(c.execute('SELECT * FROM billing WHERE reservation_id=?',(id,)).fetchone())
    def cancel_unsent(self,id):
        identity(id)
        with self.connection() as c:
            return c.execute("UPDATE billing SET status='cancelled',actual_nanos=0,settled=? WHERE reservation_id=? AND request_id IS NULL AND status='pending'",(time.time(),id)).rowcount==1
    def stats(self,pool_id,job_id=None,day=None):
        identity(pool_id);where='pool_id=?';params=[pool_id]
        if job_id is not None:identity(job_id);where+=' AND job_id=?';params.append(job_id)
        if day is not None:
            if not isinstance(day,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):raise ValueError('invalid_billing_day')
            where+=' AND day=?';params.append(day)
        with self.connection() as c:
            r=c.execute("SELECT COALESCE(SUM(CASE WHEN status='settled' THEN actual_nanos ELSE 0 END),0) actual,COALESCE(SUM(CASE WHEN status='pending' THEN reserved_nanos ELSE 0 END),0) pending,COALESCE(SUM(status='pending'),0) pending_count,COALESCE(SUM(status='settled'),0) settled_count,COALESCE(SUM(status='pending' AND request_id IS NULL),0) unidentified FROM billing WHERE "+where,params).fetchone()
            carried=c.execute("SELECT COALESCE(SUM(reserved_nanos),0),COUNT(*),COALESCE(SUM(request_id IS NULL),0) FROM billing WHERE pool_id=? AND status='pending' AND day<>?",(pool_id,day)).fetchone() if day is not None and job_id is None else (0,0,0)
        return {'actual_cost_usd':r['actual']/1e9,'pending_reserved_usd':(r['pending']+carried[0])/1e9,'pending_requests':r['pending_count']+carried[1],'settled_requests':r['settled_count'],'total_requests':r['pending_count']+r['settled_count']+carried[1],'unattributed_requests':r['unidentified']+carried[2],'carried_pending_usd':carried[0]/1e9}
    def defer_reconcile(self,reservation_id,reason='awaiting_consumption',now=None):
        identity(reservation_id)
        if not isinstance(reason,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,100}',reason):reason='awaiting_consumption'
        now=time.time() if now is None else now
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            billing=c.execute("SELECT created,status FROM billing WHERE reservation_id=?",(reservation_id,)).fetchone()
            if billing is None or billing['status']!='pending':return None
            row=c.execute('SELECT attempts,next_attempt,last_attempt FROM billing_reconcile WHERE reservation_id=?',(reservation_id,)).fetchone()
            # Foreground settlement and background reconciliation can overlap;
            # only the first result of the same attempt advances backoff.
            if row and row['last_attempt'] is not None and now-row['last_attempt']<10:return row['next_attempt']
            attempts=(row['attempts'] if row else 0)+1
            delay=(60,300,900,3600)[min(attempts-1,3)]
            age=now-billing['created']
            if age>=86400:delay=21600
            elif age>=3600:delay=3600
            due=now+delay
            c.execute('INSERT INTO billing_reconcile VALUES(?,?,?,?,?,NULL) ON CONFLICT(reservation_id) DO UPDATE SET attempts=excluded.attempts,next_attempt=excluded.next_attempt,last_attempt=excluded.last_attempt,last_reason=excluded.last_reason,lease_until=NULL',(reservation_id,attempts,due,now,reason))
            return due

    def due_reconcile(self,limit=20,now=None):
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError('invalid_billing_limit')
        now=time.time() if now is None else now
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            rows=c.execute("SELECT b.* FROM billing b LEFT JOIN billing_reconcile r ON r.reservation_id=b.reservation_id WHERE b.status='pending' AND b.request_id IS NOT NULL AND COALESCE(r.next_attempt,b.created+60)<=? AND (r.lease_until IS NULL OR r.lease_until<=?) ORDER BY COALESCE(r.next_attempt,b.created+60),b.created LIMIT ?",(now,now,limit)).fetchall()
            for row in rows:
                c.execute('INSERT INTO billing_reconcile(reservation_id,next_attempt,lease_until) VALUES(?,?,?) ON CONFLICT(reservation_id) DO UPDATE SET lease_until=excluded.lease_until',(row['reservation_id'],now,now+300))
            return [dict(row) for row in rows]

    def pending_diagnostics(self,pool_id,now=None):
        identity(pool_id);now=time.time() if now is None else now
        with self.connection() as c:
            row=c.execute("SELECT MIN(b.created) oldest,SUM(b.request_id IS NULL) missing,SUM(r.last_reason='error_log_only') errors,MIN(CASE WHEN b.request_id IS NOT NULL THEN COALESCE(r.next_attempt,b.created+60) END) next_attempt FROM billing b LEFT JOIN billing_reconcile r ON r.reservation_id=b.reservation_id WHERE b.pool_id=? AND b.status='pending'",(pool_id,)).fetchone()
        return {'oldest_pending_age_s':max(0,now-row['oldest']) if row['oldest'] is not None else None,'missing_request_id':row['missing'] or 0,'error_only_requests':row['errors'] or 0,'next_reconcile_at':row['next_attempt']}

    def pending(self,limit=100):
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError('invalid_billing_limit')
        with self.connection() as c:return [dict(r) for r in c.execute("SELECT * FROM billing WHERE status='pending' ORDER BY request_id IS NULL,created LIMIT ?",(limit,))]
