import http.client
import json
import concurrent.futures
import os
import threading
import unittest
from unittest.mock import patch
import test_launcher as fixtures
app=fixtures.app

class WindowTests(unittest.TestCase):
    setUp=fixtures.LauncherTests.setUp
    def server(self):
        self.window=app.SetupWindow()
        server=app.setup_http_server(self.window)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return server
    def request(self,server,path='/api/state',method='GET',headers=None,body=None):
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
        conn.request(method,path,body=body,headers=headers or {})
        r=conn.getresponse();data=r.read();conn.close();return r.status,data
    def test_auth_host_and_origin_are_required(self):
        s=self.server();auth={'Authorization':'Bearer '+self.window.token}
        self.assertEqual(self.request(s)[0],403)
        self.assertEqual(self.request(s,headers=auth)[0],200)
        self.assertEqual(self.request(s,headers=dict(auth,Host='evil.example'))[0],403)
        self.assertEqual(self.request(s,headers=dict(auth,Origin='https://evil.example'))[0],403)
        self.assertEqual(self.request(s,method='POST',path='/api/action',body='{"action":"connect"}')[0],403)
    def test_repeated_and_simultaneous_opens_reuse_one_server(self):
        server=self.server()
        record={'port':server.server_port,'token':self.window.token}
        def launch(*args,**kwargs):
            app.atomic_json(app.setup_server_record(),record)
            return unittest.mock.Mock()
        with patch.object(app.sys,'platform','darwin'), patch.object(app.subprocess,'Popen',side_effect=launch) as launch_process, patch('webbrowser.open',return_value=True) as browser:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                results=list(executor.map(lambda _:app.open_setup_window(),range(4)))
            self.assertEqual(results,[0]*4)
            self.assertEqual(launch_process.call_count,1)
            self.assertEqual(browser.call_count,4)
            self.assertEqual(len({call.args[0] for call in browser.call_args_list}),1)
        self.assertEqual(os.stat(app.setup_server_record()).st_mode & 0o777,0o600)

    def test_stale_or_invalid_server_record_is_not_reused(self):
        app.atomic_json(app.setup_server_record(),{'port':0,'token':'bad'})
        self.assertIsNone(app.existing_setup_server())
        server=self.server()
        app.atomic_json(app.setup_server_record(),{'port':server.server_port,'token':'wrong'})
        self.assertIsNone(app.existing_setup_server())
        app.setup_server_record().write_text('not json')
        self.assertIsNone(app.existing_setup_server())

    def test_options_are_authenticated_and_shared_without_interrupting_jobs(self):
        server=self.server();auth={'Authorization':'Bearer '+self.window.token}
        self.assertEqual(self.request(server,'/api/health')[0],403)
        self.assertEqual(self.request(server,'/api/open','POST',body='{}')[0],403)
        code,_=self.request(server,'/api/open','POST',auth,json.dumps({'account':'work','port':22284}))
        self.assertEqual(code,200);self.assertEqual((self.window.account,self.window.port),('work',22284))
        self.window.job=dict(busy=True,error=False,message='Signing in from another tab')
        code,_=self.request(server,'/api/open','POST',auth,'{}')
        self.assertEqual(code,200)
        code,_=self.request(server,'/api/open','POST',auth,json.dumps({'account':'personal'}))
        self.assertEqual(code,409);self.assertEqual(self.window.account,'work')
        self.assertEqual(self.request(server,'/api/open','POST',auth,'{"port":true}')[0],400)
        with patch.object(self.window,'work') as work:
            self.request(server,'/api/action','POST',auth,'{"action":"add","label":"extra"}')
            work.assert_not_called()
        _,body=self.request(server,headers=auth)
        self.assertEqual(json.loads(body)['job']['message'],'Signing in from another tab')

    def test_only_known_actions_and_valid_labels(self):
        s=self.server();auth={'Authorization':'Bearer '+self.window.token}
        for data in ({'action':'shell'},{'action':'add','label':'../escape'},['connect']):
            code,body=self.request(s,'/api/action','POST',auth,json.dumps(data))
            self.assertIn(code,(200,400))
            if code==200:self.assertTrue(json.loads(body)['job']['error'])
        self.assertFalse(self.window.job['busy'])
    def test_page_has_no_secrets_and_state_does_not_claim_desktop_linked(self):
        s=self.server();code,body=self.request(s,'/')
        self.assertEqual(code,200);self.assertNotIn(self.window.token.encode(),body)
        state=self.window.snapshot()
        self.assertFalse(state['ready']);self.assertNotIn('test_credentials',json.dumps(state))
        self.assertEqual(len(state['accounts']),2)
    def test_connect_uses_terminal_subprocess_and_failure_is_visible(self):
        w=app.SetupWindow(account='work',port=22284)
        with patch.object(app.subprocess,'Popen') as popen:
            popen.return_value.wait.return_value=1
            w.work('connect','')
        args=popen.call_args[0][0]
        self.assertEqual(args[-6:],['setup','--terminal','--account','work','--port','22284'])
        self.assertTrue(w.job['error']);self.assertFalse(w.job['busy'])
    def test_add_uses_generated_label_and_preserves_existing_accounts(self):
        w=app.SetupWindow()
        with patch.object(app.subprocess,'Popen') as popen,patch.object(app,'collect_status',return_value=[]):
            popen.return_value.wait.return_value=0
            w.work('add','')
        self.assertEqual(popen.call_args[0][0][-2:],['add','account-1'])
        with patch.object(app.subprocess,'Popen') as popen:w.work('add','personal')
        popen.assert_not_called();self.assertTrue(w.job['error'])
    def test_activity_api_requires_authentication(self):
        server=self.server()
        self.assertEqual(self.request(server,'/api/activity')[0],403)
        with patch.object(app,'dashboard_activity',return_value={'chats':[]}):
            code,body=self.request(server,'/api/activity',headers={'Authorization':'Bearer '+self.window.token})
        self.assertEqual(code,200);self.assertEqual(json.loads(body),{'chats':[]})
    def test_usage_history_is_persistent_and_deduplicates_recent_samples(self):
        reports=[dict(label='personal',checked_at=app.time.time(),usage=None,identity=('private@example.com','plan','cached'))]
        app.save_usage_observations(reports);app.save_usage_observations(reports)
        with app.sqlite3.connect(app.ROOT/'usage.sqlite') as db:
            rows=db.execute('SELECT * FROM samples').fetchall()
        self.assertEqual(len(rows),1)
        self.assertNotIn('private@example.com',str(rows))
        self.assertEqual(rows[0][2],'Limits unavailable')
    def test_dashboard_rejects_reused_pid_and_keeps_switch_direction(self):
        row=dict(id=self.sid,title='Example',cwd='/project',modified=1,stored_account='work',observed_accounts=['personal','work'],running=[dict(pid=123,account='personal',phase='working')])
        overview=dict(conversations=[row],unassigned_launches=[],local_unknown_count=2)
        app.record_activity(self.sid,'work','auto_switched',previous='personal')
        process=app.subprocess.CompletedProcess([],0,'123 /usr/bin/unrelated-process')
        with patch.object(app,'conversation_overview',return_value=overview),patch.object(app.subprocess,'run',return_value=process):
            data=app.dashboard_activity()
        self.assertFalse(data['chats'][0]['live'])
        self.assertEqual(data['chats'][0]['account'],'work')
        self.assertEqual(data['events'][0]['previous_account'],'personal')
        self.assertEqual(data['events'][0]['account'],'work')
    def test_parser_keeps_terminal_option_and_hides_internal_server(self):
        self.assertTrue(app.parser().parse_args(['setup','--terminal']).terminal)
        self.assertNotIn('setup-window',app.parser().format_help())

if __name__=='__main__':unittest.main()
