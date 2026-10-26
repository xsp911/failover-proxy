"""Passive, bounded capability evidence. Observations are never advertised limits."""
from .platform_io import private_mode
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

ID = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')
MODEL = re.compile(r'[A-Za-z0-9_.:/@+~-]{1,200}\Z')
HEX = re.compile(r'[a-fA-F0-9]{64}\Z')
RETENTION_SECONDS = 30 * 86400
MAX_ROWS = 10000


def _text(value, pattern, field):
    if not isinstance(value, str) or not pattern.fullmatch(value) or '://' in value:
        raise ValueError('invalid_' + field)
    return value


def _number(value, field):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1):
        raise ValueError('invalid_' + field)
    return value


def _timestamp(value):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 253402300799:
        raise ValueError('invalid_now')
    return float(value)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class CapabilityStore:
    def __init__(self, data_dir):
        root = Path(data_dir).absolute()
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError('unsafe_data_dir')
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir():
            raise ValueError('unsafe_data_dir')
        os.chmod(root, 0o700)
        self.path = root / 'capabilities.db'
        self._check_paths()
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('unsafe_database')
            private_mode(fd, 0o600)
        finally:
            os.close(fd)
        with self.connection() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                    request_id TEXT NOT NULL, source TEXT NOT NULL, provider_id TEXT NOT NULL,
                    model TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(request_id, source, provider_id, model));
                CREATE INDEX IF NOT EXISTS observations_ts ON observations(ts);
            ''')

    def _check_paths(self):
        if any(p.is_symlink() for p in (self.path.parent, *self.path.parent.parents)):
            raise ValueError('unsafe_data_dir')
        for path in (self.path, *(Path(str(self.path) + suffix) for suffix in ('-journal', '-wal', '-shm'))):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise ValueError('unsafe_database')

    @contextmanager
    def connection(self):
        self._check_paths()
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _prune(c, now):
        c.execute('DELETE FROM observations WHERE ts < ?', (now - RETENTION_SECONDS,))
        c.execute('DELETE FROM observations WHERE id IN (SELECT id FROM observations ORDER BY ts DESC, id DESC LIMIT -1 OFFSET ?)', (MAX_ROWS,))

    def record(self, *, provider_id, model, protocol, request_id, requested_output_tokens=None,
               observed_output_tokens=None, reasoning_tokens=None, http_status=None, finish_reason=None,
               outcome='success', source='gateway', error_kind=None, context=None, routing_fingerprint=None, now=None):
        ts = _timestamp(now)
        row = {'provider_id': _text(provider_id, ID, 'provider_id'), 'model': _text(model, MODEL, 'model'),
               'protocol': _text(protocol, ID, 'protocol'), 'request_id': _text(request_id, ID, 'request_id')}
        if source not in ('gateway', 'fingerprint', 'pelican') or outcome not in ('success', 'error', 'cancelled'):
            raise ValueError('invalid_source_or_outcome')
        if http_status is not None and (isinstance(http_status, bool) or not isinstance(http_status, int) or not 100 <= http_status <= 599):
            raise ValueError('invalid_http_status')
        for key, value in (('requested_output_tokens', requested_output_tokens), ('observed_output_tokens', observed_output_tokens), ('reasoning_tokens', reasoning_tokens)):
            row[key] = _number(value, key)
        for key, value in (('finish_reason', finish_reason), ('error_kind', error_kind)):
            row[key] = None if value is None else _text(value, ID, key)
        if routing_fingerprint is not None:
            routing_fingerprint = _text(routing_fingerprint, HEX, 'routing_fingerprint').lower()
        clean_context = {}
        if context is not None:
            allowed = {'estimated_input_tokens', 'estimated_total_tokens', 'context_length', 'method', 'over_limit', 'reliability'}
            if not isinstance(context, dict) or set(context) - allowed:
                raise ValueError('invalid_context')
            for key, value in context.items():
                if key in ('estimated_input_tokens', 'estimated_total_tokens', 'context_length'):
                    clean_context[key] = _number(value, key)
                elif key == 'over_limit':
                    if not isinstance(value, bool):
                        raise ValueError('invalid_over_limit')
                    clean_context[key] = value
                elif key == 'reliability':
                    if value != 'heuristic':
                        raise ValueError('invalid_reliability')
                    clean_context[key] = value
                else:
                    clean_context[key] = _text(value, ID, 'method')
        row.update(source=source, outcome=outcome, http_status=http_status, context=clean_context,
                   routing_fingerprint=routing_fingerprint, parameter_accepted=requested_output_tokens is not None and http_status == 200,
                   observed_at=_iso(ts))
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('''INSERT INTO observations(ts,request_id,source,provider_id,model,payload) VALUES(?,?,?,?,?,?)
                ON CONFLICT(request_id,source,provider_id,model) DO UPDATE SET ts=excluded.ts,payload=excluded.payload''',
                (ts, request_id, source, provider_id, model, json.dumps(row, separators=(',', ':'))))
            record = c.execute('SELECT id FROM observations WHERE request_id=? AND source=? AND provider_id=? AND model=?', (request_id, source, provider_id, model)).fetchone()
            self._prune(c, ts)
            return {'id': record['id'], **row}

    def observations(self, limit=100, provider_id=None, *, now=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('invalid_limit')
        if provider_id is not None:
            _text(provider_id, ID, 'provider_id')
        with self.connection() as c:
            self._prune(c, _timestamp(now))
            query = 'SELECT id,payload FROM observations' + (' WHERE provider_id=?' if provider_id is not None else '') + ' ORDER BY ts DESC,id DESC LIMIT ?'
            params = (provider_id, limit) if provider_id is not None else (limit,)
            return [{'id': r['id'], **json.loads(r['payload'])} for r in c.execute(query, params)]

    def summary(self, provider_id=None, *, now=None):
        if provider_id is not None:
            _text(provider_id, ID, 'provider_id')
        with self.connection() as c:
            self._prune(c, _timestamp(now))
            records = c.execute('SELECT payload FROM observations' + (' WHERE provider_id=?' if provider_id is not None else '') + ' ORDER BY ts DESC,id DESC', (provider_id,) if provider_id is not None else ()).fetchall()
        groups = {}
        fields = ('provider_id', 'model', 'protocol', 'source', 'routing_fingerprint')
        for record in records:
            row = json.loads(record['payload']); key = tuple(row[field] for field in fields)
            group = groups.setdefault(key, {**dict(zip(fields, key)), 'status_counts': {}, 'max_requested_accepted': None, 'max_observed_output': None, 'last_at': row['observed_at'], 'count': 0})
            status = str(row['http_status']) if row['http_status'] is not None else row['outcome']
            group['status_counts'][status] = group['status_counts'].get(status, 0) + 1
            group['count'] += 1
            for target, value in (('max_requested_accepted', row['requested_output_tokens'] if row['parameter_accepted'] else None), ('max_observed_output', row['observed_output_tokens'])):
                if value is not None:
                    group[target] = value if group[target] is None else max(group[target], value)
        return list(groups.values())

    aggregate = summary

    def close(self):
        """Connections are scoped to each operation; retained for lifecycle callers."""
        pass


def routing_fingerprint(value):
    """Hash a caller's routing identity without retaining its raw configuration."""
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
