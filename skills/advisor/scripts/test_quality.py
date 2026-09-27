import json
import multiprocessing
import shutil
from pathlib import Path
import subprocess
import sys
import test_runner
from advisor import digest, packed, extend_limit, operate, read, validate_ledger, attempts_digest, atomic
from quality import reclassify_invalid_completed, ensure_classifications


class QualityTests(test_runner.RunnerTests):
    def setUp(self):
        super().setUp()
        shutil.copyfile(Path(__file__).with_name('quality.py'),self.cli.with_name('quality.py'))
        self.plan=self.home/'quality-plan.json'
        self.plan.write_text(json.dumps(dict(schema_version=1,task_id='quality-test',stages=[dict(id=s,max_calls=2,criteria={'correctness':'Expected behavior demonstrated by test evidence'},evidence_files=[str(self.brief)]) for s in ['plan','first-implementation']])))
        self.note=self.home/'accept.md';self.note.write_text('Checked the actual evidence; criterion satisfied.')
    def review_code(self,status='PASS',criterion='correctness'):
        self.fake_code()
        result=json.dumps(dict(verdict=status,criteria={criterion:dict(status=status,reason='Evidence demonstrates the requested behavior',evidence=[str(self.brief)])},improvements=[]))
        self.fake.write_text(self.fake.read_text().replace("'advice'",repr(result)))
    def quality(self,command,stage='plan',*args):
        p=subprocess.run([sys.executable,str(self.cli.with_name('quality.py')),command,str(self.plan),stage,*args],env=self.env,capture_output=True,text=True,timeout=15)
        self.assertNotIn('Traceback',p.stderr)
        return json.loads(p.stdout)
    def test_quality_requires_independent_pass_and_conductor_acceptance(self):
        self.assertEqual(self.quality('gate')['action'],'HOLD')
        self.review_code()
        self.assertEqual(self.quality('review')['action'],'REVIEWED')
        self.assertEqual(self.quality('gate')['action'],'HOLD')
        self.assertEqual(self.quality('accept','plan','--note',str(self.note))['action'],'ACCEPTED')
        self.assertEqual(self.quality('gate')['action'],'READY')
        self.brief.write_text('changed actual artifact')
        self.assertEqual(self.quality('gate')['action'],'HOLD')
    def test_quality_checkpoint_has_its_own_trigger_reason(self):
        self.quality('gate')
        decision=read(self.checkpoint()/'decision.json')
        self.assertFalse(decision['explicit_request'])
        self.assertTrue(decision['quality_checkpoint'])
    def test_legacy_quality_trigger_shape_is_preserved(self):
        self.quality('gate')
        path=self.checkpoint()/'decision.json'
        decision=read(path)
        decision['explicit_request']=True
        decision.pop('quality_checkpoint')
        atomic(path,decision)
        self.quality('gate')
        preserved=read(path)
        self.assertTrue(preserved['explicit_request'])
        self.assertNotIn('quality_checkpoint',preserved)
    def test_disabled_quality_advisor_skips_before_ledger_creation(self):
        role=self.home/'agents/astra_advisor.toml'
        role.write_text(role.read_text()+'\nenabled=false\n')
        self.assertEqual(self.quality('review')['reason'],'advisor_disabled')
        self.assertFalse((self.checkpoint()/'advisor-ledger.json').exists())
    def test_weaker_quality_advisor_skips_before_ledger_creation(self):
        (self.home/'config.toml').write_text(
            'model="gpt-5.6-sol"\nmodel_reasoning_effort="medium"')
        (self.home/'agents/astra_advisor.toml').write_text(
            'model="gpt-5.6-terra"\nmodel_reasoning_effort="medium"\n'
            'sandbox_mode="read-only"\ndeveloper_instructions="no tools"')
        self.assertEqual(
            self.quality('review')['reason'],'advisor_weaker_than_conductor')
        self.assertFalse((self.checkpoint()/'advisor-ledger.json').exists())
    def test_quality_revision_and_budget(self):
        self.review_code('REVISE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.assertEqual(self.quality('accept','plan','--note',str(self.note))['action'],'HOLD')
        self.brief.write_text('revision two')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.brief.write_text('revision three')
        self.assertEqual(self.quality('review')['action'],'BUDGET_EXHAUSTED')
    def test_quality_order_and_missing_criteria(self):
        self.assertEqual(self.quality('review','first-implementation')['action'],'HOLD')
        self.review_code(criterion='wrong')
        self.assertEqual(self.quality('review')['action'],'HOLD')
    def test_quality_plan_change_invalidates_pass(self):
        self.review_code();self.quality('review');self.quality('accept','plan','--note',str(self.note))
        d=json.loads(self.plan.read_text());d['stages'][0]['criteria']['correctness']='Stronger requirement';self.plan.write_text(json.dumps(d))
        self.assertEqual(self.quality('gate')['action'],'HOLD')

    def test_invalid_review_can_retry_and_new_stage_preserves_prior_pass(self):
        self.review_code(criterion='wrong')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.review_code()
        self.assertEqual(self.quality('review')['action'],'REVIEWED')
        self.assertEqual(self.quality('accept','plan','--note',str(self.note))['action'],'ACCEPTED')
        d=json.loads(self.plan.read_text())
        d['stages'].append(dict(id='release',max_calls=2,criteria={'ready':'Release tests pass'},evidence_files=[str(self.brief)]))
        self.plan.write_text(json.dumps(d))
        self.assertEqual(self.quality('gate')['action'],'READY')

    def test_budget_change_is_rejected(self):
        self.review_code('REVISE');self.quality('review')
        d=json.loads(self.plan.read_text());d['stages'][0]['max_calls']=1;self.plan.write_text(json.dumps(d))
        self.assertEqual(self.quality('review')['action'],'HOLD')

    def test_nonobject_review_does_not_poison_reuse(self):
        self.fake_code();self.fake.write_text(self.fake.read_text().replace("'advice'", repr('[]')))
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.review_code()
        self.assertEqual(self.quality('review')['action'],'REVIEWED')

    def test_resume_status_preserves_calls_and_identifies_acceptance(self):
        out=self.quality('status')
        self.assertEqual(out['next_stage'],'plan')
        self.assertEqual(out['attempts'],0)
        self.review_code();self.quality('review')
        out=self.quality('status')
        self.assertEqual(out['stages'][0]['state'],'AWAITING_ACCEPTANCE')
        self.assertEqual(out['attempts'],1)
        self.quality('accept','plan','--note',str(self.note))
        out=self.quality('status')
        self.assertEqual(out['next_stage'],'first-implementation')
        self.assertEqual(out['stages'][0]['state'],'READY')
        self.assertEqual(out['attempts'],1)
        self.brief.write_text('updated deliverable')
        self.assertEqual(self.quality('status')['next_stage'],'plan')

    def test_changed_budget_invalidates_gate_and_resume(self):
        self.review_code();self.quality('review');self.quality('accept','plan','--note',str(self.note))
        d=json.loads(self.plan.read_text());d['stages'][0]['max_calls']=3;self.plan.write_text(json.dumps(d))
        self.assertEqual(self.quality('gate')['action'],'HOLD')
        self.assertEqual(self.quality('status')['stages'][0]['state'],'HOLD')

    def checkpoint(self, stage='plan'):
        plan=self.plan.resolve()
        return plan.parent/('quality-checkpoints-'+digest(str(plan).encode())[:16])/stage
    def owner_note(self, text='broad cross-system stage needs two more reviews'):
        p=self.home/'owner-extend.md'; p.write_text(text); return p
    def raw(self,*args):
        p=subprocess.run([sys.executable,str(self.cli.with_name('quality.py')),*args],env=self.env,capture_output=True,text=True,timeout=15)
        self.assertNotIn('Traceback',p.stderr)
        return p
    def exhaust_needs_evidence(self):
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.brief.write_text('repaired packet one')
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.brief.write_text('repaired packet two')
        self.assertEqual(self.quality('review')['action'],'BUDGET_EXHAUSTED')

    def test_exhausted_needs_evidence_extend_review_accept_ready(self):
        self.exhaust_needs_evidence()
        self.assertEqual(self.quality('gate')['action'],'HOLD')
        note=self.owner_note()
        ledger=self.checkpoint()/'advisor-ledger.json'
        before=read(ledger)['attempts']
        out=self.quality('extend','plan','--max-calls','4','--note',str(note))
        self.assertEqual(out['action'],'EXTENDED')
        self.assertEqual(out['original_max_calls'],2)
        self.assertEqual(out['effective_max_calls'],4)
        self.assertEqual(out['attempts'],2)
        self.assertEqual(out['remaining'],2)
        self.assertIn('not acceptance', out['scope'])
        state=validate_ledger(read(ledger))
        self.assertEqual(state['schema_version'],3)
        self.assertEqual(state['attempts'],before)
        self.assertEqual(self.quality('gate')['action'],'HOLD')
        st=self.quality('status')['stages'][0]
        self.assertEqual(st['state'],'HOLD')
        self.assertEqual(st['original_max_calls'],2)
        self.assertEqual(st['effective_max_calls'],4)
        self.assertEqual(st['max_calls'],4)
        self.assertEqual(st['attempts'],2)
        self.assertEqual(st['remaining'],2)
        self.assertEqual(st['extensions'],1)
        totals=self.quality('status')
        self.assertEqual(totals['original_max_calls'],4)
        self.assertEqual(totals['effective_max_calls'],6)
        self.assertEqual(totals['planned_max_calls'],4)
        self.brief.write_text('repaired packet accepted')
        self.review_code()
        self.assertEqual(self.quality('review')['action'],'REVIEWED')
        self.assertEqual(self.quality('accept','plan','--note',str(self.note))['action'],'ACCEPTED')
        self.assertEqual(self.quality('gate')['action'],'READY')
        self.assertEqual(self.quality('status')['stages'][0]['state'],'READY')

    def test_extension_rejections_and_tamper(self):
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(note))['action'],'HOLD')
        self.exhaust_needs_evidence()
        empty=self.home/'empty-note.md'; empty.write_text('  ')
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(empty))['action'],'HOLD')
        self.assertEqual(self.quality('extend','plan','--max-calls','4')['action'],'HOLD')
        self.assertEqual(self.quality('extend','plan','--max-calls','2','--note',str(note))['action'],'HOLD')
        self.assertEqual(self.quality('extend','plan','--max-calls','1','--note',str(note))['action'],'HOLD')
        p=self.raw('extend',str(self.plan),'plan','--max-calls','4.5','--note',str(note))
        self.assertNotEqual(p.returncode,0)
        first=self.quality('extend','plan','--max-calls','4','--note',str(note))
        self.assertEqual(first['action'],'EXTENDED')
        ledger=self.checkpoint()/'advisor-ledger.json'
        before=ledger.read_bytes()
        retry=self.quality('extend','plan','--max-calls','4','--note',str(note))
        self.assertEqual(retry['action'],'EXTENDED')
        self.assertEqual(ledger.read_bytes(), before)
        other=self.home/'other-extend.md'; other.write_text('a different owner note')
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(other))['action'],'HOLD')
        cfg={'advisor':{'model':'advisor','model_reasoning_effort':'medium','sandbox_mode':'read-only','developer_instructions':'no tools'}}
        self.brief.write_text('new fingerprint for pending reservation')
        start=operate('start',self.checkpoint()/'decision.json',ledger,cfg)
        # Decision snapshot is whatever prepare last wrote; force a pending row if consult occurred.
        if start['action']!='CONSULT':
            data=read(ledger); data['attempts'].append(dict(request_id='pending-test',fingerprint='x',status='pending'))
            atomic(ledger,data)
        self.assertEqual(self.quality('extend','plan','--max-calls','5','--note',str(note))['action'],'HOLD')
        data=read(ledger)
        data['attempts']=[r for r in data['attempts'] if r.get('status')!='pending']
        atomic(ledger,data)
        note.write_text('note changed after the grant')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.assertEqual(self.quality('status')['stages'][0]['state'],'HOLD')
        note.write_text('broad cross-system stage needs two more reviews')
        d=json.loads(self.plan.read_text()); d['stages'][0]['max_calls']=3; self.plan.write_text(json.dumps(d))
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.assertEqual(self.quality('gate')['action'],'HOLD')
        d['stages'][0]['max_calls']=2; self.plan.write_text(json.dumps(d))
        data=read(ledger); data['max_calls']=5; atomic(ledger,data)
        self.assertEqual(self.quality('review')['action'],'HOLD')
        data=read(ledger); data['max_calls']=4; data['budget_extensions'][0]['hash']='ab'*32; atomic(ledger,data)
        self.assertEqual(self.quality('gate')['action'],'HOLD')

    def test_calls_three_and_four_then_fifth_refused(self):
        self.exhaust_needs_evidence()
        self.quality('extend','plan','--max-calls','4','--note',str(self.owner_note()))
        self.brief.write_text('third packet')
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.brief.write_text('fourth packet')
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.brief.write_text('fifth packet')
        self.assertEqual(self.quality('review')['action'],'BUDGET_EXHAUSTED')
        self.assertEqual(len(read(self.checkpoint()/'advisor-ledger.json')['attempts']),4)

    def test_initial_limit_six_allowed_seven_rejected(self):
        d=json.loads(self.plan.read_text()); d['stages'][0]['max_calls']=6; self.plan.write_text(json.dumps(d))
        self.review_code()
        self.assertEqual(self.quality('review')['action'],'REVIEWED')
        d['stages'][0]['max_calls']=7; self.plan.write_text(json.dumps(d))
        self.assertEqual(self.quality('status')['action'],'HOLD')

    def test_legacy_unextended_status_keeps_effective_equal_original(self):
        out=self.quality('status')
        self.assertEqual(out['stages'][0]['original_max_calls'],2)
        self.assertEqual(out['stages'][0]['effective_max_calls'],2)
        self.assertEqual(out['stages'][0]['extensions'],0)
        self.review_code(); self.quality('review')
        self.assertEqual(read(self.checkpoint()/'advisor-ledger.json')['schema_version'],2)

    def _spoil_completed_result(self, row, text='[]'):
        p=Path(row['result']['path'])
        p.write_text(text)
        digest_v=digest(p.read_bytes())
        row['result']['sha256']=digest_v
        for ref in row.get('runtime_evidence') or []:
            if Path(ref['path']).resolve()==p.resolve():
                ref['sha256']=digest_v

    def test_unclassified_completed_blocks_extend_until_review_normalizes(self):
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.brief.write_text('second packet')
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        ledger=self.checkpoint()/'advisor-ledger.json'
        data=read(ledger)
        self._spoil_completed_result(data['attempts'][-1])
        atomic(ledger, data)
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(note))['action'],'HOLD')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.assertEqual(read(ledger)['attempts'][-1]['status'],'failed')
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(note))['action'],'EXTENDED')
        validate_ledger(read(ledger))

    def test_changed_valid_result_blocks_extension(self):
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.brief.write_text('second packet')
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        ledger=self.checkpoint()/'advisor-ledger.json'
        row=read(ledger)['attempts'][-1]
        Path(row['result']['path']).write_text(json.dumps(dict(verdict='NEEDS_EVIDENCE',criteria={'correctness':dict(status='NEEDS_EVIDENCE',reason='Different valid wording',evidence=[str(self.brief)])},improvements=[])))
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(note))['action'],'HOLD')
        self.assertEqual(read(ledger)['schema_version'],2)

    def test_quality_error_does_not_mutate_extension_prefix(self):
        self.exhaust_needs_evidence()
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(note))['action'],'EXTENDED')
        ledger=self.checkpoint()/'advisor-ledger.json'
        rid=read(ledger)['attempts'][0]['request_id']
        with self.assertRaises(ValueError):
            reclassify_invalid_completed(ledger, rid, 'simulated quality error')
        self.assertEqual(read(ledger)['attempts'][0]['status'],'completed')
        validate_ledger(read(ledger))

    def test_historical_classification_survives_criteria_change(self):
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        d=json.loads(self.plan.read_text())
        d['stages'][0]['criteria']={'correctness':'Expected behavior demonstrated by test evidence','completeness':'Must cover the later completeness bar'}
        self.plan.write_text(json.dumps(d))
        self.brief.write_text('criteria B packet')
        self.fake_code()
        result=json.dumps(dict(verdict='NEEDS_EVIDENCE',criteria={'correctness':dict(status='NEEDS_EVIDENCE',reason='Still missing completeness evidence',evidence=[str(self.brief)]),'completeness':dict(status='NEEDS_EVIDENCE',reason='New criterion not yet shown',evidence=[str(self.brief)])},improvements=[]))
        self.fake.write_text(self.fake.read_text().replace("'advice'",repr(result)))
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(self.owner_note()))['action'],'EXTENDED')
        state=validate_ledger(read(self.checkpoint()/'advisor-ledger.json'))
        self.assertEqual(state['schema_version'],3)
        self.assertEqual(len(state['attempts']),2)

    def test_classification_marker_tamper_fails_closed(self):
        self.exhaust_needs_evidence()
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(self.owner_note()))['action'],'EXTENDED')
        ledger=self.checkpoint()/'advisor-ledger.json'
        data=read(ledger)
        data['attempts'][0]['quality_classification']['verdict']='PASS'
        atomic(ledger, data)
        with self.assertRaises(ValueError):
            validate_ledger(read(ledger))
        self.assertEqual(self.quality('status')['stages'][0]['state'],'HOLD')
        self.assertEqual(self.quality('extend','plan','--max-calls','5','--note',str(self.owner_note()))['action'],'HOLD')

    def test_wrong_stage_marker_blocks_extension(self):
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        ledger=self.checkpoint()/'advisor-ledger.json'
        data=read(ledger)
        marker=data['attempts'][0]['quality_classification']
        marker['stage']='first-implementation'
        body={k: marker[k] for k in ('result','stage','criteria_contract','verdict')}
        marker['hash']=digest(packed(body))
        atomic(ledger, data)
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(self.owner_note()))['action'],'HOLD')
        self.assertEqual(read(ledger)['schema_version'],2)
        self.assertEqual(read(ledger)['attempts'][0]['quality_classification']['stage'],'first-implementation')

    def test_legacy_backfill_remains_resumable(self):
        self.review_code('NEEDS_EVIDENCE')
        self.assertEqual(self.quality('review')['action'],'HOLD')
        ledger=self.checkpoint()/'advisor-ledger.json'
        data=read(ledger)
        self.assertEqual(data['schema_version'],2)
        data['attempts'][0].pop('quality_classification', None)
        atomic(ledger, data)
        stage=json.loads(self.plan.read_text())['stages'][0]
        ensure_classifications(ledger, stage)
        state=validate_ledger(read(ledger))
        self.assertEqual(state['schema_version'],2)
        self.assertIn('quality_classification', state['attempts'][0])
        self.assertEqual(self.quality('status')['action'],'STATUS')
        self.assertEqual(self.quality('extend','plan','--max-calls','4','--note',str(self.owner_note()))['action'],'EXTENDED')

    def test_reordered_attempts_fail_closed_after_extension(self):
        self.exhaust_needs_evidence()
        self.quality('extend','plan','--max-calls','4','--note',str(self.owner_note()))
        ledger=self.checkpoint()/'advisor-ledger.json'
        data=read(ledger); data['attempts']=list(reversed(data['attempts'])); atomic(ledger,data)
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.assertEqual(self.quality('status')['stages'][0]['state'],'HOLD')

    def test_plan_initial_one_and_six_accepted(self):
        d=json.loads(self.plan.read_text()); d['stages'][0]['max_calls']=1; self.plan.write_text(json.dumps(d))
        self.review_code()
        self.assertEqual(self.quality('review')['action'],'REVIEWED')
        self.plan=self.home/'quality-plan-six.json'
        self.plan.write_text(json.dumps(dict(schema_version=1,task_id='quality-test-six',stages=[dict(id=s,max_calls=6,criteria={'correctness':'Expected behavior demonstrated by test evidence'},evidence_files=[str(self.brief)]) for s in ['plan','first-implementation']])))
        self.review_code()
        self.assertEqual(self.quality('review')['action'],'REVIEWED')

    def test_plan_initial_zero_seven_float_bool_rejected(self):
        d=json.loads(self.plan.read_text())
        for bad in (0, 7, 1.5, True):
            d['stages'][0]['max_calls']=bad
            self.plan.write_text(json.dumps(d))
            p=self.raw('status', str(self.plan))
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout)['action'],'HOLD')

    def test_exhausted_two_to_six(self):
        self.exhaust_needs_evidence()
        out=self.quality('extend','plan','--max-calls','6','--note',str(self.owner_note()))
        self.assertEqual(out['action'],'EXTENDED')
        self.assertEqual(out['original_max_calls'],2)
        self.assertEqual(out['effective_max_calls'],6)
        self.assertEqual(out['attempts'],2)
        self.assertEqual(len(read(self.checkpoint()/'advisor-ledger.json')['attempts']),2)

    def test_effective_six_to_ten_to_twelve(self):
        self.exhaust_needs_evidence()
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','6','--note',str(note))['action'],'EXTENDED')
        self.assertEqual(self.quality('extend','plan','--max-calls','10','--note',str(note))['action'],'EXTENDED')
        self.assertEqual(self.quality('extend','plan','--max-calls','12','--note',str(note))['action'],'EXTENDED')
        st=self.quality('status')['stages'][0]
        self.assertEqual(st['original_max_calls'],2)
        self.assertEqual(st['effective_max_calls'],12)
        self.assertEqual(st['extensions'],3)

    def test_ceiling_twelve_to_thirteen_rejected_before_mutation(self):
        self.exhaust_needs_evidence()
        note=self.owner_note()
        for n in ('6','10','12'):
            self.assertEqual(self.quality('extend','plan','--max-calls',n,'--note',str(note))['action'],'EXTENDED')
        ledger=self.checkpoint()/'advisor-ledger.json'
        before=ledger.read_bytes()
        self.assertEqual(self.quality('extend','plan','--max-calls','13','--note',str(note))['action'],'HOLD')
        self.assertEqual(ledger.read_bytes(), before)

    def test_delta_six_to_eleven_rejected_before_mutation(self):
        self.exhaust_needs_evidence()
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','6','--note',str(note))['action'],'EXTENDED')
        ledger=self.checkpoint()/'advisor-ledger.json'
        before=ledger.read_bytes()
        self.assertEqual(self.quality('extend','plan','--max-calls','11','--note',str(note))['action'],'HOLD')
        self.assertEqual(ledger.read_bytes(), before)

    def test_six_to_ten_exact_retry_and_different_note(self):
        self.exhaust_needs_evidence()
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','6','--note',str(note))['action'],'EXTENDED')
        self.assertEqual(self.quality('extend','plan','--max-calls','10','--note',str(note))['action'],'EXTENDED')
        ledger=self.checkpoint()/'advisor-ledger.json'
        before=ledger.read_bytes()
        retry=self.quality('extend','plan','--max-calls','10','--note',str(note))
        self.assertEqual(retry['action'],'EXTENDED')
        self.assertEqual(ledger.read_bytes(), before)
        other=self.home/'other-ten.md'; other.write_text('a different owner note for ten')
        self.assertEqual(self.quality('extend','plan','--max-calls','10','--note',str(other))['action'],'HOLD')
        self.assertEqual(ledger.read_bytes(), before)

    def test_cli_rejects_non_integer_max_calls(self):
        self.exhaust_needs_evidence()
        note=self.owner_note()
        for bad in ('12.5','-1','x'):
            p=self.raw('extend',str(self.plan),'plan','--max-calls',bad,'--note',str(note))
            self.assertNotEqual(p.returncode, 0)

    def test_generic_effective_thirteen_quality_holds(self):
        self.exhaust_needs_evidence()
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','6','--note',str(note))['action'],'EXTENDED')
        ledger=self.checkpoint()/'advisor-ledger.json'
        out=extend_limit(ledger, n=13, note=str(note), task_id=read(ledger)['task_id'],
                         decision_id='plan', plan_path=str(self.plan.resolve()))
        self.assertEqual(out['action'],'EXTENDED')
        self.assertEqual(validate_ledger(read(ledger))['max_calls'],13)
        self.assertEqual(self.quality('review')['action'],'HOLD')
        self.assertEqual(self.quality('status')['stages'][0]['state'],'HOLD')
        self.assertEqual(self.quality('gate')['action'],'HOLD')
        self.assertEqual(self.quality('accept','plan','--note',str(self.note))['action'],'HOLD')
        self.assertEqual(self.quality('extend','plan','--max-calls','14','--note',str(note))['action'],'HOLD')

    def test_isolated_forward_flow_two_six_ten_ready(self):
        self.exhaust_needs_evidence()
        note_a=self.owner_note('owner note A more reviews needed')
        self.assertEqual(self.quality('extend','plan','--max-calls','6','--note',str(note_a))['action'],'EXTENDED')
        while True:
            used=len(read(self.checkpoint()/'advisor-ledger.json')['attempts'])
            self.brief.write_text('wave two packet '+str(used))
            self.review_code('NEEDS_EVIDENCE')
            out=self.quality('review')
            if out['action']=='BUDGET_EXHAUSTED':
                break
            self.assertEqual(out['action'],'HOLD')
        self.assertEqual(len(read(self.checkpoint()/'advisor-ledger.json')['attempts']),6)
        note_b=self.home/'owner-note-b.md'; note_b.write_text('owner note B continue reviews')
        self.assertEqual(self.quality('extend','plan','--max-calls','10','--note',str(note_b))['action'],'EXTENDED')
        self.brief.write_text('final accepted packet')
        self.review_code()
        self.assertEqual(self.quality('review')['action'],'REVIEWED')
        self.assertEqual(self.quality('accept','plan','--note',str(self.note))['action'],'ACCEPTED')
        self.assertEqual(self.quality('gate')['action'],'READY')
        ledger=read(self.checkpoint()/'advisor-ledger.json')
        self.assertEqual(ledger['original_max_calls'],2)
        self.assertEqual(ledger['max_calls'],10)
        self.assertEqual(len(ledger['attempts']),7)
        self.assertEqual(len(ledger['budget_extensions']),2)
        st=self.quality('status')['stages'][0]
        self.assertEqual(st['remaining'],3)
        self.assertEqual(st['attempts'],7)
        self.assertEqual(st['extensions'],2)
        self.assertEqual(st['state'],'READY')
        self.assertEqual(json.loads(self.plan.read_text())['stages'][0]['max_calls'],2)

    def test_concurrent_reservation_versus_extension(self):
        self.exhaust_needs_evidence()
        ledger=self.checkpoint()/'advisor-ledger.json'
        decision=self.checkpoint()/'decision.json'
        note=self.owner_note()
        cfg={'advisor':{'model':'advisor','model_reasoning_effort':'medium','sandbox_mode':'read-only','developer_instructions':'no tools'}}
        ctx=multiprocessing.get_context('spawn'); q=ctx.Queue()
        ps=[ctx.Process(target=_quality_start_worker,args=(str(decision),str(ledger),cfg,q)),
            ctx.Process(target=_quality_extend_worker,args=(str(ledger),4,str(note),read(ledger)['task_id'],'plan',str(self.plan),q))]
        for p in ps: p.start()
        for p in ps: p.join(15); self.assertEqual(p.exitcode,0)
        q.get(timeout=2); q.get(timeout=2)
        state=validate_ledger(read(ledger))
        self.assertLessEqual(len(state['attempts']), state['max_calls'])
        self.assertIn(state['max_calls'], (2,4))

    def test_concurrent_identical_six_to_ten_quality_path(self):
        self.exhaust_needs_evidence()
        note=self.owner_note()
        self.assertEqual(self.quality('extend','plan','--max-calls','6','--note',str(note))['action'],'EXTENDED')
        ledger=self.checkpoint()/'advisor-ledger.json'
        before=read(ledger)
        self.assertEqual(before['max_calls'],6)
        before_attempts=before['attempts']
        before_identities=[(r.get('request_id'),r.get('fingerprint'),r.get('status'),r.get('result'),r.get('receipt')) for r in before_attempts]
        before_attempt_bytes=packed(before_attempts)
        before_digest=attempts_digest(before_attempts)
        before_extensions=list(before.get('budget_extensions') or [])
        quality_py=str(self.cli.with_name('quality.py'))
        ctx=multiprocessing.get_context('spawn')
        barrier=ctx.Barrier(2)
        q=ctx.Queue()
        args=(sys.executable,quality_py,str(self.plan),'plan',10,str(note),self.env,barrier,q)
        ps=[ctx.Process(target=_quality_cli_extend_worker,args=args) for _ in range(2)]
        for p in ps: p.start()
        for p in ps: p.join(25); self.assertEqual(p.exitcode,0)
        rows=[q.get(timeout=2), q.get(timeout=2)]
        for row in rows:
            self.assertNotIn('error', row)
            self.assertNotIn('Traceback', row.get('stderr') or '')
            self.assertEqual(row['returncode'],0)
            out=json.loads(row['stdout'])
            self.assertEqual(out['action'],'EXTENDED')
            self.assertEqual(out['effective_max_calls'],10)
            self.assertEqual(out['max_calls'],10)
        after=validate_ledger(read(ledger))
        self.assertEqual(after['max_calls'],10)
        self.assertEqual(after['original_max_calls'],2)
        self.assertEqual(len(after['budget_extensions']), len(before_extensions)+1)
        last=after['budget_extensions'][-1]
        self.assertEqual(last['previous_max_calls'],6)
        self.assertEqual(last['new_max_calls'],10)
        self.assertEqual(after['budget_extensions'][:-1], before_extensions)
        self.assertEqual(after['attempts'], before_attempts)
        self.assertEqual([(r.get('request_id'),r.get('fingerprint'),r.get('status'),r.get('result'),r.get('receipt')) for r in after['attempts']], before_identities)
        self.assertEqual(packed(after['attempts']), before_attempt_bytes)
        self.assertEqual(attempts_digest(after['attempts']), before_digest)


def _quality_start_worker(d, l, c, q):
    from advisor import operate
    from pathlib import Path
    q.put(operate('start', Path(d), Path(l), c)['action'])


def _quality_extend_worker(l, n, note, tid, did, plan, q):
    from advisor import extend_limit
    from pathlib import Path
    try:
        q.put(extend_limit(Path(l), n=n, note=note, task_id=tid, decision_id=did,
                           plan_path=plan, quality_ceiling=6)['action'])
    except Exception as e:
        q.put(type(e).__name__)


def _quality_cli_extend_worker(exe, quality_py, plan, stage, max_calls, note, env, barrier, q):
    import subprocess
    try:
        barrier.wait(timeout=10)
        p=subprocess.run([exe,quality_py,'extend',plan,stage,'--max-calls',str(max_calls),'--note',note],
                         env=env,capture_output=True,text=True,timeout=15)
        q.put({'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
    except Exception as e:
        q.put({'error':type(e).__name__,'msg':str(e)})
