"""Offline paginated migration check using the installed Codex app-server."""
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
    # A live CLI without a registered thread must not block unrelated moves.
    app.atomic_json(app.ROOT/'runs/unidentified.json',
                    dict(pid=os.getpid(), account='a', session=None))
    directory = app.account_home('a') / 'sessions/2026/09/29'
    directory.mkdir(parents=True)
    history = directory / f'rollout-2026-09-29T12-00-00-{sid}.jsonl'
    rows = [
        ('session_meta', {'id':sid,'timestamp':'2026-09-29T12:00:00Z','cwd':tmp,
                          'originator':'codex_cli_rs','cli_version':'0.134.0','source':'cli',
                          'history_mode':'paginated','model_provider':'openai','base_instructions':{'text':'Test only.'}}),
        ('event_msg', {'type':'task_started','turn_id':turn,'model_context_window':128000}),
        ('response_item', {'type':'message','role':'user','content':[{'type':'input_text','text':'Offline migration test.'}]}),
        ('event_msg', {'type':'item_completed','thread_id':sid,'turn_id':turn,'item':{'type':'UserMessage','id':'user-1','content':[{'type':'text','text':'Offline migration test.','text_elements':[]}]}}),
        ('response_item', {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Preserved response.'}]}),
        ('event_msg', {'type':'item_completed','thread_id':sid,'turn_id':turn,'item':{'type':'AgentMessage','id':'agent-1','content':[{'type':'Text','text':'Preserved response.'}],'phase':'final_answer'}}),
        ('event_msg', {'type':'task_complete','turn_id':turn,'last_agent_message':'Preserved response.'}),
    ]
    history.write_text(''.join(json.dumps(dict(timestamp='2026-09-29T12:00:00Z',ordinal=i,type=t,payload=p))+'\n' for i,(t,p) in enumerate(rows)))
    env = app.environment('a')
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
    def initialize():
        rpc(1, 'initialize', {'clientInfo':{'name':'launcher_test','version':'0.1.0'},
                              'capabilities':{'experimentalApi':True}})
        proc.stdin.write(b'{"method":"initialized"}\n'); proc.stdin.flush()
    def hydrate(expected):
        thread = rpc(2,'thread/resume',{'threadId':sid,'excludeTurns':True,'cwd':tmp})['thread']
        assert thread['id'] == sid
        # A native Codex writer must prevent the launcher from copying this chat.
        try:
            with app.history_writer_lock(Path(env['CODEX_HOME']), sid): pass
        except app.Error: pass
        else: raise AssertionError('Native writer lock was not respected')
        if expected == 1 and env['CODEX_HOME'] == str(app.account_home('a')):
            try:
                app.move(app.choose_session(sid), 'b')
            except app.Error as exc:
                assert 'Codex is using this conversation' in str(exc), str(exc)
            else: raise AssertionError('Move accepted an unidentified active native writer')
        cursor = None
        serialized = ''
        count = 0
        while True:
            params = {'threadId':sid,'limit':1,'itemsView':'full'}
            if cursor: params['cursor'] = cursor
            result = rpc(10+count,'thread/turns/list',params)
            serialized += json.dumps(result['data'])
            count += len(result['data'])
            cursor = result.get('nextCursor')
            if not cursor: break
        assert count == expected, (count,expected)
        assert 'Offline migration test.' in serialized
        assert 'Preserved response.' in serialized
        if expected == 2: assert 'Second turn preserved.' in serialized
    def stop():
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill();proc.wait()
    try:
        initialize(); hydrate(1)
    finally: stop()
    # Destination starts with no index. Codex must rebuild it from the copied log.
    local_row = app.local_chats(app.account_home('a'))[0]
    with contextlib.redirect_stdout(io.StringIO()):
        app.move(local_row, 'b', source_home=app.account_home('a'))
    auth.unlink()
    env = app.environment('b')
    proc = subprocess.Popen(['codex','app-server'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,env=env)
    pending.clear()
    try:
        initialize(); hydrate(1)
    finally: stop()
    # Add a completed turn while stopped, then return to A's existing stale index.
    second = str(uuid.uuid4())
    destination = Path(app.choose_session(sid)['path'])
    more = [
        ('event_msg', {'type':'task_started','turn_id':second}),
        ('response_item', {'type':'message','role':'user','content':[{'type':'input_text','text':'Second turn preserved.'}]}),
        ('event_msg', {'type':'item_completed','thread_id':sid,'turn_id':second,'item':{'type':'UserMessage','id':'user-2','content':[{'type':'text','text':'Second turn preserved.','text_elements':[]}]}}),
        ('event_msg', {'type':'task_complete','turn_id':second,'last_agent_message':''}),
    ]
    with destination.open('a') as f:
        for i,(t,payload) in enumerate(more, start=len(destination.read_text().splitlines())): f.write(json.dumps({'ordinal':i,'timestamp':'2026-09-29T12:01:00Z','type':t,'payload':payload})+'\n')
    (app.account_home('a')/'auth.json').write_text('{}')
    with contextlib.redirect_stdout(io.StringIO()): app.move(app.choose_session(sid), 'a')
    (app.account_home('a')/'auth.json').unlink()
    assert history.read_bytes() == destination.read_bytes()
    env = app.environment('a')
    proc = subprocess.Popen(['codex','app-server'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,env=env)
    pending.clear()
    try:
        initialize(); hydrate(2)
        fork = rpc(80, 'thread/fork', {'threadId':sid, 'excludeTurns':True})['thread']['id']
        nested = rpc(81, 'thread/fork', {'threadId':fork, 'excludeTurns':True})['thread']['id']
        for check_id in (fork, nested):
            inherited = rpc(82, 'thread/turns/list', {'threadId':check_id, 'limit':100, 'itemsView':'full'})
            assert len(inherited['data']) == 2, ('source fork', check_id, inherited)
        print('PASS: paginated history resumes A -> B -> A, rebuilds stale indexes, preserves both turns, and respects native writer locks. No model request.')
    finally: stop()

    # Native forks use history_base references, not self-contained histories.
    # Move to a fresh account with no ancestor files or database entries.
    sid = nested
    app.private_dir(app.account_home('c'))
    auth = app.account_home('c') / 'auth.json'
    auth.write_text('{}')
    with contextlib.redirect_stdout(io.StringIO()): app.move(app.choose_session(sid), 'c')
    auth.unlink()
    env = app.environment('c')
    proc = subprocess.Popen(['codex','app-server'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,env=env)
    pending.clear()
    try:
        initialize()
        hydrate(2)
        print('PASS: native nested fork migrates to an empty account and preserves inherited turns. No model request.')
    finally: stop()
