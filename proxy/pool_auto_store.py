"""Private durable state for explicitly opted-in automatic pool maintenance.

Schedules, leases, exact micro-dollar reservations, consecutive seller failures,
and remote route ownership survive restarts. No response bodies or credentials.
"""
from __future__ import annotations

from .platform_io import private_mode
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from .pool_store import _integer, _token

_INTERVAL = {'refresh': 6 * 3600, 'audit': 3600}
_LEASE_SECONDS = 7200
_REASON = re.compile(r'[a-z][a-z0-9_]{0,119}\Z')
_SELLER_REASONS = frozenset({
    'upstream_sse_error', 'invalid_finish_reason', 'probe_insufficient_numbers', 'content_after_finish',
    'upstream_timeout', 'upstream_connection', 'upstream_error',
    'invalid_upstream_response', 'invalid_upstream_json', 'invalid_upstream_encoding',
    'incomplete_upstream_stream', 'incomplete_upstream_response',
    'compressed_upstream_response', 'probe_upstream_response_failed',
    'probe_incomplete_stream', 'probe_incomplete_finish',
})
_CAPS = ('input_price_micros', 'output_price_micros', 'cache_read_price_micros',
         'cache_write_price_micros', 'per_request_price_micros')


def _epoch(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 253402300799):
        raise ValueError('invalid_now')
    return value


def _micros(value, field, minimum=0, maximum=100):
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ValueError('invalid_' + field)
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or not Decimal(str(minimum)) <= number <= Decimal(str(maximum))
                or number != number.quantize(Decimal('0.000001'))):
            raise ValueError('invalid_' + field)
        return int(number * 1000000)
    except (InvalidOperation, OverflowError):
        raise ValueError('invalid_' + field) from None


def _safe_reason(value, fallback='auto_operation_failed'):
    return value if isinstance(value, str) and _REASON.fullmatch(value) else fallback


def _seller_failure(reason):
    return reason in _SELLER_REASONS or bool(re.fullmatch(r'(?:targeted_probe|upstream)_http_[45][0-9]{2}', reason))


def _draft(raw):
    """Extract only route configuration fields; never persist arbitrary JSON."""
    if not isinstance(raw, dict):
        raise ValueError('invalid_remote_draft')
    result = {}
    if 'channels' in raw:
        if not isinstance(raw['channels'], list) or len(raw['channels']) > 20:
            raise ValueError('invalid_remote_channels')
        channels = []
        for row in raw['channels']:
            if not isinstance(row, dict):
                raise ValueError('invalid_remote_channels')
            priority = row.get('priority')
            channels.append({'channel_id': _integer(row.get('channel_id'), 'channel_id'),
                             'priority': None if priority is None else _integer(priority, 'priority', 0, 1000000)})
        if len({row['channel_id'] for row in channels}) != len(channels):
            raise ValueError('invalid_remote_channels')
        result['channels'] = channels
    if 'caps' in raw:
        if not isinstance(raw['caps'], dict):
            raise ValueError('invalid_remote_caps')
        result['caps'] = {key: _integer(value, key, 0) for key, value in raw['caps'].items() if key in _CAPS}
    if 'sort_strategy' in raw:
        if raw['sort_strategy'] not in ('price_asc', 'priority', 'random', 'weighted_random'):
            raise ValueError('invalid_remote_sort_strategy')
        result['sort_strategy'] = raw['sort_strategy']
    if 'user_priorities' in raw:
        priorities = raw['user_priorities']
        if not isinstance(priorities, dict) or len(priorities) > 20:
            raise ValueError('invalid_remote_user_priorities')
        clean = {}
        for key, value in priorities.items():
            if not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]{0,18}', key):
                raise ValueError('invalid_remote_user_priorities')
            clean[key] = _integer(value, 'priority', 0, 1000000)
        result['user_priorities'] = clean
    for key in ('auto_pool_enabled', 'fallback_enabled'):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ValueError('invalid_remote_' + key)
            result[key] = raw[key]
    for key in ('min_ratio', 'max_ratio'):
        if key in raw:
            value = raw[key]
            if value != '' and (not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,6}(?:\.[0-9]{1,6})?', value)):
                raise ValueError('invalid_remote_' + key)
            result[key] = value
    if 'min_cache_hit_rate_bps' in raw:
        value = raw['min_cache_hit_rate_bps']
        result['min_cache_hit_rate_bps'] = None if value is None else _integer(value, 'min_cache_hit_rate_bps', 0, 10000)
    return result


class PoolAutoStore:
    def __init__(self, data_dir):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink():
            raise ValueError('unsafe_data_directory')
        root.chmod(0o700)
        self.path = root / 'a6_pool_auto.db'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            private_mode(fd, 0o600)
        finally:
            os.close(fd)
        with self.connection() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS auto_state (
                    pool_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                    next_refresh_at REAL, next_audit_at REAL,
                    lease_kind TEXT, lease_until REAL,
                    last_refresh_at REAL, last_audit_at REAL,
                    last_finished_at REAL, last_error TEXT,
                    day TEXT, daily_requests INTEGER NOT NULL DEFAULT 0,
                    daily_reserved_micros INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS auto_channels (
                    pool_id TEXT NOT NULL, channel_id INTEGER NOT NULL,
                    failures INTEGER NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT, last_reason TEXT, last_checked_at REAL,
                    PRIMARY KEY(pool_id, channel_id));
                CREATE TABLE IF NOT EXISTS auto_outcomes (
                    pool_id TEXT NOT NULL, channel_id INTEGER NOT NULL, run_id TEXT NOT NULL,
                    PRIMARY KEY(pool_id, channel_id, run_id));
                CREATE TABLE IF NOT EXISTS auto_remote (
                    pool_id TEXT PRIMARY KEY, subscription_id INTEGER,
                    config_version INTEGER, draft TEXT NOT NULL, state TEXT NOT NULL);
            ''')
            columns={row[1] for row in c.execute('PRAGMA table_info(auto_state)')}
            for name in ('refresh_not_before','refresh_reason'):
                if name not in columns:c.execute('ALTER TABLE auto_state ADD COLUMN '+name+(' REAL' if name=='refresh_not_before' else ' TEXT'))

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA busy_timeout=10000')
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _ensure(c, pool_id):
        _token(pool_id, 'pool_id')
        c.execute('INSERT OR IGNORE INTO auto_state(pool_id) VALUES(?)', (pool_id,))

    @staticmethod
    def _state(row):
        if row is None:
            result = {'enabled': False, 'next_refresh_at': None, 'next_audit_at': None,
                      'lease_kind': None, 'lease_until': None, 'last_refresh_at': None,
                      'last_audit_at': None, 'last_finished_at': None, 'last_error': None,
                      'day': None, 'daily_requests': 0, 'daily_reserved_usd': 0.0,'refresh_not_before':None,'refresh_reason':None}
        else:
            result = dict(row)
            result.pop('pool_id')
            result['enabled'] = bool(result['enabled'])
            result['daily_reserved_usd'] = result.pop('daily_reserved_micros') / 1000000
        for kind in _INTERVAL:
            result[kind + '_lease_until'] = result['lease_until'] if result['lease_kind'] == kind else None
        return result

    def state(self, pool_id):
        _token(pool_id, 'pool_id')
        with self.connection() as c:
            return self._state(c.execute('SELECT * FROM auto_state WHERE pool_id=?', (pool_id,)).fetchone())

    def configure(self, pool_id, enabled, now):
        if not isinstance(enabled, bool):
            raise ValueError('invalid_enabled')
        _epoch(now)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self._ensure(c, pool_id)
            row = c.execute('SELECT * FROM auto_state WHERE pool_id=?', (pool_id,)).fetchone()
            if bool(row['enabled']) != enabled:
                c.execute('UPDATE auto_state SET enabled=?,next_refresh_at=?,next_audit_at=?,refresh_not_before=NULL,refresh_reason=NULL WHERE pool_id=?',
                          (int(enabled), now if enabled else None, now + 3600 if enabled else None, pool_id))
            return self._state(c.execute('SELECT * FROM auto_state WHERE pool_id=?', (pool_id,)).fetchone())

    def request_refill(self,pool_id,now,not_before=None,reason='refill_needed'):
        _epoch(now);_token(pool_id,'pool_id');reason=_safe_reason(reason,'refill_needed')
        if not_before is not None:_epoch(not_before)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE');self._ensure(c,pool_id)
            row=c.execute('SELECT * FROM auto_state WHERE pool_id=?',(pool_id,)).fetchone()
            if row['enabled']:
                lower=not_before if not_before is not None else None
                if row['refresh_reason']=='daily_budget' and row['refresh_not_before'] is not None and row['refresh_not_before']>now:
                    lower=max(lower or 0,row['refresh_not_before']);reason='daily_budget'
                due=max(now+60,lower or 0)
                if lower is None and row['next_refresh_at'] is not None:due=min(due,row['next_refresh_at'])
                # An explicit not-before replaces an earlier due time. An audit
                # discovering genuinely new available work may clear old waits;
                # unchanged no-op scheduling never bypasses the claim guard.
                c.execute('UPDATE auto_state SET next_refresh_at=?,refresh_not_before=?,refresh_reason=? WHERE pool_id=?',(due,lower,reason,pool_id))
            return self._state(c.execute('SELECT * FROM auto_state WHERE pool_id=?',(pool_id,)).fetchone())

    def start_manual(self,pool_id,kind,now):
        if kind not in _INTERVAL:raise ValueError('invalid_auto_kind')
        _epoch(now)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE');self._ensure(c,pool_id)
            return c.execute(f'UPDATE auto_state SET lease_kind=?,lease_until=?,next_{kind}_at=? WHERE pool_id=? AND enabled=1 AND (lease_until IS NULL OR lease_until<=?)',(kind,now+_LEASE_SECONDS,now+_INTERVAL[kind],pool_id,now)).rowcount==1

    def claim_due(self, pool_id, kind, now):
        _token(pool_id, 'pool_id')
        if kind not in _INTERVAL:
            raise ValueError('invalid_auto_kind')
        _epoch(now)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if kind=='refresh':
                row=c.execute('SELECT refresh_not_before FROM auto_state WHERE pool_id=?',(pool_id,)).fetchone()
                if row and row['refresh_not_before'] is not None and row['refresh_not_before']>now:return False
            # The column comes exclusively from the closed kind allowlist above.
            changed = c.execute(f'''UPDATE auto_state SET lease_kind=?,lease_until=?,next_{kind}_at=?
                WHERE pool_id=? AND enabled=1 AND next_{kind}_at<=?
                AND (lease_until IS NULL OR lease_until<=?)''',
                (kind, now + _LEASE_SECONDS, now + _INTERVAL[kind], pool_id, now, now)).rowcount
            return changed == 1

    def finish(self, pool_id, kind, error=None, now=None):
        _token(pool_id, 'pool_id')
        if kind not in _INTERVAL:
            raise ValueError('invalid_auto_kind')
        now = _epoch(time.time() if now is None else now)
        error = None if error is None else _safe_reason(error)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute(f'''UPDATE auto_state SET lease_kind=NULL,lease_until=NULL,
                last_{kind}_at=?,last_finished_at=?,last_error=? WHERE pool_id=? AND lease_kind=?''',
                      (now, now, error, pool_id, kind))
            return self._state(c.execute('SELECT * FROM auto_state WHERE pool_id=?', (pool_id,)).fetchone())

    def reserve(self, pool_id, maxusd, maxrequests, cost, now):
        limit = _micros(maxusd, 'auto_daily_usd', .001)
        _integer(maxrequests, 'auto_daily_requests', 3, 10000)
        amount = _micros(cost, 'cost')
        _epoch(now)
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            self._ensure(c, pool_id)
            c.execute('''UPDATE auto_state SET day=?,daily_requests=0,daily_reserved_micros=0
                WHERE pool_id=? AND (day IS NULL OR day<>?)''', (day, pool_id, day))
            return c.execute('''UPDATE auto_state SET daily_requests=daily_requests+1,
                daily_reserved_micros=daily_reserved_micros+? WHERE pool_id=?
                AND daily_requests<? AND daily_reserved_micros+?<=?''',
                (amount, pool_id, maxrequests, amount, limit)).rowcount == 1

    def reserve_request(self,pool_id,maxrequests,now):
        _integer(maxrequests,'auto_daily_requests',3,10000)
        _epoch(now)
        day=datetime.fromtimestamp(now,timezone.utc).date().isoformat()
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE');self._ensure(c,pool_id)
            c.execute('UPDATE auto_state SET day=?,daily_requests=0,daily_reserved_micros=0 WHERE pool_id=? AND (day IS NULL OR day<>?)',(day,pool_id,day))
            return c.execute('UPDATE auto_state SET daily_requests=daily_requests+1 WHERE pool_id=? AND daily_requests<?',(pool_id,maxrequests)).rowcount==1

    @staticmethod
    def _record(row):
        result = dict(row)
        result.pop('pool_id')
        result['banned'] = bool(result['banned'])
        return result

    def outcome(self, pool_id, channel_id, status, reason, now, run_id=None):
        _token(pool_id, 'pool_id')
        _integer(channel_id, 'channel_id')
        _epoch(now)
        if status not in ('verified', 'suspect', 'unknown', 'error', 'unverified', 'expired', 'skipped'):
            raise ValueError('invalid_verification_status')
        reason = None if reason is None else _safe_reason(reason, 'unknown_reason')
        if run_id is not None:
            _token(run_id, 'run_id')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO auto_channels(pool_id,channel_id) VALUES(?,?)', (pool_id, channel_id))
            duplicate = run_id is not None and c.execute(
                'INSERT OR IGNORE INTO auto_outcomes VALUES(?,?,?)', (pool_id, channel_id, run_id)).rowcount == 0
            if not duplicate:
                row = c.execute('SELECT * FROM auto_channels WHERE pool_id=? AND channel_id=?', (pool_id, channel_id)).fetchone()
                failures = row['failures']
                if status == 'verified':
                    failures = 0
                elif status == 'suspect' or (status == 'unknown' and _seller_failure(reason or '')):
                    failures += 1
                c.execute('''UPDATE auto_channels SET failures=?,banned=?,last_status=?,last_reason=?,last_checked_at=?
                    WHERE pool_id=? AND channel_id=?''',
                    (failures, int(bool(row['banned']) or failures >= 4), status, reason, now, pool_id, channel_id))
            return self._record(c.execute('SELECT * FROM auto_channels WHERE pool_id=? AND channel_id=?', (pool_id, channel_id)).fetchone())

    def list_records(self, pool_id):
        _token(pool_id, 'pool_id')
        with self.connection() as c:
            return [self._record(row) for row in c.execute('SELECT * FROM auto_channels WHERE pool_id=? ORDER BY channel_id', (pool_id,))]

    def is_banned(self, pool_id, channel_id):
        _token(pool_id, 'pool_id')
        _integer(channel_id, 'channel_id')
        with self.connection() as c:
            row = c.execute('SELECT banned FROM auto_channels WHERE pool_id=? AND channel_id=?', (pool_id, channel_id)).fetchone()
            return bool(row and row['banned'])

    def unban(self, pool_id, channel_id):
        _token(pool_id, 'pool_id')
        _integer(channel_id, 'channel_id')
        with self.connection() as c:
            return c.execute('UPDATE auto_channels SET failures=0,banned=0 WHERE pool_id=? AND channel_id=?', (pool_id, channel_id)).rowcount == 1

    def remote(self, pool_id):
        _token(pool_id, 'pool_id')
        with self.connection() as c:
            row = c.execute('SELECT * FROM auto_remote WHERE pool_id=?', (pool_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result.pop('pool_id')
            result['draft'] = json.loads(result['draft'])
            return result

    def save_remote(self, pool_id, subscription_id, config_version, draft, state):
        _token(pool_id, 'pool_id')
        if subscription_id is not None:
            _integer(subscription_id, 'subscription_id')
        if config_version is not None:
            _integer(config_version, 'config_version', 0)
        _token(state, 'remote_state')
        safe = _draft(draft)
        with self.connection() as c:
            c.execute('''INSERT INTO auto_remote VALUES(?,?,?,?,?) ON CONFLICT(pool_id) DO UPDATE SET
                subscription_id=excluded.subscription_id,config_version=excluded.config_version,
                draft=excluded.draft,state=excluded.state''', (pool_id, subscription_id, config_version, json.dumps(safe), state))
        return {'subscription_id': subscription_id, 'config_version': config_version, 'draft': safe, 'state': state}


# Short alias retained for callers that name the subsystem rather than the pool.
AutoStore = PoolAutoStore
