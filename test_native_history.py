"""Offline compatibility check: real Codex must discover a launcher-moved history."""
import contextlib
import io
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import time
import uuid
from test_launcher import app

if not shutil.which('codex'):
    raise SystemExit('Codex CLI is required for this optional compatibility test.')
with tempfile.TemporaryDirectory() as tmp:
    app.ROOT = Path(tmp)
    sid = str(uuid.uuid4())
    turn = str(uuid.uuid4())
    for name in ('a', 'b'):
        app.private_dir(app.account_home(name))
    # move requires a connected destination; remove the placeholder before starting Codex.
    auth = app.account_home('b') / 'auth.json'
    auth.write_text('{}')
    directory = app.account_home('a') / 'sessions/2026/09/29'
    directory.mkdir(parents=True)
    history = directory / f'rollout-2026-09-29T12-00-00-{sid}.jsonl'
    rows = [
        ('session_meta', {'id':sid,'timestamp':'2026-09-29T12:00:00Z','cwd':tmp,
                          'originator':'codex_cli_rs','cli_version':'0.134.0','source':'cli',
                          'model_provider':'openai','base_instructions':{'text':'Test only.'}}),
        ('event_msg', {'type':'task_started','turn_id':turn,'model_context_window':128000}),
        ('response_item', {'type':'message','role':'user','content':[{'type':'input_text','text':'Offline migration test.'}]}),
        ('event_msg', {'type':'user_message','message':'Offline migration test.','images':[]}),
        ('response_item', {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Preserved response.'}]}),
        ('event_msg', {'type':'agent_message','message':'Preserved response.'}),
        ('event_msg', {'type':'task_complete','turn_id':turn,'last_agent_message':'Preserved response.'}),
    ]
    history.write_text(''.join(json.dumps(dict(timestamp='2026-09-29T12:00:00Z',type=t,payload=p))+'\n' for t,p in rows))
    with contextlib.redirect_stdout(io.StringIO()):
        app.move(app.choose_session(sid), 'b')
    auth.unlink()
    env = app.environment('b')
    proc = subprocess.Popen(['codex','app-server'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,env=env)
    pending = bytearray()
    def rpc(n, method, params):
        proc.stdin.write((json.dumps(dict(id=n,method=method,params=params))+'\n').encode())
        proc.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            while b'\n' in pending:
                line, _, rest = pending.partition(b'\n')
                pending[:] = rest
                result = json.loads(line)
                if result.get('id') == n:
                    if 'error' in result:
                        raise RuntimeError(result['error'])
                    return result['result']
            if select.select([proc.stdout], [], [], .5)[0]:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('Codex exited before responding')
                pending.extend(chunk)
        raise RuntimeError('Codex RPC timed out')
    try:
        rpc(1, 'initialize', {'clientInfo':{'name':'launcher_test','version':'0.1.0'}})
        proc.stdin.write(b'{"method":"initialized"}\n'); proc.stdin.flush()
        thread = rpc(2,'thread/read',{'threadId':sid,'includeTurns':True})['thread']
        assert thread['id'] == sid
        assert Path(thread['path']).read_bytes() == history.read_bytes()
        serialized = json.dumps(thread['turns'])
        assert 'Offline migration test.' in serialized, serialized
        assert 'Preserved response.' in serialized, serialized
        rpc(3, 'thread/resume', {'threadId': sid, 'cwd': tmp})
        try:
            with app.history_writer_lock(app.account_home('b'), sid): pass
        except app.Error: pass
        else: raise AssertionError('Native legacy writer lock was not respected')
        print('PASS: real Codex reads migrated legacy history and holds its native writer lock. No model request.')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait()
