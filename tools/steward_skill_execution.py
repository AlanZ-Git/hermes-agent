"""Scoped host delegation -> native writer -> verified, source-retaining result.
The review/plan chooses content, never grants authority. No activation tool exists.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from contextvars import ContextVar
from contextlib import contextmanager
from hermes_constants import get_hermes_home

HEX = re.compile(r'[a-f0-9]{64}\Z')
PID = re.compile(r'[a-f0-9]{8}\Z')
NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}\Z')
MAX = 4 * 1024 * 1024

class Refused(ValueError):
    pass

def require(ok, reason):
    if not ok:
        raise Refused(reason)

def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()

def sha(value):
    return hashlib.sha256(value).hexdigest()

def safe(value):
    p = Path(value)
    require(p.is_absolute() and '..' not in p.parts, 'noncanonical_path')
    require(not any(x.is_symlink() for x in (p, *p.parents)), 'symlink_path')
    return p

def read(value):
    p = safe(value)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX, 'bounded_regular_file')
        raw = stream.read(MAX + 1)
        after = os.fstat(stream.fileno())
        require(len(raw) <= MAX and (before.st_ino, before.st_size, before.st_mtime_ns) ==
                (after.st_ino, after.st_size, after.st_mtime_ns), 'read_drift')
        return raw

def decode(raw):
    def pairs(items):
        out = {}
        for k, v in items:
            require(k not in out, 'duplicate_key')
            out[k] = v
        return out
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Refused('nonfinite')))

def load(path):
    return decode(read(path))

_CLOSEOUT_RECOVERY = ContextVar('steward_closeout_recovery', default=None)


@contextmanager
def closeout_recovery(path, pending_id, digest, binding):
    """Only the exact prepared archive can recover an interrupted closeout."""
    from tools.pending_history import coordinates, expected
    p, directory = coordinates(path, pending_id, digest)
    token = None
    if not p.exists() and not (directory / 'receipt.json').exists():
        require(load(directory / 'closeout.json') == binding, 'closeout_recovery_binding')
        want = dict(expected(p, pending_id, digest, 'applied_closed'),
                    closeout_sha256=sha(read(directory / 'closeout.json')))
        require(load(directory / 'intent.json') == dict(want, status='prepared'), 'closeout_recovery_intent')
        raw = read(directory / 'source.json')
        require(sha(raw) == digest, 'closeout_recovery_source_drift')
        token = _CLOSEOUT_RECOVERY.set((str(p), pending_id, digest, raw))
    try:
        yield
    finally:
        if token is not None:
            _CLOSEOUT_RECOVERY.reset(token)


def historical_record(path, pending_id, digest):
    from tools.pending_history import historical_read
    recovery = _CLOSEOUT_RECOVERY.get()
    if recovery is not None and recovery[:3] == (str(path), pending_id, digest):
        return decode(recovery[3])
    return decode(historical_read(path, pending_id, digest))

def store_new(path, value):
    path = safe(path)
    raw = packed(value) + b'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    require(read(path) == raw, 'record_readback')

def operations(record):
    payload = record['payload']
    require(isinstance(payload, dict), 'payload_shape')
    if payload.get('action') == 'batch':
        require(set(payload) == {'action', 'operations'}, 'batch_fields')
        ops = payload['operations']
    else:
        ops = [payload]
    require(isinstance(ops, list) and 1 <= len(ops) <= 20 and all(isinstance(x, dict) for x in ops), 'bounded_operations')
    return ops

def plan_targets(record):
    """Compute exact final bytes before invoking the existing atomic native writer."""
    from tools import skill_manager_tool as sm
    files, entries = {}, []
    allowed = {'action', 'name', 'file_path', 'old_string', 'new_string', 'replace_all', 'file_content'}
    for ordinal, op in enumerate(operations(record)):
        require(set(op) <= allowed, 'operation_fields')
        action, name = op.get('action'), op.get('name')
        require(action in ('patch', 'write_file') and isinstance(name, str) and NAME.fullmatch(name), 'existing_skill_text_only')
        found = sm._find_skill(name)
        require(found is not None, 'existing_skill_required')
        skill = safe(Path(found['path']).absolute())
        require(skill.name == name, 'resolved_skill_identity')
        rel = op.get('file_path') or 'SKILL.md'
        relative = Path(rel)
        require(not relative.is_absolute() and '..' not in relative.parts and str(relative) == rel, 'relative_target')
        require(rel == 'SKILL.md' or len(relative.parts) > 1 and relative.parts[0] in ('references', 'templates', 'assets'), 'no_script_target')
        require(relative.suffix in ('.md', '.txt'), 'text_target_only')
        target = safe(skill / relative)
        if target not in files:
            before = read(target) if target.exists() else None
            files[target] = {'before': before, 'after': before}
        state = files[target]
        text = state['after'].decode() if state['after'] is not None else None
        if action == 'patch':
            old, new = op.get('old_string'), op.get('new_string')
            require(text is not None and isinstance(old, str) and old and isinstance(new, str) and new and old != new, 'exact_patch_required')
            require(type(op.get('replace_all', False)) is bool and text.count(old) == 1 and 'file_content' not in op, 'unique_patch_anchor')
            after = text.replace(old, new, 1)
            if rel == 'SKILL.md' and text.startswith('---\n'):
                end = text.find('\n---\n', 4)
                require(end >= 0 and after[:end + 5] == text[:end + 5] and text.find(old) >= end + 5, 'frontmatter_immutable')
        else:
            require(rel != 'SKILL.md' and not any(k in op for k in ('old_string', 'new_string', 'replace_all')), 'support_file_only')
            after = op.get('file_content')
            require(isinstance(after, str) and after, 'nonempty_file_content')
        state['after'] = after.encode()
        require(state['after'] != state['before'], 'no_effect_payload')
        entries.append({'ordinal': ordinal, 'action': action, 'name': name, 'file_path': rel, 'target': str(target)})
    targets = [{'path': str(p), 'preimage': sha(x['before']) if x['before'] is not None else None,
                'postimage': sha(x['after'])} for p, x in sorted(files.items())]
    return targets, entries

def delegate(task_id):
    from tools.steward_skill_delegation import get_delegation
    from tools.approval_context import _is_cron_approval_context
    d = get_delegation()
    require(isinstance(d, dict) and d.get('enabled') is True, 'host_delegation_inactive')
    require(set(d) == {'enabled', 'home', 'state', 'cron_id', 'pending_roots', 'target_roots'}, 'delegation_shape')
    require(str(safe(get_hermes_home())) == d['home'], 'delegated_profile_only')
    require(isinstance(d['cron_id'], str) and d['cron_id'] and _is_cron_approval_context() and
            isinstance(task_id, str) and task_id.startswith('cron:' + d['cron_id'] + ':') and
            bool(task_id.split(':', 2)[2]), 'original_cron_identity_required')
    safe(d['state'])
    require(d['pending_roots'] and d['target_roots'], 'bounded_roots_required')
    for value in d['pending_roots'] + d['target_roots']:
        safe(value)
    from tools.write_approval import write_approval_enabled
    require(write_approval_enabled('skills'), 'write_approval_must_remain_on')
    return d

def under(path, roots):
    p = safe(path)
    require(any(p.is_relative_to(safe(root)) for root in roots), 'outside_delegated_roots')
    return p

def paths(state, event):
    require(isinstance(event, str) and HEX.fullmatch(event), 'exact_event_id')
    state = safe(state)
    return state / 'execution-plans' / (event + '.json'), state / 'execution-results' / (event + '.json')

def stage_record(home, plan, *, historical=False):
    staged = plan['stage']
    require(set(staged) == {'id', 'sha256'} and isinstance(staged['id'], str) and PID.fullmatch(staged['id']), 'stage_identity')
    p = safe(Path(home) / 'pending/skills' / (staged['id'] + '.json'))
    if historical:
        record = historical_record(p, staged['id'], staged['sha256'])
    else:
        require(sha(read(p)) == staged['sha256'], 'staged_source_drift')
        record = load(p)
    require(record.get('id') == staged['id'] and record.get('subsystem') == 'skills', 'stage_record_identity')
    return record

PARTIAL = 'steward-execution-plan/v2-partial-content'
RETAINED = 'Object-specific obligation retained only at original source ordinal.'


def partial(plan):
    return plan.get('schema') == PARTIAL


def retained_obligations(plan):
    """Public coordinates only: never materialize source content in a receipt."""
    return [{'source': {k: source[k] for k in ('scope', 'id', 'path', 'sha256')},
             'ordinal': row['ordinal'], 'part': item['part'], 'description': RETAINED}
            for source in plan['sources'] for row in source['coverage']
            for item in row['obligations'] if item['disposition'] == 'retained']


def remaining_obligations(result):
    if partial(result['plan']):
        return [f"{x['source']['scope']}:{x['source']['id']} ordinal {x['ordinal']} part {x['part']}: "
                + RETAINED for x in result['remaining']]
    return result['plan']['outstanding']


def completion(plan):
    if not partial(plan):
        return {'business_complete': not plan['outstanding']}
    return {'business_complete': False, 'content_complete': True,
            'source_complete': False, 'remaining': retained_obligations(plan)}


def partial_row(row, d, net_ops, plan):
    # Exhaustiveness and every op's semantics remain delegated professional judgment.
    # The explicit partition is authenticated with the plan; SHA is NOT semantic proof.
    require(set(row) == {'ordinal', 'all_obligations_accounted', 'obligations'} and
            row['all_obligations_accounted'] is True, 'explicit_exhaustive_partition_required')
    items = row['obligations']
    require(isinstance(items, list) and 1 <= len(items) <= 64 and
            all(isinstance(x, dict) and type(x.get('part')) is int for x in items) and
            sorted(x['part'] for x in items) == list(range(len(items))), 'complete_obligation_partition')
    net = set()
    for item in items:
        disposition = item.get('disposition')
        common = {'part', 'disposition', 'description'}
        if disposition == 'retained':
            require(set(item) == common and item['description'] == RETAINED,
                    'sanitized_retained_pointer_only')
            continue
        require(disposition in ('executed', 'reused') and
                isinstance(item.get('description'), str) and item['description'].strip() and
                isinstance(item.get('basis'), str) and item['basis'].strip(), 'obligation_semantic_basis')
        if disposition == 'executed':
            require(set(item) == common | {'operations', 'basis'}, 'executed_obligation_fields')
            indices = item['operations']
            require(isinstance(indices, list) and indices and
                    all(type(i) is int and 0 <= i < net_ops for i in indices) and
                    len(set(indices)) == len(indices), 'coverage_operation_binding')
            net.update(indices)
        else:
            require(set(item) == common | {'target', 'basis'}, 'reused_obligation_fields')
            target = item['target']
            require(isinstance(target, dict) and set(target) == {'path', 'sha256', 'text'}, 'reuse_target_fields')
            p = under(target['path'], d['target_roots'])
            require(p.suffix in ('.md', '.txt') and
                    str(p) not in {t['path'] for t in plan['targets']}, 'stable_reuse_text_target')
            raw = read(p)
            require(sha(raw) == target['sha256'] and isinstance(target['text'], str) and
                    target['text'].strip() and target['text'] in raw.decode(), 'reuse_postimage_drift')
    return net


def coverage(plan, d, net_ops, *, historical=False):
    """Verify every frozen source operation has an explicit professional coverage map.
    Mapping rationale is a delegated semantic judgment, not proved by SHA alone.
    """
    sources = plan['sources']
    require(isinstance(sources, list) and 1 <= len(sources) <= 16, 'bounded_sources')
    seen, net = set(), set()
    for source in sources:
        require(set(source) == {'scope', 'path', 'id', 'sha256', 'coverage'}, 'source_fields')
        p = under(source['path'], d['pending_roots'])
        require(p.name == source['id'] + '.json' and PID.fullmatch(source['id']) and str(p) not in seen, 'exact_unique_source')
        seen.add(str(p))
        if historical:
            record = historical_record(p, source['id'], source['sha256'])
        else:
            require(sha(read(p)) == source['sha256'], 'original_source_drift')
            record = load(p)
        require(record.get('id') == source['id'] and record.get('subsystem') == 'skills', 'original_source_identity')
        count = len(operations(record))
        rows = source['coverage']
        require(isinstance(rows, list) and len(rows) == count and
                all(type(x.get('ordinal')) is int for x in rows) and
                sorted(x['ordinal'] for x in rows) == list(range(count)), 'complete_source_coverage_required')
        for row in rows:
            if partial(plan):
                net.update(partial_row(row, d, net_ops, plan))
                continue
            require(set(row) == {'ordinal', 'operations', 'basis'} and isinstance(row['basis'], str) and row['basis'].strip(), 'coverage_basis')
            require(isinstance(row['operations'], list) and row['operations'] and
                    all(type(i) is int and 0 <= i < net_ops for i in row['operations']) and
                    len(set(row['operations'])) == len(row['operations']), 'coverage_operation_binding')
            net.update(row['operations'])
    require(net == set(range(net_ops)), 'unbound_net_operation')
    if partial(plan):
        require(plan['outstanding'] == [] and retained_obligations(plan), 'explicit_partial_retained_required')
        stage_path = str(Path(d['home']) / 'pending/skills' / (plan['stage']['id'] + '.json'))
        require(stage_path not in seen, 'partial_requires_separate_clean_stage')

def current_targets(targets, post=False):
    require(isinstance(targets, list) and targets, 'targets_required')
    for target in targets:
        require(set(target) == {'path', 'preimage', 'postimage'}, 'target_fields')
        p = safe(target['path'])
        require((sha(read(p)) if p.exists() else None) == target['postimage' if post else 'preimage'], 'target_postimage_drift' if post else 'target_preimage_drift')

def review_modules():
    """Load only the fixed current HOME/scripts modules, including in Core tool workers."""
    import importlib.util
    import sys
    scripts = safe(get_hermes_home() / 'scripts')
    modules = []
    for name in ('steward_triage_review', 'steward_triage_action'):
        path = safe(scripts / (name + '.py'))
        if name in sys.modules:
            module = sys.modules[name]
            require(Path(module.__file__).absolute() == path, 'review_module_origin')
        else:
            spec = importlib.util.spec_from_file_location(name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(name, None)
                raise
        modules.append(module)
    return modules


def review_identity(state, event, policy=None):
    r, a = review_modules()
    policy = policy or r.Policy()
    require(str(policy.state) == str(state), 'review_state_owner')
    return a.base(policy, event, time.time())

def validate_plan(plan, state, event, d, policy=None):
    require(set(plan) == {'schema', 'event_id', 'identity', 'decision', 'stage', 'sources', 'targets', 'outstanding'}, 'execution_plan_fields')
    require(plan['schema'] in ('steward-execution-plan/v1', PARTIAL) and plan['event_id'] == event and plan['decision'] == 'approved', 'approved_exact_plan_required')
    require(plan['identity'] == review_identity(state, event, policy), 'stale_review_identity')
    tokens = [sha(packed([s['scope'], s['id'], s['path'], s['sha256']])) for s in plan['sources']]
    require(plan['identity']['binding']['source_token'] in tokens, 'event_source_coverage_required')
    require(isinstance(plan['outstanding'], list) and all(isinstance(x, str) and x.strip() for x in plan['outstanding']), 'outstanding_obligations')
    record = stage_record(d['home'], plan)
    computed, entries = plan_targets(record)
    require(computed == plan['targets'], 'exact_plan_drift')
    coverage(plan, d, len(entries))
    for entry in entries:
        under(entry['target'], d['target_roots'])
    # Upstream removed personal Skill Sync and its writer callback; the native
    # writer now has no remote-sync side effect. Root admission remains exact.
    current_targets(computed)
    return record, entries

def _result_record(state, event, policy=None):
    """Authenticate the original execution inputs; never relax new-write admission."""
    pp, rp = paths(state, event)
    if not rp.exists():
        return None
    result, plan = load(rp), load(pp)
    require(result.get('schema') == 'steward-execution-result/v1' and result.get('event_id') == event and
            result.get('plan_sha256') == sha(read(pp)) and result.get('plan') == plan, 'result_plan_binding')
    require(set(plan) == {'schema', 'event_id', 'identity', 'decision', 'stage', 'sources', 'targets', 'outstanding'} and
            plan['schema'] in ('steward-execution-plan/v1', PARTIAL) and plan['event_id'] == event and
            plan['decision'] == 'approved', 'approved_exact_plan_required')
    # Keep the original live review check: missing/rewritten/expired history is not success.
    require(plan['identity'] == review_identity(state, event, policy), 'result_review_drift')
    require(isinstance(plan['outstanding'], list) and
            all(isinstance(x, str) and x.strip() for x in plan['outstanding']), 'outstanding_obligations')
    import math
    completed = result.get('completed_at')
    require(type(completed) in (int, float) and math.isfinite(completed) and
            0 < completed <= time.time(), 'result_completion_time')
    delegation = result['delegation']
    require(isinstance(delegation, dict) and
            set(delegation) == {'enabled', 'home', 'state', 'cron_id', 'pending_roots', 'target_roots'} and
            delegation.get('enabled') is True and
            delegation['home'] == str(safe(get_hermes_home())) and delegation['state'] == str(state) and
            isinstance(delegation['cron_id'], str) and delegation['cron_id'] and delegation['pending_roots'] and
            delegation['target_roots'], 'result_delegation_binding')
    require(result.get('delegation_sha256') == sha(packed(delegation)) and
            isinstance(result.get('task_id'), str) and result['task_id'].startswith('cron:' + delegation['cron_id'] + ':') and
            bool(result['task_id'].split(':', 2)[2]), 'result_execution_identity')
    # The existing write-ahead record must bind the same historical execution.
    started = load(rp.with_suffix('.started'))
    require(started.get('plan') == plan and started.get('plan_sha256') == result['plan_sha256'] and
            started.get('task_id') == result['task_id'] and
            started.get('delegation_sha256') == result['delegation_sha256'] and
            type(started.get('started_at')) in (int, float) and
            0 < started['started_at'] <= completed, 'result_started_binding')
    record = stage_record(delegation['home'], plan, historical=True)
    entries = _record_operations(record, delegation)
    require(result.get('operations') == entries, 'result_operations_binding')
    coverage(plan, delegation, len(entries), historical=True)
    tokens = [sha(packed([s['scope'], s['id'], s['path'], s['sha256']])) for s in plan['sources']]
    require(plan['identity']['binding']['source_token'] in tokens, 'event_source_coverage_required')
    targets = plan['targets']
    require(isinstance(targets, list) and targets and
            len({t['path'] for t in targets}) == len(targets) and
            {t['path'] for t in targets} == {x['target'] for x in entries}, 'result_target_binding')
    for target in targets:
        require(set(target) == {'path', 'preimage', 'postimage'} and
                isinstance(target['postimage'], str) and HEX.fullmatch(target['postimage']) and
                (target['preimage'] is None or isinstance(target['preimage'], str) and
                 HEX.fullmatch(target['preimage'])) and
                target['preimage'] != target['postimage'], 'target_fields')
        under(target['path'], delegation['target_roots'])
    require(all(result.get(k) == v and type(result.get(k)) is type(v)
                for k, v in completion(plan).items()) and result.get('source_retained') is True and
            result.get('status') == 'applied_source_retained', 'result_completion_semantics')
    return result, record


def _record_operations(record, delegation):
    """Resolve frozen operation coordinates without applying them to today's bytes."""
    from tools import skill_manager_tool as sm
    entries = []
    allowed = {'action', 'name', 'file_path', 'old_string', 'new_string', 'replace_all', 'file_content'}
    for ordinal, op in enumerate(operations(record)):
        action, name = op.get('action'), op.get('name')
        require(set(op) <= allowed and action in ('patch', 'write_file') and
                isinstance(name, str) and NAME.fullmatch(name), 'existing_skill_text_only')
        found = sm._find_skill(name)
        require(found is not None, 'existing_skill_required')
        skill = safe(Path(found['path']).absolute())
        require(skill.name == name, 'resolved_skill_identity')
        rel = op.get('file_path') or 'SKILL.md'
        relative = Path(rel)
        require(not relative.is_absolute() and '..' not in relative.parts and str(relative) == rel and
                (rel == 'SKILL.md' or len(relative.parts) > 1 and
                 relative.parts[0] in ('references', 'templates', 'assets')) and
                relative.suffix in ('.md', '.txt'), 'relative_target')
        if action == 'patch':
            old, new = op.get('old_string'), op.get('new_string')
            require(isinstance(old, str) and old and isinstance(new, str) and new and old != new and
                    type(op.get('replace_all', False)) is bool and 'file_content' not in op,
                    'exact_patch_required')
        else:
            require(rel != 'SKILL.md' and not any(k in op for k in ('old_string', 'new_string', 'replace_all')) and
                    isinstance(op.get('file_content'), str) and op['file_content'], 'support_file_only')
        target = under(str(skill / relative), delegation['target_roots'])
        entries.append({'ordinal': ordinal, 'action': action, 'name': name, 'file_path': rel, 'target': str(target)})
    return entries


def _target_preimage(result, record, target, post, *, chained=False):
    """Reverse exact patches and check the frozen before hash, not a claimed edge."""
    require(sha(post) == target['postimage'], 'target_postimage_drift')
    text = post.decode()
    selected = [op for op, entry in zip(operations(record), result['operations'])
                if entry['target'] == target['path']]
    for op in reversed(selected):
        if op['action'] == 'write_file':
            require(text == op['file_content'] and not chained, 'unprovable_successor_overwrite')
            # A write_file has no recoverable preimage bytes; never use it as a chain edge.
            return None
        old, new = op['old_string'], op['new_string']
        require(text.count(new) == 1, 'unprovable_patch_postimage')
        before = text.replace(new, old, 1)
        require(before.count(old) == 1, 'unique_patch_anchor')
        if target['path'].endswith('/SKILL.md') and before.startswith('---\n'):
            end = before.find('\n---\n', 4)
            require(end >= 0 and text[:end + 5] == before[:end + 5] and
                    before.find(old) >= end + 5, 'frontmatter_immutable')
        text = before
    raw = text.encode()
    require(sha(raw) == target['preimage'], 'result_patch_preimage_drift')
    return raw



# Fixed, reviewed evidence is code-bound, never a writable queue or admission policy.
def preservation_binding():
    """Private host installation binds historical certificates; absent means no exception."""
    from tools.steward_skill_delegation import get_preservation_binding
    return get_preservation_binding()


def _reviewed_preservation(result, record, target, ordinal, visible, terminal, chain):
    """Exact reviewed replacement only; authentication/reversal remain mandatory.

    Semantic preservation is an explicit professional assertion, not inferred from
    hashes or reversibility. The pinned certificate binds each source obligation,
    old operation, actual successor operation and full transformed contribution.
    """
    binding = preservation_binding()
    if binding is None:
        return False
    certificate, digest = binding
    require(sha(read(certificate)) == digest, 'preservation_certificate_drift')
    data = load(certificate)
    require(data.get('schema') == 'steward-reviewed-preservation/v1', 'preservation_schema')
    rows = [row for row in data['rows'] if row['original']['event_id'] == result['event_id']
            and row['target'] == target['path'] and row['ordinal'] == ordinal]
    if not rows:
        return False
    require(len(rows) == 1 and chain, 'preservation_exact_chain_required')
    row = rows[0]
    anchors = [i for i, item in enumerate(chain)
               if item[0]['event_id'] == row['successor']['event_id']]
    require(len(anchors) == 1, 'preservation_exact_anchor_required')
    anchor = anchors[0]
    successor, next_record, next_target = chain[anchor]
    state = result['delegation']['state']
    def binding(historical):
        pp, rp = paths(state, historical['event_id'])
        return {'event_id': historical['event_id'], 'result_sha256': sha(read(rp)),
                'plan_sha256': sha(read(pp)), 'stage_sha256': historical['plan']['stage']['sha256'],
                'sources_sha256': sha(packed(historical['plan']['sources']))}
    require([binding(item[0]) for item in chain] == row['chain'], 'preservation_chain_binding')
    # This fallback cannot promote partial or unresolved obligations to whole success.
    require(not partial(result['plan']) and result['business_complete'] is True and
            all(not partial(item[0]['plan']) and item[0]['business_complete'] is True for item in chain),
            'preservation_whole_results_only')
    state = result['delegation']['state']
    for historical, binding in ((result, row['original']), (successor, row['successor'])):
        pp, rp = paths(state, historical['event_id'])
        require(binding == {'event_id': historical['event_id'],
                'result_sha256': sha(read(rp)), 'plan_sha256': sha(read(pp)),
                'stage_sha256': historical['plan']['stage']['sha256'],
                'sources_sha256': sha(packed(historical['plan']['sources']))},
                'preservation_input_binding')
    op = operations(record)[ordinal]
    next_ordinal = row['successor_ordinal']
    next_op = operations(next_record)[next_ordinal]
    require(sha(packed(op)) == row['original_operation_sha256'] and
            sha(packed(next_op)) == row['successor_operation_sha256'] and
            successor['operations'][next_ordinal]['target'] == target['path'] and
            next_target['path'] == target['path'] and next_op['action'] == 'patch' and
            next_op.get('replace_all', False) is False, 'preservation_operation_binding')
    expected = [{'scope': s['scope'], 'id': s['id'], 'sha256': s['sha256'],
                 'ordinal': c['ordinal'], 'net_operation': ordinal,
                 'disposition': 'preserved_and_strengthened', 'basis': c['basis']}
                for s in result['plan']['sources'] for c in s['coverage']
                if ordinal in c['operations']]
    assertions = row['obligations']
    require(expected and len(assertions) == len(expected) and row['judgments'],
            'preservation_obligation_binding')
    for assertion, coordinate in zip(assertions, expected):
        require({k: assertion[k] for k in coordinate} == coordinate and
                isinstance(assertion['preservation_basis'], str) and assertion['preservation_basis'].strip(),
                'preservation_obligation_binding')
        source = next(s for s in result['plan']['sources'] if
                      s['scope'] == coordinate['scope'] and s['id'] == coordinate['id'])
        source_op = operations(historical_record(source['path'], source['id'], source['sha256']))[coordinate['ordinal']]
        require(sha(packed(source_op)) == assertion['source_operation_sha256'],
                'preservation_source_ordinal_binding')
    require(sha(terminal.encode()) == row['terminal_sha256'], 'preservation_terminal_drift')
    old, new = next_op['old_string'], next_op['new_string']
    for judgment in row['judgments']:
        require(judgment['old_clause'] in old and judgment['new_clause'] == new and
                judgment['basis'].strip(), 'preservation_clause_binding')
    require(sum(block.count(old) for block in visible) == 1, 'preservation_exact_replacement')
    transformed = [block.replace(old, new, 1) if old in block else block for block in visible]
    # Every post-anchor operation is explicitly reviewed and pinned, including
    # operations outside the original contribution. Reachability is not semantics.
    later = [(historical, index, op) for historical, rec, _ in chain[anchor + 1:]
             for index, (op, entry) in enumerate(zip(operations(rec), historical['operations']))
             if entry['target'] == target['path']]
    continuations = row.get('continuations', [])
    require(len(continuations) == len(later), 'preservation_continuation_binding')
    for assertion, (historical, index, op) in zip(continuations, later):
        require(set(assertion) == {'event_id', 'ordinal', 'operation_sha256', 'basis'} and
                assertion['event_id'] == historical['event_id'] and assertion['ordinal'] == index and
                assertion['operation_sha256'] == sha(packed(op)) and
                isinstance(assertion['basis'], str) and assertion['basis'].strip() and
                op['action'] == 'patch' and op.get('replace_all', False) is False,
                'preservation_continuation_binding')
        old, new = op['old_string'], op['new_string']
        count = sum(block.count(old) for block in transformed)
        require(count <= 1, 'preservation_continuation_ambiguous')
        # For changes outside a visible delta, the full original contribution
        # still has to survive unchanged; a partial overlap never proves retention.
        transformed = [block.replace(old, new, 1) if old in block else block
                       for block in transformed]
    require(all(block and block in terminal for block in transformed),
            'preservation_contribution_missing')
    return True


def verified_result(state, event, policy=None):
    """Only an unambiguous, authenticated native-success chain can advance a postimage."""
    _, rp = paths(state, event)
    if not rp.exists():
        return None
    # Discovery is not authentication: malformed matching successors must fail, not disappear.
    catalog = {}
    for path in sorted(safe(Path(state) / 'execution-results').glob('*.json')):
        try:
            catalog[path.stem] = load(path)
        except (ValueError, OSError, KeyError, TypeError):
            continue  # effect_index reports this file independently; it cannot prove an edge.
    authenticated, verified, active = {}, {}, set()
    additive = {}  # Per-target preservation across every authenticated successor operation.
    chains = {}

    def check(key):
        if key in verified:
            return verified[key]
        require(key not in active, 'result_successor_cycle')
        require(len(active) < 128, 'bounded_result_successor_chain')
        active.add(key)
        authenticated[key] = _result_record(state, key, policy)
        result, record = authenticated[key]
        images = {}
        for target in result['plan']['targets']:
            additive[key, target['path']] = True
            chains[key, target['path']] = []
            successors = []
            for candidate, raw in catalog.items():
                if not isinstance(raw, dict) or not isinstance(raw.get('plan'), dict):
                    continue
                rows = raw['plan'].get('targets')
                if isinstance(rows, list) and any(isinstance(t, dict) and
                        t.get('path') == target['path'] and t.get('preimage') == target['postimage']
                        for t in rows):
                    successors.append(candidate)
            require(len(successors) <= 1, 'ambiguous_result_successor')
            if successors:
                next_key = successors[0]
                require(next_key not in active, 'result_successor_cycle')
                # Authenticate before comparing chronology/host; no fake-success fallback.
                next_result, next_record = _result_record(state, next_key, policy)
                require(next_result['delegation'] == result['delegation'] and
                        next_result['completed_at'] > result['completed_at'], 'result_successor_identity_or_time')
                next_images = check(next_key)
                next_target = next(t for t in next_result['plan']['targets'] if t['path'] == target['path'])
                chains[key, target['path']] = [(next_result, next_record, next_target)] + chains[next_key, target['path']]
                post = _target_preimage(next_result, next_record, next_target,
                                       next_images[target['path']], chained=True)
                additive[key, target['path']] = additive[next_key, target['path']] and all(
                    op['action'] == 'patch' and op.get('replace_all', False) is False and
                    op['new_string'].count(op['old_string']) == 1
                    for op, entry in zip(operations(next_record), next_result['operations'])
                    if entry['target'] == target['path'])
                # Reachability alone is not preservation of the original adopted content.
                from difflib import SequenceMatcher
                terminal = read(target['path']).decode()
                for ordinal, (op, entry) in enumerate(zip(operations(record), result['operations'])):
                    if entry['target'] == target['path']:
                        # Compare whole lines, not scattered matching character fragments.
                        new_lines = op['new_string'].split('\n') if op['action'] == 'patch' else []
                        visible = [op['file_content']] if op['action'] == 'write_file' else [
                            '\n'.join(new_lines[j:k]) for tag, _, _, j, k in
                            SequenceMatcher(None, op['old_string'].split('\n'), new_lines,
                                            autojunk=False).get_opcodes()
                            if tag in ('insert', 'replace')]
                        # No positive delta (deletion-only): retain the conservative full-string obligation.
                        if not visible or not all(visible):
                            visible = [op['new_string']]
                        require(all(block in terminal for block in visible) or
                                (op['action'] == 'write_file' and target['preimage'] is None and
                                 additive[key, target['path']]) or
                                _reviewed_preservation(result, record, target, ordinal, visible, terminal,
                                                       chains[key, target['path']]),
                                'adopted_content_not_visible')
            else:
                post = read(target['path'])
            _target_preimage(result, record, target, post)
            images[target['path']] = post
        active.remove(key)
        verified[key] = images
        return images

    check(event)
    return authenticated[event][0]

def _failed_source_tokens(state, event):
    """Conservative input-coordinate union, never evidence of adoption."""
    try:
        pp, rp = paths(state, event)
        docs = [load(pp)]
        home = None
        if rp.exists():
            result = load(rp)
            docs.append(result['plan'])
            home = result['delegation']['home']
        started = rp.with_suffix('.started')
        if started.exists():
            docs.append(load(started)['plan'])
        if home is None:
            from tools.steward_skill_delegation import get_delegation
            home = get_delegation()['home']
        tokens = set()
        for plan in docs:
            require(plan['sources'], 'failed_sources_unknown')
            for source in plan['sources']:
                tokens.add(sha(packed([source['scope'], source['id'], source['path'], source['sha256']])))
            stage = plan['stage']
            stage_path = str(Path(home) / 'pending/skills' / (stage['id'] + '.json'))
            tokens.add(sha(packed(['steward', stage['id'], stage_path, stage['sha256']])))
        return sorted(tokens)
    except (ValueError, OSError, KeyError, TypeError):
        return None


def effect_index(state, policy=None):
    """One read-only pass; all consumers share exact frozen-source coverage facts."""
    sources, stages, errors = {}, set(), []
    directory = safe(Path(state) / 'execution-results')
    if not directory.exists():
        return sources, stages, errors
    for path in sorted(directory.glob('*.json')):
        try:
            result = verified_result(state, path.stem, policy)
            if not result:
                continue
            plan = result['plan']
            for source in plan['sources']:
                token = sha(packed([source['scope'], source['id'], source['path'], source['sha256']]))
                # Never let another result's whole-source effect hide retained work.
                if token not in sources or partial(plan) or not partial(sources[token]['plan']):
                    sources[token] = result
            stages.add((str(Path(result['delegation']['home']) / 'pending/skills' /
                            (plan['stage']['id'] + '.json')), plan['stage']['sha256']))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append({'event_id': path.stem, 'error': 'execution_result_invalid:' + type(exc).__name__,
                           'source_tokens': _failed_source_tokens(state, path.stem)})
    for path in sorted(directory.glob('*.started')):
        if not path.with_suffix('.json').exists():
            errors.append({'event_id': path.stem, 'error': 'execution_result_unknown',
                           'source_tokens': _failed_source_tokens(state, path.stem)})
    return sources, stages, errors


def covered_event(state, event, index, policy=None):
    r, _ = review_modules()
    policy = policy or r.Policy()
    item = r.queue(policy)[event]
    if item.get('status') == 'decided':
        return None
    return index.get(item['source_token'])


def filter_work(result, state, policy=None):
    sources, stages, errors = effect_index(state, policy)
    remaining, applied, terminal = [], [], []
    for row in result.get('work', []):
        token = sha(packed([row['scope'], row['id'], row['source'], row['sha256']]))
        if not isinstance(row['id'], str) or not PID.fullmatch(row['id']) or Path(row['source']).suffix != '.json':
            remaining.append(row)
            continue
        effect = sources.get(token)
        relevant_errors = [e for e in errors if e.get('source_tokens') is None or token in e['source_tokens']]
        from tools.pending_history import archived, coordinates
        try:
            p, directory = coordinates(row['source'], row['id'], row['sha256'])
            if not p.exists() and (directory / 'receipt.json').exists():
                archived(p, row['id'], row['sha256'])
                if not relevant_errors and not (effect is not None and partial(effect['plan'])):
                    terminal.append(row)
                    continue
        except (ValueError, OSError, KeyError, TypeError) as exc:
            errors.append({'id': row['id'], 'error': 'pending_terminal_invalid:' + type(exc).__name__,
                           'source_tokens': [token]})
            remaining.append(row)
            continue
        if not relevant_errors and ((effect is not None and not partial(effect['plan'])) or (
                effect is None and (row['source'], row['sha256']) in stages)):
            applied.append(row)
        else:
            remaining.append(row)
    out = dict(result, work=remaining, execution_applied_work=applied,
               terminal_disposition_work=terminal, execution_errors=errors)
    # Preflight may carry independent resolution/failure wake reasons: preserve them.
    if (applied or terminal) and not remaining and not errors and result.get('status') == 'ok':
        out['wakeAgent'] = bool(result.get('resolution_work') or result.get('review_work') or result.get('action_work'))
    return out


def execution_ready(state, event, policy=None):
    from tools.steward_skill_delegation import get_delegation
    d = get_delegation()
    if not isinstance(d, dict) or d.get('enabled') is not True or d.get('home') != str(get_hermes_home()) or d.get('state') != str(state):
        return False
    pp, rp = paths(state, event)
    if not pp.exists() or rp.exists() or rp.with_suffix('.started').exists():
        return False
    validate_plan(load(pp), state, event, d, policy)
    return True


@contextmanager
def locked_result(state, event):
    import fcntl
    _, rp = paths(state, event)
    safe(rp.parent).mkdir(parents=True, exist_ok=True)
    fd = os.open(rp.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield rp
    finally:
        os.close(fd)

def execute(event_id, *, task_id, policy=None):
    d = delegate(task_id)
    state = safe(d['state'])
    pp, _ = paths(state, event_id)
    with locked_result(state, event_id) as result_path:
        plan = load(pp)
        if result_path.exists():
            return verified_result(state, event_id, policy)
        started = result_path.with_suffix('.started')
        require(not started.exists(), 'execution_result_unknown_do_not_retry')
        record, entries = validate_plan(plan, state, event_id, d, policy)
        plan_sha = sha(read(pp))
        # Preserve exact inputs before the first target mutation, not a claim of success.
        store_new(started, {'plan': plan, 'plan_sha256': plan_sha, 'task_id': task_id,
                            'delegation_sha256': sha(packed(d)), 'started_at': time.time()})
        from tools.skill_manager_tool import apply_skill_pending
        outcome = json.loads(apply_skill_pending(record['payload']))
        require(outcome.get('success') is True, 'native_writer_failed_result_unknown')
        require(sha(read(pp)) == plan_sha, 'plan_changed_during_execution')
        current_targets(plan['targets'], post=True)
        stage_record(d['home'], plan)
        coverage(plan, d, len(entries))
        require(plan['identity'] == review_identity(state, event_id, policy), 'review_changed_during_execution')
        result = {'schema': 'steward-execution-result/v1', 'status': 'applied_source_retained',
                  'event_id': event_id, 'plan_sha256': plan_sha, 'plan': plan, 'task_id': task_id,
                  'delegation': d, 'delegation_sha256': sha(packed(d)), 'operations': entries,
                  'completed_at': time.time(), 'source_retained': True,
                  **completion(plan)}
        store_new(result_path, result)
        return verified_result(state, event_id, policy)

def handle(args, **kwargs):
    try:
        if isinstance(args, dict) and args.get('action') in ('closeout-applied', 'closeout-verify'):
            from tools.steward_skill_closeout import closeout
            require(set(args) == {'action', 'source'}, 'closeout_tool_fields')
            return json.dumps(closeout(args['source'], dry_run=args['action'] == 'closeout-verify'), ensure_ascii=False)
        require(isinstance(args, dict) and set(args) == {'event_id'}, 'only_event_id_accepted')
        return json.dumps(execute(args['event_id'], task_id=kwargs.get('task_id')), ensure_ascii=False)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return json.dumps({'success': False, 'status': 'blocked', 'reason': str(exc), 'business_complete': False})

SCHEMA = {'name': 'steward_skill_execute',
          'description': 'Apply one approved plan under the original Cron delegation, or verify/close one exact already-applied source under a separate host permission. Closeout never invokes the content writer; no arbitrary approval, rejection or deletion.',
          'parameters': {'type': 'object', 'properties': {
              'event_id': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
              'action': {'type': 'string', 'enum': ['closeout-verify', 'closeout-applied']},
              'source': {'type': 'object', 'properties': {
                  'scope': {'type': 'string'}, 'id': {'type': 'string', 'pattern': '^[a-f0-9]{8}$'},
                  'sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                  'event_id': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                  'result_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}},
                  'required': ['scope', 'id', 'sha256', 'event_id', 'result_sha256'], 'additionalProperties': False}},
              'oneOf': [{'required': ['event_id'], 'not': {'anyOf': [{'required': ['action']}, {'required': ['source']}]}},
                        {'required': ['action', 'source'], 'not': {'required': ['event_id']}}],
              'additionalProperties': False}}
