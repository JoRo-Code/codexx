"""Live code adoption preserves process/socket/backend state and in-flight work."""
from collections import deque
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import test_launcher as fixtures
from test_router import Backend, Client, wait_for
from test_auto import FakeBackend, FakeWS

app = fixtures.app
SOURCE = Path(__file__).with_name('codex-accounts').read_text()

class LiveUpdateTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp

    def runtime(self):
        self.source = app.ROOT/'installed/codex-accounts'
        self.source.parent.mkdir()
        self.source.write_text(SOURCE)
        runtime = app.LiveRuntime(self.source)
        self.addCleanup(runtime.close)
        return runtime

    def replace(self, runtime, version='0.12.2', extra='', abi=1):
        payload = SOURCE.replace('VERSION = '+repr(app.VERSION), 'VERSION = '+repr(version))
        payload = payload.replace('LIVE_RUNTIME_ABI = 1', 'LIVE_RUNTIME_ABI = '+str(abi))
        self.source.write_text(payload+'\n'+extra)
        runtime.refresh(force=True)

    def bridge(self):
        bridge = app.AutoBridge('personal', FakeWS(), app.ROOT/'runs/test.json', factory=FakeBackend)
        bridge.backend.messages=deque()
        bridge.set_thread({'id':self.sid,'model':'chosen-model'})
        self.addCleanup(bridge.close)
        return bridge

    def test_terminal_adopts_new_methods_without_restarting_backend_or_locks(self):
        runtime=self.runtime();bridge=self.bridge()
        backend,account_lock,session_lock=bridge.backend,bridge.account_lock,bridge.session_lock
        self.replace(runtime,extra='AutoBridge.live_probe = lambda self: "new code"')
        self.assertTrue(app.adopt_bridge_runtime(bridge,runtime))
        self.assertEqual(bridge.live_probe(),'new code')
        self.assertIs(bridge.backend,backend);self.assertFalse(backend.closed)
        self.assertIs(bridge.account_lock,account_lock)
        self.assertIs(bridge.session_lock,session_lock)
        self.assertEqual(bridge.thread_options['model'],'chosen-model')
        self.assertEqual(app.read_json(bridge.marker)['tracking_version'],'0.12.2')

    def test_active_turn_and_pending_approval_keep_old_code_until_idle(self):
        runtime=self.runtime();bridge=self.bridge();old=bridge.__class__
        self.replace(runtime,extra='AutoBridge.live_probe = lambda self: "new code"')
        for phase in ('working','starting turn','waiting for input','switching account'):
            bridge.phase=phase
            self.assertFalse(app.adopt_bridge_runtime(bridge,runtime))
            self.assertIs(bridge.__class__,old)
        bridge.phase='idle';bridge.approvals.add('approval-1')
        self.assertFalse(app.adopt_bridge_runtime(bridge,runtime))
        bridge.approvals.clear();bridge.pending['1']='account/read'
        self.assertFalse(app.adopt_bridge_runtime(bridge,runtime))
        bridge.pending.clear()
        self.assertTrue(app.adopt_bridge_runtime(bridge,runtime))

    def test_invalid_and_incompatible_updates_keep_loaded_generation(self):
        runtime=self.runtime();original=runtime.module
        self.source.write_text('invalid python !!!')
        runtime.refresh(force=True)
        self.assertIs(runtime.module,original);self.assertIsNotNone(runtime.error)
        self.replace(runtime,abi=2)
        self.assertIs(runtime.module,original);self.assertIn('ABI',runtime.error)
        self.replace(runtime,extra='AutoBridge = lambda: None')
        self.assertIs(runtime.module,original);self.assertIn('layout',runtime.error)
        self.replace(runtime)
        self.assertEqual(runtime.module.VERSION,'0.12.2');self.assertIsNone(runtime.error)

    def test_rollback_is_adopted_and_explicit_paths_survive(self):
        runtime=self.runtime();self.replace(runtime)
        self.assertEqual(runtime.module.ROOT,app.ROOT)
        self.assertEqual(runtime.module.BINARY,app.BINARY)
        self.replace(runtime,version='0.12.1')
        self.assertEqual(runtime.module.VERSION,'0.12.1')

    def test_router_keeps_clients_requests_backend_and_process_during_update(self):
        runtime=self.runtime()
        router=app.MultiRouter(factory=Backend);router.runtime.close();router.runtime=runtime
        self.addCleanup(router.close)
        client=Client()
        router.route(client,{'id':1,'method':'initialize','params':{}})
        router.route(client,{'id':2,'method':'thread/resume','params':{'threadId':self.sid}})
        wait_for(lambda:any(m.get('id')==2 for m in client.messages))
        lane=router.sessions[self.sid];backend=lane.bridge.backend;pid=os.getpid()
        lane.bridge.phase='working'
        self.replace(runtime,extra='AutoBridge.live_probe = lambda self: "new code"\nMultiRouter.live_probe = lambda self: "new router"')
        router.route(client,{'id':3,'method':'thread/loaded/list','params':{}})
        self.assertEqual(router.live_probe(),'new router')
        self.assertFalse(hasattr(lane.bridge,'live_probe'))
        lane.bridge.phase='idle'
        wait_for(lambda:hasattr(lane.bridge,'live_probe'))
        self.assertEqual(lane.bridge.live_probe(),'new code')
        self.assertIs(router.sessions[self.sid],lane)
        self.assertIs(lane.bridge.backend,backend);self.assertFalse(backend.closed)
        self.assertIn(client,router.clients);self.assertEqual(os.getpid(),pid)
        router.route(client,{'id':4,'method':'thread/read','params':{'threadId':self.sid}})
        wait_for(lambda:any(m.get('id')==4 for m in client.messages))

    def test_background_update_is_nonblocking_throttled_and_obeys_policy(self):
        runtime=self.runtime()
        app.atomic_json(app.install_marker(self.source),{'repository':app.RELEASE_REPO})
        app.record_update(policy='auto',last_check=0)
        with patch.dict(os.environ,{'CODEX_ACCOUNTS_NO_UPDATE':'0'}), patch.object(app.subprocess,'Popen') as spawn:
            spawn.return_value.poll.return_value=None
            runtime.check_update();runtime.check_update()
            self.assertEqual(spawn.call_count,1)
            self.assertEqual(spawn.call_args.args[0][-1],'update')
            spawn.return_value.poll.return_value=0
            runtime.check_update();runtime.check_update()
            self.assertEqual(spawn.call_count,1)
            app.record_update(policy='notify',last_check=0)
            runtime.check_update()
            self.assertEqual(spawn.call_args.args[0][-1],'--check')
            spawn.return_value.poll.return_value=0
            runtime.check_update()
            app.record_update(policy='off',last_check=0)
            runtime.check_update()
            self.assertEqual(spawn.call_count,2)

if __name__=='__main__':unittest.main()
