import copy
import json
import multiprocessing
import subprocess
import sys
import shutil
from pathlib import Path
import tempfile
import unittest
from advisor import (operate, digest, read, settings, select_advisor_model,
                     extend_limit, validate_ledger, original_limit,
                     effective_limit, attempts_digest, atomic)


def start_worker(d, l, c, q):
    q.put(operate('start', Path(d), Path(l), c)['action'])


def extend_worker(l, n, note, tid, did, plan, q):
    try:
        q.put(extend_limit(Path(l), n=n, note=note, task_id=tid, decision_id=did,
                           plan_path=plan, quality_ceiling=6)['action'])
    except Exception as e:
        q.put(type(e).__name__)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.d = self.root/'decision.json'; self.l = self.root/'ledger.json'
        self.brief = self.root/'brief.md'; self.brief.write_text('Review this bounded decision')
        self.evidence = self.root/'evidence.txt'; self.evidence.write_text('current evidence')
        self.c = {'advisor': {'model':'test-model','model_reasoning_effort':'medium','sandbox_mode':'read-only','developer_instructions':'advice only'}}
        self.data = dict(schema_version=2,task_id='t',decision_id='d',brief_file=str(self.brief),evidence_files=[str(self.evidence)],failed_repair_files=[],same_cause_failed_repairs=0,explicit_request=False,unresolved_evidence_conflict=False,architecture_change=True,hard_to_reverse_action=False)
        self.save(); operate('init',self.d,self.l,self.c,max_calls=2)
    def save(self): self.d.write_text(json.dumps(self.data))
    def call(self,cmd,**kw): return operate(cmd,self.d,self.l,self.c,**kw)
    def complete(self,start,**changes):
        result=self.root/'result.md';result.write_text('Independent advice')
        r=dict(status='completed',request_id=start['request_id'],runtime_call_id='runtime-test',fingerprint=start['fingerprint'],model='test-model',effort='medium',result_sha256=digest(result.read_bytes()),usage=None)
        r.update(changes); receipt=self.root/('receipt-'+start['request_id']+'.json');receipt.write_text(json.dumps(r))
        return self.call('complete',request_id=start['request_id'],receipt=str(receipt),result=str(result))
    def test_real_artifacts_required_for_reuse(self):
        self.data['prior_consult']={'decision_id':'d','evidence_revision':'r1'};self.save()
        self.assertEqual(self.call('check')['action'],'CONSULT')
        s=self.call('start');self.complete(s)
        self.assertEqual(self.call('check')['action'],'REUSE')
        (self.root/'result.md').unlink()
        self.assertEqual(self.call('check')['action'],'ERROR')
    def test_result_and_receipt_tamper(self):
        s=self.call('start');self.complete(s)
        (self.root/'result.md').write_text('changed')
        self.assertEqual(self.call('check')['action'],'ERROR')
        (self.root/'result.md').write_text('Independent advice')
        (self.root/('receipt-'+s['request_id']+'.json')).write_text('{}')
        self.assertEqual(self.call('check')['action'],'ERROR')
    def test_evidence_brief_flags_model_effort_change(self):
        s=self.call('start');self.complete(s)
        for target in ['evidence','brief','flag','model','effort']:
            with self.subTest(target=target):
                oldc=copy.deepcopy(self.c); oldd=copy.deepcopy(self.data)
                olde=self.evidence.read_text();oldb=self.brief.read_text()
                if target=='evidence':self.evidence.write_text('new evidence')
                if target=='brief':self.brief.write_text('new question')
                if target=='flag':self.data['hard_to_reverse_action']=True;self.save()
                if target=='model':self.c['advisor']['model']='different'
                if target=='effort':self.c['advisor']['model_reasoning_effort']='high'
                self.assertEqual(self.call('check')['action'],'CONSULT')
                self.c=oldc;self.data=oldd;self.save();self.evidence.write_text(olde);self.brief.write_text(oldb)
    def test_concurrent_start(self):
        ctx=multiprocessing.get_context('spawn');q=ctx.Queue()
        ps=[ctx.Process(target=start_worker,args=(str(self.d),str(self.l),self.c,q)) for _ in range(2)]
        for p in ps:p.start()
        for p in ps:p.join(10);self.assertEqual(p.exitcode,0)
        self.assertCountEqual([q.get(timeout=2),q.get(timeout=2)],['CONSULT','BUSY'])
        self.assertEqual(len(read(self.l)['attempts']),1)
    def test_budget_includes_failed_attempts(self):
        for _ in range(2):
            s=self.call('start');p=self.root/'failure.json';p.write_text(json.dumps(dict(status='failed',request_id=s['request_id'],runtime_call_id='actual-failure')))
            self.call('fail',request_id=s['request_id'],receipt=str(p))
        self.assertEqual(self.call('start')['action'],'BUDGET_EXHAUSTED')
    def test_completion_rejects_wrong_binding_and_double_finish(self):
        s=self.call('start')
        for change in [dict(model='wrong'),dict(effort='high'),dict(status='running'),dict(fingerprint='wrong'),dict(result_sha256='wrong')]:
            with self.subTest(change=change),self.assertRaises(ValueError):self.complete(s,**change)
        self.complete(s)
        with self.assertRaises(ValueError):self.complete(s)
    def test_changed_evidence_pending_and_failure_recovery(self):
        s=self.call('start');self.evidence.write_text('changed')
        with self.assertRaises(ValueError):self.complete(s)
        self.evidence.unlink();p=self.root/'failure.json';p.write_text(json.dumps(dict(status='cancelled',request_id=s['request_id'],runtime_call_id='cancel-receipt')))
        self.assertEqual(self.call('fail',request_id=s['request_id'],receipt=str(p))['action'],'FAILED')
    def test_skip_and_failure_evidence(self):
        self.data['architecture_change']=False;self.save()
        self.assertEqual(self.call('check')['action'],'SKIP')
        self.data['same_cause_failed_repairs']=2;self.save()
        with self.assertRaises(ValueError):self.call('check')
        logs=[]
        for i in range(2):p=self.root/f'failed-{i}';p.write_text('repair and retest failed');logs.append(str(p))
        self.data['failed_repair_files']=logs;self.save();self.assertEqual(self.call('check')['action'],'CONSULT')
    def test_quality_checkpoint_is_distinct_from_explicit_user_request(self):
        self.data['architecture_change']=False
        self.data['quality_checkpoint']=True
        self.save()
        out=self.call('start')
        self.assertEqual(out['action'],'CONSULT')
        self.assertEqual(out['reasons'],['quality_checkpoint'])
        self.assertFalse(out['dispatch'] is None)
        row=read(self.l)['attempts'][0]
        self.assertFalse(row['binding']['flags']['explicit_request'])
        self.assertTrue(row['binding']['flags']['quality_checkpoint'])
    def test_legacy_and_missing_evidence_rejected(self):
        self.data['schema_version']=1;self.save()
        with self.assertRaises(ValueError):self.call('check')
        self.data['schema_version']=2;self.save();self.evidence.unlink()
        with self.assertRaises(ValueError):self.call('check')
    def test_usage_validation(self):
        start=self.call('start')
        for usage in [{'input_tokens':1}, {'input_tokens':1,'cached_input_tokens':2,'output_tokens':1}, {'input_tokens':True,'cached_input_tokens':0,'output_tokens':1}]:
            with self.subTest(usage=usage),self.assertRaises(ValueError):self.complete(start,usage=usage)
        self.complete(start,usage={'input_tokens':100,'cached_input_tokens':40,'output_tokens':10})
        self.assertEqual(read(self.l)['attempts'][0]['usage']['input_tokens'],100)
    def test_fresh_cli_report_unknown_usage(self):
        start=self.call('start');self.complete(start)
        script=self.root/'skills/advisor/scripts/advisor.py';script.parent.mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name('advisor.py'),script)
        (self.root/'config.toml').write_text('model="sol"\nmodel_reasoning_effort="medium"')
        (self.root/'agents').mkdir()
        (self.root/'agents/astra_advisor.toml').write_text('model="test-model"\nmodel_reasoning_effort="medium"\nsandbox_mode="read-only"\ndeveloper_instructions="advice only"')
        out=json.loads(subprocess.check_output([sys.executable,str(script),'report','--ledger',str(self.l)],text=True))
        self.assertEqual(out['completed'],1);self.assertEqual(out['attempts_without_usage'],1)
        self.assertEqual(out['attempts_with_usage'],0)
    def test_fresh_settings(self):
        (self.root/'config.toml').write_text('model="sol"\nmodel_reasoning_effort="medium"')
        (self.root/'agents').mkdir();p=self.root/'agents/astra_advisor.toml'
        for model,effort in [('one','medium'),('two','high')]:
            p.write_text(f'model="{model}"\nmodel_reasoning_effort="{effort}"\nsandbox_mode="read-only"\ndeveloper_instructions="advise"')
            self.assertEqual(settings(self.root)['advisor']['model'],model)
            self.assertEqual(settings(self.root)['advisor']['model_reasoning_effort'],effort)

    def test_model_selector_and_capability_guard(self):
        (self.root/'config.toml').write_text(
            'model="gpt-5.6-sol"\nmodel_reasoning_effort="medium"')
        (self.root/'agents').mkdir(exist_ok=True)
        role=self.root/'agents/astra_advisor.toml'
        role.write_text(
            'model="gpt-6-astra"\nmodel_reasoning_effort="medium"\n'
            'sandbox_mode="read-only"\ndeveloper_instructions="advise"')
        ready=settings(self.root)
        self.assertTrue(ready['advisor_control']['enabled'])
        self.assertTrue(ready['advisor_control']['compatible'])
        weaker=select_advisor_model(self.root,'terra')
        self.assertFalse(weaker['advisor_control']['compatible'])
        self.assertEqual(weaker['advisor_control']['reason'],
                         'advisor_weaker_than_conductor')
        off=select_advisor_model(self.root,'off')
        self.assertFalse(off['advisor_control']['enabled'])
        restored=select_advisor_model(self.root,'astra')
        self.assertTrue(restored['advisor_control']['enabled'])
        self.assertTrue(restored['advisor_control']['compatible'])
        with self.assertRaises(ValueError):
            select_advisor_model(self.root,'unknown')

    def test_model_selector_ignores_multiline_content_and_accepts_indented_key(self):
        (self.root/'config.toml').write_text(
            'model="gpt-5.6-luna"\nmodel_reasoning_effort="medium"')
        (self.root/'agents').mkdir(exist_ok=True)
        role=self.root/'agents/astra_advisor.toml'
        role.write_text(
            '  model = "gpt-5.6-terra"\n'
            'model_reasoning_effort="medium"\nsandbox_mode="read-only"\n'
            'developer_instructions="""first\nmodel = "content, not a key"\nlast\n"""\n')
        updated=select_advisor_model(self.root,'sol')
        self.assertEqual(updated['advisor']['model'],'gpt-5.6-sol')
        self.assertIn('model = "content, not a key"',
                      updated['advisor']['developer_instructions'])
        self.assertEqual(role.read_text().count('gpt-5.6-sol'),1)

    def test_direct_executor_high_initial_limit_still_works(self):
        other=self.root/'high.json'
        operate('init',self.d,other,self.c,max_calls=10)
        self.assertEqual(read(other)['schema_version'],2)
        self.assertEqual(read(other)['max_calls'],10)


class BudgetExtensionTests(LedgerTests):
    def owner_note(self, text='broad cross-system work needs more reviews'):
        p=self.root/'owner-note.md'; p.write_text(text); return p
    def exhaust(self):
        for i in range(2):
            s=self.call('start'); p=self.root/('failure-'+str(i)+'.json')
            p.write_text(json.dumps(dict(status='failed',request_id=s['request_id'],runtime_call_id='actual-failure')))
            self.call('fail',request_id=s['request_id'],receipt=str(p))
        self.assertEqual(self.call('start')['action'],'BUDGET_EXHAUSTED')
    def extend(self, n=4, note=None, **kw):
        return extend_limit(self.l, n=n, note=str(note or self.owner_note()), task_id='t',
                            decision_id='d', plan_path=str(self.d), quality_ceiling=6, **kw)

    def test_schema2_migrates_on_first_extension_and_preserves_attempts(self):
        self.exhaust()
        before=read(self.l)['attempts']
        out=self.extend()
        self.assertEqual(out['action'],'EXTENDED')
        state=validate_ledger(read(self.l))
        self.assertEqual(state['schema_version'],3)
        self.assertEqual(original_limit(state),2)
        self.assertEqual(effective_limit(state),4)
        self.assertEqual(state['attempts'],before)
        self.assertEqual(self.call('start')['action'],'CONSULT')
        self.assertEqual(len(read(self.l)['attempts']),3)

    def test_pending_same_decrease_nonint_ceiling_and_missing_ledger_rejected(self):
        s=self.call('start')
        with self.assertRaises(ValueError): self.extend()
        p=self.root/'failure.json'; p.write_text(json.dumps(dict(status='failed',request_id=s['request_id'],runtime_call_id='actual-failure')))
        self.call('fail',request_id=s['request_id'],receipt=str(p))
        s=self.call('start'); p=self.root/'failure-2.json'
        p.write_text(json.dumps(dict(status='failed',request_id=s['request_id'],runtime_call_id='actual-failure')))
        self.call('fail',request_id=s['request_id'],receipt=str(p))
        self.assertEqual(self.call('start')['action'],'BUDGET_EXHAUSTED')
        self.extend()
        before=self.l.read_bytes()
        retry=self.extend(4)
        self.assertEqual(retry['action'],'EXTENDED')
        self.assertEqual(self.l.read_bytes(), before)
        other=self.root/'other-note.md'; other.write_text('different owner note text')
        with self.assertRaises(ValueError): self.extend(4, note=other)
        with self.assertRaises(ValueError):
            extend_limit(self.l, n=4, note=str(self.owner_note()), task_id='t', decision_id='other-stage',
                         plan_path=str(self.d), quality_ceiling=6)
        with self.assertRaises(ValueError): self.extend(1)
        with self.assertRaises(ValueError): self.extend(True)
        with self.assertRaises(ValueError): self.extend(7)
        empty=self.root/'empty.md'; empty.write_text('   ')
        with self.assertRaises(ValueError): self.extend(5, note=empty)
        missing=self.root/'no-ledger.json'
        with self.assertRaises(ValueError):
            extend_limit(missing, n=4, note=str(self.owner_note()), task_id='t', decision_id='d',
                         plan_path=str(self.d), quality_ceiling=6)
        with self.assertRaises(ValueError):
            extend_limit(self.l, n=5, note=str(self.owner_note()), task_id='other', decision_id='d',
                         plan_path=str(self.d), quality_ceiling=6)

    def test_changed_note_direct_limit_broken_hash_and_reordered_attempts_fail_closed(self):
        self.exhaust(); note=self.owner_note(); self.extend(note=note)
        note.write_text('changed after grant')
        with self.assertRaises(ValueError): self.call('check')
        note.write_text('broad cross-system work needs more reviews')
        self.assertEqual(self.call('start')['action'],'CONSULT')
        p=self.root/'failure-x.json'; st=read(self.l); rid=st['attempts'][-1]['request_id']
        p.write_text(json.dumps(dict(status='failed',request_id=rid,runtime_call_id='actual-failure')))
        self.call('fail',request_id=rid,receipt=str(p))
        state=read(self.l); state['max_calls']=5; atomic(self.l, state)
        with self.assertRaises(ValueError): validate_ledger(read(self.l))
        with self.assertRaises(ValueError): self.call('check')
        self.l.write_text(json.dumps({k:v for k,v in state.items() if k!='max_calls'} | {'max_calls':4}))
        state=read(self.l); state['budget_extensions'][0]['hash']='0'*64; atomic(self.l,state)
        with self.assertRaises(ValueError): self.call('check')
        # restore via a fresh exhausted ledger
        self.l.unlink(); operate('init',self.d,self.l,self.c,max_calls=2)
        self.exhaust(); self.extend()
        state=read(self.l); state['attempts']=list(reversed(state['attempts'])); atomic(self.l,state)
        with self.assertRaises(ValueError): self.call('check')

    def test_previous_hash_mismatch_and_report_fields(self):
        self.exhaust(); self.extend()
        state=read(self.l)
        self.assertEqual(original_limit(state),2)
        self.assertEqual(effective_limit(state),4)
        self.assertEqual(len(state['budget_extensions']),1)
        self.assertEqual(state['max_calls']-len(state['attempts']),2)
        state['budget_extensions'][0]['previous_hash']='1'*64; atomic(self.l,state)
        with self.assertRaises(ValueError): self.call('check')

    def test_crash_before_publish_leaves_schema2(self):
        self.exhaust()
        import advisor
        real=advisor.atomic
        advisor.atomic=lambda path, data: (_ for _ in ()).throw(OSError('simulated crash before replace'))
        try:
            with self.assertRaises(OSError): self.extend()
        finally:
            advisor.atomic=real
        state=read(self.l)
        self.assertEqual(state['schema_version'],2)
        self.assertEqual(state['max_calls'],2)
        self.assertEqual(len(state['attempts']),2)
        self.assertNotIn('budget_extensions', state)

    def test_concurrent_reservation_versus_extension(self):
        self.exhaust()
        note=self.owner_note()
        ctx=multiprocessing.get_context('spawn'); q=ctx.Queue()
        ps=[ctx.Process(target=start_worker,args=(str(self.d),str(self.l),self.c,q)),
            ctx.Process(target=extend_worker,args=(str(self.l),4,str(note),'t','d',str(self.d),q))]
        for p in ps: p.start()
        for p in ps: p.join(10); self.assertEqual(p.exitcode,0)
        results=[q.get(timeout=2), q.get(timeout=2)]
        state=validate_ledger(read(self.l))
        self.assertLessEqual(len(state['attempts']), state['max_calls'])
        self.assertIn(state['max_calls'], (2,4))
        self.assertEqual(attempts_digest(state['attempts'], min(2,len(state['attempts']))),
                         attempts_digest(state['attempts'][:min(2,len(state['attempts']))]))
        if state['max_calls']==4:
            self.assertEqual(original_limit(state),2)
            self.assertIn('EXTENDED', results)
        else:
            self.assertEqual(state['schema_version'],2)

if __name__=='__main__':unittest.main()
