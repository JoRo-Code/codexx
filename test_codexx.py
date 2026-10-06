"""Exercise the new entry point without touching real accounts or launching inference."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('codexx', str(Path(__file__).with_name('codexx')))
spec = importlib.util.spec_from_loader(loader.name, loader)
cx = importlib.util.module_from_spec(spec)
loader.exec_module(cx)
real_router_address = cx.router_address
real_popen = subprocess.Popen


class Executed(BaseException):
    pass


class CodexxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cx-', dir='/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = cx.load_accounts()
        self.app.ROOT = self.root
        self.app.BINARY = sys.executable
        for name in ('a', 'b'):
            home = self.app.account_home(name)
            home.mkdir(parents=True)
            (home / 'auth.json').write_text('{}')
        self.addCleanup(patch.stopall)
        patch.object(cx, 'load_accounts', return_value=self.app).start()
        patch.object(cx, 'native_syntax', return_value=(
            {'exec', 'e', 'review', 'resume', 'fork', 'login', 'mcp', 'completion', 'help'},
            {'-m', '--model', '-c', '--config', '-C', '--cd', '-i', '--image'})).start()
        self.execv = patch.object(cx.os, 'execv', side_effect=Executed).start()
        self.execve = patch.object(cx.os, 'execve', side_effect=Executed).start()
        self.interactive = patch.object(cx, 'interactive', side_effect=Executed).start()
        self.address = patch.object(cx, 'router_address', return_value='/tmp/existing.sock').start()
        patch.object(cx.sys.stdin, 'isatty', return_value=True).start()
        patch.dict(os.environ, {'CODEX_ACCOUNTS_NO_UPDATE': '1'}).start()
        os.environ.pop('CODEXX_ACCOUNT', None)

    def launch(self, args):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Executed):
            cx.main(args)

    def test_plain_invocation_reuses_router_and_preserves_arguments(self):
        for args in ([], ['fix tests'], ['-m', 'exec', '--image', 'a.png', 'fix tests'],
                     ['--', 'exec'], ['resume', '--last'], ['fork', 'session-id']):
            self.launch(args)
            self.assertEqual(self.interactive.call_args.args[1:4],
                             (sys.executable, args, '/tmp/existing.sock'))

    def test_admin_commands_delegate_to_existing_installation(self):
        self.launch(['accounts', 'status', '--json'])
        self.assertEqual(self.execv.call_args.args[1][-2:], ['status', '--json'])
        self.address.assert_not_called()
        self.launch(['accounts', 'desktop', 'setup', '--terminal'])
        self.assertEqual(self.execv.call_args.args[1][-2:], ['setup', '--terminal'])

    def test_setup_delegates_to_existing_web_setup(self):
        for options in ([], ['--terminal'], ['--account', 'a']):
            self.launch(['accounts', 'setup', *options])
            self.assertEqual(self.execv.call_args.args[1],
                             [sys.executable, self.app.__file__, 'setup', *options])
        self.address.assert_not_called()

    def test_native_tools_and_explicit_transport_pass_through(self):
        for args in (['mcp', 'list'], ['login'], ['completion', 'zsh'],
                     ['--remote=unix:///tmp/custom.sock'], ['--oss', 'hello']):
            self.launch(args)
            self.assertEqual(self.execv.call_args.args[1], [sys.executable, *args])
        self.address.assert_not_called()

    def test_exec_preserves_streams_arguments_and_isolates_auth(self):
        args = ['exec', '--json', '-c', 'model="explicit"', '--', 'literal $() prompt']
        with patch.dict(os.environ, {'CODEXX_ACCOUNT': 'b', 'OPENAI_API_KEY': 'never-forward'}):
            self.launch(args)
        call = self.execve.call_args.args
        self.assertEqual(call[1][-len(args):], args)
        self.assertEqual(call[2]['CODEX_HOME'], str(self.app.account_home('b')))
        self.assertNotIn('OPENAI_API_KEY', call[2])
        self.address.assert_not_called()

    def test_exec_resume_resolves_owner_and_last(self):
        rows = [dict(id='abcd-123', account='b', title='Saved', cwd=str(Path.cwd()))]
        with patch.object(self.app, 'session_records', return_value=rows):
            self.launch(['exec', 'resume', 'abcd', '--json'])
            self.assertEqual(self.execve.call_args.args[1][-4:], ['exec', 'resume', 'abcd-123', '--json'])
            self.assertTrue(self.execve.call_args.args[2]['CODEX_HOME'].endswith('/b'))
            self.launch(['exec', 'resume', '--last', 'continue'])
            self.assertEqual(self.execve.call_args.args[1][-4:], ['exec', 'resume', 'abcd-123', 'continue'])

    def test_existing_router_never_spawns_or_restarts(self):
        address = str(self.root / 'router.sock')
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(address)
            server.listen(4)
            (self.root / 'desktop').mkdir()
            (self.root / 'desktop/settings.json').write_text(json.dumps({'socket': address}))
            with patch.object(cx.subprocess, 'Popen') as spawn:
                self.assertEqual(real_router_address(self.app), address)
                spawn.assert_not_called()

    def test_cli_only_router_starts_once_without_desktop(self):
        children = []
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        try:
            with patch.object(cx.subprocess, 'Popen', side_effect=spawn):
                address = real_router_address(self.app)
                self.assertTrue(cx.reachable(address))
                self.assertEqual(real_router_address(self.app), address)
                self.assertEqual(len(children), 1)
                self.assertFalse((self.root / 'desktop').exists())
        finally:
            for child in children:
                child.terminate()
                child.wait(timeout=10)
            if 'address' in locals():
                Path(address).parent.rmdir()


class InstallTests(unittest.TestCase):
    def test_add_only_install_preserves_existing_launcher_and_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            launcher = root / 'codex-accounts'
            launcher.write_text('# Local ChatGPT account selection for Codex CLI\nold installation\n')
            marker = root / '.codex-accounts.install.json'
            marker.write_text('existing marker')
            fake = root / 'codex'
            fake.write_text('#!/bin/sh\nexit 0\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('install.py')),
                                     '--codexx-only', '--bin-dir', tmp], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('old installation', launcher.read_text())
            self.assertEqual(marker.read_text(), 'existing marker')
            self.assertTrue(os.access(root / 'codexx', os.X_OK))


class FilterTests(unittest.TestCase):
    def test_new_chat_uses_client_directory_not_router_service_directory(self):
        gate = cx.ChatFilter()
        gate.cwd = '/client/project'
        message = {'id': 1, 'method': 'thread/start', 'params': {'cwd': None}}
        gate.from_client(message)
        self.assertEqual(message['params']['cwd'], '/client/project')
        explicit = {'id': 2, 'method': 'thread/start', 'params': {'cwd': '/explicit'}}
        gate.from_client(explicit)
        self.assertEqual(explicit['params']['cwd'], '/explicit')
        resume = {'id': 3, 'method': 'thread/resume', 'params': {'threadId': 'saved'}}
        gate.from_client(resume)
        self.assertNotIn('cwd', resume['params'])

    def test_other_chats_and_approvals_cannot_take_over_the_terminal(self):
        gate = cx.ChatFilter()
        gate.from_client({'id': 1, 'method': 'thread/start', 'params': {}})
        self.assertFalse(gate.accepts({'method': 'thread/started', 'params': {'thread': {'id': 'other'}}}))
        self.assertTrue(gate.accepts({'id': 1, 'result': {'thread': {'id': 'mine'}}}))
        self.assertEqual(gate.current, 'mine')
        for method in ('turn/started', 'thread/name/updated', 'item/completed', 'error'):
            self.assertTrue(gate.accepts({'method': method, 'params': {'threadId': 'mine'}}))
            self.assertFalse(gate.accepts({'method': method, 'params': {'threadId': 'other'}}))
        self.assertFalse(gate.accepts({'id': 'approval', 'method': 'item/commandExecution/requestApproval',
                                       'params': {'threadId': 'other'}}))
        self.assertTrue(gate.accepts({'id': 'approval', 'method': 'item/commandExecution/requestApproval',
                                      'params': {'threadId': 'mine'}}))
        self.assertFalse(gate.accepts({'method': 'codex/event/task_started', 'params': {'id': 'unknown'}}))

    def test_resume_replays_own_notifications_before_response(self):
        gate = cx.ChatFilter()
        gate.from_client({'id': 1, 'method': 'thread/resume', 'params': {'threadId': 'mine'}})
        self.assertTrue(gate.accepts({'method': 'turn/started', 'params': {'threadId': 'mine'}}))
        self.assertFalse(gate.accepts({'method': 'turn/started', 'params': {'threadId': 'other'}}))


class ScriptTests(unittest.TestCase):
    def test_exec_and_review_keep_tools_when_original_package_is_removed(self):
        for command in ('exec', 'e', 'review'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                home = root / 'accounts/a'
                home.mkdir(parents=True)
                (home / 'auth.json').write_text('{}')
                package = root / 'installed-package'
                (package / 'bin').mkdir(parents=True)
                (package / 'codex-package.json').write_text(json.dumps({
                    'layoutVersion': 1, 'entrypoint': 'bin/codex'}))
                (package / 'resource.txt').write_text('original resource')
                binary = package / 'bin/codex'
                binary.write_text('#!' + sys.executable + '\n' + '''import os, shutil, sys
from pathlib import Path
if sys.argv[1:] == ['--help']:
    print('Commands:\\n  exec  Run non-interactively [aliases: e]\\n  review  Review code')
else:
    package = Path(__file__).resolve().parent.parent
    shutil.rmtree(os.environ['TEST_INSTALLED_PACKAGE'])
    os.execv(sys.executable, [sys.executable, str(package / 'bin/codex-code-mode-host'), *sys.argv[1:]])
''')
                binary.chmod(0o755)
                (package / 'bin/codex-code-mode-host').write_text('''import json, os, sys
from pathlib import Path
print(json.dumps({'stdin': sys.stdin.read(), 'args': sys.argv[1:],
                  'home': os.environ['CODEX_HOME'],
                  'resource': (Path(__file__).parent.parent / 'resource.txt').read_text()}))
print('native stderr', file=sys.stderr)
sys.exit(7)
''')
                env = dict(os.environ, CODEX_ACCOUNTS_HOME=str(root), CODEX_ACCOUNTS_BINARY=str(binary),
                           CODEXX_ACCOUNT='a', CODEX_ACCOUNTS_NO_UPDATE='1', TEST_INSTALLED_PACKAGE=str(package))
                args = [command, '-c', 'model="explicit"', '--', 'exact prompt']
                result = subprocess.run([sys.executable, str(Path(__file__).with_name('codexx')), *args],
                                        input='piped input', text=True, capture_output=True, env=env, timeout=15)
                self.assertFalse(package.exists())
                self.assertEqual(result.returncode, 7, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload['resource'], 'original resource')
                self.assertEqual(payload['stdin'], 'piped input')
                self.assertEqual(payload['args'][-len(args):], args)
                self.assertEqual(payload['home'], str(home))
                self.assertEqual(result.stderr, 'native stderr\n')

    def test_real_exec_keeps_json_stdout_stdin_stderr_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / 'accounts/a'
            home.mkdir(parents=True)
            (home / 'auth.json').write_text('{}')
            fake = root / 'codex'
            fake.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
if sys.argv[1:] == ['--help']:
    print('Commands:\\n  exec  Run non-interactively\\nOptions:\\n  -c, --config <VALUE>')
else:
    print(json.dumps({'stdin':sys.stdin.read(), 'args':sys.argv[1:], 'home':os.environ['CODEX_HOME']}))
    print('native stderr', file=sys.stderr)
    sys.exit(7)
''')
            fake.chmod(0o755)
            env = dict(os.environ, CODEX_ACCOUNTS_HOME=tmp, CODEX_ACCOUNTS_BINARY=str(fake),
                       CODEXX_ACCOUNT='a', CODEX_ACCOUNTS_NO_UPDATE='1')
            args = ['exec', '--json', '--', 'exact prompt']
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('codexx')), *args],
                                    input='piped input', text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 7, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload['stdin'], 'piped input')
            self.assertEqual(payload['args'][-len(args):], args)
            self.assertEqual(payload['home'], str(home.resolve()))
            self.assertEqual(result.stderr, 'native stderr\n')


if __name__ == '__main__':
    unittest.main()
