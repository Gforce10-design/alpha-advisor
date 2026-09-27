import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name); scripts=self.home/'skills/advisor/scripts';scripts.mkdir(parents=True)
        for name in ('advisor.py','runner.py'):shutil.copyfile(Path(__file__).with_name(name),scripts/name)
        self.cli=scripts/'advisor.py';(self.home/'agents').mkdir()
        (self.home/'config.toml').write_text('model="conductor"\nmodel_reasoning_effort="medium"')
        (self.home/'agents/astra_advisor.toml').write_text('model="advisor"\nmodel_reasoning_effort="medium"\nsandbox_mode="read-only"\ndeveloper_instructions="no tools"')
        self.brief=self.home/'brief.txt';self.brief.write_text('review duplicate id contract')
        self.decision=self.home/'decision.json';self.ledger=self.home/'advisor-ledger.json'
        self.decision.write_text(json.dumps(dict(schema_version=2,task_id='trial',decision_id='d',brief_file=str(self.brief),evidence_files=[],failed_repair_files=[],same_cause_failed_repairs=0,explicit_request=False,unresolved_evidence_conflict=True,architecture_change=False,hard_to_reverse_action=False)))
        self.bin=self.home/'bin';self.bin.mkdir();self.fake=self.bin/'codex';self.env=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'])
        self.env.pop('ADVISOR_AUTH_CODEX_HOME', None)
        self.env.pop('CODEX_HOME', None)
        self.fake_code()
    def fake_code(self, mode='ok'):
        self.fake.write_text('#!'+sys.executable+'\n'+f'''import json,sys,time
from pathlib import Path
mode={mode!r}
sys.stdin.read()
if mode=='timeout':time.sleep(30)
p=Path(sys.argv[sys.argv.index('--output-last-message')+1])
if mode=='authhome':(p.parent/'authhome.txt').write_text(__import__('os').environ.get('CODEX_HOME',''))
p.write_text('advice' if mode!='mismatch' else 'different')
print(json.dumps({{'type':'thread.started','thread_id':'test-thread'}}))
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'advice'}}}}))
if mode=='tool':print(json.dumps({{'type':'item.completed','item':{{'type':'command_execution'}}}}))
if mode!='missing':print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':100,'cached_input_tokens':20,'output_tokens':10}}}}))
if mode=='nonzero':sys.exit(1)
''');self.fake.chmod(0o700)
    def call(self,*extra):
        p=subprocess.run([sys.executable,str(self.cli),'run',str(self.decision),*extra],env=self.env,capture_output=True,text=True,timeout=15)
        self.assertNotIn('Traceback',p.stderr)
        return json.loads(p.stdout)
    def test_single_command_completion_and_fresh_reuse(self):
        first=self.call();self.assertEqual(first['action'],'COMPLETED');self.assertEqual(first['usage']['input_tokens'],100)
        self.assertEqual(self.call()['action'],'REUSE')
        state=json.loads(self.ledger.read_text());self.assertEqual(len(state['attempts']),1)
        launch=json.loads((self.home/'advisor-runs'/first['request_id']/'launch.json').read_text())
        self.assertIn('read-only',launch['argv']);self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',launch['argv'])
    def test_active_codex_home_is_used_when_skill_is_installed_elsewhere(self):
        selected=self.home/'active-login';selected.mkdir()
        (selected/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt'}))
        self.env['CODEX_HOME']=str(selected)
        self.fake_code('authhome')
        out=self.call();self.assertEqual(out['action'],'COMPLETED')
        run=self.home/'advisor-runs'/out['request_id']
        launch=json.loads((run/'launch.json').read_text())
        self.assertEqual(launch['credential_home'],str(selected.resolve()))
        self.assertEqual((run/'authhome.txt').read_text(),str(selected.resolve()))
        self.assertEqual(json.loads(self.ledger.read_text())['attempts'][0]['binding']['advisor']['model'],'advisor')
    def test_credential_diagnostic_reports_effective_home_without_reserving(self):
        selected=self.home/'active-login';selected.mkdir()
        (selected/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt','secret':'do-not-output'}))
        self.env['CODEX_HOME']=str(selected)
        result=subprocess.run([sys.executable,str(self.cli),'credentials'],env=self.env,
                              capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout),{'credential_home':str(selected.resolve()),'source':'CODEX_HOME'})
        self.assertNotIn('do-not-output',result.stdout)
        self.assertFalse(self.ledger.exists())
    def test_invalid_active_home_does_not_fall_back_to_install_account(self):
        self.env['CODEX_HOME']=str(self.home/'missing-active-login')
        self.assertEqual(self.call()['action'],'ERROR')
        self.assertFalse(self.ledger.exists())
    def test_explicit_credential_home_keeps_advisor_role_and_records_selection(self):
        selected=self.home/'other-login';selected.mkdir()
        (selected/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt'}))
        self.env['CODEX_HOME']=str(self.home/'unselected-login')
        self.env['ADVISOR_AUTH_CODEX_HOME']=str(selected)
        self.fake_code('authhome')
        out=self.call();self.assertEqual(out['action'],'COMPLETED')
        run=self.home/'advisor-runs'/out['request_id']
        launch=json.loads((run/'launch.json').read_text())
        self.assertEqual(launch['credential_home'],str(selected.resolve()))
        self.assertEqual((run/'authhome.txt').read_text(),str(selected.resolve()))
        self.assertEqual(json.loads(self.ledger.read_text())['attempts'][0]['binding']['advisor']['model'],'advisor')
    def test_invalid_credential_home_fails_before_reservation(self):
        self.env['ADVISOR_AUTH_CODEX_HOME']=str(self.home/'missing-login')
        self.assertEqual(self.call()['action'],'ERROR')
        self.assertFalse(self.ledger.exists())
    def test_explicit_request_is_idempotent(self):
        decision=json.loads(self.decision.read_text());decision['explicit_request']=True
        self.decision.write_text(json.dumps(decision))
        self.assertEqual(self.call()['action'],'COMPLETED')
        self.fake_code('nonzero')
        self.assertEqual(self.call()['action'],'REUSE')
        self.assertEqual(len(json.loads(self.ledger.read_text())['attempts']),1)

    def test_errors_never_complete(self):
        for mode in ['mismatch','missing','nonzero','tool']:
            with self.subTest(mode=mode):
                self.fake_code(mode)
                # Separate tasks, explicit test isolation (not a runtime budget bypass).
                ledger=self.home/(mode+'.json')
                out=self.call('--ledger',str(ledger));self.assertEqual(out['action'],'FAILED')
    def test_timeout_terminates_and_records(self):
        self.fake_code('timeout');out=self.call('--timeout','1');self.assertEqual(out['action'],'FAILED')
        terminal=json.loads((self.home/'advisor-runs'/out['request_id']/'terminal.json').read_text())
        self.assertIn('terminated and reaped',terminal['reason'])
    def test_runtime_event_tamper_blocks_reuse(self):
        out=self.call();(self.home/'advisor-runs'/out['request_id']/'events.jsonl').write_text('{}')
        self.assertEqual(self.call()['action'],'ERROR')
    def test_terminal_recovery_does_not_call_model(self):
        out=self.call();state=json.loads(self.ledger.read_text());state['attempts'][0]['status']='pending';self.ledger.write_text(json.dumps(state))
        self.fake_code('nonzero')
        self.assertEqual(self.call()['action'],'RECOVERY_REQUIRED')
        self.assertEqual(self.call('--recover')['action'],'COMPLETED')
        self.assertEqual(len(json.loads(self.ledger.read_text())['attempts']),1)
    def test_changed_brief_dispatches_new_request(self):
        first=self.call();self.brief.write_text('different material question');second=self.call()
        self.assertEqual(second['action'],'COMPLETED');self.assertNotEqual(first['request_id'],second['request_id'])
    def test_packet_limit_before_reservation(self):
        self.brief.write_text('x'*48001);self.assertEqual(self.call()['action'],'ERROR')
        self.assertEqual(json.loads(self.ledger.read_text())['attempts'],[])
    def test_missing_executable_is_explicit_error(self):
        self.fake.unlink();self.env['PATH']=str(self.bin)
        self.assertEqual(self.call()['action'],'ERROR')

    def test_disabled_advisor_skips_before_ledger_creation(self):
        role=self.home/'agents/astra_advisor.toml'
        role.write_text(role.read_text()+'\nenabled=false\n')
        out=self.call()
        self.assertEqual(out,{'action':'SKIP','reason':'advisor_disabled'})
        self.assertFalse(self.ledger.exists())

    def test_weaker_advisor_skips_before_ledger_creation(self):
        (self.home/'config.toml').write_text(
            'model="gpt-5.6-sol"\nmodel_reasoning_effort="medium"')
        (self.home/'agents/astra_advisor.toml').write_text(
            'model="gpt-5.6-terra"\nmodel_reasoning_effort="medium"\n'
            'sandbox_mode="read-only"\ndeveloper_instructions="no tools"')
        out=self.call()
        self.assertEqual(out['action'],'SKIP')
        self.assertEqual(out['reason'],'advisor_weaker_than_conductor')
        self.assertEqual(out['conductor_model'],'gpt-5.6-sol')
        self.assertEqual(out['advisor_model'],'gpt-5.6-terra')
        self.assertFalse(self.ledger.exists())

    def test_disabling_advisor_does_not_orphan_pending_recovery(self):
        completed=self.call()
        state=json.loads(self.ledger.read_text())
        state['attempts'][0]['status']='pending'
        self.ledger.write_text(json.dumps(state))
        role=self.home/'agents/astra_advisor.toml'
        role.write_text(role.read_text()+'\nenabled=false\n')
        self.assertEqual(self.call()['action'],'RECOVERY_REQUIRED')
        recovered=self.call('--recover')
        self.assertEqual(recovered['action'],'COMPLETED')
        self.assertEqual(recovered['request_id'],completed['request_id'])

    def test_pending_schema3_note_tamper_fails_before_recovery(self):
        self.assertEqual(self.call()['action'],'COMPLETED')
        note=self.home/'owner-extend.md'; note.write_text('broad cross-system work')
        from advisor import extend_limit, operate
        extend_limit(self.ledger, n=4, note=str(note), task_id='trial', decision_id='d',
                     plan_path=str(self.decision.resolve()), quality_ceiling=6)
        self.brief.write_text('second consult')
        cfg={'advisor':{'model':'advisor','model_reasoning_effort':'medium','sandbox_mode':'read-only','developer_instructions':'no tools'}}
        start=operate('start', self.decision, self.ledger, cfg)
        self.assertEqual(start['action'],'CONSULT')
        note.write_text('tampered after pending')
        out=self.call()
        self.assertEqual(out['action'],'ERROR')
        self.assertNotEqual(out.get('action'),'RECOVERY_REQUIRED')

    def test_wrong_decision_does_not_recover_or_write_receipts(self):
        out=self.call(); self.assertEqual(out['action'],'COMPLETED')
        state=json.loads(self.ledger.read_text()); rid=state['attempts'][0]['request_id']
        state['attempts'][0]['status']='pending'; self.ledger.write_text(json.dumps(state))
        receipt=self.home/'advisor-runs'/rid/'receipt.json'
        before=receipt.read_bytes(); stamp=receipt.stat().st_mtime_ns
        other=self.home/'other-decision.json'
        other.write_text(json.dumps(dict(schema_version=2,task_id='other-task',decision_id='d',brief_file=str(self.brief),evidence_files=[],failed_repair_files=[],same_cause_failed_repairs=0,explicit_request=False,unresolved_evidence_conflict=True,architecture_change=False,hard_to_reverse_action=False)))
        p=subprocess.run([sys.executable,str(self.cli),'run',str(other),'--ledger',str(self.ledger),'--recover'],env=self.env,capture_output=True,text=True,timeout=15)
        self.assertNotIn('Traceback',p.stderr)
        self.assertEqual(json.loads(p.stdout)['action'],'ERROR')
        self.assertNotEqual(json.loads(p.stdout).get('action'),'RECOVERY_REQUIRED')
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(receipt.stat().st_mtime_ns, stamp)
        p=subprocess.run([sys.executable,str(self.cli),'run',str(other),'--ledger',str(self.ledger)],env=self.env,capture_output=True,text=True,timeout=15)
        self.assertEqual(json.loads(p.stdout)['action'],'ERROR')
        self.assertEqual(receipt.read_bytes(), before)

    def test_runner_complete_includes_runtime_evidence_atomically(self):
        import advisor, runner
        old_home=runner.HOME
        writes=[]
        real=advisor.atomic
        def spy(path, data):
            writes.append((str(Path(path).resolve()), json.loads(json.dumps(data))))
            return real(path, data)
        advisor.atomic=spy
        runner.HOME=self.home
        try:
            out=runner.execute(self.decision, self.ledger, timeout=15, binary=str(self.fake))
        finally:
            advisor.atomic=real
            runner.HOME=old_home
        self.assertEqual(out['action'],'COMPLETED')
        snaps=[d for p,d in writes if Path(p)==self.ledger.resolve()]
        self.assertTrue(snaps)
        for snap in snaps:
            for row in snap.get('attempts') or []:
                if row.get('status')=='completed':
                    self.assertTrue(row.get('runtime_evidence'))
        self.assertTrue(json.loads(self.ledger.read_text())['attempts'][0].get('runtime_evidence'))

if __name__=='__main__':unittest.main()
