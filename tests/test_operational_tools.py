"""Operational tools use only temporary stores and generated fixture credentials."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from operational import OperationalError, backup, verify


def fixture_store(tmp_path):
    source = tmp_path / 'data'
    source.mkdir()
    key = Fernet.generate_key()
    secret = 'fixture-only-' + os.urandom(12).hex()
    token = Fernet(key).encrypt(secret.encode()).decode()
    (source / 'secret.key').write_bytes(key + b'\n')
    with sqlite3.connect(source / 'proxy.db') as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE config (payload TEXT)')
        conn.execute('INSERT INTO config VALUES (?)', (json.dumps({'providers': [{'_api_key_encrypted': token}], '_a6_pat_encrypted': token}),))
    with sqlite3.connect(source / 'pool.db') as conn:
        conn.execute('CREATE TABLE example(value TEXT)')
        conn.execute('INSERT INTO example VALUES (?)', ('fixture-value',))
    return source, key, secret


def test_actual_store_backup_restores_provider_secret(tmp_path):
    from proxy.store import Store
    source = tmp_path / 'actual-store'
    store = Store(source)
    fixture_secret = 'temporary-provider-' + os.urandom(12).hex()
    config = store.get_config()
    config['providers'] = [{'id': 'fixture', 'name': 'Fixture', 'priority': 1, 'enabled': True,
                            'api_key': fixture_secret, 'base_url': 'https://example.invalid/v1'}]
    store.save_config(config, config['revision'])
    target = tmp_path / 'actual-snapshot'
    backup(str(source), str(target))
    assert verify(str(target))['encrypted_values_verified'] == 1
    restored = tmp_path / 'restored-actual-store'
    shutil.copytree(target, restored)
    assert Store(restored).get_config(True)['providers'][0]['api_key'] == fixture_secret
    assert Store(source).get_config(True)['providers'][0]['api_key'] == fixture_secret


def test_online_backup_verify_restore_temp(tmp_path):
    source, key, secret = fixture_store(tmp_path)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.suffix == '.db' or p.name == 'secret.key'}
    # Keep an active WAL writer: backup must include committed content, not copy the main db file.
    with sqlite3.connect(source / 'pool.db') as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('INSERT INTO example VALUES (?)', ('committed-in-wal',))
        writer.commit()
        result = backup(str(source), str(tmp_path / 'snapshot'))
        assert result == {'ok': True, 'databases': 2, 'key_included': True}
    target = tmp_path / 'snapshot'
    result = verify(str(target))
    assert result['encrypted_values_verified'] == 2
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())
    assert hashlib.sha256((source / 'proxy.db').read_bytes()).hexdigest() == before['proxy.db']
    assert hashlib.sha256((source / 'secret.key').read_bytes()).hexdigest() == before['secret.key']
    restored = tmp_path / 'restored-test-only'
    shutil.copytree(target, restored)
    with sqlite3.connect(restored / 'proxy.db') as conn:
        payload = json.loads(conn.execute('SELECT payload FROM config').fetchone()[0])
    assert Fernet((restored / 'secret.key').read_bytes().strip()).decrypt(payload['_a6_pat_encrypted'].encode()).decode() == secret
    with sqlite3.connect(restored / 'pool.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM example').fetchone()[0] == 2
    assert key.decode() not in json.dumps(json.loads((target / 'manifest.json').read_text()))


@pytest.mark.parametrize('inside', [True, False])
def test_reject_overwrite_or_nested_output(tmp_path, inside):
    source, _, _ = fixture_store(tmp_path)
    target = source / 'nested' if inside else tmp_path / 'existing'
    if not inside:
        target.mkdir()
        (target / 'sentinel').write_text('keep')
    with pytest.raises(OperationalError):
        backup(str(source), str(target))
    if not inside:
        assert (target / 'sentinel').read_text() == 'keep'


def test_reject_source_symlink(tmp_path):
    source, _, _ = fixture_store(tmp_path)
    (source / 'alias.db').symlink_to(source / 'proxy.db')
    with pytest.raises(OperationalError, match='symlink_rejected'):
        backup(str(source), str(tmp_path / 'snapshot'))
    assert not (tmp_path / 'snapshot').exists()


def test_checksum_and_wrong_key_rejected(tmp_path):
    source, _, _ = fixture_store(tmp_path)
    target = tmp_path / 'snapshot'
    backup(str(source), str(target))
    (target / 'secret.key').write_bytes(Fernet.generate_key())
    with pytest.raises(OperationalError, match='checksum_mismatch'):
        verify(str(target))
    manifest = json.loads((target / 'manifest.json').read_text())
    for record in manifest['files']:
        if record['name'] == 'secret.key':
            record.update(size=(target / 'secret.key').stat().st_size, sha256=hashlib.sha256((target / 'secret.key').read_bytes()).hexdigest())
    (target / 'manifest.json').write_text(json.dumps(manifest))
    from cryptography.fernet import InvalidToken
    with pytest.raises(InvalidToken):
        verify(str(target))


def test_missing_key_cannot_verify_encrypted_config(tmp_path):
    source, _, _ = fixture_store(tmp_path)
    # Rename within the test fixture only: allowlist must ignore unrelated files.
    (source / 'secret.key').rename(source / 'fixture-unselected.key')
    target = tmp_path / 'snapshot'
    assert backup(str(source), str(target))['key_included'] is False
    with pytest.raises(OperationalError, match='encrypted_config_requires_key'):
        verify(str(target))
    assert not (target / 'fixture-unselected.key').exists()


def test_reject_symlinked_output_parent(tmp_path):
    source, _, _ = fixture_store(tmp_path)
    (tmp_path / 'destination').mkdir()
    (tmp_path / 'alias').symlink_to(tmp_path / 'destination', target_is_directory=True)
    with pytest.raises(OperationalError, match='symlink_rejected'):
        backup(str(source), str(tmp_path / 'alias' / 'snapshot'))
    assert not (tmp_path / 'destination' / 'snapshot').exists()


def test_cli_safe_errors_and_absolute_paths(tmp_path):
    source, key, secret = fixture_store(tmp_path)
    target = tmp_path / 'snapshot'
    command = [sys.executable, str(ROOT / 'tools' / 'backup.py'), '--data-dir', str(source), '--output-dir', str(target)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0
    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode == 1
    checked = subprocess.run([sys.executable, str(ROOT / 'tools' / 'verify-backup.py'), '--backup-dir', str(target)], capture_output=True, text=True)
    assert checked.returncode == 0
    relative = subprocess.run([sys.executable, str(ROOT / 'tools' / 'backup.py'), '--data-dir', 'relative', '--output-dir', str(target)], capture_output=True, text=True)
    assert relative.returncode == 1
    for output in (first, second, checked, relative):
        assert secret not in output.stdout + output.stderr
        assert key.decode() not in output.stdout + output.stderr


def test_verify_rejects_traversal_and_public_files(tmp_path):
    source, _, _ = fixture_store(tmp_path)
    target = tmp_path / 'snapshot'
    backup(str(source), str(target))
    (target / 'proxy.db').chmod(0o644)
    with pytest.raises(OperationalError, match='not_private'):
        verify(str(target))
    (target / 'proxy.db').chmod(0o600)
    manifest = json.loads((target / 'manifest.json').read_text())
    manifest['files'][0]['name'] = '../outside.db'
    (target / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(OperationalError, match='invalid_manifest_names'):
        verify(str(target))


def test_integrity_checked_even_with_matching_checksum(tmp_path):
    source, _, _ = fixture_store(tmp_path)
    target = tmp_path / 'snapshot'
    backup(str(source), str(target))
    (target / 'pool.db').write_bytes(b'not SQLite')
    manifest = json.loads((target / 'manifest.json').read_text())
    for record in manifest['files']:
        if record['name'] == 'pool.db':
            record.update(size=10, sha256=hashlib.sha256(b'not SQLite').hexdigest())
    (target / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(sqlite3.DatabaseError):
        verify(str(target))


def test_release_report_does_not_import_application():
    result = subprocess.run([sys.executable, str(ROOT / 'tools' / 'release-report.py'), '--test-result', 'not-run'], capture_output=True, text=True)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report['version'] and len(report['git_sha']) == 40
    assert isinstance(report['dirty'], bool)
    assert report['test_result'] == 'not-run'


def test_secret_scanner_synthetic_and_nonsecret_hash(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    source = tmp_path / 'source.py'
    source.write_text('digest = "' + 'a' * 64 + '"\n')
    command = [sys.executable, str(ROOT / 'tools' / 'check-secrets.py'), '--root', str(tmp_path)]
    assert subprocess.run(command, capture_output=True).returncode == 0
    synthetic = 'ghp_' + 'A1b2C3d4' * 5
    source.write_text('TOKEN = "' + synthetic + '"\n')
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert synthetic not in result.stdout + result.stderr
    source.write_text('TOKEN = "' + synthetic + '"  # secret-check: allow synthetic\n')
    assert subprocess.run(command, capture_output=True).returncode == 0
    (tmp_path / '.env').write_text('TOKEN=' + synthetic)
    assert subprocess.run(command, capture_output=True).returncode == 0


def test_secret_scanner_checks_tracked_ignored_files(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'tracked.py').write_text('value = 1\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'tracked.py'], check=True)
    (tmp_path / 'tracked.py').write_text('token = "ghp_' + 'B2c3D4e5' * 5 + '"\n')
    (tmp_path / '.gitignore').write_text('tracked.py\n')
    result = subprocess.run([sys.executable, str(ROOT / 'tools' / 'check-secrets.py'), '--root', str(tmp_path)], capture_output=True)
    assert result.returncode == 1
