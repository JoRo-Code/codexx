"""Regression coverage for paginated forks crossing account boundaries."""
import json
import unittest
import uuid
from unittest.mock import patch
from test_launcher import LauncherTests, app
from test_auto import AutoTests

class LineageTests(unittest.TestCase):
    def setUp(self):
        LauncherTests.setUp(self)
        projection = patch.object(app, 'copy_ancestor_projection')
        projection.start()
        self.addCleanup(projection.stop)
    quiet_move = LauncherTests.quiet_move

    def fork(self, parent, sid=None):
        sid = sid or str(uuid.uuid4())
        path = parent.with_name('rollout-' + sid + '.jsonl')
        meta = json.loads(parent.read_text().splitlines()[0])['payload']
        path.write_text(json.dumps({'type':'session_meta', 'payload':{
            'id':sid, 'cwd':self.tmp.name, 'history_mode':'paginated',
            'history_base':{'thread_id':meta['id'], 'end_byte_offset':parent.stat().st_size,
                            'end_ordinal_exclusive':2}}})+'\n')
        return sid, path

    def test_nested_fork_moves_and_resumes_round_trip_with_ancestors(self):
        first, first_path = self.fork(self.path)
        child, child_path = self.fork(first_path)
        self.quiet_move(app.choose_session(child), 'work')
        dependencies = app.account_home('work') / 'sessions/lineage'
        self.assertEqual((dependencies / self.path.name).read_bytes(), self.path.read_bytes())
        self.assertEqual((dependencies / first_path.name).read_bytes(), first_path.read_bytes())
        self.assertEqual(app.choose_session(self.sid)['account'], 'personal')
        self.assertEqual(app.choose_session(first)['account'], 'personal')
        self.assertEqual([r['id'] for r in app.session_records('work')], [child])
        self.quiet_move(app.choose_session(child), 'personal')
        self.assertEqual(app.choose_session(child)['account'], 'personal')

    def test_ancestor_can_be_moved_after_its_fork(self):
        child, _ = self.fork(self.path)
        self.quiet_move(app.choose_session(child), 'work')
        self.quiet_move(app.choose_session(self.sid), 'work')
        self.assertEqual(app.choose_session(self.sid)['account'], 'work')
        self.assertEqual({r['id'] for r in app.session_records('work')}, {child, self.sid})

    def test_missing_ancestor_does_not_change_owner(self):
        child, _ = self.fork(self.path)
        self.path.unlink()
        with self.assertRaisesRegex(app.Error, 'Missing.*source rollout'):
            self.quiet_move(app.choose_session(child), 'work')
        self.assertEqual(app.choose_session(child)['account'], 'personal')

    def test_conflicting_destination_is_preserved(self):
        child, _ = self.fork(self.path)
        destination = app.account_home('work')/'sessions'/self.path.name
        destination.parent.mkdir()
        destination.write_text(self.path.read_text().replace('Fix login page', 'Other history'))
        before = destination.read_bytes()
        with self.assertRaisesRegex(app.Error, 'differs'):
            self.quiet_move(app.choose_session(child), 'work')
        self.assertEqual(destination.read_bytes(), before)

    def test_active_ancestor_blocks_copy(self):
        child, _ = self.fork(self.path)
        with app.history_writer_lock(app.account_home('personal'), self.sid):
            with self.assertRaisesRegex(app.Error, 'using this conversation'):
                self.quiet_move(app.choose_session(child), 'work')
        self.assertEqual(app.choose_session(child)['account'], 'personal')

    def test_cyclic_lineage_is_rejected(self):
        child, child_path = self.fork(self.path)
        self.path.write_text(json.dumps({'type':'session_meta','payload':{
            'id':self.sid,'history_mode':'paginated','history_base':{
                'thread_id':child, 'end_byte_offset':child_path.stat().st_size}}})+'\n')
        with self.assertRaisesRegex(app.Error, 'Cyclic'):
            self.quiet_move(app.choose_session(child), 'work')

class RecoveryTests(unittest.TestCase):
    setUp = AutoTests.setUp
    make_bridge = AutoTests.make_bridge
    fake_rpc = AutoTests.fake_rpc

    def test_failed_target_resume_restores_source_backend_and_owner(self):
        bridge = self.make_bridge()
        def rpc(method, params):
            if method == 'thread/resume' and bridge.name == 'work':
                raise app.Error('missing source rollout')
            return self.fake_rpc(method, params)
        with patch.object(bridge, 'rpc', side_effect=rpc):
            with self.assertRaisesRegex(app.Error, 'missing source rollout'):
                bridge.reconnect_account('work')
        self.assertEqual(bridge.name, 'personal')
        self.assertEqual(app.choose_session(self.sid)['account'], 'personal')
        self.assertFalse(bridge.backend.closed)
        self.assertEqual(bridge.phase, 'idle')

if __name__ == '__main__': unittest.main()
