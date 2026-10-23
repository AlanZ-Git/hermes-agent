"""Already-applied source closeout under a private, exact host permission."""
import os
import sqlite3
from pathlib import Path

from hermes_constants import get_hermes_home
from tools.steward_skill_execution import (
    HEX, PID, effect_index, load, locked_result, packed, partial, paths, read,
    require, safe, sha, under, verified_result,
)
from tools.steward_skill_delegation import get_closeout_policy, get_delegation


def permission():
    policy = get_closeout_policy()
    require(policy is not None, 'host_closeout_inactive')
    require(set(policy) == {'enabled', 'task_id', 'board', 'home', 'profile', 'grant', 'sources'},
            'closeout_permission_shape')
    require(all(isinstance(policy[k], str) and policy[k] for k in
                ('task_id', 'board', 'home', 'profile', 'grant')), 'closeout_permission_identity')
    require(isinstance(policy['sources'], list) and policy['sources'] and
            all(isinstance(row, list) and len(row) == 3 and
                all(isinstance(value, str) for value in row) and
                PID.fullmatch(row[1]) and HEX.fullmatch(row[2]) for row in policy['sources']),
            'closeout_permission_sources')
    safe(policy['home'])
    safe(policy['board'])
    return policy


def allowed(request, policy):
    require(isinstance(request, dict) and set(request) ==
            {'scope', 'id', 'sha256', 'event_id', 'result_sha256'}, 'closeout_request_fields')
    require(all(isinstance(v, str) for v in request.values()) and PID.fullmatch(request['id']) and
            all(HEX.fullmatch(request[k]) for k in ('sha256', 'event_id', 'result_sha256')),
            'closeout_exact_coordinates')
    require([request['scope'], request['id'], request['sha256']] in policy['sources'],
            'closeout_outside_exact_permission')


def closeout_actor():
    from agent.delegation_context import is_dispatcher_owned_worker_context
    policy = permission()
    require(is_dispatcher_owned_worker_context(), 'closeout_dispatcher_owner_required')
    require(str(safe(get_hermes_home())) == policy['home'] and
            os.environ.get('HERMES_PROFILE') == policy['profile'], 'closeout_profile_required')
    tid, rid, lock = (os.environ.get(x) for x in
                      ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_RUN_ID', 'HERMES_KANBAN_CLAIM_LOCK'))
    require(tid == policy['task_id'] and rid and rid.isdecimal() and lock,
            'closeout_exact_authorized_task_required')
    conn = sqlite3.connect(safe(policy['board']).as_uri() + '?mode=ro', uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        task = conn.execute('SELECT assignee,status,current_run_id,claim_lock FROM tasks WHERE id=?', (tid,)).fetchone()
        run = conn.execute('SELECT id,task_id,profile,status,claim_lock FROM task_runs WHERE id=?', (int(rid),)).fetchone()
        require(task is not None and run is not None and task['assignee'] == run['profile'] == policy['profile'] and
                task['status'] == run['status'] == 'running' and task['current_run_id'] == run['id'] and
                run['task_id'] == tid and task['claim_lock'] == run['claim_lock'] == lock,
                'closeout_live_claim_mismatch')
    finally:
        conn.close()
    return {'profile': policy['profile'], 'task_id': tid, 'run_id': int(rid), 'authorization': policy['grant']}


def closeout_effect(request, state, policy=None):
    host = permission()
    allowed(request, host)
    pp, rp = paths(state, request['event_id'])
    require(rp.exists(), 'closeout_effect_missing_or_unknown')
    require(sha(read(rp)) == request['result_sha256'], 'closeout_result_drift')
    result = verified_result(state, request['event_id'], policy)
    require(result is not None and not partial(result['plan']) and result.get('business_complete') is True and
            result['plan']['outstanding'] == [], 'closeout_all_obligations_required')
    plan, d = result['plan'], result['delegation']
    require(d['state'] == str(state) and d['home'] == str(get_hermes_home()) == host['home'], 'closeout_effect_owner')
    matches = [s for s in plan['sources'] if
               (s['scope'], s['id'], s['sha256']) == (request['scope'], request['id'], request['sha256'])]
    stage_path = str(Path(d['home']) / 'pending/skills' / (plan['stage']['id'] + '.json'))
    is_stage = (request['scope'] == host['profile'] and plan['stage'] ==
                {'id': request['id'], 'sha256': request['sha256']})
    require(len(matches) == 1 or (not matches and is_stage), 'closeout_source_effect_binding')
    if matches:
        source = matches[0]
        path = under(source['path'], d['pending_roots'])
        rows = source['coverage']
        role = 'source_and_stage' if is_stage and str(path) == stage_path else 'source'
    else:
        path = under(stage_path, d['pending_roots'])
        rows = [{'ordinal': e['ordinal'], 'operations': [e['ordinal']], 'basis': 'authenticated stage operation'}
                for e in result['operations']]
        role = 'stage'
    expected_home = Path(d['home']) if request['scope'] == host['profile'] else Path(d['home']).parent / request['scope']
    require(path == expected_home / 'pending/skills' / (request['id'] + '.json'), 'closeout_scope_path_binding')
    token = sha(packed([request['scope'], request['id'], str(path), request['sha256']]))
    index, _, failures = effect_index(state, policy)
    require(not any(e.get('source_tokens') is None or token in e['source_tokens'] for e in failures),
            'closeout_related_execution_failure')
    require(token not in index or not partial(index[token]['plan']), 'closeout_retained_source_obligations')
    return result, path, {'request': request, 'role': role, 'coverage': rows,
                         'plan_sha256': sha(read(pp)), 'result_sha256': sha(read(rp)),
                         'outstanding': [], 'source_complete': True}


def closeout(request, *, state=None, policy=None, dry_run=False):
    from tools.pending_history import close_applied, coordinates, historical_read, archived
    from tools.steward_skill_execution import closeout_recovery
    from contextlib import nullcontext
    host = permission()
    allowed(request, host)
    d = get_delegation()
    require(isinstance(d, dict) and d.get('home') == host['home'], 'closeout_delegation_binding')
    state = safe(state if state is not None else d['state'])
    actor = None if dry_run else closeout_actor()

    def inspect():
        return closeout_effect(request, state, policy)

    if dry_run:
        _, path, binding = inspect()
        historical_read(path, request['id'], request['sha256'])
        terminal = None
        if not path.exists():
            _, directory = coordinates(path, request['id'], request['sha256'])
            archived(path, request['id'], request['sha256'])
            terminal = load(directory / 'receipt.json')
        return {'status': 'closeout_verified', 'writes': 0, 'writer_calls': 0,
                'active': path.exists(), 'binding': binding, 'terminal': terminal}
    with locked_result(state, request['event_id']):
        require(permission() == host, 'closeout_permission_changed')
        home = Path(d['home']) if request['scope'] == host['profile'] else Path(d['home']).parent / request['scope']
        original = safe(home / 'pending/skills' / (request['id'] + '.json'))
        _, directory = coordinates(original, request['id'], request['sha256'])
        saved = load(directory / 'closeout.json') if (directory / 'closeout.json').exists() else None
        if saved is not None:
            require(saved.get('request') == request and saved.get('actor', {}).get('task_id') == actor['task_id'] and
                    saved['actor'].get('authorization') == actor['authorization'], 'closeout_saved_binding')
        recovery = closeout_recovery(original, request['id'], request['sha256'], saved) if saved else nullcontext()
        with recovery:
            _, path, binding = inspect()
            binding = dict(binding, schema='steward-applied-closeout/v1', actor=saved['actor'] if saved else actor)
            require(saved is None or saved == binding, 'closeout_saved_effect_drift')

            def precommit():
                require(permission() == host and get_delegation() == d, 'closeout_permission_changed')
                require(closeout_actor() == actor, 'closeout_actor_changed')
                _, current_path, current = inspect()
                require(current_path == path and dict(current, schema='steward-applied-closeout/v1', actor=binding['actor']) == binding,
                        'closeout_effect_changed')

            receipt = close_applied(path.parent.parent.parent, request['id'], request['sha256'], binding,
                                    precommit=precommit)
        verified_result(state, request['event_id'], policy)
        return {'status': 'applied_closed', 'writer_calls': 0, 'active': False,
                'binding': binding, 'receipt': receipt}
