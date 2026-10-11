import contextlib
import io
from collections import deque
from unittest.mock import patch
import unittest
from test_launcher import app, LauncherTests


class RefreshTests(unittest.TestCase):
    setUp = LauncherTests.setUp

    def backend(self, *, read_error=False, usage_error=False, account=True):
        class Stub:
            def __init__(self):
                self.messages = deque()
                self.sent = []
                self.closed = False
            def send(self, message):
                self.sent.append(message)
                if 'id' not in message:
                    return
                method = message['method']
                result = {}
                if method == 'account/read':
                    result = {'account': {'type': 'chatgpt', 'email': 'a@example.com',
                                          'planType': 'pro'} if account else None}
                if method == 'account/rateLimits/read':
                    result = {'rateLimits': {'primary': {'usedPercent': 20}}}
                if (read_error and method == 'account/read') or (usage_error and method == 'account/rateLimits/read'):
                    self.messages.append({'id': message['id'], 'error': {'message': 'SECRET'}})
                else:
                    self.messages.append({'id': message['id'], 'result': result})
            def close(self):
                self.closed = True
        return Stub()

    def test_refresh_uses_native_manager_and_verifies_live_access(self):
        backend = self.backend()
        with patch.object(app, 'Backend', return_value=backend), app.lock('account-personal', shared=True):
            report = app.refresh_account('personal')
        self.assertIsNotNone(report['usage'])
        self.assertTrue(backend.closed)
        self.assertEqual([m['method'] for m in backend.sent],
                         ['initialize', 'initialized', 'account/read', 'account/rateLimits/read'])
        self.assertEqual(backend.sent[2]['params'], {'refreshToken': True})
        self.assertEqual((app.account_home('personal') / 'auth.json').read_text(), '{"test_credentials":true}')

    def test_failures_never_claim_success_or_expose_raw_errors(self):
        for args in ({'read_error': True}, {'usage_error': True}, {'account': False}):
            with self.subTest(args=args):
                backend = self.backend(**args)
                with patch.object(app, 'Backend', return_value=backend), self.assertRaises(app.Error) as error:
                    app.refresh_account('personal')
                self.assertIn('codex-accounts login personal', str(error.exception))
                self.assertNotIn('SECRET', str(error.exception))
                self.assertTrue(backend.closed)

    def test_refreshes_are_serialized_per_account(self):
        with app.lock('token-refresh-personal'), patch.object(app, 'Backend') as backend:
            with self.assertRaises(app.Error):
                app.refresh_account('personal')
        backend.assert_not_called()

    def test_cli_dispatch_and_unknown_account(self):
        with patch.object(app, 'maybe_auto_update'), patch.object(app, 'refresh_account') as refresh, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(app.main(['refresh', 'personal']), 0)
        refresh.assert_called_once_with('personal')
        self.assertIn('live access verified', out.getvalue())
        with patch.object(app, 'Backend') as backend, self.assertRaises(app.Error):
            app.refresh_account('missing')
        backend.assert_not_called()

    def test_browser_login_stages_credentials_and_preserves_history(self):
        home=app.account_home('personal')
        original=(home/'auth.json').read_text()
        history=self.path.read_text()
        staged_paths=[]
        def sign_in(args,env):
            staged=app.Path(env['CODEX_HOME'])
            staged_paths.append(staged)
            self.assertNotEqual(staged,home)
            self.assertFalse((staged/'auth.json').exists())
            self.assertEqual((home/'auth.json').read_text(),original)
            app.atomic_json(staged/'auth.json',{'tokens':{'access_token':'replacement'}})
            return 0
        with app.lock('account-personal',shared=True),patch.object(app.subprocess,'call',side_effect=sign_in),contextlib.redirect_stdout(io.StringIO()):
            app.login('personal')
        self.assertEqual(app.read_json(home/'auth.json'),{'tokens':{'access_token':'replacement'}})
        self.assertEqual((home/'auth.json').stat().st_mode & 0o777,0o600)
        self.assertEqual(self.path.read_text(),history)
        self.assertTrue(all(not p.exists() for p in staged_paths))
        self.assertEqual((app.account_home('work')/'auth.json').read_text(),original)

    def test_browser_login_failure_keeps_previous_credentials(self):
        home=app.account_home('personal')
        original=(home/'auth.json').read_text()
        for status in (1,0):
            with self.subTest(status=status),patch.object(app.subprocess,'call',return_value=status),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(app.Error):
                app.login('personal')
            self.assertEqual((home/'auth.json').read_text(),original)
            self.assertEqual(list((app.ROOT/'login-staging').iterdir()),[])
        with app.lock('account-personal'),patch.object(app.subprocess,'call') as call,self.assertRaises(app.Error):
            app.login('personal')
        call.assert_not_called()
        with app.lock('token-refresh-personal'),patch.object(app.subprocess,'call') as call,self.assertRaises(app.Error):
            app.login('personal')
        call.assert_not_called()

    def test_relogin_rejects_different_user_or_workspace(self):
        def credentials(workspace,user):
            payload=app.base64.urlsafe_b64encode(app.json.dumps({'sub':user}).encode()).decode().rstrip('=')
            return {'tokens':{'account_id':workspace,'id_token':'header.'+payload+'.signature','access_token':'fixture'}}
        home=app.account_home('personal')
        original=credentials('org-1','user-1')
        app.atomic_json(home/'auth.json',original)
        for replacement in (credentials('org-2','user-1'),credentials('org-1','user-2'),{'tokens':{'access_token':'fixture'}}):
            def sign_in(args,env):
                app.atomic_json(app.Path(env['CODEX_HOME'])/'auth.json',replacement)
                return 0
            with self.subTest(replacement=replacement),app.lock('account-personal',shared=True),patch.object(app.subprocess,'call',side_effect=sign_in),contextlib.redirect_stdout(io.StringIO()),self.assertRaisesRegex(app.Error,'same ChatGPT account'):
                app.login('personal')
            self.assertEqual(app.read_json(home/'auth.json'),original)

    def test_setup_credentials_are_not_blocked_by_old_window_usage_job(self):
        window=app.SetupWindow()
        report={'identity':('fixture@example.com','pro','cached'),'usage':{'rateLimits':{}},'usage_error':None,'checked_at':1}
        with app.lock('setup-window-job'),patch.object(app,'refresh_account',return_value=report) as refresh:
            window.work('refresh-token','personal')
        refresh.assert_called_once_with('personal')
        self.assertFalse(window.job['error'])
        with app.lock('setup-window-job'),patch.object(app.subprocess,'Popen') as popen,patch.object(app,'collect_status',return_value=[]):
            popen.return_value.wait.return_value=0
            window.work('reconnect','personal')
        self.assertEqual(popen.call_args[0][0][-2:],['login','personal'])
        self.assertFalse(window.job['error'])
        with app.lock('token-refresh-personal'):
            window.work('refresh-token','personal')
        self.assertTrue(window.job['error'])
        self.assertIn('running for personal',window.job['message'])
        self.assertNotIn('Another setup window',window.job['message'])

    def test_setup_refresh_updates_only_selected_account(self):
        window = app.SetupWindow()
        window.reports = [{'label': 'work', 'usage': None}]
        report = {'identity': ('a@example.com', 'pro', 'cached'), 'usage': {'rateLimits': {}},
                  'usage_error': None, 'checked_at': 1}
        with patch.object(app, 'refresh_account', return_value=report) as refresh:
            window.work('refresh-token', 'personal')
        refresh.assert_called_once_with('personal')
        self.assertFalse(window.job['error'])
        self.assertEqual({r['label'] for r in window.reports}, {'personal', 'work'})
        with patch.object(app, 'refresh_account', side_effect=app.Error('Reconnect required')):
            window.work('refresh-token', 'personal')
        self.assertTrue(window.job['error'])
        self.assertEqual(window.job['message'], 'Reconnect required')


if __name__ == '__main__':
    unittest.main()
