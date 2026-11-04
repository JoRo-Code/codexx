"""Large fragmented replies must not stall the router's actor loop."""
from collections import deque
import json
import os
import queue
import select
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from test_launcher import app


class BackendIOTests(unittest.TestCase):
    def test_large_plugin_reply_wakes_an_already_running_idle_actor(self):
        size = 28*1024*1024
        child = subprocess.Popen([sys.executable, '-c',
            'import os,json\n'
            'data=(json.dumps({"id":1,"result":{"plugins":"x"*'+str(size)+'}})+"\\n").encode()\n'
            'for i in range(0,len(data),65536): os.write(1,data[i:i+65536])\n'],
            stdout=subprocess.PIPE)
        backend = app.Backend.__new__(app.Backend)
        backend.process = child
        backend.buffer = bytearray()
        backend.messages = deque()
        backend.router_inbox = queue.Queue(maxsize=1024)
        started = time.monotonic()
        try:
            while not backend.messages and time.monotonic()-started < 3:
                # The loop that is already running cannot change during a live
                # update. Its original 200ms inbox wait must be woken by read().
                try: backend.router_inbox.get(timeout=.2)
                except queue.Empty: pass
                if select.select([child.stdout], [], [], .02)[0]: backend.read()
            self.assertTrue(backend.messages, 'Large plugin reply stalled the idle actor')
            self.assertEqual(len(backend.messages.popleft()['result']['plugins']), size)
        finally:
            if child.poll() is None: child.terminate()
            child.wait(timeout=5)
            child.stdout.close()

    def test_json_socket_only_searches_new_fragment_bytes_for_a_delimiter(self):
        class Buffer(bytearray):
            searches = []
            def find(self, value, start=0):
                self.searches.append(start)
                return super().find(value, start)
        class Socket:
            chunks = iter([b'{"id":1,"result":"large ', b'reply"}\n{"id":2}\n{"id":', b'3}\n'])
            def recv(self, size): return next(self.chunks)
        transport = app.JsonSocket(Socket())
        transport.buffer = Buffer()
        self.assertEqual(transport.read(), [])
        self.assertEqual(transport.read(), [{'id':1,'result':'large reply'}, {'id':2}])
        self.assertEqual(transport.buffer.searches[1], len(b'{"id":1,"result":"large '))
        self.assertEqual(transport.read(), [{'id':3}])
        self.assertFalse(transport.buffer)

    def test_fragmented_history_reply_is_drained_without_per_fragment_delay(self):
        child = subprocess.Popen([sys.executable, '-c',
            'import os,json\n'
            'data=(json.dumps({"id":1,"result":{"history":"x"*(4*1024*1024)}})+"\\n").encode()\n'
            'for i in range(0,len(data),128): os.write(1,data[i:i+128])\n'],
            stdout=subprocess.PIPE)
        backend = app.Backend.__new__(app.Backend)
        backend.process = child
        backend.buffer = bytearray()
        backend.messages = deque()
        started = time.monotonic()
        try:
            while not backend.messages and time.monotonic()-started < 3:
                # Match the actor's wait for client input between reads.
                time.sleep(.02)
                if select.select([child.stdout], [], [], .02)[0]: backend.read()
            self.assertTrue(backend.messages, 'Fragmented history stalled behind actor polling')
            message = backend.messages.popleft()
            self.assertEqual(message['id'], 1)
            self.assertEqual(len(message['result']['history']), 4*1024*1024)
            self.assertFalse(backend.buffer)
        finally:
            if child.poll() is None: child.terminate()
            child.wait(timeout=5)
            child.stdout.close()

    def test_busy_writer_yields_to_other_actor_work(self):
        backend = app.Backend.__new__(app.Backend)
        backend.process = type('Process', (), {'stdout':sys.stdout})()
        backend.buffer = bytearray()
        backend.messages = deque()
        with patch.object(app.os, 'read', return_value=b'x'*65536) as read, \
             patch.object(app.select, 'select', return_value=([sys.stdout],[],[])), \
             patch.object(app.time, 'monotonic', return_value=0):
            backend.read()
        self.assertEqual(read.call_count, 16)
        self.assertEqual(len(backend.buffer), 1024*1024)

    def test_delivers_last_response_before_reporting_eof(self):
        r,w = os.pipe()
        backend = app.Backend.__new__(app.Backend)
        backend.process = type('Process', (), {'stdout':os.fdopen(r,'rb')})()
        backend.buffer = bytearray()
        backend.messages = deque()
        os.write(w,b'{"id":7,"result":{}}\n');os.close(w)
        try:
            backend.read()
            self.assertEqual(backend.messages.popleft()['id'],7)
            with self.assertRaises(app.Error): backend.read()
        finally: backend.process.stdout.close()


if __name__ == '__main__': unittest.main()
