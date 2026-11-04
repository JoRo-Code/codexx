import json
import os
import socket
import threading
import time
import uuid
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch
import test_launcher as fixtures
app=fixtures.app

class ReplyTransportTests(unittest.TestCase):
    def test_partial_write_disconnects_before_another_reply(self):
        for transport_class in (app.JsonSocket, app.WebSocket):
            with self.subTest(transport=transport_class.__name__):
                sender, peer = socket.socketpair()
                self.addCleanup(sender.close)
                self.addCleanup(peer.close)
                peer.settimeout(.2)

                class PartialSocket:
                    failed = False
                    def settimeout(self, value): sender.settimeout(value)
                    def shutdown(self, how): sender.shutdown(how)
                    def sendall(self, data):
                        if not self.failed:
                            self.failed = True
                            sender.sendall(data[:24])
                            raise socket.timeout('slow reader')
                        sender.sendall(data)

                transport = transport_class.__new__(transport_class)
                transport.sock = PartialSocket()
                client = app.RouterClient(transport)
                router = app.MultiRouter.__new__(app.MultiRouter)
                router.clients = {client}
                router.reply(client, {'id':1, 'result':{'history':'x'*1000}})
                wait_for(lambda: client.writer_stopped.is_set())
                # Pending requests can still refer to a disconnected client.
                router.reply(client, {'id':2, 'error':{'message':'retry'}})
                self.assertNotIn(client, router.clients)
                self.assertEqual(len(peer.recv(4096)), 24)
                self.assertEqual(peer.recv(4096), b'')

    def test_reply_extends_handshake_timeout_for_large_messages(self):
        sender, peer = socket.socketpair()
        self.addCleanup(sender.close)
        self.addCleanup(peer.close)
        sender.settimeout(5)
        client = app.RouterClient(app.JsonSocket(sender))
        self.addCleanup(lambda: app.RouterClient.close(client))
        router = app.MultiRouter.__new__(app.MultiRouter)
        router.clients = {client}
        router.reply(client, {'id':1, 'result':{}})
        self.assertEqual(sender.gettimeout(), 45)
        self.assertEqual(app.JsonSocket(peer).read(), [{'id':1, 'result':{}}])

    def test_slow_reader_cannot_block_other_clients_or_grow_an_unbounded_queue(self):
        sender, peer = socket.socketpair()
        self.addCleanup(sender.close)
        self.addCleanup(peer.close)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        client = app.RouterClient(app.JsonSocket(sender))
        self.addCleanup(lambda: app.RouterClient.close(client))
        router = app.MultiRouter.__new__(app.MultiRouter)
        router.clients = {client}
        router.mutex = threading.RLock()
        started = threading.Event()
        original = client.send
        def sending(message):
            started.set()
            original(message)
        client.send = sending
        with router.mutex:
            router.reply(client, {'id':1, 'result':{'history':'x'*(1024*1024)}})
        self.assertTrue(started.wait(1))
        healthy = Client()
        with router.mutex:
            router.reply(healthy, {'id':2, 'result':{'ready':True}})
        self.assertEqual(healthy.messages, [{'id':2, 'result':{'ready':True}}])
        for ident in range(3, 1100):
            with router.mutex:
                router.reply(client, {'id':ident, 'result':{}})
        self.assertTrue(client.writer_stopped.is_set())
        self.assertNotIn(client, router.clients)
        client.writer.join(timeout=1)
        self.assertFalse(client.writer.is_alive())

    def test_normal_notification_burst_preserves_order(self):
        sender, peer = socket.socketpair()
        self.addCleanup(sender.close)
        self.addCleanup(peer.close)
        peer.settimeout(2)
        client = app.RouterClient(app.JsonSocket(sender))
        self.addCleanup(lambda: app.RouterClient.close(client))
        router = app.MultiRouter.__new__(app.MultiRouter)
        router.clients = {client}
        for ident in range(256):
            router.reply(client, {'method':'item/agentMessage/delta', 'params':{'delta':str(ident)}})
        stream = peer.makefile('rb')
        self.addCleanup(stream.close)
        messages = [json.loads(stream.readline()) for _ in range(256)]
        self.assertEqual([m['params']['delta'] for m in messages], [str(i) for i in range(256)])
        self.assertFalse(client.writer_stopped.is_set())

    def test_disconnect_releases_writer_and_queued_replies(self):
        sender, peer = socket.socketpair()
        self.addCleanup(sender.close)
        self.addCleanup(peer.close)
        client = app.RouterClient(app.JsonSocket(sender))
        router = app.MultiRouter.__new__(app.MultiRouter)
        router.mutex = threading.RLock()
        router.clients = {client}
        router.client_initializes = {client:{}}
        router.requests = {}
        router.reply(client, {'id':1, 'result':{}})
        router.disconnect(client)
        client.writer.join(timeout=1)
        self.assertFalse(client.writer.is_alive())
        self.assertTrue(client.outbox.empty())

class Client:
    def __init__(self):self.messages=[]
    def send(self,message):self.messages.append(message)

class Backend:
    def __init__(self,name):
        self.name=name;self.sent=[];self.messages=deque();self.closed=False
        r,self.w=os.pipe();self.process=SimpleNamespace(stdout=os.fdopen(r,'rb',buffering=0))
    def send(self,message):
        self.sent.append(message)
        method=message.get('method');p=message.get('params') or {}
        if 'id' not in message or not method:return
        result={}
        if method in ('thread/start','thread/resume','thread/fork'):
            sid=p.get('threadId') if method=='thread/resume' else str(uuid.uuid4())
            result={'thread':{'id':sid,'ephemeral':p.get('ephemeral',False)}}
        elif method=='thread/list':
            result={'data':[dict(id=r['id'],createdAt=1) for r in app.session_records() if r['account']==self.name],'nextCursor':None}
        elif method=='thread/read':result={'thread':{'id':p['threadId']}}
        elif method=='account/read':result={'account':{'type':'chatgpt','planType':'pro'}}
        self.messages.append({'id':message['id'],'result':result})
    def read(self):pass
    def close(self):
        self.closed=True;self.process.stdout.close();os.close(self.w)

def wait_for(predicate):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.01)
    raise AssertionError('Timed out')

class RouterTests(unittest.TestCase):
    setUp=fixtures.LauncherTests.setUp
    def router(self):
        r=app.MultiRouter(factory=Backend);self.addCleanup(r.close)
        c=Client();r.route(c,{'id':1,'method':'initialize','params':{'clientInfo':{'name':'test','version':'1'}}})
        return r,c
    def request(self,r,c,method,params,ident=2):
        c.messages.clear();r.route(c,{'id':ident,'method':method,'params':params})
        wait_for(lambda:any(x.get('id')==ident for x in c.messages))
        return next(x for x in c.messages if x.get('id')==ident)
    def test_two_chats_isolate_quota_failure_and_preserve_settings(self):
        r,c=self.router()
        self.request(r,c,'thread/resume',{'threadId':self.sid,'model':'chosen','sandbox':'danger-full-access','approvalPolicy':'never'})
        first=r.sessions[self.sid];first.ready.result()
        sid2=str(uuid.uuid4());path2=self.path.with_name('rollout-'+sid2+'.jsonl')
        path2.write_text(self.path.read_text().replace(self.sid,sid2))
        self.request(r,c,'thread/resume',{'threadId':sid2})
        second=r.sessions[sid2];original=second.bridge.backend
        self.request(r,c,'turn/start',{'threadId':self.sid,'effort':'high','input':[]})
        first.bridge.backend.messages.append({'method':'turn/completed','params':{'threadId':self.sid,'turn':{'id':'t','status':'failed','error':{'codexErrorInfo':'usageLimitExceeded'}}}})
        wait_for(lambda:first.bridge.name=='work')
        wait_for(lambda:any(x.get('method')=='turn/start' for x in first.bridge.backend.sent))
        self.assertIs(second.bridge.backend,original);self.assertFalse(original.closed)
        self.assertEqual(app.choose_session(self.sid)['account'],'work')
        self.assertEqual(app.choose_session(sid2)['account'],'personal')
        resume=next(x for x in first.bridge.backend.sent if x.get('method')=='thread/resume')
        self.assertEqual(resume['params']['sandbox'],'danger-full-access')
        turn=next(x for x in first.bridge.backend.sent if x.get('method')=='turn/start')
        self.assertEqual(turn['params']['effort'],'high')
    def test_reconnect_keeps_backend_and_client_ids_do_not_collide(self):
        r,c=self.router();self.request(r,c,'thread/resume',{'threadId':self.sid})
        lane=r.sessions[self.sid]
        r.disconnect(c);self.assertFalse(lane.stopped.is_set())
        d=Client();r.route(d,{'id':1,'method':'initialize','params':{}})
        result=self.request(r,d,'thread/resume',{'threadId':self.sid})
        self.assertEqual(result['result']['thread']['id'],self.sid)
        self.assertIs(r.sessions[self.sid],lane)
        self.assertFalse(any(x.get('method')=='account/updated' for x in d.messages))
    def test_read_does_not_claim_conversation_and_two_clients_can_reuse_ids(self):
        r,c=self.router();d=Client();r.route(d,{'id':1,'method':'initialize','params':{}})
        for client in (c,d):r.route(client,{'id':42,'method':'thread/read','params':{'threadId':self.sid}})
        wait_for(lambda:all(any(x.get('id')==42 for x in cl.messages) for cl in (c,d)))
        self.assertNotIn(self.sid,r.sessions)
    def test_server_approval_ids_are_namespaced_and_replied_once(self):
        r,c=self.router();self.request(r,c,'thread/resume',{'threadId':self.sid});lane=r.sessions[self.sid]
        for _ in range(2):r.output(lane,{'id':1,'method':'item/commandExecution/requestApproval','params':{'threadId':self.sid}})
        ids=[x['id'] for x in c.messages if x.get('method')=='item/commandExecution/requestApproval']
        self.assertEqual(len(set(ids)),2)
        for _ in range(2):r.route(c,{'id':ids[0],'result':{'decision':'accept'}})
        wait_for(lambda:any(x.get('result') for x in lane.bridge.backend.sent))
        self.assertEqual(sum('result' in x for x in lane.bridge.backend.sent),1)
    def test_merged_history_cursor_and_expired_cursor(self):
        r,c=self.router()
        sid2=str(uuid.uuid4());dest=app.account_home('work')/'sessions'/('rollout-'+sid2+'.jsonl')
        dest.parent.mkdir();dest.write_text(self.path.read_text().replace(self.sid,sid2))
        one=self.request(r,c,'thread/list',{'limit':1})['result']
        two=self.request(r,c,'thread/list',{'limit':1,'cursor':one['nextCursor']})['result']
        self.assertEqual({one['data'][0]['id'],two['data'][0]['id']},{self.sid,sid2})
        with self.assertRaises(app.Error):r.thread_list({'cursor':'invalid'})
    def test_ephemeral_helper_uses_same_lane_without_replacing_root(self):
        r,c=self.router();self.request(r,c,'thread/resume',{'threadId':self.sid})
        lane=r.sessions[self.sid]
        result=self.request(r,c,'thread/fork',{'threadId':self.sid,'ephemeral':True})
        helper=result['result']['thread']['id']
        self.assertIs(r.sessions[helper],lane)
        self.assertEqual(lane.bridge.sid,self.sid)
    def test_each_chat_preserves_its_client_identity_and_paginated_capability(self):
        r,c=self.router()
        d=Client();r.route(d,{'id':1,'method':'initialize','params':{'clientInfo':{'name':'desktop','version':'2'}}})
        self.request(r,d,'thread/resume',{'threadId':self.sid})
        lane=r.sessions[self.sid]
        init=next(x for x in lane.bridge.backend.sent if x.get('method')=='initialize')
        self.assertEqual(init['params']['clientInfo']['name'],'desktop')
        self.assertTrue(init['params']['capabilities']['experimentalApi'])

    def test_auth_mutation_is_rejected(self):
        r,c=self.router()
        with self.assertRaises(app.Error):r.route(c,{'id':9,'method':'account/logout'})

if __name__=='__main__':unittest.main()
