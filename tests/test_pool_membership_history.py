"""Membership journal invariants, security and independent retention."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from proxy.pool_membership_history import MembershipHistory


class MembershipHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / 'private'
        self.history = MembershipHistory(self.root)
        self.now = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)

    def tearDown(self):
        self.directory.cleanup()

    def record(self, before=None, after=None, **kwargs):
        now = kwargs.pop('now', self.now)
        return self.history.record(
            'pool-1', before=[] if before is None else before,
            after=[1] if after is None else after,
            reason=kwargs.pop('reason', 'published'),
            source=kwargs.pop('source', 'manual'),
            now=now.timestamp(), **kwargs)

    def test_empty_creation_is_noop(self):
        self.assertIsNone(self.record(before=[], after=[]))
        self.assertEqual([], self.history.events('pool-1'))

    def test_add_remove_exact_fields_and_normalized_ids(self):
        event = self.record(before=[3, 1, 1], after=[4, 3, 4],
                            job_id='job-1', subscription_id=42, config_version=7)
        self.assertEqual({
            'id', 'pool_id', 'created_at', 'before', 'after', 'added',
            'removed', 'reason', 'source', 'job_id', 'subscription_id',
            'config_version',
        }, set(event))
        self.assertEqual('pool-1', event['pool_id'])
        self.assertEqual([1, 3], event['before'])
        self.assertEqual([3, 4], event['after'])
        self.assertEqual([4], event['added'])
        self.assertEqual([1], event['removed'])
        self.assertEqual('published', event['reason'])
        self.assertEqual('manual', event['source'])
        self.assertEqual('job-1', event['job_id'])
        self.assertEqual(42, event['subscription_id'])
        self.assertEqual(7, event['config_version'])
        timestamp = datetime.fromisoformat(event['created_at'].replace('Z', '+00:00'))
        self.assertEqual(timedelta(0), timestamp.utcoffset())
        self.assertEqual(self.now, timestamp)
        self.assertEqual(event, self.history.events('pool-1')[0])

    def test_no_duplicate_or_config_only_membership_event(self):
        first = self.record(before=[], after=[2, 1, 2], config_version=1)
        self.assertIsNone(self.record(before=[1, 2], after=[2, 1, 1], config_version=2))
        self.assertEqual([first], self.history.events('pool-1'))

    def test_newest_first_limit_and_pool_isolation(self):
        first = self.record(now=self.now)
        second = self.record(before=[1], after=[1, 2], now=self.now + timedelta(seconds=1))
        third = self.record(before=[1, 2], after=[], now=self.now + timedelta(seconds=2), source='delete')
        self.history.record('pool-2', before=[], after=[8], reason='published', source='system', now=self.now.timestamp())
        self.assertEqual([third, second, first], self.history.events('pool-1'))
        self.assertEqual([third, second], self.history.events('pool-1', limit=2))
        self.assertEqual([], self.history.events('missing-pool'))
        self.assertEqual(1, len(self.history.events('pool-2')))

    def test_default_limit_is_fifty(self):
        recorded = []
        for number in range(60):
            recorded.append(self.record(before=[number + 1], after=[number + 2],
                                        now=self.now + timedelta(seconds=number)))
        self.assertEqual(list(reversed(recorded[-50:])), self.history.events('pool-1'))

    def test_accepts_each_closed_source(self):
        for number, source in enumerate(('manual', 'auto_refresh', 'auto_audit', 'delete', 'system')):
            event = self.record(before=[number + 1], after=[number + 2], source=source)
            self.assertEqual(source, event['source'])
        self.assertEqual(5, len(self.history.events('pool-1')))

    def test_optional_metadata_rejects_messages(self):
        for field, value in (('job_id', 'secret token'), ('subscription_id', True),
                             ('subscription_id', 'https://secret.example'),
                             ('config_version', -1), ('config_version', 'raw secret')):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.record(**{field: value})
        self.assertEqual([], self.history.events('pool-1'))

    def test_input_lists_are_not_mutated(self):
        before, after = [3, 1, 3], [4, 2, 4]
        event = self.record(before=before, after=after)
        self.assertEqual([3, 1, 3], before)
        self.assertEqual([4, 2, 4], after)
        before.append(99)
        after.clear()
        self.assertEqual([1, 3], event['before'])
        self.assertEqual([2, 4], event['after'])

    def test_equal_timestamp_has_deterministic_append_order(self):
        first = self.record(before=[], after=[1])
        second = self.record(before=[1], after=[2])
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual([second, first], self.history.events('pool-1'))

    def test_reopen_preserves_removed_pool_history(self):
        first = self.record()
        removed = self.record(before=[1], after=[], reason='pool_deleted', source='delete')
        reopened = MembershipHistory(self.root)
        self.assertEqual([removed, first], reopened.events('pool-1'))

    def test_private_database_and_wal(self):
        self.assertEqual(0o700, self.root.stat().st_mode & 0o777)
        self.assertEqual(0o600, Path(self.history.path).stat().st_mode & 0o777)
        with sqlite3.connect(self.history.path) as connection:
            self.assertEqual('wal', connection.execute('PRAGMA journal_mode').fetchone()[0])
        for suffix in ('-wal', '-shm'):
            companion = Path(str(self.history.path) + suffix)
            if companion.exists():
                self.assertEqual(0o600, companion.stat().st_mode & 0o777)

    def test_rejects_arbitrary_messages_and_invalid_sources(self):
        for reason in ('', 'x' * 101, 'Bearer secret', 'https://secret.example/token',
                       'failed\nsecret', '../secret', {'token': 'secret'}):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.record(reason=reason)
        for source in ('provider', 'MANUAL', 'manual\nsecret', '', None):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.record(source=source)
        self.assertEqual([], self.history.events('pool-1'))

    def test_rejects_invalid_identifiers_channel_ids_and_limits(self):
        for pool_id in ('', '../pool', 'pool secret', 'x' * 201, None):
            with self.subTest(pool_id=pool_id), self.assertRaises(ValueError):
                self.history.events(pool_id)
        for members in ([True], [0], [-1], ['1'], [2**63], None, '1'):
            with self.subTest(members=members), self.assertRaises(ValueError):
                self.history.record('pool-1', before=members, after=[1],
                                    reason='published', source='manual', now=self.now.timestamp())
        for limit in (0, -1, True, '50', None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.history.events('pool-1', limit=limit)

    def test_rejects_symlink_database_and_directory(self):
        other = Path(self.directory.name) / 'other'
        other.mkdir()
        target = other / 'target.db'
        target.write_bytes(b'untouched')
        linked_root = Path(self.directory.name) / 'linked-directory'
        linked_root.symlink_to(other, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            MembershipHistory(linked_root)
        database_root = Path(self.directory.name) / 'linked-database'
        database_root.mkdir()
        (database_root / Path(self.history.path).name).symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            MembershipHistory(database_root)
        self.assertEqual(b'untouched', target.read_bytes())

    def test_thirty_day_retention_boundary(self):
        old = self.record(now=self.now - timedelta(days=30, seconds=1))
        boundary = self.record(before=[1], after=[2], now=self.now - timedelta(days=30))
        recent = self.record(before=[2], after=[3], now=self.now - timedelta(days=29))
        self.history.prune(now=self.now.timestamp())
        events = self.history.events('pool-1')
        self.assertNotIn(old, events)
        self.assertEqual([recent, boundary], events)
        self.history.prune(now=(self.now + timedelta(days=31)).timestamp())
        self.assertEqual([], self.history.events('pool-1'))


if __name__ == '__main__':
    unittest.main()
