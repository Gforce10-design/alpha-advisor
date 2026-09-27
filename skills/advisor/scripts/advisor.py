#!/usr/bin/env python3
"""Bounded advisor ledger and runner CLI; `run` may call the configured model."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import tomllib
import uuid

ADVISOR_MODEL_ALIASES = {
    'luna': 'gpt-5.6-luna',
    'terra': 'gpt-5.6-terra',
    'sol': 'gpt-5.6-sol',
    'astra': 'gpt-6-astra',
}
MODEL_CAPABILITY = {
    'gpt-5.6-luna': 1,
    'gpt-5.6-terra': 2,
    'gpt-5.6-sol': 3,
    'gpt-6-astra': 4,
}


def strict_object(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError('duplicate JSON key: ' + k)
        out[k] = v
    return out


def read(p):
    return json.loads(Path(p).read_text(), object_pairs_hook=strict_object,
                      parse_constant=lambda s: (_ for _ in ()).throw(ValueError(s)))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def packed(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def artifact(path):
    p = Path(path)
    if not p.is_absolute() or not p.is_file():
        raise ValueError('artifact must be an existing absolute file')
    raw = p.read_bytes()
    if not raw.strip():
        raise ValueError('empty artifact')
    return {'path': str(p.resolve()), 'sha256': digest(raw)}


def intact(ref):
    try:
        return artifact(ref['path']) == ref
    except (OSError, ValueError, KeyError, TypeError):
        return False


def settings(home):
    c = tomllib.loads((home / 'config.toml').read_text())
    a = tomllib.loads((home / 'agents/astra_advisor.toml').read_text())
    for cfg in (c, a):
        if not isinstance(cfg.get('model'), str) or not cfg['model'].strip():
            raise ValueError('missing model')
        if cfg.get('model_reasoning_effort') not in ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            raise ValueError('invalid effort')
    if a.get('sandbox_mode') != 'read-only' or not a.get('developer_instructions'):
        raise ValueError('advisor instructions/read-only required')
    enabled = a.get('enabled', True)
    if type(enabled) is not bool:
        raise ValueError('advisor enabled must be boolean')
    conductor = {k: c[k] for k in ('model', 'model_reasoning_effort')}
    advisor = {k: a[k] for k in ('model', 'model_reasoning_effort', 'sandbox_mode', 'developer_instructions')}
    main_rank = MODEL_CAPABILITY.get(ADVISOR_MODEL_ALIASES.get(c['model'], c['model']))
    advisor_rank = MODEL_CAPABILITY.get(ADVISOR_MODEL_ALIASES.get(a['model'], a['model']))
    compatible = None if main_rank is None or advisor_rank is None else advisor_rank >= main_rank
    return {'conductor': conductor, 'advisor': advisor,
            'advisor_control': {
                'enabled': enabled,
                'compatible': compatible,
                'reason': (
                    'advisor_disabled' if not enabled else
                    'advisor_weaker_than_conductor' if compatible is False else
                    'capability_order_unverified' if compatible is None else
                    'ready'),
            }}


def advisor_guard(config):
    control = config.get('advisor_control', {})
    if control.get('enabled', True) is False:
        return {'action': 'SKIP', 'reason': 'advisor_disabled'}
    if control.get('compatible') is False:
        return {'action': 'SKIP', 'reason': 'advisor_weaker_than_conductor',
                'conductor_model': config['conductor']['model'],
                'advisor_model': config['advisor']['model']}
    return None


def atomic_text(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(value)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def replace_top_level_assignment(raw, key, value):
    lines = raw.splitlines(keepends=True)
    multiline = None
    matches = []
    for index, line in enumerate(lines):
        if multiline:
            if line.count(multiline) % 2:
                multiline = None
            continue
        if re.match(r'^[ \t]*' + re.escape(key) + r'[ \t]*=', line):
            matches.append(index)
        for marker in ('"""', "'''"):
            if line.count(marker) % 2:
                multiline = marker
                break
    replacement = key + ' = ' + value
    if len(matches) > 1:
        raise ValueError('duplicate top-level advisor setting: ' + key)
    if matches:
        newline = '\n' if lines[matches[0]].endswith('\n') else ''
        lines[matches[0]] = replacement + newline
    else:
        lines.insert(0, replacement + '\n')
    return ''.join(lines)


def select_advisor_model(home, value=None):
    role = home / 'agents/astra_advisor.toml'
    raw = role.read_text()
    if value is None:
        return settings(home)
    selected = value.lower()
    enabled = selected != 'off'
    if enabled:
        model = ADVISOR_MODEL_ALIASES.get(selected, selected)
        if model not in MODEL_CAPABILITY:
            raise ValueError('advisor model must be luna, terra, sol, astra, or off')
        raw = replace_top_level_assignment(raw, 'model', json.dumps(model))
    raw = replace_top_level_assignment(raw, 'enabled', 'true' if enabled else 'false')
    before = tomllib.loads(role.read_text())
    after = tomllib.loads(raw)
    expected = dict(before)
    if enabled:
        expected['model'] = model
    expected['enabled'] = enabled
    if after != expected:
        raise ValueError('advisor model update changed unrelated role settings')
    atomic_text(role, raw)
    return settings(home)


def snapshot(d, config):
    if not isinstance(d, dict) or d.get('schema_version') != 2:
        raise ValueError('requires decision schema_version 2; legacy records cannot prove reuse')
    for k in ('task_id', 'decision_id'):
        if not isinstance(d.get(k), str) or not d[k].strip():
            raise ValueError('missing ' + k)
    flags = ('explicit_request', 'unresolved_evidence_conflict', 'architecture_change', 'hard_to_reverse_action')
    for k in flags:
        if type(d.get(k)) is not bool:
            raise ValueError('invalid ' + k)
    n = d.get('same_cause_failed_repairs')
    if type(n) is not int or n < 0:
        raise ValueError('invalid failure count')
    for k in ('evidence_files', 'failed_repair_files'):
        if not isinstance(d.get(k), list) or any(not isinstance(x, str) for x in d[k]):
            raise ValueError('invalid ' + k)
    if n != len(set(d['failed_repair_files'])):
        raise ValueError('failure count requires distinct repair/retest evidence files')
    refs = [artifact(p) for p in sorted(set(d['evidence_files'] + d['failed_repair_files']))]
    brief = artifact(d['brief_file'])
    quality_checkpoint = d.get('quality_checkpoint', False)
    if type(quality_checkpoint) is not bool:
        raise ValueError('invalid quality_checkpoint')
    reasons = [k for k in flags if d[k]]
    if quality_checkpoint:
        reasons.append('quality_checkpoint')
    reasons += ['two_same_cause_failed_repairs'] if n >= 2 else []
    bound_flags = {k: d[k] for k in flags}
    # Keep old schema-2 fingerprints stable when the optional flag is absent/false.
    if quality_checkpoint:
        bound_flags['quality_checkpoint'] = True
    binding = {'task_id': d['task_id'], 'decision_id': d['decision_id'], 'brief': brief,
               'evidence': refs, 'flags': bound_flags, 'failures': n, 'advisor': config['advisor']}
    return binding, digest(packed(binding)), reasons


def atomic(path, data):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.write-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(f, fcntl.LOCK_UN)


def attempts_digest(attempts, count=None):
    rows = attempts if count is None else attempts[:count]
    payload = [{'request_id': r.get('request_id'), 'fingerprint': r.get('fingerprint'),
                'status': r.get('status'), 'result': r.get('result'), 'receipt': r.get('receipt'),
                'quality_classification': r.get('quality_classification')}
               for r in rows]
    return digest(packed(payload))


def genesis_hash(task_id, original_max_calls):
    return digest(packed({'kind': 'ledger-baseline', 'schema_version': 2,
                          'task_id': task_id, 'original_max_calls': original_max_calls}))


def hash_extension(entry):
    return digest(packed({k: entry[k] for k in entry if k != 'hash'}))


def original_limit(state):
    if state.get('schema_version') == 3:
        return state['original_max_calls']
    return state['max_calls']


def effective_limit(state):
    return state['max_calls']


def validate_ledger(state):
    """Validate schema-2 ledgers and schema-3 extension chains. Hashes are local consistency checks, not authenticity proof."""
    if not isinstance(state, dict):
        raise ValueError('ledger must be an object')
    version = state.get('schema_version')
    if version not in (2, 3):
        raise ValueError('unsupported ledger schema_version')
    if not isinstance(state.get('task_id'), str) or not state['task_id'].strip():
        raise ValueError('ledger task_id required')
    if not isinstance(state.get('attempts'), list):
        raise ValueError('ledger attempts required')
    if type(state.get('max_calls')) is not int or not 1 <= state['max_calls'] <= 20:
        raise ValueError('invalid call limit')
    if version == 2:
        if 'original_max_calls' in state or 'budget_extensions' in state:
            raise ValueError('schema-2 ledger cannot carry extension fields')
        return state
    orig = state.get('original_max_calls')
    if type(orig) is not int or not 1 <= orig <= 20:
        raise ValueError('invalid original call limit')
    if orig >= state['max_calls']:
        raise ValueError('effective limit must exceed frozen original')
    extensions = state.get('budget_extensions')
    if not isinstance(extensions, list) or not extensions:
        raise ValueError('schema-3 ledger requires budget_extensions')
    prev_hash = genesis_hash(state['task_id'], orig)
    prev_limit = orig
    prev_used = 0
    required = ('sequence', 'task_id', 'decision_id', 'plan_path', 'previous_max_calls',
                'new_max_calls', 'used_attempts', 'attempts_digest', 'timestamp',
                'note', 'previous_hash', 'hash')
    for i, entry in enumerate(extensions, 1):
        if not isinstance(entry, dict):
            raise ValueError('extension entry must be an object')
        if any(k not in entry for k in required):
            raise ValueError('extension entry missing fields')
        if entry['sequence'] != i:
            raise ValueError('extension sequence must be consecutive')
        if entry['task_id'] != state['task_id']:
            raise ValueError('extension task binding mismatch')
        if not isinstance(entry['decision_id'], str) or not entry['decision_id'].strip():
            raise ValueError('extension decision/stage required')
        if not isinstance(entry['plan_path'], str) or not Path(entry['plan_path']).is_absolute():
            raise ValueError('extension plan_path must be absolute')
        if entry['previous_max_calls'] != prev_limit:
            raise ValueError('extension previous limit mismatch')
        if type(entry['new_max_calls']) is not int or entry['new_max_calls'] <= prev_limit:
            raise ValueError('extension must strictly increase the effective limit')
        if type(entry['used_attempts']) is not int or entry['used_attempts'] < prev_used:
            raise ValueError('extension used_attempts must be monotonic')
        if entry['used_attempts'] > len(state['attempts']):
            raise ValueError('extension used_attempts exceeds attempts')
        if attempts_digest(state['attempts'], entry['used_attempts']) != entry['attempts_digest']:
            raise ValueError('extension attempt prefix mismatch')
        if entry['previous_hash'] != prev_hash:
            raise ValueError('extension previous-entry hash mismatch')
        if hash_extension(entry) != entry['hash']:
            raise ValueError('extension hash mismatch')
        if not intact(entry.get('note', {})):
            raise ValueError('extension note missing or changed')
        if type(entry['timestamp']) not in (int, float):
            raise ValueError('extension timestamp required')
        prev_hash = entry['hash']
        prev_limit = entry['new_max_calls']
        prev_used = entry['used_attempts']
    if prev_limit != state['max_calls']:
        raise ValueError('effective limit inconsistent with extension chain')
    return state


def extension_result(state):
    n = effective_limit(state)
    orig = original_limit(state)
    used = len(state['attempts'])
    return {'action': 'EXTENDED', 'original_max_calls': orig, 'max_calls': n,
            'effective_max_calls': n, 'attempts': used, 'remaining': n - used,
            'extensions': len(state.get('budget_extensions') or []),
            'scope': 'review budget only; not acceptance, implementation, or deployment authorization'}


def extend_limit(ledger, *, n, note, task_id, decision_id, plan_path, quality_ceiling=None):
    """Commit one extension entry and the new effective limit together under the ledger lock."""
    if type(n) is not int:
        raise ValueError('extension max_calls must be an integer')
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ValueError('extension decision/stage required')
    note_ref = artifact(note)
    plan_path = str(Path(plan_path).resolve())
    if not Path(plan_path).is_absolute():
        raise ValueError('plan_path must be absolute')
    with locked(ledger):
        if not Path(ledger).is_file():
            raise ValueError('uninitialized ledger')
        state = validate_ledger(read(ledger))
        if state['task_id'] != task_id:
            raise ValueError('ledger task/stage binding mismatch')
        if any(x.get('status') == 'pending' for x in state['attempts']):
            raise ValueError('pending attempt blocks extension')
        current = effective_limit(state)
        last = (state.get('budget_extensions') or [None])[-1]
        if (last is not None and n == current and last.get('new_max_calls') == n
                and last.get('task_id') == task_id and last.get('decision_id') == decision_id
                and last.get('plan_path') == plan_path and last.get('note') == note_ref
                and intact(last.get('note', {}))):
            return extension_result(state)
        if n <= current:
            raise ValueError('extension max_calls must exceed the current effective limit')
        if quality_ceiling is not None and n > quality_ceiling:
            raise ValueError('extension max_calls exceeds quality ceiling')
        if not 1 <= n <= 20:
            raise ValueError('invalid call limit')
        orig = original_limit(state)
        preserved = state['attempts']
        if state.get('schema_version') == 2:
            state = {'schema_version': 3, 'task_id': state['task_id'],
                     'original_max_calls': orig, 'max_calls': current,
                     'attempts': preserved, 'budget_extensions': []}
            prev_hash = genesis_hash(state['task_id'], orig)
        else:
            prev_hash = state['budget_extensions'][-1]['hash']
        used = len(state['attempts'])
        before = attempts_digest(state['attempts'])
        entry = {
            'sequence': len(state['budget_extensions']) + 1,
            'task_id': state['task_id'],
            'decision_id': decision_id,
            'plan_path': plan_path,
            'previous_max_calls': current,
            'new_max_calls': n,
            'used_attempts': used,
            'attempts_digest': attempts_digest(state['attempts'], used),
            'timestamp': time.time(),
            'note': note_ref,
            'previous_hash': prev_hash,
        }
        entry['hash'] = hash_extension(entry)
        state['budget_extensions'].append(entry)
        state['max_calls'] = n
        atomic(Path(ledger), state)
        readback = validate_ledger(read(ledger))
        if readback['max_calls'] != n or original_limit(readback) != orig:
            raise ValueError('extension readback mismatch')
        if attempts_digest(readback['attempts']) != before:
            raise ValueError('extension mutated attempts')
        return extension_result(readback)


def gate(state, fingerprint, reasons, explicit):
    if any(x['status'] == 'pending' for x in state['attempts']):
        return {'action': 'BUSY'}
    # Repeating a saved explicit request is a resume, not a new consultation.
    for x in reversed(state['attempts']):
        if x['fingerprint'] == fingerprint and x['status'] == 'completed':
            if not intact(x['result']) or not intact(x['receipt']):
                return {'action': 'ERROR', 'error': 'completed artifacts missing or changed'}
            return {'action': 'REUSE', 'result': x['result'], 'request_id': x['request_id']}
    if not reasons: return {'action': 'SKIP'}
    if len(state['attempts']) >= state['max_calls']: return {'action': 'BUDGET_EXHAUSTED'}
    return {'action': 'CONSULT', 'reasons': reasons}


def operate(command, decision, ledger, config, request_id=None, receipt=None, result=None, max_calls=3,
            runtime_evidence=None, observation_grade=None):
    if command == 'fail':
        binding, fingerprint, reasons = {'task_id': read(decision)['task_id']}, None, []
    else:
        binding, fingerprint, reasons = snapshot(read(decision), config)
    with locked(ledger):
        if command == 'init':
            if ledger.exists(): raise ValueError('ledger already exists')
            if type(max_calls) is not int or not 1 <= max_calls <= 20: raise ValueError('invalid call limit')
            state = {'schema_version': 2, 'task_id': binding['task_id'], 'max_calls': max_calls, 'attempts': []}
            atomic(ledger, state)
            return {'action': 'INITIALIZED', 'max_calls': max_calls}
        state = validate_ledger(read(ledger))
        if state['task_id'] != binding['task_id']:
            raise ValueError('ledger task/schema mismatch')
        if command in ('check', 'start'):
            out = gate(state, fingerprint, reasons, binding['flags']['explicit_request'])
            if command == 'start' and out['action'] == 'CONSULT':
                rid = str(uuid.uuid4())
                state['attempts'].append({'request_id': rid, 'fingerprint': fingerprint, 'binding': binding,
                                          'status': 'pending', 'started_at': time.time()})
                atomic(ledger, state)
                out.update(request_id=rid, fingerprint=fingerprint, dispatch=config['advisor'])
            return out
        rows = [x for x in state['attempts'] if x['request_id'] == request_id]
        if len(rows) != 1 or rows[0]['status'] != 'pending': raise ValueError('no matching pending attempt')
        row = rows[0]
        ref = artifact(receipt)
        r = read(receipt)
        if r.get('request_id') != request_id or not isinstance(r.get('runtime_call_id'), str) or not r['runtime_call_id'].strip():
            raise ValueError('receipt request/runtime call mismatch')
        if command == 'fail':
            if r.get('status') not in ('failed', 'cancelled', 'unavailable'): raise ValueError('requires terminal failure receipt; timeout is not cancellation')
            row.update(status='failed', receipt=ref, ended_at=time.time())
        else:
            if fingerprint != row['fingerprint']: raise ValueError('decision/evidence/config changed during consult')
            a = row['binding']['advisor']
            if r.get('status') != 'completed' or r.get('fingerprint') != fingerprint or r.get('model') != a['model'] or r.get('effort') != a['model_reasoning_effort']:
                raise ValueError('completion binding mismatch')
            output = artifact(result)
            if output['sha256'] != r.get('result_sha256'): raise ValueError('result hash mismatch')
            usage = r.get('usage')
            if usage is not None:
                if not isinstance(usage, dict) or set(usage) != {'input_tokens', 'cached_input_tokens', 'output_tokens'} or any(type(v) is not int or v < 0 for v in usage.values()): raise ValueError('invalid usage')
                if usage['cached_input_tokens'] > usage['input_tokens']: raise ValueError('cached input exceeds input')
            row.update(status='completed', result=output, receipt=ref, ended_at=time.time(), usage=usage,
                       observation_grade=observation_grade or 'caller-supplied-runtime-receipt-not-authenticated')
            if runtime_evidence is not None:
                row['runtime_evidence'] = runtime_evidence
        atomic(ledger, state)
        return {'action': row['status'].upper(), 'request_id': request_id}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['settings', 'credentials', 'model', 'init', 'check', 'start', 'complete', 'fail', 'report', 'run'])
    p.add_argument('decision', nargs='?', type=Path)
    p.add_argument('--ledger', type=Path)
    p.add_argument('--request-id'); p.add_argument('--receipt'); p.add_argument('--result')
    p.add_argument('--max-calls', type=int, default=3)
    p.add_argument('--timeout', type=int, default=180)
    p.add_argument('--recover', action='store_true')
    args = p.parse_args()
    try:
        config = settings(Path(__file__).resolve().parents[3])
        if args.command == 'settings': out = config
        elif args.command == 'credentials':
            from runner import credential_home
            source = ('ADVISOR_AUTH_CODEX_HOME' if os.environ.get('ADVISOR_AUTH_CODEX_HOME')
                      else 'CODEX_HOME' if os.environ.get('CODEX_HOME') else 'installation_home')
            out = {'credential_home': str(credential_home()), 'source': source}
        elif args.command == 'model':
            out = select_advisor_model(
                Path(__file__).resolve().parents[3],
                str(args.decision) if args.decision else None)
        elif args.command == 'run':
            if not args.decision: raise ValueError('decision file required')
            from runner import execute
            out = execute(args.decision, args.ledger or args.decision.resolve().parent/'advisor-ledger.json', args.timeout, args.recover)
        elif args.command == 'report':
            if not args.ledger: raise ValueError('--ledger required')
            with locked(args.ledger):
                state = validate_ledger(read(args.ledger))
            rows = state['attempts']
            known = [r['usage'] for r in rows if r.get('usage') is not None]
            orig = original_limit(state); eff = effective_limit(state)
            out = {'action': 'REPORT', 'attempts': len(rows), 'pending': sum(r['status'] == 'pending' for r in rows),
                   'completed': sum(r['status'] == 'completed' for r in rows), 'failed': sum(r['status'] == 'failed' for r in rows),
                   'attempts_with_usage': len(known), 'attempts_without_usage': len(rows)-len(known),
                   'original_max_calls': orig, 'max_calls': eff, 'effective_max_calls': eff,
                   'remaining': max(0, eff - len(rows)),
                   'extensions': len(state.get('budget_extensions') or []),
                   'known_usage_only': {k: sum(u[k] for u in known) for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')},
                   'scope': 'advisor attempts only; not whole workflow cost; missing usage is unknown, not zero'}
        else:
            if not args.decision or not args.ledger: raise ValueError('decision and --ledger required')
            out = advisor_guard(config) or operate(
                args.command, args.decision, args.ledger, config,
                args.request_id, args.receipt, args.result, args.max_calls)
        print(json.dumps(out, ensure_ascii=False))
        return 2 if out.get('action') in ('ERROR','FAILED','BUDGET_EXHAUSTED','RECOVERY_REQUIRED') else 0
    except (ValueError, OSError, KeyError, TypeError) as e:
        print(json.dumps({'action': 'ERROR', 'error': str(e)})); return 2


if __name__ == '__main__': sys.exit(main())
