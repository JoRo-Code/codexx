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
        self.paths = {}
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
            if sid in self.paths: response['result']['thread']['path'] = self.paths[sid]
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

    def test_persisted_dynamic_tools_restore_and_missing_definitions_stay_loaded(self):
        router, client = self.router()
        lane = self.loaded(router, client)
        lane.volatile.add('dynamic_tools')  # Includes lanes from older live code.
        self.assertIn('dynamic_tools_not_persisted', lane.request_unload(True).result(5)['blockers'])
        lines = self.path.read_text().splitlines()
        meta = json.loads(lines[0])
        tool = {'type':'namespace','name':'functions','description':'fixture','tools':[
            {'type':'function','name':'fixture','description':'fixture','inputSchema':{'type':'object'}}]}
        meta['payload']['dynamic_tools'] = [tool]
        lines[0] = json.dumps(meta)
        self.path.write_text('\n'.join(lines)+'\n')
        original = self.path.read_bytes()
        self.assertEqual(lane.request_unload(True).result(5)['result'], 'unloaded')
        self.request(router, client, 'thread/resume', {'threadId':self.sid})
        self.assertEqual(self.path.read_bytes(), original)
        options = next(m['params'] for m in lane.bridge.backend.sent if m.get('method') == 'thread/resume')
        self.assertNotIn('dynamicTools', options)  # Not a native resume parameter.
        lane.volatile.add('stateful_tool')
        self.assertIn('stateful_tool', lane.request_unload(True).result(5)['blockers'])

    def test_dynamic_metadata_malformed_shapes_fail_closed(self):
        for record in [[], None, {'payload':[]}, {'payload':{'id':self.sid,'dynamic_tools':[None]}}]:
            self.path.write_text(json.dumps(record)+'\n')
            self.assertFalse(app.persisted_dynamic_tools({'path':str(self.path)}, self.sid))

    def test_empty_failed_start_is_retired_but_loaded_ephemeral_is_protected(self):
        router, client = self.router()
        lane = router.lane(router.primary, initialize=router.initialize)
        lane.ready.result(5)
        wait_for(lambda: lane.state == 'loaded')
        backend = lane.bridge.backend
        lane.volatile.add('dynamic_tools')  # A rejected start created no thread.
        self.assertIsNone(lane.bridge.sid)
        self.assertEqual(lane.request_unload().result(5)['result'], 'would_retire')
        backend.ids.add('ephemeral')
        self.assertIn('untracked_loaded_threads', lane.request_unload(True).result(5)['blockers'])
        self.assertFalse(backend.closed)
        backend.ids.clear()
        self.assertEqual(lane.request_unload(True).result(5)['result'], 'retired')
        wait_for(lambda: lane.stopped.is_set())
        self.assertTrue(backend.closed)

    def test_empty_lane_unknown_probe_pending_and_racing_request_are_protected(self):
        router, client = self.router()
        lane = router.lane(router.primary, initialize=router.initialize)
        lane.ready.result(5)
        wait_for(lambda: lane.state == 'loaded')
        backend = lane.bridge.backend
        backend.fail_method = 'thread/loaded/list'
        self.assertIn('safety_check_unavailable', lane.request_unload(True).result(5)['blockers'])
        backend.fail_method = None
        lane.bridge.pending['held'] = 'thread/start'
        self.assertIn('pending_requests', lane.request_unload(True).result(5)['blockers'])
        lane.bridge.pending.clear()
        entered, release = threading.Event(), threading.Event()
        backend.probe_hook = lambda: (entered.set(), release.wait(3))
        pending = lane.request_unload(True)
        self.assertTrue(entered.wait(3))
        lane.submit({'id':'new-start','method':'thread/start','params':{}})
        release.set()
        self.assertEqual(pending.result(5)['result'], 'kept')
        wait_for(lambda: lane.bridge.sid is not None)
        self.assertFalse(backend.closed)

    def test_automatic_maintenance_retires_empty_lanes_individually(self):
        router, client = self.router()
        old = router.lane(router.primary, initialize=router.initialize)
        new = router.lane(router.primary, initialize=router.initialize)
        for index, lane in enumerate([old, new]):
            lane.ready.result(5)
            wait_for(lambda: lane.state == 'loaded')
            lane.bridge.backend.process.pid = 500 + index
        self.ps.return_value = {500:dict(parent=1,rss_kb=1,name='codex',identity='a'),
                                501:dict(parent=1,rss_kb=1,name='codex',identity='b')}
        old.last_activity = time.time()-1000
        app.atomic_json(app.ROOT/'resource-policy.json', {'enabled':True,'max_idle':6,'idle_seconds':600})
        router.maintain_resources(force=True)
        wait_for(lambda: old.stopped.is_set())
        self.assertFalse(new.stopped.is_set())

    def test_unknown_capability_and_unsaved_history_never_unload(self):
        router, client = self.router()
        lane = self.loaded(router,client)
        lane.bridge.backend.fail_method = 'thread/backgroundTerminals/list'
        self.assertIn('safety_check_unavailable',lane.request_unload(True).result(5)['blockers'])
        lane.bridge.backend.fail_method = None
        self.path.unlink()
        self.assertIn('history_unavailable',lane.request_unload(True).result(5)['blockers'])
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

    def append_events(self, *kinds):
        with self.path.open('a') as stream:
            for kind in kinds:
                stream.write(json.dumps({'type':'event_msg','payload':{'type':kind}})+'\n')

    def stateful_lane(self, router, client, sid=None, pid=120):
        lane = self.loaded(router, client, sid)
        lane.bridge.backend.process.pid = pid
        lane.volatile.add('stateful_tool')
        self.ps.return_value.update({
            pid:dict(parent=1,rss_kb=40000,name='codex',identity='server-%d' % pid),
            pid+1:dict(parent=pid,rss_kb=60000,name='node_repl',identity='repl-%d' % pid)})
        return lane

    def expire_policy(self, **values):
        app.atomic_json(app.ROOT/'resource-policy.json', dict(
            {'enabled':True,'idle_seconds':600,'max_idle':6,'expire_seconds':7200,'max_loaded':12}, **values))

    def test_stateful_runtime_expires_and_its_process_is_stopped(self):
        router, client = self.router()
        self.ps.return_value = {}
        lane = self.stateful_lane(router, client)
        self.assertIn('stateful_tool', lane.request_unload(True).result(5)['blockers'])
        lane.volatile.discard('stateful_tool')
        self.assertIn('stateful_runtime', lane.request_unload(True).result(5)['blockers'])
        lane.volatile.add('stateful_tool')
        # Pins, approvals and running tools still protect an expired runtime.
        app.atomic_json(app.ROOT/'resource-preferences.json', {self.sid:{'pinned':True}})
        self.assertIn('pinned', lane.request_unload(True, True).result(5)['blockers'])
        app.atomic_json(app.ROOT/'resource-preferences.json', {})
        lane.bridge.approvals.add('approval')
        self.assertIn('approval_or_user_input', lane.request_unload(True, True).result(5)['blockers'])
        lane.bridge.approvals.clear()
        lane.bridge.backend.terminals = [{'processId':'p','osPid':999}]
        self.assertIn('background_terminal', lane.request_unload(True, True).result(5)['blockers'])
        lane.bridge.backend.terminals = []
        with patch.object(app.os, 'kill') as kill:
            self.assertEqual(lane.request_unload(True, True).result(5)['result'], 'unloaded')
            kill.assert_called_once_with(121, app.signal.SIGTERM)
        self.assertNotIn('stateful_tool', lane.volatile)
        # History and resume survive; only the REPL state is lost.
        self.assertNotIn('error', self.request(router, client, 'turn/start', {'threadId':self.sid,'input':[]}))
        self.assertEqual(lane.state, 'loaded')

    def test_maintenance_expires_stateful_lanes_only_after_threshold(self):
        router, client = self.router()
        self.ps.return_value = {}
        lane = self.stateful_lane(router, client)
        self.assertEqual(lane.request_unload(True).result(5)['result'], 'kept')
        lane.last_check = 0
        lane.last_activity = time.time()-3600
        self.expire_policy()
        router.maintain_resources(force=True)
        time.sleep(.3)
        self.assertEqual(lane.state, 'loaded')
        lane.last_activity = time.time()-7300
        with patch.object(app.os, 'kill'):
            router.maintain_resources(force=True)
            wait_for(lambda: lane.state == 'sleeping')
        # A disabled expiry keeps stateful runtimes protected indefinitely.
        other = self.stateful_lane(router, client, self.saved_chat(), pid=130)
        other.request_unload(True).result(5)
        other.last_check, other.last_activity = 0, time.time()-100000
        self.expire_policy(expire_seconds=0, max_loaded=0)
        router.maintain_resources(force=True)
        time.sleep(.3)
        self.assertEqual(other.state, 'loaded')

    def test_stale_unfinished_items_and_working_phase_reconcile_with_rollout(self):
        router, client = self.router()
        lane = self.loaded(router, client)
        lane.items.add((self.sid, 'lost-completion'))
        lane.bridge.phase = 'working'
        self.append_events('task_started')
        blockers = lane.request_unload(True, True).result(5)['blockers']
        self.assertIn('unfinished_items', blockers)
        self.assertIn('phase:working', blockers)
        self.append_events('task_complete', 'token_count')
        # Saved history alone is not enough while the native thread is active.
        lane.bridge.backend.statuses[self.sid] = 'active'
        self.assertIn('thread_not_idle', lane.request_unload(True, True).result(5)['blockers'])
        lane.bridge.backend.statuses[self.sid] = 'idle'
        lane.bridge.phase = 'working'
        self.assertIn('unfinished_items', lane.request_unload(True).result(5)['blockers'])
        self.assertEqual(lane.request_unload(True, True).result(5)['result'], 'unloaded')
        self.assertEqual(lane.items, set())
        self.assertNotIn('error', self.request(router, client, 'turn/start', {'threadId':self.sid,'input':[]}))
        # turn_aborted is also final.
        lane.bridge.phase = 'working'
        self.append_events('task_started', 'turn_aborted')
        self.assertEqual(lane.request_unload(True, True).result(5)['result'], 'unloaded')

    def test_rollout_reconciliation_reads_tail_and_fails_closed(self):
        self.assertFalse(app.rollout_turn_finished(self.path))
        self.append_events('task_complete')
        with self.path.open('a') as stream:
            stream.write(json.dumps({'type':'response_item','payload':{'text':'x'*5000}})+'\n')
        self.assertTrue(app.rollout_turn_finished(self.path))
        self.assertFalse(app.rollout_turn_finished(self.path, tail=1000))
        self.append_events('task_started')
        self.assertFalse(app.rollout_turn_finished(self.path))

    def test_history_unavailable_retires_only_unmaterialized_threads_on_expiry(self):
        router, client = self.router()
        lane = self.loaded(router, client)
        backend = lane.bridge.backend
        self.path.unlink()
        self.assertIn('history_unavailable', lane.request_unload(True).result(5)['blockers'])
        # Without a native rollout path, fail closed.
        self.assertIn('history_unavailable', lane.request_unload(True, True).result(5)['blockers'])
        other = app.ROOT/'elsewhere.jsonl'
        other.write_text('{}\n')
        backend.paths[self.sid] = str(other)
        self.assertIn('history_unavailable', lane.request_unload(True, True).result(5)['blockers'])
        backend.paths[self.sid] = str(self.path)
        lane.items.add((self.sid, 'item'))  # No saved turn can prove this stale.
        self.assertIn('unfinished_items', lane.request_unload(True, True).result(5)['blockers'])
        lane.items.clear()
        self.assertEqual(lane.request_unload(False, True).result(5)['result'], 'would_retire')
        self.assertEqual(lane.request_unload(True, True).result(5)['result'], 'retired')
        wait_for(lambda: lane.stopped.is_set())
        self.assertTrue(backend.closed)
        self.assertNotIn(self.sid, router.sessions)
        self.assertFalse((app.ROOT/'dormant'/(self.sid+'.json')).exists())

    def test_secondary_and_superseded_control_servers_are_retired(self):
        router, client = self.router()
        self.expire_policy(control_idle_seconds=1800)
        primary = router.controls[router.primary]
        with router.mutex:
            secondary = router.control('work')
        secondary.ready.result(5)
        self.assertIsNot(primary, secondary)
        primary.last_used = secondary.last_used = time.time()-60
        router.maintain_resources(force=True)
        self.assertFalse(secondary.stopped.is_set())
        secondary.last_used = time.time()-4000
        secondary.bridge.pending['held'] = 'thread/list'
        router.maintain_resources(force=True)
        self.assertFalse(secondary.stopped.is_set())
        secondary.bridge.pending.clear()
        primary.last_used = time.time()-100000
        router.maintain_resources(force=True)
        wait_for(lambda: secondary.bridge.backend is None or secondary.bridge.backend.closed)
        self.assertNotIn('work', router.controls)
        self.assertFalse(primary.stopped.is_set())
        # A later metadata request starts a fresh control server on demand.
        with router.mutex:
            again = router.control('work')
        self.assertIsNot(again, secondary)
        # A control server left behind by an upgrade never lingers.
        with router.mutex:
            router.controls['work'] = router.lane('work', control=True, initialize=router.initialize)
        again.ready.result(5)
        router.maintain_resources(force=True)
        wait_for(lambda: again.stopped.is_set())
        self.assertEqual(sum(lane.control and not lane.stopped.is_set() for lane in router.lanes), 2)

    def test_loaded_cap_evicts_least_recently_used_safe_lanes(self):
        router, client = self.router()
        self.ps.return_value = {}
        lanes = [self.stateful_lane(router, client, self.saved_chat(), pid=200+10*i) for i in range(4)]
        for lane, age in zip(lanes, [5000, 4000, 300, 900]):
            lane.request_unload(True).result(5)
            lane.last_check, lane.last_activity = 0, time.time()-age
        lanes[0].bridge.approvals.add('approval')  # Hard blockers are never evicted.
        self.expire_policy(max_loaded=2)
        with patch.object(app.os, 'kill'):
            router.maintain_resources(force=True)
            wait_for(lambda: lanes[1].state == 'sleeping' and lanes[3].state == 'sleeping')
        self.assertEqual(lanes[0].state, 'loaded')
        self.assertEqual(lanes[2].state, 'loaded')  # Too recently used to drop its REPL.

    def test_status_reports_totals_and_memory_warning(self):
        router, client = self.router()
        self.ps.return_value = {}
        self.stateful_lane(router, client)
        self.expire_policy(rss_warning_mb=50, max_loaded=1)
        result = self.request(router, client, 'codexx/status', {})['result']
        self.assertEqual(result['totals']['chat_servers'], 1)
        self.assertEqual(result['totals']['rss_kb'], 100000)
        self.assertTrue(any('MB RSS' in w for w in result['totals']['warnings']))
        totals = app.resource_totals([dict(state='loaded', control=False, rss_kb=10)]*3, app.resource_policy())
        self.assertTrue(any('cap 1' in w for w in totals['warnings']))
        with self.assertRaises(app.Error): app.main(['resources','policy','--max-loaded','-1'])
        with contextlib.redirect_stdout(io.StringIO()):
            app.main(['resources','policy','--expire-seconds','3600','--max-loaded','8'])
        self.assertEqual(app.resource_policy()['expire_seconds'], 3600)
        self.assertEqual(app.resource_policy()['max_loaded'], 8)

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
