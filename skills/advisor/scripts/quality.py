"""Persistent quality checkpoints using the existing advisor executor; no deploy authority."""
import argparse
import fcntl
from pathlib import Path
import json
import re
import sys
from advisor import (advisor_guard, artifact, atomic, intact, locked, read, settings,
                     operate, digest, packed, extend_limit, validate_ledger,
                     original_limit, effective_limit)
import runner

QUALITY_INITIAL_MAX = 6
QUALITY_EFFECTIVE_MAX = 12
QUALITY_EXTENSION_MAX_DELTA = 4


def validate(plan):
    if not isinstance(plan,dict): raise ValueError('plan must be an object')
    if plan.get('schema_version') != 1 or not isinstance(plan.get('task_id'),str) or not plan['task_id'].strip():
        raise ValueError('quality plan schema/task required')
    stages=plan.get('stages')
    if not isinstance(stages,list) or not stages: raise ValueError('stages required')
    ids=[]
    for s in stages:
        if not isinstance(s,dict): raise ValueError('stage must be an object')
        sid=s.get('id','')
        if not sid or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in sid): raise ValueError('invalid stage id')
        ids.append(sid)
        if type(s.get('max_calls')) is not int or not 1 <= s['max_calls'] <= QUALITY_INITIAL_MAX: raise ValueError('stage max_calls must be 1..6')
        criteria=s.get('criteria')
        if not isinstance(criteria,dict) or not criteria or any(not isinstance(k,str) or not k or not isinstance(v,str) or not v.strip() for k,v in criteria.items()): raise ValueError('measurable criteria required')
        if not isinstance(s.get('evidence_files'),list) or not s['evidence_files'] or any(not isinstance(p,str) or not Path(p).is_absolute() for p in s['evidence_files']): raise ValueError('absolute evidence paths required')
    if len(set(ids)) != len(ids): raise ValueError('duplicate stage id')
    return stages


def prepare(plan_path, stage):
    plan=read(plan_path);stages=validate(plan)
    s=next((s for s in stages if s['id']==stage),None)
    if s is None: raise ValueError('unknown stage')
    root=plan_path.parent/('quality-checkpoints-'+digest(str(plan_path).encode())[:16])/stage;root.mkdir(parents=True,exist_ok=True)
    for p in s['evidence_files']: artifact(p)
    brief=root/'brief.md'
    brief.write_text('Independently assess actual artifact quality, not merely completion. Look for better approaches, maintainability, user value, correctness and evidence gaps against the criteria. Do not rubber-stamp the producer. Missing evidence means NEEDS_EVIDENCE. Return ONLY JSON, without markdown fences, shaped as '+json.dumps({'verdict':'PASS|REVISE|NEEDS_EVIDENCE','criteria':{'criterion_id':{'status':'PASS|REVISE|NEEDS_EVIDENCE','reason':'specific finding','evidence':['absolute supplied evidence path']}},'improvements':['concrete improvement or empty if none']})+'\nEvery criterion must appear exactly once. Overall PASS only if every criterion passes.\nCriteria:\n'+json.dumps(s['criteria'],ensure_ascii=False))
    decision=root/'decision.json'
    prior = read(decision) if decision.is_file() else {}
    legacy = prior.get('explicit_request') is True and 'quality_checkpoint' not in prior
    payload = dict(schema_version=2,task_id=plan['task_id']+':'+stage,decision_id=stage,
                   brief_file=str(brief),evidence_files=s['evidence_files'],
                   failed_repair_files=[],same_cause_failed_repairs=0,
                   explicit_request=legacy,unresolved_evidence_conflict=False,
                   architecture_change=False,hard_to_reverse_action=False)
    if not legacy:
        payload['quality_checkpoint'] = True
    atomic(decision,payload)
    return s,root,decision


def verdict(result, stage):
    v=read(result)
    if not isinstance(v,dict): raise ValueError('review must be an object')
    if v.get('verdict') not in ('PASS','REVISE','NEEDS_EVIDENCE') or not isinstance(v.get('criteria'),dict) or set(v['criteria'])!=set(stage['criteria']): raise ValueError('review criterion/verdict mismatch')
    for row in v['criteria'].values():
        if not isinstance(row,dict): raise ValueError('criterion finding must be an object')
        if row.get('status') not in ('PASS','REVISE','NEEDS_EVIDENCE') or not isinstance(row.get('reason'),str) or not row['reason'].strip(): raise ValueError('review finding required')
        refs=row.get('evidence')
        if not isinstance(refs,list) or any(p not in stage['evidence_files'] for p in refs) or (row['status']=='PASS' and not refs): raise ValueError('PASS requires supplied artifact evidence')
    if v['verdict']=='PASS' and any(r['status']!='PASS' for r in v['criteria'].values()): raise ValueError('contradictory PASS')
    return v


def parse_limit(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):
        raise argparse.ArgumentTypeError('integer required')
    return int(value)


def classification_marker(result_ref, stage_id, criteria, verdict_name):
    body = {'result': {'path': result_ref['path'], 'sha256': result_ref['sha256']},
            'stage': stage_id, 'criteria_contract': digest(packed(criteria)), 'verdict': verdict_name}
    return dict(body, hash=digest(packed(body)))


def validate_classification(marker, row, stage):
    if not isinstance(marker, dict):
        raise ValueError('quality classification marker invalid')
    body = {k: marker.get(k) for k in ('result', 'stage', 'criteria_contract', 'verdict')}
    if digest(packed(body)) != marker.get('hash'):
        raise ValueError('quality classification marker mismatch')
    if marker.get('result') != row.get('result') or not intact(marker.get('result', {})):
        raise ValueError('quality classification result identity mismatch')
    if marker.get('verdict') not in ('PASS', 'REVISE', 'NEEDS_EVIDENCE'):
        raise ValueError('quality classification verdict invalid')
    if not isinstance(marker.get('stage'), str) or not marker['stage'].strip():
        raise ValueError('quality classification stage invalid')
    if marker['stage'] != stage['id']:
        raise ValueError('quality classification stage mismatch')
    if not isinstance(marker.get('criteria_contract'), str) or len(marker['criteria_contract']) != 64:
        raise ValueError('quality classification contract invalid')


def record_classification(ledger, request_id, stage, verdict_obj):
    with locked(ledger):
        state = validate_ledger(read(ledger))
        row = next(r for r in state['attempts'] if r['request_id'] == request_id)
        if row.get('status') != 'completed' or not intact(row.get('result', {})):
            raise ValueError('quality classification expected an intact completed result')
        marker = classification_marker(row['result'], stage['id'], stage['criteria'], verdict_obj['verdict'])
        if row.get('quality_classification') == marker:
            return
        idx = state['attempts'].index(row)
        covered = max((e['used_attempts'] for e in state.get('budget_extensions') or []), default=0)
        if idx < covered:
            raise ValueError('cannot reclassify an attempt already bound in an extension prefix')
        row['quality_classification'] = marker
        atomic(ledger, state)


def ensure_classifications(ledger, stage):
    with locked(ledger):
        state = validate_ledger(read(ledger))
        changed = False
        for row in state.get('attempts') or []:
            if row.get('status') != 'completed':
                continue
            ref = row.get('result')
            evidence = row.get('runtime_evidence')
            if not isinstance(ref, dict) or not intact(ref) or not evidence or not all(intact(r) for r in evidence):
                raise ValueError('unclassified completed review must be normalized before extension')
            marker = row.get('quality_classification')
            if marker:
                validate_classification(marker, row, stage)
                continue
            try:
                v = verdict(Path(ref['path']), stage)
            except (ValueError, OSError, KeyError, TypeError):
                raise ValueError('unclassified completed review must be normalized before extension')
            row['quality_classification'] = classification_marker(ref, stage['id'], stage['criteria'], v['verdict'])
            changed = True
        if changed:
            atomic(ledger, state)


def reclassify_invalid_completed(ledger, request_id, error):
    with locked(ledger):
        state=validate_ledger(read(ledger))
        row=next(r for r in state['attempts'] if r['request_id']==request_id)
        idx=state['attempts'].index(row)
        covered=max((e['used_attempts'] for e in state.get('budget_extensions') or []), default=0)
        if idx < covered:
            raise ValueError('cannot reclassify an attempt already bound in an extension prefix')
        if row.get('status') != 'completed':
            raise ValueError('quality classification expected a completed attempt')
        row.update(status='failed',quality_error=str(error))
        atomic(ledger,state)


def require_classified_completed(stage, state):
    for row in state.get('attempts') or []:
        if row.get('status') != 'completed':
            continue
        ref = row.get('result')
        evidence = row.get('runtime_evidence')
        if not isinstance(ref, dict) or not intact(ref):
            raise ValueError('unclassified completed review must be normalized before extension')
        if not evidence or not all(intact(r) for r in evidence):
            raise ValueError('unclassified completed review must be normalized before extension')
        marker = row.get('quality_classification')
        if not marker:
            raise ValueError('unclassified completed review must be normalized before extension')
        validate_classification(marker, row, stage)


def bind_quality_ledger(plan_path, plan, stage, state):
    validate_ledger(state)
    sid = stage['id']
    if original_limit(state) != stage['max_calls']:
        raise ValueError('stage budget immutable after initialization')
    if effective_limit(state) > QUALITY_EFFECTIVE_MAX:
        raise ValueError('quality effective limit exceeds 12')
    if state['task_id'] != plan['task_id'] + ':' + sid:
        raise ValueError('ledger task/stage binding mismatch')
    resolved = str(Path(plan_path).resolve())
    for entry in state.get('budget_extensions') or []:
        if entry.get('decision_id') != sid or entry.get('plan_path') != resolved:
            raise ValueError('extension binding mismatch')
    return state


def reviewed(plan_path, sid):
    s,root,decision=prepare(plan_path,sid)
    ledger=root/'advisor-ledger.json'
    if not ledger.exists(): raise ValueError('review not run: '+sid)
    bind_quality_ledger(plan_path, read(plan_path), s, read(ledger))
    cfg=settings(runner.HOME)
    check=operate('check',decision,ledger,cfg)
    if check['action']!='REUSE': raise ValueError('current review missing/stale: '+sid)
    row=next(r for r in read(ledger)['attempts'] if r['request_id']==check['request_id'])
    if not row.get('runtime_evidence') or not all(intact(r) for r in row['runtime_evidence']): raise ValueError('runtime evidence missing/changed')
    v=verdict(Path(row['result']['path']),s)
    if v['verdict']!='PASS': raise ValueError('quality '+v['verdict']+': '+sid)
    return root,row


def accepted(plan_path,sid):
    root,row=reviewed(plan_path,sid)
    a=read(root/'acceptance.json')
    if a.get('request_id')!=row['request_id'] or a.get('fingerprint')!=row['fingerprint'] or a.get('result')!=row['result'] or not intact(a.get('conductor_note',{})): raise ValueError('conductor acceptance missing/stale')


def action(command, plan_path, sid, note=None, timeout=180, recover=False, max_calls=None):
    plan_path=plan_path.resolve();plan=read(plan_path);stages=validate(plan)
    ids=[s['id'] for s in stages]
    if sid not in ids: raise ValueError('unknown stage')
    # Ordered checkpoints: no dependent advancement before accepted predecessors.
    for previous in ids[:ids.index(sid)]: accepted(plan_path,previous)
    if command=='gate':
        accepted(plan_path,sid)
        return {'action':'READY','stage':sid,'scope':'quality checkpoint only; not deployment authorization'}
    if command=='accept':
        root,row=reviewed(plan_path,sid)
        ref=artifact(note)
        atomic(root/'acceptance.json',dict(request_id=row['request_id'],fingerprint=row['fingerprint'],result=row['result'],conductor_note=ref))
        return {'action':'ACCEPTED','stage':sid}
    s,root,decision=prepare(plan_path,sid);ledger=root/'advisor-ledger.json'
    if command=='extend':
        if max_calls is None: raise ValueError('extension max_calls required')
        if note is None: raise ValueError('extension note required')
        if not ledger.exists(): raise ValueError('uninitialized ledger')
        with ledger.with_suffix('.execution.lock').open('a') as execution_lock:
            try: fcntl.flock(execution_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError('another executor owns this stage')
            state=read(ledger)
            bind_quality_ledger(plan_path, plan, s, state)
            decision_data=read(decision)
            if decision_data.get('task_id') != state['task_id'] or decision_data.get('decision_id') != sid:
                raise ValueError('decision/stage binding mismatch')
            current=effective_limit(state)
            if type(max_calls) is int and max_calls > current:
                if max_calls > QUALITY_EFFECTIVE_MAX:
                    raise ValueError('quality effective limit exceeds 12')
                if max_calls - current > QUALITY_EXTENSION_MAX_DELTA:
                    raise ValueError('extension increment exceeds 4')
                ensure_classifications(ledger, s)
                require_classified_completed(s, validate_ledger(read(ledger)))
            out=extend_limit(ledger, n=max_calls, note=note, task_id=state['task_id'],
                             decision_id=sid, plan_path=plan_path, quality_ceiling=QUALITY_EFFECTIVE_MAX)
            out.update(stage=sid)
            return out
    config = settings(runner.HOME)
    blocked = advisor_guard(config)
    if blocked and not ledger.exists():
        return blocked
    if not ledger.exists(): operate('init',decision,ledger,config,max_calls=s['max_calls'])
    bind_quality_ledger(plan_path, plan, s, read(ledger))
    out=runner.execute(decision,ledger,timeout,recover)
    if out['action'] not in ('COMPLETED','REUSE'): return out
    result=out['result'];result=result['path'] if isinstance(result,dict) else result
    try:
        v=verdict(Path(result),s)
    except (ValueError,KeyError,TypeError) as e:
        # Transport completion is not a valid quality review. Preserve raw evidence,
        # consume the attempt, and permit a bounded retry of the same packet.
        reclassify_invalid_completed(ledger, out['request_id'], e)
        raise
    record_classification(ledger, out['request_id'], s, v)
    return {'action':'REVIEWED' if v['verdict']=='PASS' else 'HOLD','stage':sid,'verdict':v['verdict'],'result':result,'next':'conductor assess and accept' if v['verdict']=='PASS' else 'fix evidence/artifacts and review again within stage budget'}


def status(plan_path):
    """Resume the existing plan without launching a provider or resetting budgets."""
    plan_path=plan_path.resolve()
    plan=read(plan_path); stages=validate(plan)
    rows=[]; first=None
    for s in stages:
        sid=s['id']
        root=plan_path.parent/('quality-checkpoints-'+digest(str(plan_path).encode())[:16])/sid
        ledger=root/'advisor-ledger.json'
        used=0; orig=s['max_calls']; eff=s['max_calls']; ext=0
        state='NOT_REVIEWED'; reason='review not run'; next_action='review'
        if ledger.exists():
            data=read(ledger); used=len(data.get('attempts') or [])
            try:
                bind_quality_ledger(plan_path, plan, s, data)
                orig=original_limit(data); eff=effective_limit(data)
                ext=len(data.get('budget_extensions') or [])
                if any(r['status']=='pending' for r in data['attempts']):
                    state='PENDING'; reason='inspect existing execution; do not redispatch'; next_action='inspect/recover'
                else:
                    try:
                        reviewed(plan_path,sid)
                        try:
                            accepted(plan_path,sid)
                            state='READY'; reason='review and conductor acceptance verified'; next_action=None
                        except (ValueError,OSError,KeyError,TypeError,StopIteration) as e:
                            state='AWAITING_ACCEPTANCE'; reason=str(e); next_action='assess findings and accept'
                    except (ValueError,OSError,KeyError,TypeError,StopIteration) as e:
                        state='HOLD'; reason=str(e); next_action='inspect evidence/review'
                        if used >= eff: next_action='resolve exhausted budget; no new call'
            except (ValueError,OSError,KeyError,TypeError) as e:
                state='HOLD'; reason=str(e); next_action='restore plan budget or repair ledger'
                try:
                    orig=original_limit(data); eff=effective_limit(data)
                    ext=len(data.get('budget_extensions') or [])
                except (ValueError,KeyError,TypeError):
                    orig,eff,ext=s['max_calls'],s['max_calls'],0
        remaining=max(0, eff-used)
        if state!='READY' and first is None: first=sid
        rows.append(dict(stage=sid,state=state,reason=reason,attempts=used,max_calls=eff,
                         original_max_calls=orig,effective_max_calls=eff,remaining=remaining,
                         extensions=ext,next_action=next_action,blocked_by=first if first!=sid else None))
    return dict(action='STATUS',task_id=plan['task_id'],stages=rows,next_stage=first,
                planned_max_calls=sum(s['max_calls'] for s in stages),
                original_max_calls=sum(r['original_max_calls'] for r in rows),
                effective_max_calls=sum(r['effective_max_calls'] for r in rows),
                remaining=sum(r['remaining'] for r in rows),
                attempts=sum(r['attempts'] for r in rows),
                all_ready=first is None,provider_calls_started=0)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['review','accept','gate','status','extend']);p.add_argument('plan',type=Path);p.add_argument('stage',nargs='?')
    p.add_argument('--note');p.add_argument('--timeout',type=int,default=180);p.add_argument('--recover',action='store_true')
    p.add_argument('--max-calls',type=parse_limit,default=None)
    a=p.parse_args()
    try:
        # Serialize plan preparation and acceptance with review, including concurrent callers.
        with locked(a.plan.resolve().with_suffix('.quality-lock')):
            if a.command != 'status' and not a.stage: raise ValueError('stage required')
            out=status(a.plan) if a.command=='status' else action(a.command,a.plan,a.stage,a.note,a.timeout,a.recover,a.max_calls)
    except (ValueError,OSError,KeyError,TypeError,StopIteration) as e: out={'action':'HOLD','reason':str(e)}
    print(json.dumps(out,ensure_ascii=False))
    return 0 if out['action'] in ('READY','ACCEPTED','REVIEWED','STATUS','EXTENDED') else 2

if __name__=='__main__':sys.exit(main())
