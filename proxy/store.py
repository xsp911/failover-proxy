"""Synchronous SQLite persistence and security for the standalone proxy.

Secrets never enter metrics. Each operation owns its connection; write operations
use SQLite transactions, so one Store can safely be shared by request threads.
"""
from __future__ import annotations

from .platform_io import private_mode
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from cryptography.fernet import Fernet

DEFAULT_SETTINGS = {
    "request_timeout_s": 180,
    "connect_timeout_s": 10,
    "read_timeout_s": 60,
    "cooldown_s": 60,
    "failure_threshold": 1,
    "max_request_bytes": 33554432,
    "max_response_bytes": 16777216,
    "max_buffer_events": 32768,
    "max_concurrent_requests": 8,
    "max_attempts": 3,
    "trust_private_upstreams": False,
}
SESSION_TTL_SECONDS = 12 * 60 * 60
PBKDF2_ITERATIONS = 600_000
METRICS_RETENTION_SECONDS = 30 * 86400
METRICS_MAX_ROWS = 250_000  # Per table; oldest rows evicted in batches.
METRICS_PRUNE_INTERVAL_SECONDS = 3600
METRICS_EVICTION_BATCH = 1000
STATS_PERCENTILE_SAMPLE_LIMIT = 50_000
STATS_PROVIDER_LIMIT = 1000
_PERIODS = {"hour": 3600, "1h": 3600, "6h": 21600, "day": 86400,
            "24h": 86400, "week": 604800, "7d": 604800,
            "month": 2592000, "30d": 2592000, "all": 2592000}
_STATUS = {"success", "error", "cancelled"}
_KIND = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any = None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.timestamp()
    elif isinstance(value, datetime):
        value = value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("invalid_timestamp")
    # Check that conversion for output is valid on this platform, too.
    _iso(result)
    return result


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _number(value: Any, integer: bool = False) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(value)
        if not math.isfinite(n) or n < 0:
            return None
        return int(n) if integer else n
    except (TypeError, ValueError, OverflowError):
        return None


def _text(value: Any, limit: int = 256) -> str | None:
    return str(value)[:limit] if value is not None else None


def _kind(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value if _KIND.fullmatch(value) else "upstream_error"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Store:
    session_ttl_seconds = SESSION_TTL_SECONDS

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.data_dir.is_symlink() or not self.data_dir.is_dir():
            raise ValueError("unsafe_data_directory")
        self.data_dir.chmod(0o700)
        self.db_path = self.data_dir / "proxy.db"
        self.key_path = self.data_dir / "secret.key"
        self._fernet = Fernet(self._load_key())
        fd = os.open(self.db_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("unsafe_database_file")
            private_mode(fd, 0o600)
        finally:
            os.close(fd)
        # Key loading finished (and released its lock) before schema locking.
        # busy_timeout alone does not serialize journal_mode transitions.
        with self._directory_lock(), self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS config (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    username TEXT NOT NULL, salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL, iterations INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
                    csrf_token TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
                CREATE TABLE IF NOT EXISTS api_keys (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    prefix TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, enabled INTEGER NOT NULL,
                    models TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, ts REAL NOT NULL,
                    provider_id TEXT, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
                CREATE INDEX IF NOT EXISTS requests_provider_ts ON requests(provider_id,ts);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, request_id TEXT,
                    ts REAL NOT NULL, provider_id TEXT,
                    ordinal INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS attempts_ts ON attempts(ts);
                CREATE INDEX IF NOT EXISTS attempts_request ON attempts(request_id);
                CREATE INDEX IF NOT EXISTS attempts_provider_ts ON attempts(provider_id,ts);
                CREATE TABLE IF NOT EXISTS metrics_maintenance (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    last_pruned_at REAL NOT NULL,
                    request_rows INTEGER NOT NULL, attempt_rows INTEGER NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS requests_count_insert AFTER INSERT ON requests BEGIN
                    UPDATE metrics_maintenance SET request_rows=request_rows+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS requests_count_delete AFTER DELETE ON requests BEGIN
                    UPDATE metrics_maintenance SET request_rows=request_rows-1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS attempts_count_insert AFTER INSERT ON attempts BEGIN
                    UPDATE metrics_maintenance SET attempt_rows=attempt_rows+1 WHERE singleton=1;
                END;
                CREATE TRIGGER IF NOT EXISTS attempts_count_delete AFTER DELETE ON attempts BEGIN
                    UPDATE metrics_maintenance SET attempt_rows=attempt_rows-1 WHERE singleton=1;
                END;
            """)
            conn.execute("INSERT OR IGNORE INTO config VALUES(1,0,?)", (_json({
                "settings": DEFAULT_SETTINGS.copy(), "providers": [], "models": []}),))
            conn.execute("""INSERT OR IGNORE INTO metrics_maintenance
                SELECT 1,0,(SELECT COUNT(*) FROM requests),(SELECT COUNT(*) FROM attempts)
                WHERE NOT EXISTS(SELECT 1 FROM metrics_maintenance WHERE singleton=1)""")
            self._maintain_metrics(conn)

    @contextmanager
    def _directory_lock(self) -> Iterator[None]:
        from .platform_io import directory_lock
        with directory_lock(self.data_dir):
            yield

    def _load_key(self) -> bytes:
        # Concurrent initializers must never observe the O_EXCL-created key
        # before its contents have been flushed. Never nest directory locks.
        with self._directory_lock():
            return self._read_or_create_key()

    def _read_or_create_key(self) -> bytes:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(self.key_path, flags, 0o600)
        except FileExistsError:
            fd = os.open(self.key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("unsafe_key_file")
                private_mode(fd, 0o600)
                key = os.read(fd, 128).strip()
            finally:
                os.close(fd)
            # Never silently regenerate a damaged key; that would lose secrets.
            Fernet(key)
            return key
        key = Fernet.generate_key()
        try:
            private_mode(fd, 0o600)
            os.write(fd, key + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        return key

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA recursive_triggers=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize_admin(self, username: str, password: str) -> None:
        salt = secrets.token_bytes(32)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
        with self._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO admin VALUES(1,?,?,?,?)", (
                username, base64.b64encode(salt).decode(), digest.hex(), PBKDF2_ITERATIONS))

    def authenticate_admin(self, username: str, password: str) -> bool:
        if not isinstance(username, str) or not isinstance(password, str):
            return False
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM admin WHERE singleton=1").fetchone()
        salt = base64.b64decode(row["salt"]) if row else bytes(32)
        iterations = row["iterations"] if row else PBKDF2_ITERATIONS
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex()
        valid_password = hmac.compare_digest(digest, row["password_hash"] if row else "0" * 64)
        valid_username = hmac.compare_digest(username.encode(), row["username"].encode() if row else b"")
        return bool(row and valid_password and valid_username)

    def update_admin_credentials(self,expected_username,current_password,new_username,new_password,session_cookie):
        if not isinstance(new_username,str) or not 1<=len(new_username.strip())<=100 or any(ord(c)<33 or ord(c)==127 for c in new_username.strip()):raise ValueError('invalid_admin_username')
        username=new_username.strip()
        if not isinstance(current_password,str) or not 1<=len(current_password)<=1024:raise ValueError('invalid_current_password')
        if not isinstance(new_password,str) or len(new_password)>1024 or new_password and (len(new_password)<12 or new_password.isspace()):raise ValueError('invalid_admin_password')
        with self._connection() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM admin WHERE singleton=1').fetchone()
            live=conn.execute('SELECT username FROM sessions WHERE token_hash=? AND expires_at>?',(_hash(session_cookie),time.time())).fetchone()
            if not live or not row or live['username']!=expected_username or row['username']!=expected_username:raise ValueError('admin_login_required')
            digest=hashlib.pbkdf2_hmac('sha256',current_password.encode(),base64.b64decode(row['salt']),row['iterations']).hex()
            if not hmac.compare_digest(digest,row['password_hash']):raise ValueError('invalid_current_password')
            if username==row['username'] and not new_password:raise ValueError('admin_credentials_unchanged')
            if new_password:
                salt=secrets.token_bytes(32)
                password_hash=hashlib.pbkdf2_hmac('sha256',new_password.encode(),salt,PBKDF2_ITERATIONS).hex()
                conn.execute('UPDATE admin SET username=?,salt=?,password_hash=?,iterations=? WHERE singleton=1',(username,base64.b64encode(salt).decode(),password_hash,PBKDF2_ITERATIONS))
            else:conn.execute('UPDATE admin SET username=? WHERE singleton=1',(username,))
            conn.execute('DELETE FROM sessions')

    def create_session(self, username: str) -> tuple[str, str]:
        raw_cookie, csrf_token = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        now = time.time()
        with self._connection() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?)", (
                _hash(raw_cookie), username, csrf_token, now + self.session_ttl_seconds))
        return raw_cookie, csrf_token

    def get_session(self, raw_cookie: str) -> dict | None:
        if not isinstance(raw_cookie, str) or not raw_cookie:
            return None
        with self._connection() as conn:
            row = conn.execute("SELECT username,csrf_token FROM sessions WHERE token_hash=? AND expires_at>?",
                               (_hash(raw_cookie), time.time())).fetchone()
        return dict(row) if row else None

    def delete_session(self, raw_cookie: str) -> None:
        if not isinstance(raw_cookie, str):
            return
        with self._connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(raw_cookie),))

    def _config(self, row: sqlite3.Row, include_secrets: bool = False) -> dict:
        config = json.loads(row["payload"])
        config["revision"] = row["revision"]
        # Console credentials are separate admin settings, never part of the
        # routing config API even in its internal provider-secret view.
        config.pop('_a6_pat_encrypted',None)
        config.pop('a6_user_id',None)
        for provider in config["providers"]:
            encrypted = provider.pop("_api_key_encrypted", None)
            provider.pop("api_key", None)
            provider["has_api_key"] = bool(encrypted)
            if include_secrets:
                provider["api_key"] = self._fernet.decrypt(encrypted.encode()).decode() if encrypted else ""
        return config

    def get_config(self, include_secrets: bool = False) -> dict:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM config WHERE singleton=1").fetchone()
        return self._config(row, include_secrets)

    def save_config(self, config: dict, expected_revision: int) -> dict:
        # Make a JSON copy: never mutate callers' objects or retain shared state.
        incoming = json.loads(_json(config))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM config WHERE singleton=1").fetchone()
            if row["revision"] != expected_revision:
                raise ValueError("revision_conflict")
            old = json.loads(row["payload"])
            old_keys = {p["id"]: p.get("_api_key_encrypted") for p in old["providers"]}
            providers = incoming.get("providers", old["providers"])
            for provider in providers:
                supplied = provider.pop("api_key", None)
                provider.pop("has_api_key", None)
                provider.pop("_api_key_encrypted", None)
                if isinstance(supplied, str) and supplied.strip():
                    provider["_api_key_encrypted"] = self._fernet.encrypt(supplied.strip().encode()).decode()
                elif old_keys.get(provider["id"]):
                    provider["_api_key_encrypted"] = old_keys[provider["id"]]
            payload = {"settings": {**DEFAULT_SETTINGS, **incoming.get("settings", old["settings"])},
                       "providers": providers, "models": incoming.get("models", old["models"])}
            conn.execute("UPDATE config SET revision=?,payload=? WHERE singleton=1",
                         (expected_revision + 1, _json(payload)))
            for private in ('_a6_pat_encrypted','a6_user_id'):
                if private in old:payload[private]=old[private]
            conn.execute('UPDATE config SET payload=? WHERE singleton=1',(_json(payload),))
            saved = conn.execute("SELECT * FROM config WHERE singleton=1").fetchone()
            return self._config(saved)

    @staticmethod
    def _key_metadata(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "name": row["name"], "prefix": row["prefix"],
                "created_at": row["created_at"], "enabled": bool(row["enabled"]),
                "models": json.loads(row["models"]), "requests": row["requests"]}

    def list_keys(self) -> list[dict]:
        with self._connection() as conn:
            return [self._key_metadata(row) for row in conn.execute(
                "SELECT * FROM api_keys ORDER BY created_at,id")]

    def create_key(self, name: str, models: list) -> dict:
        token = "fp-" + secrets.token_urlsafe(48)
        key_id = secrets.token_hex(16)
        with self._connection() as conn:
            conn.execute("INSERT INTO api_keys VALUES(?,?,?,?,?,?,?,0)", (
                key_id, name, token[:11], _hash(token), _iso(time.time()), 1, _json(models)))
            row = conn.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        return {"key": self._key_metadata(row), "token": token}

    def update_key(self, id: str, enabled: bool) -> bool:
        with self._connection() as conn:
            return bool(conn.execute("UPDATE api_keys SET enabled=? WHERE id=?", (int(enabled), id)).rowcount)

    def delete_key(self, id: str) -> bool:
        with self._connection() as conn:
            return bool(conn.execute("DELETE FROM api_keys WHERE id=?", (id,)).rowcount)

    def authenticate_key(self, raw_token: str) -> dict | None:
        if not isinstance(raw_token, str) or not raw_token.startswith("fp-"):
            return None
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM api_keys WHERE token_hash=? AND enabled=1",
                               (_hash(raw_token),)).fetchone()
        return self._key_metadata(row) if row else None

    def increment_key_requests(self, id: str) -> None:
        with self._connection() as conn:
            conn.execute("UPDATE api_keys SET requests=requests+1 WHERE id=?", (id,))

    @staticmethod
    def _usage(record: dict) -> dict:
        return {field: _number(record.get(field), integer=True) for field in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}

    @staticmethod
    def _maintain_metrics(conn: sqlite3.Connection) -> None:
        """Run in the writer's transaction; counters make ordinary writes O(1).

        Expiry runs hourly across all Store instances/processes. Hard row caps
        apply immediately, evicting oldest rows in batches to amortize cleanup.
        SQLite reuses freed pages; no blocking VACUUM runs in a request path.
        """
        now = time.time()
        state = conn.execute("SELECT * FROM metrics_maintenance WHERE singleton=1").fetchone()
        if now - state["last_pruned_at"] >= METRICS_PRUNE_INTERVAL_SECONDS:
            cutoff = now - METRICS_RETENTION_SECONDS
            conn.execute("DELETE FROM requests WHERE ts<?", (cutoff,))
            conn.execute("DELETE FROM attempts WHERE ts<?", (cutoff,))
            conn.execute("UPDATE metrics_maintenance SET last_pruned_at=? WHERE singleton=1", (now,))
            state = conn.execute("SELECT * FROM metrics_maintenance WHERE singleton=1").fetchone()
        for table, field in (("requests", "request_rows"), ("attempts", "attempt_rows")):
            count = state[field]
            if count > METRICS_MAX_ROWS:
                remove = min(count, max(count - METRICS_MAX_ROWS, METRICS_EVICTION_BATCH))
                # Identifiers are internal constants, never user-controlled.
                conn.execute(f"DELETE FROM {table} WHERE rowid IN "
                             f"(SELECT rowid FROM {table} ORDER BY ts,rowid LIMIT ?)", (remove,))

    def record_request(self, record: dict) -> None:
        ts = _timestamp(record.get("time", record.get("ts")))
        status = record.get("status", "error")
        if status not in _STATUS:
            raise ValueError("invalid_status")
        payload = {"id": _text(record.get("id") or secrets.token_hex(16)),
                   "time": _iso(ts), "model": _text(record.get("model")), "status": status,
                   "attempts": _number(record.get("attempts"), integer=True),
                   "provider_id": _text(record.get("provider_id")),
                   "total_ms": _number(record.get("total_ms")),
                   "client_ttft_ms": _number(record.get("client_ttft_ms", record.get("ttft_ms"))),
                   "error_kind": _kind(record.get("error_kind")),
                   "key_id": _text(record.get("key_id")), **self._usage(record)}
        with self._connection() as conn:
            conn.execute("""INSERT INTO requests VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                ts=excluded.ts,provider_id=excluded.provider_id,payload=excluded.payload""", (
                payload["id"], ts, payload["provider_id"], _json(payload)))
            self._maintain_metrics(conn)

    def record_attempt(self, record: dict) -> None:
        ts = _timestamp(record.get("ts", record.get("time")))
        status = record.get("status", "error")
        if status not in _STATUS:
            raise ValueError("invalid_status")
        payload = {"id": _text(record.get("id") or secrets.token_hex(16)),
                   "request_id": _text(record.get("request_id")), "ts": _iso(ts),
                   "provider_id": _text(record.get("provider_id")), "status": status,
                   "http_status": _number(record.get("http_status"), integer=True),
                   "error_kind": _kind(record.get("error_kind")),
                   **{field: _number(record.get(field)) for field in ("latency_ms", "ttft_ms", "generation_ms")},
                   **self._usage(record)}
        ordinal = _number(record.get("ordinal", record.get("attempt", record.get("attempt_number"))), integer=True)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not ordinal:
                existing = conn.execute("SELECT ordinal FROM attempts WHERE id=?", (payload["id"],)).fetchone()
                if existing:
                    ordinal = existing["ordinal"]
                elif payload["request_id"]:
                    ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM attempts WHERE request_id=?",
                                           (payload["request_id"],)).fetchone()[0]
                else:
                    ordinal = 1
            payload["ordinal"] = ordinal
            conn.execute("""INSERT INTO attempts VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                request_id=excluded.request_id,ts=excluded.ts,provider_id=excluded.provider_id,
                ordinal=excluded.ordinal,payload=excluded.payload""", (
                payload["id"], payload["request_id"], ts, payload["provider_id"], ordinal, _json(payload)))
            self._maintain_metrics(conn)

    @staticmethod
    def _aggregate_sql() -> str:
        usage = ",".join(f"COALESCE(SUM(json_extract(payload,'$.{field}')),0) AS {field}"
                         for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        generated = "json_extract(payload,'$.output_tokens') IS NOT NULL AND json_extract(payload,'$.generation_ms')>0"
        return f"""COUNT(*) AS requests,
            COALESCE(SUM(json_extract(payload,'$.status')='success'),0) AS success,
            COALESCE(SUM(json_extract(payload,'$.status')='error'),0) AS errors,
            COALESCE(SUM(json_extract(payload,'$.status')='cancelled'),0) AS cancelled,
            COALESCE(SUM(ordinal>1),0) AS switches, {usage},
            AVG(json_extract(payload,'$.latency_ms')) AS avg_latency_ms,
            AVG(json_extract(payload,'$.ttft_ms')) AS avg_ttft_ms,
            1000.0*SUM(CASE WHEN {generated} THEN json_extract(payload,'$.output_tokens') END)
                /NULLIF(SUM(CASE WHEN {generated} THEN json_extract(payload,'$.generation_ms') END),0) AS avg_tps,
            COUNT(json_extract(payload,'$.latency_ms')) AS latency_count,
            COALESCE(SUM(json_extract(payload,'$.input_tokens') IS NOT NULL
                OR json_extract(payload,'$.output_tokens') IS NOT NULL),0) AS with_usage,
            COUNT(json_extract(payload,'$.ttft_ms')) AS with_ttft,
            COUNT(json_extract(payload,'$.generation_ms')) AS with_generation"""

    @staticmethod
    def _finish_aggregate(row: sqlite3.Row | dict, latencies: list[float]) -> dict:
        result = dict(row)
        for field in ("provider_id", "latency_count", "with_usage", "with_ttft", "with_generation"):
            result.pop(field, None)
        count = result["requests"]
        result["success_rate"] = 100 * result["success"] / count if count else None
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
        latencies.sort()
        for name, fraction in (("p50_ms", 0.5), ("p95_ms", 0.95)):
            if latencies:
                index = (len(latencies) - 1) * fraction
                lo, hi = math.floor(index), math.ceil(index)
                result[name] = latencies[lo] + (latencies[hi] - latencies[lo]) * (index - lo)
            else:
                result[name] = None
        return result

    def stats(self, period: str) -> dict:
        if period not in _PERIODS:
            raise ValueError("invalid_period")
        now = time.time()
        duration = _PERIODS[period]
        since = now - min(duration, 30 * 86400)
        bucket_seconds = 60 if duration <= 3600 else 3600 if duration <= 604800 else 21600
        bounds = (since, now)
        predicate = "ts>=? AND ts<=?"
        with self._connection() as conn:
            # One read snapshot: exact SQL aggregates, only bounded detail rows
            # and latency scalars cross into Python. Large history stays in SQLite.
            conn.execute("BEGIN")
            aggregate_sql = self._aggregate_sql()
            totals = conn.execute(f"SELECT {aggregate_sql} FROM attempts WHERE {predicate}", bounds).fetchone()
            final = conn.execute(f"""SELECT COUNT(*) AS final_requests,
                COALESCE(SUM(json_extract(payload,'$.status')='success'),0) AS final_success,
                100.0*SUM(json_extract(payload,'$.status')='success')/NULLIF(COUNT(*),0) AS final_success_rate,
                COALESCE(SUM(json_extract(payload,'$.status')='error'),0) AS final_errors,
                AVG(json_extract(payload,'$.client_ttft_ms')) AS avg_client_ttft_ms
                FROM requests WHERE {predicate}""", bounds).fetchone()
            grouped = {row["provider_id"]: row for row in conn.execute(
                f"SELECT provider_id,{aggregate_sql} FROM attempts WHERE {predicate} GROUP BY provider_id "
                "ORDER BY requests DESC,provider_id LIMIT ?", (*bounds, STATS_PROVIDER_LIMIT))}
            empty = conn.execute(f"SELECT {aggregate_sql} FROM attempts WHERE 0").fetchone()
            # Most-recent bounded sample, not an unbiased reservoir. Clearly
            # labelled below; totals, averages and timeline remain exact.
            latency_sample = list(conn.execute(f"""SELECT provider_id,
                json_extract(payload,'$.latency_ms') AS latency_ms FROM attempts
                WHERE {predicate} AND json_extract(payload,'$.latency_ms') IS NOT NULL
                ORDER BY ts DESC,rowid DESC LIMIT ?""", (*bounds, STATS_PERCENTILE_SAMPLE_LIMIT)))
            timeline = [{"time": _iso(row["bucket"]), "requests": row["requests"],
                         "errors": row["errors"], "switches": row["switches"]}
                        for row in conn.execute(f"""SELECT CAST(ts/? AS INTEGER)*? AS bucket,
                            COUNT(*) AS requests, SUM(json_extract(payload,'$.status')='error') AS errors,
                            SUM(ordinal>1) AS switches FROM attempts WHERE {predicate}
                            GROUP BY bucket ORDER BY bucket""", (bucket_seconds, bucket_seconds, *bounds))]
            requests = [json.loads(row[0]) for row in conn.execute(
                f"SELECT payload FROM requests WHERE {predicate} ORDER BY ts DESC,id DESC LIMIT 30", bounds)]
            error_rows = [json.loads(row[0]) for row in conn.execute(
                f"SELECT payload FROM attempts WHERE {predicate} AND json_extract(payload,'$.status')='error' "
                "ORDER BY ts DESC,id DESC LIMIT 30", bounds)]
            config = self._config(conn.execute("SELECT * FROM config WHERE singleton=1").fetchone())
            # Configured providers always have exact metrics even when many
            # deleted historical provider IDs exhaust the display-group limit.
            for provider in config["providers"]:
                if provider["id"] not in grouped:
                    grouped[provider["id"]] = conn.execute(
                        f"SELECT {aggregate_sql} FROM attempts WHERE {predicate} AND provider_id=?",
                        (*bounds, provider["id"])).fetchone()
            provider_count = conn.execute(f"SELECT COUNT(DISTINCT provider_id)+MAX(provider_id IS NULL) "
                                          f"FROM attempts WHERE {predicate}", bounds).fetchone()[0] or 0
        latencies = [row["latency_ms"] for row in latency_sample]
        provider_latencies: dict[str | None, list[float]] = {}
        for row in latency_sample:
            provider_latencies.setdefault(row["provider_id"], []).append(row["latency_ms"])
        summary = self._finish_aggregate(totals, latencies)
        summary.update(dict(final))
        names = {p["id"]: p.get("name", p["id"]) for p in config["providers"]}
        providers = []
        for provider in config["providers"]:
            provider_id = provider["id"]
            metrics = grouped.pop(provider_id, None)
            providers.append({"id": provider_id, "name": provider.get("name", provider_id),
                              "priority": provider.get("priority", 0), "enabled": provider.get("enabled", True),
                              **self._finish_aggregate(metrics if metrics is not None else empty,
                                                       provider_latencies.get(provider_id, []))})
        for provider_id, metrics in grouped.items():
            providers.append({"id": provider_id, "name": provider_id or "Unknown provider", "priority": None,
                              "enabled": False, **self._finish_aggregate(metrics, provider_latencies.get(provider_id, []))})
        errors = [{"time": a["ts"], "kind": a["error_kind"], "status": a["http_status"],
                   "provider": names.get(a["provider_id"], a["provider_id"] or "Unknown provider"),
                   "provider_id": a["provider_id"], "request_id": a["request_id"]} for a in error_rows]
        recent = [{"id": r["id"], "time": r["time"], "model": r["model"], "status": r["status"],
                   "attempts": r["attempts"], "provider": names.get(r["provider_id"], r["provider_id"]),
                   "total_ms": r["total_ms"], "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]}
                  for r in requests]
        sampled = totals["latency_count"] > len(latency_sample)
        coverage = {"attempts": totals["requests"], "with_usage": totals["with_usage"],
                    "with_ttft": totals["with_ttft"], "with_generation": totals["with_generation"],
                    "percentiles_sampled": sampled, "percentile_sample_size": len(latency_sample),
                    "percentile_population": totals["latency_count"], "percentile_sample_limit": STATS_PERCENTILE_SAMPLE_LIMIT,
                    "providers_truncated": provider_count > STATS_PROVIDER_LIMIT,
                    "retention_days": 30, "max_rows_per_table": METRICS_MAX_ROWS,
                    "note": "Учтены только токены, сообщённые провайдером; неизвестные значения остаются null. "
                            "Суммы и средние рассчитаны точно по сохранённым записям. Запросы означают попытки; "
                            "переключения — попытки с порядковым номером больше 1. Успешность указана в процентах. "
                            f"История хранится 30 дней с ежечасной очисткой, не более {METRICS_MAX_ROWS:,} записей "
                            f"в каждой таблице; при заполнении старейшие записи удаляются пакетами по {METRICS_EVICTION_BATCH:,}. "
                            f"Перцентили рассчитаны максимум по {STATS_PERCENTILE_SAMPLE_LIMIT:,} последним известным "
                            "значениям задержки; для провайдеров используется та же выборка. "
                            f"Показано не более {STATS_PROVIDER_LIMIT:,} исторических групп провайдеров."}
        return {"period": period, "since": _iso(since), "until": _iso(now), "summary": summary,
                "providers": providers, "timeline": timeline,
                "recent_errors": errors, "recent_requests": recent, "coverage": coverage}

    def close(self) -> None:
        """Compatibility no-op: connections are closed after every operation."""
        return None
