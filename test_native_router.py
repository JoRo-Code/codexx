"""Native multi-client routing and disconnect/resume smoke test; no inference."""
import json, os, select, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path

binary=shutil.which('codex')
if not binary:raise SystemExit('Codex CLI required')
source=Path(__file__).with_name('codex-accounts').resolve()
proactive='--proactive' in sys.argv
live='--live' in sys.argv
with tempfile.TemporaryDirectory(prefix='car-',dir='/tmp') as tmp:
    root=Path(tmp);address=root/'r.sock';sid=str(uuid.uuid4())
    if live:
        installed=root/'installed/codex-accounts';installed.parent.mkdir()
        installed.write_text(source.read_text());source=installed
    if proactive:(root/'routing-policy.json').write_text(json.dumps({'threshold':95}))
    for account in ('a','b'):
        home=root/'accounts'/account;home.mkdir(parents=True)
        (home/'auth.json').write_text('{}')
    common=root/'common';common.mkdir()
    fixture=common/'mcp.py'
    fixture.write_text('''import json,sys
for line in sys.stdin:
    request=json.loads(line)
    if 'id' not in request: continue
    method=request.get('method')
    if method=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'shared','version':'1'}}
    elif method=='tools/list': result={'tools':[{'name':'shared_fixture','description':'Offline test','inputSchema':{'type':'object','properties':{}}}]}
    elif method=='resources/list': result={'resources':[]}
    elif method=='resources/templates/list': result={'resourceTemplates':[]}
    else: result={}
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
''')
    (common/'config.toml').write_text('[mcp_servers.shared_fixture]\ncommand='+json.dumps(sys.executable)+'\nargs=['+json.dumps(str(fixture))+']\n')
    (root/'shared.json').write_text(json.dumps({'enabled':True,'home':str(common)}))
    logs=root/'accounts/a/sessions/2026/09/29';logs.mkdir(parents=True)
    meta={'id':sid,'timestamp':'2026-09-29T12:00:00Z','cwd':tmp,'originator':'codex_cli_rs',
          'cli_version':'0.159.2','source':'cli','model_provider':'openai','base_instructions':{'text':'Test only.'}}
    (logs/('rollout-2026-09-29T12-00-00-'+sid+'.jsonl')).write_text(json.dumps({'timestamp':'2026-09-29T12:00:00Z','type':'session_meta','payload':meta})+'\n')
    with (logs/('rollout-2026-09-29T12-00-00-'+sid+'.jsonl')).open('a') as f:
        for kind,payload in [
            ('response_item',{'type':'message','role':'user','content':[{'type':'input_text','text':'Router history fixture'}]}),
            ('event_msg',{'type':'user_message','message':'Router history fixture','images':[]}),
            ('event_msg',{'type':'task_complete','turn_id':str(uuid.uuid4()),'last_agent_message':'Fixture complete'})]:
            f.write(json.dumps({'timestamp':'2026-09-29T12:00:00Z','type':kind,'payload':payload})+'\n')
    env=dict(os.environ,CODEX_ACCOUNTS_HOME=tmp,CODEX_ACCOUNTS_BINARY=binary,CODEX_ACCOUNTS_NO_UPDATE='1')
    env.pop('CODEX_THREAD_ID',None)
    logfile=(root/'router.log').open('w+')
    runner=root/'runner.py'
    runner.write_text("""
import importlib.machinery,importlib.util,json,sys,uuid
loader=importlib.machinery.SourceFileLoader('router_app',sys.argv[1])
spec=importlib.util.spec_from_loader(loader.name,loader)
app=importlib.util.module_from_spec(spec);loader.exec_module(app)
class FaultBackend(app.Backend):
    def __init__(self,name):
        super().__init__(name)
        with (app.ROOT/'backend-starts.jsonl').open('a') as f:
            f.write(json.dumps({'account':name,'pid':self.process.pid})+'\\n')
    def send(self,message):
        if message.get('method')=='account/read':
            self.messages.append({'id':message['id'],'result':{'account':{'type':'chatgpt','planType':'pro','email':'fixture@example.test'},'requiresOpenaiAuth':True}})
            return
        if message.get('method')=='account/rateLimits/read':
            self.messages.append({'id':message['id'],'result':{'ordinaryUsageAllowed':True,
                'rateLimits':{'primary':{'usedPercent':97 if self.name=='a' else 0},'credits':{'hasCredits':True},'planType':'pro'}}})
            return
        if message.get('method')=='turn/interrupt':
            self.messages.append({'id':message['id'],'result':{}})
            self.messages.append({'method':'turn/completed','params':{'threadId':message['params']['threadId'],
                'turn':{'id':'held-turn','status':'completed','items':[],'error':None}}})
            return
        if message.get('method')=='turn/start' and message['params']['input'][0]['text']=='Hold update test':
            turn={'id':'held-turn','status':'inProgress','items':[]}
            self.messages.append({'id':message['id'],'result':{'turn':turn}})
            self.messages.append({'method':'turn/started','params':{'threadId':message['params']['threadId'],'turn':turn}})
            return
        if message.get('method')!='turn/start': return super().send(message)
        with (app.ROOT/'sent-turns.jsonl').open('a') as f:
            f.write(json.dumps({'account':self.name,'input':message['params']['input']})+'\\n')
        # Inject protocol events only; never send inference requests.
        sid=message['params']['threadId'];turn={'id':str(uuid.uuid4()),'status':'inProgress','items':[]}
        self.messages.append({'id':message['id'],'result':{'turn':dict(turn)}})
        self.messages.append({'method':'turn/started','params':{'threadId':sid,'turn':dict(turn)}})
        if self.name=='a':
            turn.update(status='failed',error={'message':'Injected quota','codexErrorInfo':'usageLimitExceeded'})
        else: turn.update(status='completed',error=None)
        self.messages.append({'method':'turn/completed','params':{'threadId':sid,'turn':turn}})
app.MultiRouter.__init__.__defaults__=(None,FaultBackend)
app.account_snapshot=lambda name,**kwargs: {'usage':{'ordinaryUsageAllowed':True,
    'rateLimits':{'primary':{'usedPercent':97 if name=='a' else 0},'credits':{'hasCredits':True}}}}
app.serve_router(sys.argv[2], 'a')
""")
    server=subprocess.Popen(['python3',str(runner),str(source),str(address)],env=env,stderr=logfile)
    clients=[]
    class Client:
        def __init__(self):
            self.p=subprocess.Popen([binary,'app-server','proxy','--sock',str(address)],env=env,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            self.buf=b'';self.i=0;clients.append(self)
        def rpc(self,method,params):
            self.i+=1;i=self.i
            self.p.stdin.write((json.dumps({'id':i,'method':method,'params':params})+'\n').encode());self.p.stdin.flush()
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                while b'\n' in self.buf:
                    line,self.buf=self.buf.split(b'\n',1);result=json.loads(line)
                    if result.get('id')==i:
                        if 'error' in result:raise AssertionError(result['error'])
                        return result['result']
                if select.select([self.p.stdout],[],[],.2)[0]:
                    chunk=os.read(self.p.stdout.fileno(),65536)
                    if not chunk:raise AssertionError('Proxy disconnected')
                    self.buf+=chunk
            raise AssertionError('RPC timeout: '+method)
        def initialize(self):
            self.rpc('initialize',{'clientInfo':{'name':'native_router_test','version':'1'},'capabilities':{'experimentalApi':True}})
            self.p.stdin.write(b'{"method":"initialized"}\n');self.p.stdin.flush()
        def close(self):
            if self.p.poll() is None:
                self.p.terminate();self.p.wait(timeout=5)
    try:
        deadline=time.monotonic()+10
        while not address.exists() and time.monotonic()<deadline:time.sleep(.05)
        a=Client();a.initialize();b=Client();b.initialize()
        assert a.rpc('thread/resume',{'threadId':sid,'excludeTurns':True})['thread']['id']==sid
        other=b.rpc('thread/start',{'cwd':tmp,'experimentalRawEvents':False})['thread']['id']
        assert other!=sid
        assert set(a.rpc('thread/loaded/list',{})['data'])=={sid,other}
        a.close()
        c=Client();c.initialize()
        assert c.rpc('thread/resume',{'threadId':sid,'excludeTurns':True})['thread']['id']==sid
        assert b.rpc('thread/read',{'threadId':other})['thread']['id']==other
        assert any('shared_fixture' in row['tools'] for row in c.rpc('mcpServerStatus/list',{'threadId':sid,'serverName':'shared_fixture'})['data'])
        listed=c.rpc('thread/list',{'sourceKinds':['cli','appServer','vscode'],'limit':100})['data']
        assert sid in {r['id'] for r in listed}, listed
        if live:
            def markers():return [json.loads(p.read_text()) for p in (root/'runs').glob('router-*.json')]
            def wait_until(predicate):
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    if predicate():return
                    time.sleep(.05)
                raise AssertionError('Live update timed out: '+repr(markers()))
            c.rpc('turn/start',{'threadId':sid,'input':[{'type':'text','text':'Hold update test','text_elements':[]}]})
            wait_until(lambda:any(m['session']==sid and m['phase']=='working' for m in markers()))
            starts=(root/'backend-starts.jsonl').read_text()
            source.write_text(source.read_text().replace("VERSION = '0.13.0'", "VERSION = '0.13.1'")+'''
account_snapshot=lambda name,**kwargs: {'usage':{'ordinaryUsageAllowed':True,
    'rateLimits':{'primary':{'usedPercent':97 if name=='a' else 0},'credits':{'hasCredits':True}}}}
''')
            wait_until(lambda:any(m['session']==other and m['tracking_version']=='0.13.1' for m in markers()))
            assert any(m['session']==sid and m['tracking_version']=='0.13.0' and m['phase']=='working' for m in markers())
            assert (root/'backend-starts.jsonl').read_text()==starts
            assert b.rpc('thread/read',{'threadId':other})['thread']['id']==other
            c.rpc('turn/interrupt',{'threadId':sid,'turnId':'held-turn'})
            wait_until(lambda:any(m['session']==sid and m['tracking_version']=='0.13.1' for m in markers()))
            assert (root/'backend-starts.jsonl').read_text()==starts
            assert c.rpc('thread/read',{'threadId':sid})['thread']['id']==sid
            assert any('shared_fixture' in row['tools'] for row in c.rpc('mcpServerStatus/list',{'threadId':sid,'serverName':'shared_fixture'})['data'])
        c.rpc('turn/start',{'threadId':sid,'input':[{'type':'text','text':'Injected test only','text_elements':[]}]})
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            ownership=json.loads((root/'owners.json').read_text()) if (root/'owners.json').exists() else {}
            if ownership.get(sid)=='b':break
            time.sleep(.05)
        assert ownership.get(sid)=='b', ownership
        assert c.rpc('thread/read',{'threadId':sid})['thread']['id']==sid
        assert b.rpc('thread/read',{'threadId':other})['thread']['id']==other
        assert any('shared_fixture' in row['tools'] for row in c.rpc('mcpServerStatus/list',{'threadId':sid,'serverName':'shared_fixture'})['data'])
        for name in ('a','b'):
            assert (root/'accounts'/name/'auth.json').read_text()=='{}'
            assert (root/'accounts'/name/'config.toml').resolve()==(common/'config.toml').resolve()
        assert set(b.rpc('thread/loaded/list',{})['data'])=={sid,other}
        sent=[json.loads(line) for line in (root/'sent-turns.jsonl').read_text().splitlines()]
        if proactive:
            assert [row['account'] for row in sent]==['b'],sent
            assert sent[0]['input'][0]['text']=='Injected test only',sent
            print('PASS: proactive quota rotation before original input, native history and MCP tools preserved, second chat accessible; no model requests.')
        else:
            assert [row['account'] for row in sent]==['a','b'],sent
            print('PASS: native proxy, two chats, reconnect, combined history and shared MCP tools after injected quota failover; separate credentials, no model requests.')
        if live:print('PASS: installed file update during active turn; idle chat adopts immediately, active chat after completion; same clients and backend PIDs, shared MCP retained.')
    finally:
        for c in clients:c.close()
        server.terminate()
        try:server.wait(timeout=45)
        except subprocess.TimeoutExpired:server.kill();server.wait()
        logfile.seek(0)
        if server.returncode:print(logfile.read())
        logfile.close()
