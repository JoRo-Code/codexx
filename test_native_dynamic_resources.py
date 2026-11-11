"""Native dynamic-tool persistence regression using a localhost fake provider.

Run explicitly: python3 test_native_dynamic_resources.py
No credentials or external model requests are used.
"""
import http.server
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest


class NativeClient:
    def __init__(self, binary, home):
        env = dict(os.environ, CODEX_HOME=str(home))
        for key in ('CODEX_THREAD_ID', 'OPENAI_API_KEY', 'HTTP_PROXY', 'HTTPS_PROXY',
                    'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            env.pop(key, None)
        self.process = subprocess.Popen(
            [binary, 'app-server'], env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        self.buffer = b''
        self.ident = 0
        self.notifications = []

    def read(self, deadline):
        while time.monotonic() < deadline:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                return json.loads(line)
            if select.select([self.process.stdout], [], [], .1)[0]:
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise AssertionError('Native backend closed its output')
                self.buffer += chunk
        raise AssertionError('Native backend response timed out')

    def rpc(self, method, params):
        self.ident += 1
        self.process.stdin.write((json.dumps(
            {'id': self.ident, 'method': method, 'params': params})+'\n').encode())
        self.process.stdin.flush()
        deadline = time.monotonic()+20
        while True:
            response = self.read(deadline)
            if response.get('id') == self.ident:
                if 'error' in response:
                    raise AssertionError(response['error'])
                return response['result']
            self.notifications.append(response)

    def initialize(self):
        self.rpc('initialize', {'clientInfo': {'name': 'offline_dynamic_resources', 'version': '1'},
                                'capabilities': {'experimentalApi': True}})
        self.process.stdin.write(b'{"method":"initialized"}\n')
        self.process.stdin.flush()

    def finish_turn(self, sid):
        deadline = time.monotonic()+20
        while True:
            response = self.notifications.pop(0) if self.notifications else self.read(deadline)
            if response.get('method') == 'turn/completed' and response['params']['threadId'] == sid:
                return

    def close(self):
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        finally:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)
            self.process.stdin.close()
            self.process.stdout.close()


class NativeDynamicResourcesTests(unittest.TestCase):
    def test_native_resume_restores_function_and_namespace_tools(self):
        binary = os.environ.get('CODEX_ACCOUNTS_BINARY') or shutil.which('codex')
        if not binary:
            self.skipTest('Native Codex CLI is required')
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Offline fixture: no inference"}}')

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='codexx-dynamic-resources-') as directory:
                home = Path(directory)
                (home/'config.toml').write_text(
                    'model_provider="fixture"\nmodel="gpt-5.1-codex"\n'
                    '[model_providers.fixture]\nname="Offline fixture"\n'
                    f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
                    'wire_api="responses"\nrequires_openai_auth=false\n'
                    'request_max_retries=0\nstream_max_retries=0\n')
                tool = {'type': 'function', 'name': 'offline_probe', 'description': 'Fixture',
                        'inputSchema': {'type': 'object', 'properties': {}}}
                tools = [tool, {'type': 'namespace', 'name': 'offline_namespace',
                                'description': 'Fixture namespace', 'tools': [dict(tool)]}]
                client = NativeClient(binary, home)
                try:
                    client.initialize()
                    sid = client.rpc('thread/start', {'cwd': directory, 'dynamicTools': tools})['thread']['id']
                    client.rpc('turn/start', {'threadId': sid, 'input': [
                        {'type': 'text', 'text': 'Offline fixture only', 'text_elements': []}]})
                    client.finish_turn(sid)
                finally:
                    client.close()
                self.assertEqual(len(requests), 1)
                first = [t for t in requests[0]['tools'] if t.get('name', '').startswith('offline_')]
                self.assertEqual({t['name'] for t in first}, {'offline_probe', 'offline_namespace'})
                files = list((home/'sessions').rglob('*.jsonl'))
                metadata = []
                for path in files:
                    with path.open() as stream:
                        record = json.loads(stream.readline())
                    if record.get('type') == 'session_meta' and record['payload']['id'] == sid:
                        metadata.append(record['payload'])
                self.assertEqual(len(metadata), 1)
                self.assertEqual([t['name'] for t in metadata[0]['dynamic_tools']],
                                 ['offline_probe', 'offline_namespace'])
                client = NativeClient(binary, home)
                try:
                    client.initialize()
                    # Resume has no dynamicTools parameter: the native runtime
                    # must recover the exact capability schemas from saved history.
                    self.assertEqual(client.rpc('thread/resume', {'threadId': sid})['thread']['id'], sid)
                    client.rpc('turn/start', {'threadId': sid, 'input': [
                        {'type': 'text', 'text': 'Offline resume fixture', 'text_elements': []}]})
                    client.finish_turn(sid)
                finally:
                    client.close()
                self.assertEqual(len(requests), 2)
                restored = [t for t in requests[1]['tools'] if t.get('name', '').startswith('offline_')]
                self.assertEqual(restored, first)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
