"""Private, revisioned A6 pool policies; bounded, secret-free operational data."""
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
import uuid

_SAFE = re.compile(r'[A-Za-z0-9_.:/@+\-]{1,200}\Z')
_TOKEN = re.compile(r'[A-Za-z0-9_.:\-]{1,160}\Z')
_HEX = re.compile(r'[a-f0-9]{64}\Z')
_DEFAULT_RUNTIME = {'status': 'draft', 'last_discovered_at': None, 'last_sync_at': None,
                    'last_error': None, 'candidates': [], 'members': [], 'discovery_summary': None}


def _integer(value, field, low=1, high=2**63-1):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError('invalid_' + field)
    return value


def _number(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('invalid_' + field)
    try:
        valid = math.isfinite(value) and low <= value <= high
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError('invalid_' + field)
    return value


def _decimal(value, field, low=0, high=1000):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError('invalid_' + field)
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not Decimal(str(low)) <= number <= Decimal(str(high)):
            raise ValueError('invalid_' + field)
        # Trailing zeroes do not increase monetary precision.
        if number != number.quantize(Decimal('0.000001')):
            raise ValueError('invalid_' + field)
        return float(number)
    except (InvalidOperation, OverflowError):
        raise ValueError('invalid_' + field) from None


def _text(value, field, maximum=200, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value)
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('invalid_' + field)
    return value


def _model(value, field='model'):
    value = _text(value, field)
    if not _SAFE.fullmatch(value):
        raise ValueError('invalid_' + field)
    return value


def _token(value, field):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError('invalid_' + field)
    return value


def _timestamp(value, field):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid_' + field)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        raise ValueError('invalid_' + field) from None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _policy(raw):
    if not isinstance(raw, dict):
        raise ValueError('invalid_pool_policy')
    # A policy write is an allowlist extraction: credentials and runtime fields
    # supplied by clients never reach storage.
    name = _text(raw.get('name'), 'name', 100)
    if not name.strip():
        raise ValueError('invalid_name')
    if 'test_token_ids' in raw:
        test_token_ids = raw['test_token_ids']
        if not isinstance(test_token_ids, list) or not 1 <= len(test_token_ids) <= 4:
            raise ValueError('invalid_test_token_ids')
        test_token_ids = [_integer(value, 'test_token_ids') for value in test_token_ids]
        if len(set(test_token_ids)) != len(test_token_ids):
            raise ValueError('invalid_test_token_ids')
        if ('test_token_id' in raw
                and _integer(raw['test_token_id'], 'test_token_id') != test_token_ids[0]):
            raise ValueError('conflicting_test_token_id')
    else:
        test_token_ids = [_integer(raw.get('test_token_id'), 'test_token_id')]
    out = {'name': name,
           'token_id': _integer(raw.get('token_id'), 'token_id'),
           'test_token_id': test_token_ids[0],
           'test_token_ids': test_token_ids,
           'model': _model(raw.get('model')),
           'reference_model': _model(raw.get('reference_model'), 'reference_model')}
    if out['token_id'] in test_token_ids:
        raise ValueError('separate_test_token_required')

    protocol = raw.get('protocol', 'chat')
    if protocol not in ('chat', 'responses'):
        raise ValueError('invalid_protocol')
    out['protocol'] = protocol
    for key in ('max_input_usd', 'max_output_usd', 'max_cache_read_usd', 'max_cache_write_usd'):
        value=raw.get(key)
        out[key] = None if value is None or value=='' else _decimal(value,key)
    for key, default, low in [('min_cache_hit_percent', 40, 0), ('min_success_percent', 90, 0), ('threshold_percent', 90, 50)]:
        out[key] = _number(raw.get(key, default), key, low, 100)
    if 'ttl_hours' in raw and 'verification_ttl_hours' in raw and raw['ttl_hours'] != raw['verification_ttl_hours']:
        raise ValueError('conflicting_ttl_hours')
    out['verification_ttl_hours'] = _integer(raw.get('verification_ttl_hours', raw.get('ttl_hours', 24)), 'verification_ttl_hours', 1, 168)
    for key, default, low, high in [('refresh_minutes', 10, 5, 1440), ('max_candidates', 10, 1, 100),
                                  ('max_members', 3, 1, 20), ('max_probe_requests', 60, 3, 1800)]:
        out[key] = _integer(raw.get(key, default), key, low, high)
    out['max_probe_usd'] = _decimal(raw.get('max_probe_usd', 1), 'max_probe_usd', .001, 100)
    out['auto_daily_usd'] = _decimal(raw.get('auto_daily_usd', 5), 'auto_daily_usd', .001, 100)
    out['auto_daily_requests'] = _integer(raw.get('auto_daily_requests', 600), 'auto_daily_requests', 3, 10000)
    for key in ('enabled', 'auto_publish'):
        value = raw.get(key, False)
        if not isinstance(value, bool):
            raise ValueError('invalid_' + key)
        out[key] = value
    if out['auto_publish'] and not out['enabled']:
        raise ValueError('auto_pool_must_be_enabled')
    return out


def _candidate(raw):
    if not isinstance(raw, dict):
        raise ValueError('invalid_candidate')
    out = {}
    for key in ('channel_id', 'listing_id', 'actual_channel_id'):
        if key in raw:
            out[key] = None if raw[key] is None else _integer(raw[key], key)
    for key in ('input_micros', 'output_micros', 'cache_read_micros', 'cache_write_micros', 'sample_count'):
        if key in raw:
            out[key] = None if raw[key] is None else _integer(raw[key], key, 0)
    for key in ('cache_hit_percent', 'success_percent', 'probability', 'predicted_probability', 'duration_ms'):
        if key in raw:
            limit = 1 if key in ('probability','predicted_probability') else (3600000 if key == 'duration_ms' else 100)
            out[key] = None if raw[key] is None else _number(raw[key], key, 0, limit)
    if 'supplier' in raw:
        out['supplier'] = _text(raw['supplier'], 'supplier', 100, empty=True)
    if 'model_name' in raw:
        out['model_name'] = _model(raw['model_name'], 'model_name')
    for key in ('charge_type', 'pricing_currency', 'pricing_unit', 'cache_hit_state', 'verification_status', 'reason', 'test_endpoint', 'reference_version', 'predicted_model', 'last_check_status', 'last_check_reason'):
        if key in raw:
            out[key] = None if raw[key] is None else _model(raw[key], key)
    for key in ('verified_at','last_checked_at','next_check_at'):
        if key in raw:
            out[key] = _timestamp(raw[key], key)
    if 'eligible' in raw:
        if not isinstance(raw['eligible'], bool):
            raise ValueError('invalid_eligible')
        out['eligible'] = raw['eligible']
    if 'configuration_key' in raw:
        if not isinstance(raw['configuration_key'], str) or not _HEX.fullmatch(raw['configuration_key']):
            raise ValueError('invalid_configuration_key')
        out['configuration_key'] = raw['configuration_key']
    if 'request_ids' in raw:
        ids = raw['request_ids']
        if not isinstance(ids, list) or len(ids) > 1800:
            raise ValueError('invalid_request_ids')
        out['request_ids'] = [_token(value, 'request_id') for value in ids]
    if 'technical_confirmation' in raw:
        confirmation=raw['technical_confirmation']
        if not isinstance(confirmation,dict) or type(confirmation.get('confirmed')) is not bool:raise ValueError('invalid_technical_confirmation')
        out['technical_confirmation']={'confirmed':confirmation['confirmed'],'first_reason':_model(confirmation.get('first_reason'),'first_reason'),'second_status':_model(confirmation.get('second_status'),'second_status')}
    if 'rounds' in raw:
        rounds = raw['rounds']
        if not isinstance(rounds, list) or len(rounds) > 3:
            raise ValueError('invalid_rounds')
        out['rounds'] = []
        for row in rounds:
            if not isinstance(row, dict):
                raise ValueError('invalid_rounds')
            out['rounds'].append({'round': _integer(row.get('round'), 'round', 1, 3),
                                  'probability': _number(row.get('probability'), 'probability', 0, 1)})
    return out


def _runtime(fields):
    if set(fields) - set(_DEFAULT_RUNTIME):
        raise ValueError('invalid_runtime_field')
    out = {}
    for key, value in fields.items():
        if key in ('candidates', 'members'):
            if not isinstance(value, list) or len(value) > (100 if key == 'candidates' else 20):
                raise ValueError('invalid_' + key)
            out[key] = [_candidate(row) for row in value]
        elif key == 'discovery_summary':
            if value is None:out[key]=None
            elif isinstance(value,dict):out[key]={field:_integer(value.get(field),field,0,10000) for field in ('total','eligible','excluded','shown')}
            else:raise ValueError('invalid_discovery_summary')
        elif key in ('last_discovered_at', 'last_sync_at'):
            out[key] = _timestamp(value, key)
        elif key == 'last_error':
            out[key] = None if value is None else _token(value, key)
        else:
            out[key] = _token(value, key)
    return out


class PoolStore:
    def __init__(self, data_dir):
        root = Path(data_dir)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink():
            raise ValueError('unsafe_data_directory')
        root.chmod(0o700)
        self.path = root / 'a6_pools.db'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            private_mode(fd, 0o600)
        finally:
            os.close(fd)
        with self.connection() as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript('''
                CREATE TABLE IF NOT EXISTS pools (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    token_id INTEGER NOT NULL, model TEXT NOT NULL,
                    policy TEXT NOT NULL, runtime TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(token_id, model));
                CREATE TABLE IF NOT EXISTS verifications (
                    pool_id TEXT NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                    channel_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                    configuration_key TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(pool_id, channel_id));
                CREATE TABLE IF NOT EXISTS pool_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, pool_id TEXT NOT NULL,
                    created_at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            c.execute('PRAGMA busy_timeout=10000')
            c.execute('PRAGMA foreign_keys=ON')
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        policy = json.loads(row['policy'])
        # Hydrate legacy rows without rewriting policy or changing revisions.
        policy.setdefault('test_token_ids', [policy['test_token_id']])
        policy['test_token_id'] = policy['test_token_ids'][0]
        policy.setdefault('auto_publish', False)
        policy.setdefault('auto_daily_usd', 5.0)
        policy.setdefault('auto_daily_requests', 600)
        return {**policy, **json.loads(row['runtime']), 'id': row['id'],
                'revision': row['revision'], 'created_at': row['created_at'], 'updated_at': row['updated_at']}

    @staticmethod
    def _require(c, id, expected_revision):
        _integer(expected_revision, 'revision')
        row = c.execute('SELECT * FROM pools WHERE id=?', (id,)).fetchone()
        if row is None or row['revision'] != expected_revision:
            raise ValueError('revision_conflict')
        return row

    def list(self):
        with self.connection() as c:
            return [self._row(row) for row in c.execute('SELECT * FROM pools ORDER BY created_at,id')]

    def get(self, id):
        with self.connection() as c:
            return self._row(c.execute('SELECT * FROM pools WHERE id=?', (id,)).fetchone())

    def save(self, policy, expected_revision=None):
        value = _policy(policy)
        id = policy.get('id')
        if id is not None:
            _token(id, 'pool_id')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            now = _now()
            if id is not None:
                self._require(c, id, expected_revision)
            elif expected_revision is not None:
                raise ValueError('revision_conflict')
            duplicate = c.execute('SELECT id FROM pools WHERE token_id=? AND model=?',
                                  (value['token_id'], value['model'])).fetchone()
            if duplicate and duplicate['id'] != id:
                raise ValueError('duplicate_token_model')
            # Test routing is token-scoped remotely. A dedicated test token may
            # never be shared, nor be any other policy's production token.
            for other in c.execute('SELECT id,policy FROM pools'):
                if other['id'] == id:
                    continue
                other_policy = json.loads(other['policy'])
                other_test_ids = set(other_policy.get('test_token_ids', [other_policy['test_token_id']]))
                if (set(value['test_token_ids']) & (other_test_ids | {other_policy['token_id']})
                        or value['token_id'] in other_test_ids):
                    raise ValueError('test_token_in_use')
            try:
                if id is None:
                    if expected_revision is not None:
                        raise ValueError('revision_conflict')
                    if c.execute('SELECT COUNT(*) FROM pools').fetchone()[0] >= 100:
                        raise ValueError('pool_limit_reached')
                    id = 'pool_' + uuid.uuid4().hex
                    c.execute('INSERT INTO pools VALUES(?,?,?,?,?,?,?,?)',
                              (id, 1, value['token_id'], value['model'], json.dumps(value), json.dumps(_DEFAULT_RUNTIME), now, now))
                else:
                    old = self._require(c, id, expected_revision)
                    runtime = json.loads(old['runtime'])
                    runtime.update(status='draft', candidates=[], last_discovered_at=None, last_error=None,discovery_summary=None)
                    c.execute('UPDATE pools SET revision=revision+1,token_id=?,model=?,policy=?,runtime=?,updated_at=? WHERE id=?',
                              (value['token_id'], value['model'], json.dumps(value), json.dumps(runtime), now, id))
                    c.execute('DELETE FROM verifications WHERE pool_id=?', (id,))
            except sqlite3.IntegrityError:
                raise ValueError('duplicate_token_model') from None
            return self._row(c.execute('SELECT * FROM pools WHERE id=?', (id,)).fetchone())

    def disable_for_delete(self,id,expected_revision):
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row=self._require(c,id,expected_revision)
            policy=json.loads(row['policy']);policy.update(enabled=False,auto_publish=False)
            runtime=json.loads(row['runtime']);runtime.update(status='blocked',last_error='pool_delete_pending')
            c.execute('UPDATE pools SET policy=?,runtime=? WHERE id=?',(json.dumps(policy),json.dumps(runtime),id))

    def delete(self, id, expected_revision, safely_detached=False):
        _integer(expected_revision, 'revision')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM pools WHERE id=?', (id,)).fetchone()
            if row is None:
                return False
            if row['revision'] != expected_revision:
                raise ValueError('revision_conflict')
            runtime = json.loads(row['runtime'])
            if not safely_detached and (runtime.get('last_sync_at') or runtime.get('status') == 'published' or runtime.get('members')):
                raise ValueError('published_pool_requires_explicit_detach')
            c.execute('DELETE FROM pools WHERE id=?', (id,))
            c.execute('DELETE FROM pool_events WHERE pool_id=?', (id,))
            return True

    def update_runtime(self, id, expected_revision, **fields):
        clean = _runtime(fields)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._require(c, id, expected_revision)
            runtime = json.loads(row['runtime'])
            runtime.update(clean)
            c.execute('UPDATE pools SET runtime=? WHERE id=?', (json.dumps(runtime), id))
            return self._row(c.execute('SELECT * FROM pools WHERE id=?', (id,)).fetchone())

    def verification(self, pool_id, channel_id):
        _integer(channel_id, 'channel_id')
        with self.connection() as c:
            row = c.execute('SELECT v.payload FROM verifications v JOIN pools p ON p.id=v.pool_id '
                            'WHERE v.pool_id=? AND v.channel_id=? AND v.revision=p.revision', (pool_id, channel_id)).fetchone()
            return json.loads(row[0]) if row else None

    def verifications(self,pool_id):
        with self.connection() as c:
            return [json.loads(row[0]) for row in c.execute('SELECT v.payload FROM verifications v JOIN pools p ON p.id=v.pool_id WHERE v.pool_id=? AND v.revision=p.revision',(pool_id,))]

    def save_verification(self, pool_id, expected_revision, record):
        clean = _candidate(record)
        channel = _integer(clean.get('channel_id'), 'channel_id')
        key = clean.get('configuration_key')
        if not isinstance(key, str) or not _HEX.fullmatch(key):
            raise ValueError('invalid_configuration_key')
        if clean.get('verification_status') not in ('verified', 'suspect', 'unknown', 'error', 'unverified', 'expired', 'skipped'):
            raise ValueError('invalid_verification_status')
        if clean['verification_status'] in ('verified', 'suspect') and (clean.get('probability') is None or not clean.get('verified_at')):
            raise ValueError('invalid_verification')
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            row = self._require(c, pool_id, expected_revision)
            policy = json.loads(row['policy'])
            if clean.get('model_name') != policy['model']:
                raise ValueError('verification_model_mismatch')
            clean.update(pool_id=pool_id, revision=expected_revision)
            c.execute('INSERT INTO verifications VALUES(?,?,?,?,?) ON CONFLICT(pool_id,channel_id) '
                      'DO UPDATE SET revision=excluded.revision,configuration_key=excluded.configuration_key,payload=excluded.payload',
                      (pool_id, channel, expected_revision, key, json.dumps(clean)))
            return clean

    def append_event(self, id, kind, detail):
        _token(kind, 'event_kind')
        # No arbitrary strings or nested remote responses. Reuse the explicit
        # candidate schema, plus a small operational event vocabulary.
        clean = _candidate(detail)
        for field in ('from_status', 'to_status', 'error_kind'):
            if field in detail:
                clean[field] = None if detail[field] is None else _token(detail[field], field)
        with self.connection() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM pools WHERE id=?', (id,)).fetchone():
                raise ValueError('pool_not_found')
            now = _now()
            cursor = c.execute('INSERT INTO pool_events(pool_id,created_at,kind,detail) VALUES(?,?,?,?)', (id, now, kind, json.dumps(clean)))
            c.execute('DELETE FROM pool_events WHERE id NOT IN (SELECT id FROM pool_events ORDER BY id DESC LIMIT 10000)')
            return {'id': cursor.lastrowid, 'pool_id': id, 'created_at': now, 'kind': kind, 'detail': clean}

    def events(self, pool_id=None, limit=100):
        _integer(limit, 'limit', 1, 1000)
        with self.connection() as c:
            query = 'SELECT * FROM pool_events'
            args = []
            if pool_id is not None:
                query += ' WHERE pool_id=?'
                args.append(pool_id)
            query += ' ORDER BY id DESC LIMIT ?'
            args.append(limit)
            return [{**dict(row), 'detail': json.loads(row['detail'])} for row in c.execute(query, args)]


__all__ = ['PoolStore']
