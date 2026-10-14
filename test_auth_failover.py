"""Account authentication failures use the same safe history migration as quota."""
import time
import unittest
from unittest.mock import patch
import test_auto as auto
import test_router as routing_tests
app = auto.app


class AuthFailoverTests(unittest.TestCase):
    setUp = auto.AutoTests.setUp
    make_bridge = auto.AutoTests.make_bridge
    completion = auto.AutoTests.completion
    fake_rpc = auto.AutoTests.fake_rpc
    def auth_completion(self):
        event = self.completion('other')
        event['params']['turn']['error']['message'] = 'workspace routing discovery unauthorized (401)'
        return event

    def test_discovery_401_rotates_and_exhausts_accounts_without_looping(self):
        bridge = self.make_bridge()
        first = bridge.backend
        bridge.tried = {'personal'}
        bridge.snapshot = {'primary': {'usedPercent': 100, 'resetsAt': time.time() + 86400}}
        with patch.object(bridge, 'rpc', side_effect=self.fake_rpc) as rpc:
            bridge.from_backend(self.auth_completion())
            self.assertEqual(bridge.name, 'work')
            self.assertTrue(first.closed)
            self.assertEqual(app.choose_session(self.sid)['account'], 'work')
            state = app.read_json(app.ROOT / 'routing.json')['personal']
            self.assertEqual(state['reason'], 'authenticationFailed')
            self.assertLess(state['retry_after'], time.time() + 301)
            bridge.from_backend(self.auth_completion())
        starts = [c for c in rpc.call_args_list if c.args[0] == 'turn/start']
        self.assertEqual(len(starts), 1)
        self.assertIn('authentication was rejected', starts[0].args[1]['input'][0]['text'])
        self.assertEqual(app.eligible_accounts(), [])

    def test_terminal_notification_is_retained_when_completion_has_other_error(self):
        bridge = self.make_bridge()
        with patch.object(bridge, 'failover') as failover:
            bridge.from_backend({'method': 'error', 'params': {
                'threadId': self.sid, 'turnId': 'failed-turn', 'willRetry': False,
                'error': self.auth_completion()['params']['turn']['error']}})
            failover.assert_not_called()
            bridge.from_backend(self.completion('other'))
        failover.assert_called_once_with('authenticationFailed')

    def test_structured_model_http_401(self):
        for kind in ('httpConnectionFailed', 'responseStreamConnectionFailed', 'responseStreamDisconnected'):
            with self.subTest(kind=kind):
                self.assertEqual(app.account_failure({'codexErrorInfo': {kind: {'httpStatusCode': 401}}}),
                                 'authenticationFailed')
                self.assertIsNone(app.account_failure({'codexErrorInfo': {kind: {'httpStatusCode': 500}}}))

    def test_tool_errors_and_retrying_errors_do_not_rotate(self):
        bridge = self.make_bridge()
        with patch.object(bridge, 'failover') as failover:
            bridge.from_backend({'method': 'item/completed', 'params': {
                'threadId': self.sid, 'item': {'error': self.auth_completion()['params']['turn']['error']}}})
            bridge.from_backend({'method': 'error', 'params': {
                'threadId': self.sid, 'turnId': 'failed-turn', 'willRetry': True,
                'error': self.auth_completion()['params']['turn']['error']}})
            bridge.from_backend(self.completion('other'))
        failover.assert_not_called()
        self.assertIsNone(app.account_failure({'message': 'MCP tool unauthorized (401)'}))


class AuthRouterTests(unittest.TestCase):
    setUp = routing_tests.RouterTests.setUp
    router = routing_tests.RouterTests.router
    request = routing_tests.RouterTests.request

    def test_live_router_rotates_then_reports_all_accounts_denied(self):
        r, c = self.router()
        self.request(r, c, 'thread/resume', {'threadId': self.sid})
        lane = r.sessions[self.sid]
        self.request(r, c, 'turn/start', {'threadId': self.sid, 'input': []})
        def failure(turn):
            return {'method': 'turn/completed', 'params': {'threadId': self.sid,
                'turn': {'id': turn, 'status': 'failed', 'error': {
                    'message': 'workspace routing discovery unauthorized (401)', 'codexErrorInfo': 'other'}}}}
        lane.bridge.backend.messages.append(failure('one'))
        routing_tests.wait_for(lambda: lane.bridge.name == 'work')
        routing_tests.wait_for(lambda: any(x.get('method') == 'turn/start' for x in lane.bridge.backend.sent))
        lane.bridge.backend.messages.append(failure('two'))
        routing_tests.wait_for(lambda: lane.bridge.phase == 'waiting for authentication')
        self.assertEqual(lane.bridge.tried, {'personal', 'work'})
        routing_tests.wait_for(lambda: any('codexx accounts login' in str(x) for x in c.messages))


if __name__ == '__main__':
    unittest.main()
