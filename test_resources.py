"""Resource lifecycle tests use isolated homes and never contact a model."""
import contextlib
import io
import json
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import test_launcher as fixtures
from test_router import Backend, Client, wait_for

app = fixtures.app


class ResourceBackend(Backend):
    def __init__(self, name):
        super().__init__(name)
        self.ids = set()
        self.statuses = {}
        self.terminals = []
        self.queued = []
        self.goal = None
        self.fail_method = None
        self.probe_hook = None
        self.close_hook = None

    def send(self, message):
        method = message.get('method')
        if method == 'thread/loaded/list' and self.probe_hook:
            self.probe_hook()
        super().send(message)
        if 'id' not in message: return
        response = self.messages[-1]
        if method == self.fail_method:
            response.pop('result', None)
            response['error'] = {'message':'Unsupported method'}
        elif method in ('thread/start','thread/resume','thread/fork'):
            thread = response['result']['thread']
            thread['status'] = {'type':'idle'}
            self.ids.add(thread['id'])
        elif method == 'thread/loaded/list':
            response['result'] = {'data':sorted(self.ids)}
        elif method == 'thread/read':
            sid = message['params']['threadId']
            response['result']['thread']['status'] = {'type':self.statuses.get(sid,'idle')}
        elif method == 'thread/backgroundTerminals/list':
            response['result'] = {'data':self.terminals}
        elif method == 'thread/queue/list':
            response['result'] = {'data':self.queued}
        elif method == 'thread/goal/get':
            response['result'] = {'goal':self.goal}

    def close(self):
        if self.close_hook: self.close_hook()
        super().close()


class ResourceTests(unittest.TestCase):
    def setUp(self):
        fixtures.LauncherTests.setUp(self)
        self.ps = patch.object(app, 'process_resources', return_value={}).start()
        self.addCleanup(patch.stopall)
        app.atomic_json(app.ROOT/'resource-policy.json', {'enabled':False})

    def router(self):
        router = app.MultiRouter(factory=ResourceBackend)
        self.addCleanup(router.close)
        client = Client()
        router.route(client, {'id':1,'method':'initialize','params':{'clientInfo':{'name':'test','version':'1'}}})
        return router, client

    def request(self, router, client, method, params, ident=2):
        client.messages.clear()
        router.route(client, {'id':ident,'method':method,'params':params})
        wait_for(lambda: any(m.get('id') == ident for m in client.messages))
        return next(m for m in client.messages if m.get('id') == ident)

    def loaded(self, router, client, sid=None):
        sid = sid or self.sid
        self.request(router, client, 'thread/resume',
                     {'threadId':sid,'model':'chosen','sandbox':'danger-full-access','approvalPolicy':'never','cwd':self.tmp.name})
        lane = router.sessions[sid]
        wait_for(lambda: not lane.bridge.pending)
        return lane

    def saved_chat(self):
        sid = str(uuid.uuid4())
        self.path.with_name('rollout-'+sid+'.jsonl').write_text(self.path.read_text().replace(self.sid,sid))
        return sid

    def test_preview_unload_and_next_turn_preserve_history_options_and_writer_lock(self):
        router, client = self.router()
        lane = self.loaded(router, client)
        history = self.path.read_bytes()
        old = lane.bridge.backend
        self.assertEqual(lane.request_unload().result(5)['result'], 'would_unload')
        self.assertFalse(old.closed)
        self.assertEqual(lane.request_unload(True).result(5)['result'], 'unloaded')
        self.assertTrue(old.closed)
        self.assertIsNone(lane.bridge.backend)
        self.assertIsNone(lane.bridge.session_lock)
        with app.lock('session-'+self.sid): pass
        self.assertEqual(self.request(router,client,'thread/loaded/list',{})['result']['data'], [])
        # Browsing saved history must not wake a dormant server.
        self.request(router,client,'thread/read',{'threadId':self.sid})
        self.assertEqual(lane.state,'sleeping')
        result = self.request(router,client,'turn/start',{'threadId':self.sid,'input':[]})
        self.assertNotIn('error',result)
        new = lane.bridge.backend
        self.assertIsNot(new,old)
        resume = next(m['params'] for m in new.sent if m.get('method') == 'thread/resume')
        self.assertEqual(resume['model'],'chosen')
        self.assertEqual(resume['approvalPolicy'],'never')
        self.assertEqual(resume['sandbox'],'danger-full-access')
        self.assertEqual(resume['cwd'],self.tmp.name)
        self.assertEqual(sum(m.get('method') == 'turn/start' for m in new.sent),1)
        self.assertEqual(self.path.read_bytes(),history)
        with self.assertRaises(app.Error):
            with app.lock('session-'+self.sid): pass

    def test_request_during_probe_prevents_unload(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        old = lane.bridge.backend
        entered, release = threading.Event(), threading.Event()
        old.probe_hook = lambda: (entered.set(), release.wait(3))
        pending = lane.request_unload(True)
        self.assertTrue(entered.wait(3))
        router.route(client,{'id':70,'method':'turn/start','params':{'threadId':self.sid,'input':[]}})
        release.set()
        self.assertEqual(pending.result(5)['result'],'kept')
        wait_for(lambda: any(m.get('id') == 70 for m in client.messages))
        self.assertIs(lane.bridge.backend,old)
        self.assertFalse(old.closed)
        self.assertEqual(sum(m.get('method') == 'turn/start' for m in old.sent),1)

    def test_request_during_close_wakes_once_and_is_not_lost(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        old = lane.bridge.backend
        entered, release = threading.Event(), threading.Event()
        old.close_hook = lambda: (entered.set(), release.wait(3))
        pending = lane.request_unload(True)
        self.assertTrue(entered.wait(3))
        router.route(client,{'id':71,'method':'turn/start','params':{'threadId':self.sid,'input':[]}})
        release.set()
        self.assertEqual(pending.result(5)['result'],'unloaded')
        wait_for(lambda: any(m.get('id') == 71 for m in client.messages))
        self.assertEqual(sum(m.get('method') == 'turn/start' for m in lane.bridge.backend.sent),1)
        self.assertEqual(sum(m.get('id') == 71 for m in client.messages),1)

    def test_active_work_approvals_items_and_volatile_state_are_protected(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        for phase in ['working','starting turn','waiting for input','switching account','unknown']:
            lane.bridge.phase = phase
            self.assertEqual(lane.request_unload(True).result(5)['result'],'kept')
        lane.bridge.phase = 'idle'
        for container, value in [(lane.bridge.approvals,'approval'),(lane.items,('child','tool')),
                                 (lane.volatile,'stateful_tool'),(lane.processes,'process')]:
            container.add(value)
            self.assertEqual(lane.request_unload(True).result(5)['result'],'kept')
            container.clear()
        self.assertFalse(lane.bridge.backend.closed)

    def test_native_subagents_background_jobs_queue_and_goals_are_protected(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        backend = lane.bridge.backend
        backend.ids.add('child')
        backend.statuses['child'] = 'active'
        self.assertIn('thread_not_idle',lane.request_unload(True).result(5)['blockers'])
        backend.statuses['child'] = 'idle'
        for field, value, blocker in [('terminals',[{'processId':'p'}],'background_terminal'),
                                      ('queued',[{'id':'q'}],'queued_work'),
                                      ('goal',{'status':'active'},'continuing_goal')]:
            setattr(backend,field,value)
            self.assertIn(blocker,lane.request_unload(True).result(5)['blockers'])
            setattr(backend,field,None if field == 'goal' else [])
        self.assertEqual(lane.request_unload(True).result(5)['result'],'unloaded')

    def test_unknown_capability_and_unsaved_history_never_unload(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        lane.bridge.backend.fail_method = 'thread/backgroundTerminals/list'
        self.assertIn('safety_check_unavailable',lane.request_unload(True).result(5)['blockers'])
        lane.bridge.backend.fail_method = None
        self.path.unlink()
        self.assertIn('safety_check_unavailable',lane.request_unload(True).result(5)['blockers'])
        self.assertFalse(lane.bridge.backend.closed)

    def test_pin_and_persistent_resources_use_process_birth_identity(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        app.atomic_json(app.ROOT/'resource-preferences.json',{self.sid:{'pinned':True}})
        self.assertIn('pinned',lane.request_unload(True).result(5)['blockers'])
        resource = {'pid':123,'identity':'original','kind':'preview','lifetime':'persistent'}
        app.atomic_json(app.ROOT/'resource-preferences.json',{self.sid:{'resources':[resource]}})
        self.ps.return_value = {123:{'parent':1,'rss_kb':10,'name':'bun','identity':'original'}}
        self.assertIn('persistent_preview',lane.request_unload(True).result(5)['blockers'])
        self.ps.return_value[123]['identity'] = 'reused PID'
        self.assertEqual(lane.request_unload(True).result(5)['result'],'unloaded')

    def test_node_repl_descendant_is_protected(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        lane.bridge.backend.process.pid = 120
        self.ps.return_value = {
            120:dict(parent=1,rss_kb=10,name='codex',identity='a'),
            121:dict(parent=120,rss_kb=10,name='node_repl',identity='b')}
        self.assertIn('stateful_runtime',lane.request_unload(True).result(5)['blockers'])

    def test_session_scoped_preview_can_stop_but_reused_pid_is_not_signalled(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        resource = {'pid':123,'identity':'original','kind':'preview','lifetime':'session'}
        app.atomic_json(app.ROOT/'resource-preferences.json',{self.sid:{'resources':[resource]}})
        self.ps.return_value = {123:{'parent':1,'rss_kb':10,'name':'bun','identity':'original'}}
        lane.bridge.backend.terminals = [{'processId':'p','osPid':123}]
        with patch.object(app.os,'kill') as kill:
            self.assertEqual(lane.request_unload(True).result(5)['result'],'unloaded')
            kill.assert_called_once_with(123,app.signal.SIGTERM)
        self.request(router,client,'thread/resume',{'threadId':self.sid})
        self.ps.return_value[123]['identity'] = 'reused'
        with patch.object(app.os,'kill') as kill:
            self.assertEqual(lane.request_unload(True).result(5)['result'],'unloaded')
            kill.assert_not_called()

    def test_resume_reconnect_keeps_original_overrides(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        self.request(router,client,'thread/resume',{'threadId':self.sid})
        self.assertEqual(lane.bridge.thread_options['approvalPolicy'],'never')
        self.assertEqual(lane.bridge.thread_options['sandbox'],'danger-full-access')

    def test_settings_and_dormant_descriptor_survive_router_restart(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        self.request(router,client,'thread/settings/update',{'threadId':self.sid,'effort':'high','model':'updated'})
        self.assertEqual(lane.request_unload(True).result(5)['result'],'unloaded')
        router.close()
        other, attached = self.router()
        result = self.request(other,attached,'turn/start',{'threadId':self.sid,'input':[]})
        self.assertNotIn('error',result)
        new = other.sessions[self.sid].bridge.backend
        settings = next(m['params'] for m in new.sent if m.get('method') == 'thread/settings/update')
        self.assertEqual(settings['model'],'updated')
        self.assertEqual(settings['effort'],'high')
        self.assertEqual(sum(m.get('method') == 'turn/start' for m in new.sent),1)

    def test_resume_failure_reports_once_and_keeps_dormant_history(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        lane.request_unload(True).result(5)
        original = lane.bridge.factory
        def broken(name):
            backend = original(name)
            backend.fail_method = 'thread/resume'
            return backend
        lane.bridge.factory = broken
        result = self.request(router,client,'turn/start',{'threadId':self.sid,'input':[]})
        self.assertIn('error',result)
        self.assertEqual(lane.state,'sleeping')
        self.assertTrue((app.ROOT/'dormant'/(self.sid+'.json')).exists())
        lane.bridge.factory = original
        self.assertNotIn('error',self.request(router,client,'turn/start',{'threadId':self.sid,'input':[]}))

    def test_budget_is_shared_between_routers_and_evicts_oldest(self):
        one,a = self.router()
        first = self.loaded(one,a)
        two,b = self.router()
        second = self.loaded(two,b,self.saved_chat())
        first.last_activity = time.time()-100
        second.last_activity = time.time()-10
        one.maintain_resources(force=True)
        two.maintain_resources(force=True)
        app.atomic_json(app.ROOT/'resource-policy.json',{'enabled':True,'idle_seconds':600,'max_idle':1})
        one.maintain_resources(force=True)
        wait_for(lambda: first.state == 'sleeping')
        self.assertEqual(second.state,'loaded')

    def test_timeout_and_off_policy(self):
        router,client = self.router()
        lane = self.loaded(router,client)
        lane.last_activity = time.time()-1000
        router.maintain_resources(force=True)
        self.assertEqual(lane.state,'loaded')
        app.atomic_json(app.ROOT/'resource-policy.json',{'enabled':True})
        router.maintain_resources(force=True)
        wait_for(lambda: lane.state == 'sleeping')

    def test_repeated_prune_advances_past_protected_batch(self):
        router,client = self.router()
        lanes = [self.loaded(router,client,self.saved_chat()) for _ in range(9)]
        app.atomic_json(app.ROOT/'resource-preferences.json',
                        {lane.bridge.sid:{'pinned':True} for lane in lanes[:8]})
        first = router.prune_resources({'apply':True})
        self.assertEqual(first['remaining'],1)
        self.assertTrue(all(r['result'] == 'kept' for r in first['results']))
        second = router.prune_resources({'apply':True})
        self.assertTrue(any(r['result'] == 'unloaded' for r in second['results']))
        self.assertEqual(lanes[-1].state,'sleeping')

    def test_status_is_private_and_resident_memory_is_labelled(self):
        router,client = self.router()
        self.loaded(router,client)
        result = self.request(router,client,'codexx/status',{})['result']
        self.assertTrue(result['resourceManagement'])
        self.assertIn('resident only',result['memoryMetric'])
        self.assertNotIn('Fix login page',json.dumps(result))
        self.assertNotIn('test_credentials',json.dumps(result))

    def test_resource_cli_validates_policy_and_does_not_start_routers(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(app.main(['resources','status','--json']),0)
        self.assertEqual(json.loads(output.getvalue())['routers'],[])
        with self.assertRaises(app.Error): app.main(['resources','policy','--max-idle','-1'])
        with contextlib.redirect_stdout(io.StringIO()):
            app.main(['resources','pin',self.sid])
        self.assertTrue(app.resource_preferences(self.sid)['pinned'])


if __name__ == '__main__':
    unittest.main()
