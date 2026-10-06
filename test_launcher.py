import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

loader = importlib.machinery.SourceFileLoader('launcher', str(Path(__file__).with_name('codex-accounts')))
spec = importlib.util.spec_from_loader(loader.name, loader)
app = importlib.util.module_from_spec(spec)
loader.exec_module(app)

class LauncherTests(unittest.TestCase):
    def setUp(self):
        binary_patch = patch.object(app, "BINARY", sys.executable)
        binary_patch.start()
        self.addCleanup(binary_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app.ROOT = Path(self.tmp.name)
        for name in ['personal', 'work']:
            app.private_dir(app.account_home(name))
            (app.account_home(name) / 'auth.json').write_text('{"test_credentials":true}')
        self.sid = str(uuid.uuid4())
        self.path = app.account_home('personal') / 'sessions/2026/09/29' / f'rollout-2026-09-29T12-00-00-{self.sid}.jsonl'
        self.path.parent.mkdir(parents=True)
        self.rows = [dict(type='session_meta', payload=dict(id=self.sid, cwd=self.tmp.name, history_mode='legacy')),
                     dict(type='event_msg', payload=dict(type='user_message', message='Fix login page'))]
        self.path.write_text(''.join(json.dumps(x) + '\n' for x in self.rows))

    def quiet_move(self, row, target):
        with contextlib.redirect_stdout(io.StringIO()):
            app.move(row, target)

    def test_packaged_runtime_survives_upgrade_and_new_accounts_use_new_release(self):
        source = app.ROOT / 'installed' / 'v1'
        (source / 'bin').mkdir(parents=True)
        (source / 'codex-package.json').write_text(json.dumps({
            'layoutVersion': 1, 'entrypoint': 'bin/codex'}))
        binary = source / 'bin/codex'
        binary.write_text('#!/bin/sh\nread ready\nexec "$(dirname "$0")/codex-code-mode-host"\n')
        binary.chmod(0o700)
        helper = source / 'bin/codex-code-mode-host'
        helper.write_text('#!/bin/sh\necho original-helper\n')
        helper.chmod(0o700)
        (source / 'codex-resources').mkdir()
        (source / 'codex-resources/data').write_text('resource')
        link = app.ROOT / 'codex'
        link.symlink_to(binary)
        with patch.object(app, 'BINARY', str(link)):
            pinned = app.command('app-server')[0]
            self.assertEqual(app.command('resume', self.sid)[0], pinned)
            running = subprocess.Popen([pinned], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(lambda: running.poll() is None and running.kill())
            replacement = source.with_name('v2')
            shutil.copytree(source, replacement)
            (replacement / 'bin/codex-code-mode-host').write_text('#!/bin/sh\necho new-helper\n')
            link.unlink()
            link.symlink_to(replacement / 'bin/codex')
            shutil.rmtree(source)
            self.assertEqual(running.communicate('continue\n', timeout=5)[0].strip(), 'original-helper')
            fresh = app.command('app-server')[0]
            self.assertNotEqual(fresh, pinned)
            for account in ('personal', 'work'):
                result = subprocess.run([fresh], input='continue\n', env=app.environment(account),
                                        text=True, capture_output=True, check=True)
                self.assertEqual(result.stdout.strip(), 'new-helper')
            self.assertEqual((Path(pinned).parent.parent / 'codex-resources/data').read_text(), 'resource')

    def test_failed_runtime_capture_is_not_published(self):
        source = app.ROOT / 'installed'
        (source / 'bin').mkdir(parents=True)
        (source / 'codex-package.json').write_text('{"layoutVersion":1,"entrypoint":"bin/codex"}')
        binary = source / 'bin/codex'
        binary.write_text('#!/bin/sh\n')
        binary.chmod(0o700)
        with patch.object(app, 'BINARY', str(binary)), patch.object(app.shutil, 'copytree', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError): app.command('app-server')
        self.assertEqual([p.name for p in (app.ROOT / 'native-runtimes').iterdir()], ['.lock'])

    def test_environment_isolates_accounts_and_removes_ambient_auth(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY':'secret', 'CODEX_THREAD_ID':'existing', 'CODEX_HOME':'old'}):
            a, b = app.environment('personal'), app.environment('work')
        self.assertNotEqual(a['CODEX_HOME'], b['CODEX_HOME'])
        self.assertNotIn('OPENAI_API_KEY', a)
        self.assertNotIn('CODEX_THREAD_ID', a)
        self.assertEqual(Path(a['CODEX_HOME']), app.account_home('personal'))

    def test_shared_settings_apply_to_manual_remote_and_backend(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app.permissions('yolo')
            app.defaults('test-model', 'high')
        for args in [(), ('resume', self.sid), ('--remote', 'unix:///tmp/test.sock'), ('app-server',)]:
            cmd = app.command(*args)
            self.assertIn('approval_policy="never"', cmd)
            self.assertIn('sandbox_mode="danger-full-access"', cmd)
            self.assertIn('model="test-model"', cmd)
            self.assertIn('model_reasoning_effort="high"', cmd)
        app.private_dir(app.account_home('new-account'))
        self.assertEqual((app.account_home('personal')/'auth.json').read_text(), '{"test_credentials":true}')
        with contextlib.redirect_stdout(io.StringIO()):
            app.permissions('default')
            app.defaults(clear=True)
        self.assertEqual(app.permission_args(), [])
        self.assertEqual(app.model_args(), [])

    def test_shared_settings_are_opt_in_and_invalid_modes_fail(self):
        self.assertEqual(app.permission_args(), [])
        self.assertEqual(app.model_args(), [])
        app.atomic_json(app.ROOT/'permissions.json', {'mode':'invalid'})
        with self.assertRaises(app.Error):
            app.command('app-server')

    def test_remote_resume_omits_unsupported_permission_overrides(self):
        app.atomic_json(app.ROOT/'permissions.json', {'mode':'yolo'})
        cmd = app.command('resume', self.sid, '--remote', 'unix:///tmp/test.sock', include_permissions=False)
        self.assertNotIn('approval_policy="never"', cmd)
        self.assertNotIn('sandbox_mode="danger-full-access"', cmd)
        self.assertIn('approval_policy="never"', app.command('app-server'))

    def test_auto_exit_prints_valid_launcher_resume_for_saved_chat(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app.auto_exit_message(self.sid)
        self.assertIn('codex-accounts auto --resume ' + self.sid, output.getvalue())
        self.assertIn('work is not continuing', output.getvalue())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            app.auto_exit_message('unsaved-session')
        self.assertNotIn('--resume', output.getvalue())
        self.assertIn('No saved chat', output.getvalue())

    def test_model_defaults_keep_explicit_override_and_partial_updates(self):
        with contextlib.redirect_stdout(io.StringIO()):
            app.defaults('test-model', 'high')
            app.defaults(effort='low')
        cmd = app.command('--model', 'override-model')
        self.assertIn('model_reasoning_effort="low"', cmd)
        self.assertEqual(cmd[cmd.index('--model')+1], 'override-model')
        self.assertIn('model="test-model"', cmd)

    def test_move_preserves_full_history_and_roundtrip_avoids_duplicates(self):
        original = self.path.read_bytes()
        self.quiet_move(app.choose_session(self.sid), 'work')
        row = app.choose_session(self.sid)
        self.assertEqual(row['account'], 'work')
        self.assertEqual(Path(row['path']).read_bytes(), original)
        self.assertEqual(self.path.read_bytes(), original)
        with Path(row['path']).open('a') as f:
            f.write(json.dumps({'type':'event_msg','payload':{'type':'agent_message','message':'Done'}})+'\n')
        updated = Path(row['path']).read_bytes()
        self.quiet_move(app.choose_session(self.sid), 'personal')
        self.assertEqual(self.path.read_bytes(), updated)
        self.assertEqual(len(list((app.account_home('personal')/'sessions').rglob('*.jsonl'))), 1)
        self.assertEqual(len(app.session_records()), 1)
        self.assertTrue(list((app.ROOT/'backups').glob('*.jsonl')))

    def test_resume_passes_account_cwd_and_no_daemon(self):
        with patch.object(app.subprocess, 'call', return_value=7) as call, contextlib.redirect_stdout(io.StringIO()):
            result = app.run('personal', app.choose_session(self.sid), prompt='Continue safely')
        self.assertEqual(result, 7)
        args = call.call_args.args[0]
        self.assertIn('--no-daemon', args)
        self.assertEqual(args[1:3], ['resume', self.sid])
        self.assertEqual(args[-2:], ['--', 'Continue safely'])
        self.assertEqual(call.call_args.kwargs['env']['CODEX_HOME'], str(app.account_home('personal')))
        self.assertFalse(list((app.ROOT/'runs').glob('*.json')))

    def test_move_rejects_running_session(self):
        app.atomic_json(app.ROOT/'runs/active.json', dict(pid=os.getpid(), account='personal',session=self.sid))
        with self.assertRaises(app.Error):
            self.quiet_move(app.choose_session(self.sid), 'work')
        self.assertFalse(app.owners())

    def test_move_allows_other_known_conversation_to_continue(self):
        app.atomic_json(app.ROOT/'runs/active.json', dict(pid=os.getpid(), account='personal',session=str(uuid.uuid4())))
        self.quiet_move(app.choose_session(self.sid), 'work')
        self.assertEqual(app.choose_session(self.sid)['account'], 'work')

    def test_unknown_history_move_fails_without_changes(self):
        row = app.choose_session(self.sid)
        row['history_mode']='future-format'
        with self.assertRaises(app.Error):
            self.quiet_move(row, 'work')
        self.assertFalse(list((app.account_home('work')/'sessions').rglob('*.jsonl')))

    def test_paginated_move_invalidates_only_selected_thread_projection(self):
        self.rows[0]['payload']['history_mode'] = 'paginated'
        self.path.write_text(''.join(json.dumps(x)+'\n' for x in self.rows))
        dbpath = app.account_home('work')/'thread_history_1.sqlite'
        with sqlite3.connect(dbpath) as db:
            for table in ('thread_items','thread_turns','thread_history_projection_state','thread_realtime_items'):
                db.execute('CREATE TABLE '+table+' (thread_id TEXT, content TEXT)')
                db.executemany('INSERT INTO '+table+' VALUES (?,?)', [(self.sid,'stale'),('other','keep')])
        self.quiet_move(app.choose_session(self.sid), 'work')
        self.assertEqual(Path(app.choose_session(self.sid)['path']).read_bytes(), self.path.read_bytes())
        with sqlite3.connect(dbpath) as db:
            for table in ('thread_items','thread_turns','thread_history_projection_state','thread_realtime_items'):
                self.assertEqual(db.execute('SELECT * FROM '+table).fetchall(), [('other','keep')])

    def test_unknown_projection_schema_leaves_destination_and_owner_unchanged(self):
        dest = app.account_home('work')/'sessions'/self.path.name
        dest.parent.mkdir(); dest.write_text('previous content')
        with sqlite3.connect(app.account_home('work')/'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE future_history (thread_id TEXT)')
        with self.assertRaises(app.Error): self.quiet_move(app.choose_session(self.sid), 'work')
        self.assertEqual(dest.read_text(), 'previous content')
        self.assertFalse(app.owners())

    def test_move_respects_native_writer_locks(self):
        for name in ('personal','work'):
            with app.history_writer_lock(app.account_home(name), self.sid):
                with self.assertRaises(app.Error): self.quiet_move(app.choose_session(self.sid), 'work')
        self.assertFalse(app.owners())

    def test_projection_and_rollout_rollback_if_activation_fails(self):
        dbpath = app.account_home('work')/'thread_history_1.sqlite'
        with sqlite3.connect(dbpath) as db:
            db.execute('CREATE TABLE thread_items (thread_id TEXT, content TEXT)')
            db.execute('INSERT INTO thread_items VALUES (?,?)',(self.sid,'old'))
        with patch.object(app.os,'replace',side_effect=OSError('test activation failure')):
            with self.assertRaises(OSError): self.quiet_move(app.choose_session(self.sid),'work')
        with sqlite3.connect(dbpath) as db:
            self.assertEqual(db.execute('SELECT content FROM thread_items').fetchone(),('old',))
        self.assertFalse(app.owners())

    def test_failed_projection_commit_restores_replaced_rollout(self):
        dest = app.account_home('work')/'sessions'/self.path.name
        dest.parent.mkdir(); dest.write_text('older destination')
        original = app.reset_history_projection
        @contextlib.contextmanager
        def fail_commit(home, sid):
            with original(home,sid):
                yield
                raise sqlite3.OperationalError('test commit failure')
        with patch.object(app,'reset_history_projection',fail_commit):
            with self.assertRaises(sqlite3.Error): self.quiet_move(app.choose_session(self.sid),'work')
        self.assertEqual(dest.read_text(),'older destination')
        self.assertFalse(app.owners())

    def test_two_account_processes_can_hold_locks_concurrently(self):
        with app.lock('account-personal', shared=True), app.lock('account-work', shared=True):
            with self.assertRaises(app.Error):
                with app.lock('account-personal'):
                    pass

    def test_duplicate_session_lock_rejects_second_resume(self):
        with app.lock('session-' + self.sid):
            with self.assertRaises(app.Error):
                app.run('personal', app.choose_session(self.sid))

    def test_bad_name_and_missing_account_are_rejected(self):
        with self.assertRaises(app.Error):
            app.account_home('../outside')
        with self.assertRaises(app.Error):
            app.environment('missing')

    def test_titles_are_extracted_without_tool_output(self):
        self.assertEqual(app.choose_session(self.sid)['title'], 'Fix login page')

    def test_list_displays_email_and_plan_and_handles_one_failed_lookup(self):
        def identity(name):
            if name == 'work':
                raise app.Error('do not expose raw backend errors or tokens')
            return ('person@example.com', 'pro', 'cached')
        output = io.StringIO()
        with patch.object(app, 'account_identity', side_effect=identity), contextlib.redirect_stdout(output):
            app.list_accounts()
        text = output.getvalue()
        self.assertIn('person@example.com', text)
        self.assertIn('pro', text)
        self.assertIn('identity unavailable', text)
        self.assertNotIn('raw backend errors', text)

    def test_identity_uses_account_read_without_refresh_and_closes_backend(self):
        from collections import deque
        class Stub:
            def __init__(self):
                self.messages = deque()
                self.sent = []
                self.closed = False
            def send(self, message):
                self.sent.append(message)
                if message.get('method') == 'initialize':
                    self.messages.append({'id': 1, 'result': {}})
                if message.get('method') == 'account/read':
                    self.messages.append({'id': 2, 'result': {'account': {
                        'type': 'chatgpt', 'email': 'person@example.com', 'planType': 'plus'}}})
            def close(self):
                self.closed = True
        backend = Stub()
        with patch.object(app, 'Backend', return_value=backend):
            self.assertEqual(app.account_identity('personal'), ('person@example.com', 'plus', 'cached'))
        self.assertTrue(backend.closed)
        self.assertIn({'id': 2, 'method': 'account/read', 'params': {'refreshToken': False}}, backend.sent)

if __name__ == '__main__':
    unittest.main()
