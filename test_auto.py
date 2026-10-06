import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import test_launcher as fixtures
app = fixtures.app

class FakeWS:
    def __init__(self): self.sent=[]
    def send(self, message): self.sent.append(message)

class FakeBackend:
    def __init__(self, name): self.name=name;self.closed=False;self.sent=[]
    def close(self): self.closed=True
    def send(self, message): self.sent.append(message)

class AutoTests(unittest.TestCase):
    setUp = fixtures.LauncherTests.setUp
    def make_bridge(self):
        bridge=app.AutoBridge('personal',FakeWS(),app.ROOT/'runs/bridge.json',factory=FakeBackend)
        bridge.initialize={'clientInfo':{'name':'test','version':'1'}}
        bridge.set_thread({'id':self.sid,'cwd':self.tmp.name,'model':'test-model'})
        self.addCleanup(bridge.close)
        return bridge

    def completion(self, code='usageLimitExceeded', sid=None):
        return {'method':'turn/completed','params':{'threadId':sid or self.sid,
          'turn':{'id':'failed-turn','status':'failed','items':[],
                  'error':{'message':'quota test','codexErrorInfo':code}}}}

    def fake_rpc(self, method, params):
        if method=='thread/resume': return {'thread':{'id':self.sid,'cwd':self.tmp.name}}
        if method=='account/read': return {'account':{'type':'chatgpt','planType':'pro'}}
        return {}

    def test_quota_failover_moves_history_and_starts_continuation(self):
        bridge=self.make_bridge();first=bridge.backend
        bridge.tried={'personal'}
        bridge.continuation_options={'model':'test-model','approvalPolicy':'on-request'}
        with patch.object(bridge,'rpc',side_effect=self.fake_rpc) as rpc:
            bridge.from_backend(self.completion())
        self.assertTrue(first.closed)
        self.assertEqual(bridge.name,'work')
        self.assertEqual(app.choose_session(self.sid)['account'],'work')
        calls={call.args[0]:call.args[1] for call in rpc.call_args_list}
        self.assertEqual(calls['thread/resume']['threadId'], self.sid)
        self.assertEqual(calls['turn/start']['model'],'test-model')
        self.assertEqual(calls['turn/start']['approvalPolicy'],'on-request')
        self.assertIn('do not repeat completed side effects',calls['turn/start']['input'][0]['text'])
        self.assertNotIn('personal', app.eligible_accounts())

    def test_failover_preserves_full_access_and_model_choices(self):
        bridge = self.make_bridge()
        bridge.from_client({'id': 1, 'method': 'thread/resume', 'params': {
            'threadId': self.sid, 'model': 'chosen-model', 'approvalPolicy': 'never',
            'sandbox': 'danger-full-access'}})
        bridge.from_backend({'id': 1, 'result': {'thread': {'id': self.sid}}})
        bridge.from_client({'id': 2, 'method': 'turn/start', 'params': {
            'threadId': self.sid, 'model': 'chosen-model', 'effort': 'high',
            'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'dangerFullAccess'},
            'input': [{'type': 'text', 'text': 'Continue'}]}})
        with patch.object(bridge, 'rpc', side_effect=self.fake_rpc) as rpc:
            bridge.from_backend(self.completion())
        calls = {call.args[0]: call.args[1] for call in rpc.call_args_list}
        self.assertEqual(calls['thread/resume']['sandbox'], 'danger-full-access')
        self.assertEqual(calls['turn/start']['sandboxPolicy'], {'type': 'dangerFullAccess'})
        self.assertEqual(calls['turn/start']['approvalPolicy'], 'never')
        self.assertEqual(calls['turn/start']['model'], 'chosen-model')
        self.assertEqual(calls['turn/start']['effort'], 'high')

    def test_ephemeral_helpers_cannot_replace_main_chat_or_options(self):
        bridge = self.make_bridge()
        bridge.title = 'Main chat'
        bridge.thread_options.update(approvalPolicy='never', sandbox='danger-full-access')
        bridge.continuation_options = {'effort': 'high'}
        options = dict(bridge.thread_options)
        session_lock = bridge.session_lock
        for method in ('thread/start', 'thread/resume', 'thread/fork'):
            other = str(uuid.uuid4())
            bridge.from_client({'id': 10, 'method': method, 'params': {
                'model': 'helper-model', 'sandbox': 'read-only', 'ephemeral': True}})
            bridge.from_backend({'id': 10, 'result': {'thread': {
                'id': other, 'ephemeral': True, 'name': 'Helper', 'model': 'helper-model'}}})
            bridge.from_client({'id': 11, 'method': 'turn/start', 'params': {
                'threadId': other, 'model': 'helper-model', 'effort': 'low'}})
            with patch.object(bridge, 'failover') as failover:
                bridge.from_backend(self.completion(sid=other))
            failover.assert_not_called()
        self.assertEqual(bridge.sid, self.sid)
        self.assertEqual(bridge.title, 'Main chat')
        self.assertIs(bridge.session_lock, session_lock)
        self.assertEqual(bridge.thread_options, options)
        self.assertEqual(bridge.continuation_options, {'effort': 'high'})
        self.assertEqual(bridge.pending_thread_options, {})
        self.assertEqual({e['session'] for e in app.activity_records()}, {self.sid})

    def test_failed_resume_does_not_change_main_options(self):
        bridge = self.make_bridge()
        options = dict(bridge.thread_options)
        bridge.from_client({'id': 10, 'method': 'thread/resume', 'params': {'model': 'other'}})
        bridge.from_backend({'id': 10, 'error': {'code': -1, 'message': 'failed'}})
        self.assertEqual(bridge.thread_options, options)
        self.assertEqual(bridge.pending_thread_options, {})

    def test_paginated_failover_uses_metadata_resume_then_continues(self):
        self.rows[0]['payload']['history_mode'] = 'paginated'
        self.path.write_text(''.join(json.dumps(x)+'\n' for x in self.rows))
        bridge = self.make_bridge()
        with patch.object(bridge,'rpc',side_effect=self.fake_rpc) as rpc:
            bridge.from_backend(self.completion())
        calls = {call.args[0]:call.args[1] for call in rpc.call_args_list}
        self.assertTrue(calls['thread/resume']['excludeTurns'])
        self.assertEqual(calls['turn/start']['threadId'],self.sid)
        self.assertEqual(app.choose_session(self.sid)['account'],'work')

    def test_all_accounts_exhausted_stops_without_looping(self):
        bridge=self.make_bridge()
        with patch.object(bridge,'rpc',side_effect=self.fake_rpc) as rpc:
            bridge.from_backend(self.completion())
            bridge.from_backend(self.completion())
        self.assertEqual(bridge.name,'work')
        self.assertEqual(sum(c.args[0]=='turn/start' for c in rpc.call_args_list),1)
        self.assertEqual(app.eligible_accounts(),[])
        self.assertEqual(app.choose_session(self.sid)['account'],'work')

    def test_generic_errors_do_not_switch(self):
        bridge=self.make_bridge()
        with patch.object(bridge,'failover') as failover:
            bridge.from_backend(self.completion('internalServerError'))
        failover.assert_not_called()

    def test_subagent_events_do_not_steal_main_thread_or_trigger_switch(self):
        bridge=self.make_bridge();other=str(uuid.uuid4())
        with patch.object(bridge,'failover') as failover:
            bridge.from_backend({'method':'thread/started','params':{'thread':{'id':other}}})
            bridge.from_backend(self.completion(sid=other))
        failover.assert_not_called()
        self.assertEqual(bridge.sid,self.sid)

    def test_waits_for_final_failed_turn_not_retry_notifications(self):
        bridge=self.make_bridge()
        with patch.object(bridge,'failover') as failover:
            bridge.from_backend({'method':'error','params':{'threadId':self.sid,'willRetry':True,
                    'error':{'codexErrorInfo':'usageLimitExceeded'}}})
        failover.assert_not_called()

    def test_known_reset_is_shared_but_sparse_metadata_does_not_clear_it(self):
        bridge=self.make_bridge();reset=time.time()+4000
        bridge.inspect({'method':'account/rateLimits/updated','params':{'rateLimits':{
            'limitId':'codex','primary':{'usedPercent':100,'resetsAt':reset}}}})
        bridge.inspect({'method':'account/rateLimits/updated','params':{'rateLimits':{
            'limitId':'codex','primary':None}}})
        app.mark_quota_failure('personal','usageLimitExceeded',bridge.snapshot)
        self.assertEqual(app.read_json(app.ROOT/'routing.json')['personal']['retry_after'],reset)
        self.assertEqual(app.eligible_accounts(),['work'])

    def test_selection_balances_active_sessions(self):
        app.atomic_json(app.ROOT/'runs/current.json',{'pid':os.getpid(),'account':'personal','session':self.sid})
        self.assertEqual(app.eligible_accounts()[0],'work')

    def test_cooldown_expiry_only_makes_account_eligible_for_retry(self):
        app.atomic_json(app.ROOT/'routing.json',{'personal':{'retry_after':time.time()-1}})
        self.assertIn('personal',app.eligible_accounts())

    def test_pending_requests_receive_error_during_switch(self):
        bridge=self.make_bridge();bridge.pending[json.dumps(55)]='account/read'
        with patch.object(bridge,'rpc',side_effect=self.fake_rpc):
            bridge.from_backend(self.completion())
        self.assertTrue(any(x.get('id')==55 and 'error' in x for x in bridge.ws.sent))

    def quota_policy(self, threshold=95):
        app.atomic_json(app.ROOT/'routing-policy.json', {'threshold':threshold})

    def usage(self, percent=0, allowed=True, secondary=None):
        return {'ordinaryUsageAllowed':allowed, 'rateLimits':{
            'primary':{'usedPercent':percent}, 'secondary':secondary,
            'credits':{'hasCredits':True, 'unlimited':True}}}

    def test_proactive_rotation_forwards_original_input_once_with_settings(self):
        bridge=self.make_bridge();original=bridge.backend
        bridge.thread_options.update(model='saved-model',sandbox='danger-full-access',approvalPolicy='never')
        self.quota_policy()
        message={'id':88,'method':'turn/start','params':{'threadId':self.sid,'model':'chosen-model',
            'effort':'high','input':[{'type':'text','text':'Exact original request'}]}}
        def rpc(method,params):
            if method=='account/rateLimits/read':return self.usage(97 if bridge.name=='personal' else 0)
            return self.fake_rpc(method,params)
        with patch.object(bridge,'rpc',side_effect=rpc) as calls, patch.object(app,'account_snapshot',return_value={'usage':self.usage()}):
            bridge.from_client(message)
        self.assertTrue(original.closed)
        self.assertEqual(bridge.name,'work')
        self.assertFalse(any(m.get('method')=='turn/start' for m in original.sent))
        self.assertEqual(bridge.backend.sent.count(message),1)
        self.assertEqual(sum(m.get('method')=='turn/start' for m in bridge.backend.sent),1)
        self.assertEqual(app.choose_session(self.sid)['account'],'work')
        resume=next(c.args[1] for c in calls.call_args_list if c.args[0]=='thread/resume')
        self.assertEqual(resume['sandbox'],'danger-full-access')
        self.assertEqual(resume['approvalPolicy'],'never')
        self.assertTrue(any(e['event']=='quota_rotation' for e in app.activity_records()))

    def test_unsaved_new_chat_accepts_first_turn_then_rotates_when_saved(self):
        bridge = self.make_bridge()
        original = bridge.backend
        self.path.unlink()  # Native thread/start can return before history exists.
        self.quota_policy()
        message = {'id': 88, 'method': 'turn/start', 'params': {
            'threadId': self.sid, 'input': [{'type': 'text', 'text': 'First request'}]}}

        def rpc(method, params):
            return self.usage(97) if method == 'account/rateLimits/read' else self.fake_rpc(method, params)

        with patch.object(bridge, 'rpc', side_effect=rpc), patch.object(
                app, 'account_snapshot', return_value={'usage': self.usage()}):
            bridge.from_client(message)
            self.assertEqual(original.sent, [message])
            self.assertFalse(original.closed)
            self.assertFalse(any('error' in m for m in bridge.ws.sent))
            self.assertFalse(any(e['event'] == 'quota_rotation' for e in app.activity_records()))

            # Once the first turn is saved, normal proactive routing still works.
            self.path.write_text(''.join(json.dumps(x) + '\n' for x in self.rows))
            bridge.inspect({'method': 'turn/completed', 'params': {
                'threadId': self.sid, 'turn': {'id': 'first', 'status': 'completed'}}})
            next_message = {'id': 89, 'method': 'turn/start', 'params': {
                'threadId': self.sid, 'input': [{'type': 'text', 'text': 'Next request'}]}}
            bridge.from_client(next_message)
        self.assertTrue(original.closed)
        self.assertEqual(bridge.name, 'work')
        self.assertEqual(original.sent, [message])
        self.assertEqual(bridge.backend.sent.count(next_message), 1)

    def test_credits_do_not_override_ordinary_quota_or_threshold(self):
        self.assertFalse(app.ordinary_quota_ready(self.usage(100),95))
        self.assertFalse(app.ordinary_quota_ready(self.usage(95),95))
        self.assertFalse(app.ordinary_quota_ready(self.usage(0,False),95))
        self.assertFalse(app.ordinary_quota_ready(self.usage(0,secondary={'usedPercent':97}),95))
        self.assertTrue(app.ordinary_quota_ready(self.usage(94),95))
        self.assertFalse(app.ordinary_quota_ready({},95))
        self.assertFalse(app.ordinary_quota_ready(self.usage(None),95))

    def test_all_accounts_over_threshold_continue_on_current_account(self):
        bridge=self.make_bridge();original=bridge.backend;self.quota_policy()
        with patch.object(bridge,'rpc',return_value=self.usage(97)), patch.object(app,'account_snapshot',return_value={'usage':self.usage(100)}):
            bridge.from_client({'id':7,'method':'turn/start','params':{'threadId':self.sid,'input':[]}})
        self.assertIs(bridge.backend,original)
        self.assertFalse(original.closed)
        self.assertEqual(sum(m.get('method')=='turn/start' for m in original.sent),1)
        self.assertFalse(any(m.get('id')==7 and 'error' in m for m in bridge.ws.sent))

    def test_unknown_limits_do_not_block_current_account(self):
        bridge=self.make_bridge();self.quota_policy()
        with patch.object(bridge,'rpc',side_effect=app.Error('offline')):
            bridge.from_client({'id':7,'method':'turn/start','params':{'threadId':self.sid}})
        self.assertEqual(len(bridge.backend.sent),1)
        self.assertFalse(any(m.get('id')==7 and 'error' in m for m in bridge.ws.sent))

    def test_missing_limits_do_not_trigger_a_migration(self):
        bridge=self.make_bridge();self.quota_policy();original=bridge.backend
        with patch.object(bridge,'rpc',return_value={}), patch.object(app,'account_snapshot') as lookup:
            bridge.from_client({'id':7,'method':'turn/start','params':{'threadId':self.sid}})
        lookup.assert_not_called()
        self.assertIs(bridge.backend,original)
        self.assertEqual(len(original.sent),1)

    def test_unknown_alternative_limits_leave_current_account_usable(self):
        bridge=self.make_bridge();self.quota_policy();original=bridge.backend
        with patch.object(bridge,'rpc',return_value=self.usage(97)), patch.object(app,'account_snapshot',side_effect=app.Error('offline')):
            bridge.from_client({'id':7,'method':'turn/start','params':{'threadId':self.sid}})
        self.assertIs(bridge.backend,original)
        self.assertEqual(len(original.sent),1)

    def test_exact_threshold_triggers_rotation(self):
        bridge=self.make_bridge();self.quota_policy()
        def rpc(method,params):
            return self.usage(95) if method=='account/rateLimits/read' else self.fake_rpc(method,params)
        with patch.object(bridge,'rpc',side_effect=rpc), patch.object(app,'account_snapshot',return_value={'usage':self.usage(94)}):
            bridge.from_client({'id':7,'method':'turn/start','params':{'threadId':self.sid}})
        self.assertEqual(bridge.name,'work')
        self.assertEqual(sum(m.get('method')=='turn/start' for m in bridge.backend.sent),1)

    def test_active_turn_is_never_interrupted_for_proactive_rotation(self):
        bridge=self.make_bridge();self.quota_policy();bridge.phase='working'
        with patch.object(bridge,'rpc') as rpc:
            bridge.from_client({'id':7,'method':'turn/start','params':{'threadId':self.sid}})
        rpc.assert_not_called()
        self.assertFalse(bridge.backend.closed)
        self.assertEqual(len(bridge.backend.sent),1)

    def test_eligible_current_account_keeps_backend_and_sends_request(self):
        bridge=self.make_bridge();self.quota_policy();original=bridge.backend
        message={'id':7,'method':'turn/start','params':{'threadId':self.sid}}
        with patch.object(bridge,'rpc',return_value=self.usage(94)), patch.object(app,'account_snapshot') as lookup:
            bridge.from_client(message)
        lookup.assert_not_called()
        self.assertIs(bridge.backend,original)
        self.assertEqual(original.sent,[message])

    def test_reactive_failover_can_use_credits_if_ordinary_quota_unavailable(self):
        bridge=self.make_bridge();self.quota_policy()
        with patch.object(app,'account_snapshot',return_value={'usage':self.usage(100)}), patch.object(bridge,'rpc',side_effect=self.fake_rpc) as rpc:
            bridge.from_backend(self.completion())
        self.assertTrue(rpc.called)
        self.assertEqual(bridge.name,'work')

    def test_quota_race_does_not_block_use_of_replacement_credits(self):
        bridge=self.make_bridge();self.quota_policy()
        with patch.object(bridge,'rpc',side_effect=lambda m,p:self.usage(100) if m=='account/rateLimits/read' else self.fake_rpc(m,p)), patch.object(app,'account_snapshot',return_value={'usage':self.usage(0)}):
            bridge.from_client({'id':7,'method':'turn/start','params':{'threadId':self.sid}})
        self.assertEqual(bridge.name,'work')
        self.assertTrue(any(m.get('method')=='turn/start' for m in bridge.backend.sent))
        self.assertFalse(any(m.get('id')==7 and 'error' in m for m in bridge.ws.sent))

    def test_routing_policy_can_be_configured_and_disabled(self):
        with contextlib.redirect_stdout(io.StringIO()):app.main(['routing','--threshold','90'])
        self.assertEqual(app.routing_threshold(),90)
        with contextlib.redirect_stdout(io.StringIO()):app.main(['routing','--off'])
        self.assertIsNone(app.routing_threshold())

class TransportTests(unittest.TestCase):
    def test_masked_fragmented_messages_and_ping(self):
        server,client=socket.socketpair();self.addCleanup(server.close);self.addCleanup(client.close)
        key=base64.b64encode(b'0123456789abcdef').decode()
        client.sendall(('GET / HTTP/1.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: '+key+'\r\n\r\n').encode())
        ws=app.WebSocket(server)
        self.assertIn(b'101 Switching Protocols',client.recv(4096))
        def frame(op,payload,final=True):
            mask=b'1234';return bytes([(0x80 if final else 0)|op,0x80|len(payload)])+mask+bytes(v^mask[i%4] for i,v in enumerate(payload))
        client.sendall(frame(1,b'{"id":',False)+frame(9,b'ping')+frame(0,b'7}'))
        self.assertEqual(ws.read(),[{'id':7}])
        self.assertEqual(client.recv(4096),b'\x8a\x04ping')
        ws.send({'ok':True})
        data=client.recv(4096)
        self.assertEqual(data[0],0x81)
        self.assertEqual(json.loads(data[2:]),{'ok':True})

if __name__=='__main__': unittest.main()
