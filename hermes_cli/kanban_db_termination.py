"""Task-wide responsibility for an external block's original execution."""
import json
import os
import select
import signal


def _data(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_request(row, data):
    return (isinstance(data, dict) and type(row['run_id']) is int
            and data.get('run_id') == row['run_id']
            and isinstance(data.get('claim_lock'), str) and bool(data['claim_lock'])
            and isinstance(data.get('workers'), list)
            and all(isinstance(w, dict) and type(w.get('pid')) is int for w in data['workers']))


def pending_requests(conn, task_id):
    requests = {}
    for row in conn.execute(
            "SELECT id, task_id, run_id, kind, payload FROM task_events WHERE task_id=? "
            "AND kind IN ('block_termination_requested','block_termination_settled') ORDER BY id", (task_id,)):
        payload = _data(row['payload'])
        if row['kind'] == 'block_termination_requested':
            requests[row['id']] = dict(row, data=payload)
            continue
        if payload is None or type(payload.get('request_event_id')) is not int:
            continue
        request = requests.get(payload['request_event_id'])
        if request and _valid_request(request, request['data']):
            original = request['data']
            if (row['run_id'] == request['run_id'] == payload.get('run_id')
                    and payload.get('claim_lock') == original['claim_lock']
                    and payload.get('workers') == original['workers']
                    and payload.get('outcome') in ('blocked', 'superseded')
                    and payload.get('exit_proven') is True):
                del requests[request['id']]
    return list(requests.values())


def termination_pending(conn, task_id):
    return bool(pending_requests(conn, task_id))


def execution_held(conn, task_id):
    from hermes_cli.kanban_stop import execution_stopped
    return termination_pending(conn, task_id) or execution_stopped(conn, task_id)


def producer_witness(process):
    import subprocess
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    if not isinstance(process, subprocess.Popen) or process.poll() is not None:
        return None
    birth = _process_fingerprint(process.pid)
    owner = _process_fingerprint(os.getpid())
    if not birth or not owner:
        return None
    return {'pid': process.pid, 'started_at': birth, 'producer_pid': os.getpid(),
            'producer_started_at': owner, 'binding': 'owned_popen_v1'}


def _workers(conn, task_id, run_id, claim_lock):
    workers = {}
    for event in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? AND run_id=? "
            "AND kind IN ('spawned','worker_registered','worker_bound','worker_spawn_refused','owner_worker_reserved') ORDER BY id",
            (task_id, run_id)):
        data = _data(event['payload'])
        if not data or type(data.get('pid')) is not int or data.get('claim_lock') != claim_lock:
            continue
        old = workers.get(data['pid'])
        if old and old.get('binding') == 'owned_popen_v1' and not data.get('binding'):
            continue
        workers[data['pid']] = data
    return list(workers.values())


def _launch_state(conn, task_id, run_id):
    events = [(r['kind'], _data(r['payload']) or {}) for r in conn.execute(
        "SELECT kind,payload FROM task_events WHERE task_id=? AND run_id=? ORDER BY id", (task_id, run_id))]
    protocol = any(k == 'claimed' and d.get('launch_protocol') == 1 for k, d in events)
    dispatch = any(k == 'dispatch_intent' for k, _ in events)
    admitted = any(k == 'owner_admitted' for k, _ in events)
    launches = {d.get('admission_id') for k, d in events if k == 'owner_launch_intent'}
    closed = {d.get('admission_id') for k, d in events if k in ('owner_worker_reserved', 'owner_launch_closed')}
    owner_done = not admitted or (bool(closed) and launches <= closed)
    return {'not_dispatched': protocol and not dispatch and not admitted, 'owner_closed': owner_done}


def request_block(conn, task_id, *, reason, kind):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_stop import spawn_gate
    with spawn_gate(conn), kb.write_txn(conn):
        pending = pending_requests(conn, task_id)
        if pending:
            return pending[0]['id']
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None or row['status'] not in ('running', 'ready'):
            return None
        if not (row['current_run_id'] or row['claim_lock'] or row['worker_pid']):
            return None
        workers = _workers(conn, task_id, row['current_run_id'], row['claim_lock'])
        if row['worker_pid'] and not any(w['pid'] == row['worker_pid'] for w in workers):
            workers.append({'pid': row['worker_pid'], 'started_at': row['worker_started_at']})
        payload = {'run_id': row['current_run_id'], 'claim_lock': row['claim_lock'],
                   'reason': reason, 'kind': kind, 'workers': workers,
                   **_launch_state(conn, task_id, row['current_run_id'])}
        return kb._append_event(conn, task_id, 'block_termination_requested', payload, run_id=row['current_run_id'])


def observe_worker(worker, claim_lock, *, terminate=True):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    from gateway.drain_control import current_instantiation_epoch
    pid, birth = worker.get('pid'), worker.get('started_at')
    if (type(pid) is not int or pid <= 0 or pid == os.getpid()
            or worker.get('binding') != 'owned_popen_v1'
            or not isinstance(birth, str) or not birth.startswith(current_instantiation_epoch() + '|')
            or not str(claim_lock or '').startswith(kb._host_prefix())
            or not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal')):
        return 'UNKNOWN'
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return 'GONE_VERIFIED'
    except OSError:
        return 'UNKNOWN'
    try:
        if _process_fingerprint(pid) != birth:
            return 'UNKNOWN'
        if select.select([fd], [], [], 0)[0]:
            return 'GONE_VERIFIED'
        if terminate:
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            except OSError:
                return 'UNKNOWN'
            if select.select([fd], [], [], 5)[0]:
                return 'GONE_VERIFIED'
        return 'ALIVE_MATCH'
    finally:
        os.close(fd)


def _snapshot(conn, request):
    data = request['data']
    workers = {w['pid']: w for w in data['workers']}
    workers.update({w['pid']: w for w in _workers(conn, request['task_id'], request['run_id'], data['claim_lock'])})
    return list(workers.values()), _launch_state(conn, request['task_id'], request['run_id'])


def settle_request(conn, task_id, request_id, *, terminate=True):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_stop import spawn_gate
    original = next((r for r in pending_requests(conn, task_id) if r['id'] == request_id), None)
    if original is None or not _valid_request(original, original['data']):
        return False
    data = original['data']
    workers, launch = _snapshot(conn, original)
    if not launch['owner_closed'] or (not workers and not launch['not_dispatched']):
        return False
    outcomes = [observe_worker(w, data['claim_lock'], terminate=terminate) for w in workers]
    if any(outcome != 'GONE_VERIFIED' for outcome in outcomes):
        return False
    with spawn_gate(conn), kb.write_txn(conn):
        current = next((r for r in pending_requests(conn, task_id) if r['id'] == request_id), None)
        if current != original or _snapshot(conn, original) != (workers, launch):
            return False
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            return False
        newer = conn.execute("SELECT id,kind FROM task_events WHERE task_id=? AND id>? "
                             "AND kind IN ('execution_stopped','archived') ORDER BY id DESC LIMIT 1",
                             (task_id, request_id)).fetchone()
        if newer:
            outcome = 'superseded'
        elif (row['current_run_id'] == original['run_id'] and row['claim_lock'] == data['claim_lock']):
            run = conn.execute('SELECT status,claim_lock FROM task_runs WHERE id=? AND task_id=?',
                               (original['run_id'], task_id)).fetchone()
            if not run or run['status'] != 'running' or run['claim_lock'] != data['claim_lock']:
                return False
            if not kb._block_task_transition(conn, task_id, reason=data.get('reason'), kind=data.get('kind'),
                                              expected_run_id=original['run_id'], nested=True):
                return False
            outcome = 'blocked'
        else:
            return False
        kb._append_event(conn, task_id, 'block_termination_settled', {
            'request_event_id': request_id, 'run_id': original['run_id'], 'claim_lock': data['claim_lock'],
            'workers': data['workers'], 'observed_workers': workers, 'outcome': outcome,
            'superseding_event_id': newer['id'] if newer else None, 'exit_proven': True}, run_id=original['run_id'])
    return outcome == 'blocked'


def resume_settlement_audit(conn, settlement, stopped):
    data, stop = _data(settlement['payload']), _data(stopped['payload'])
    if (data is None or stop is None or data.get('outcome') != 'superseded'
            or data.get('exit_proven') is not True
            or data.get('superseding_event_id') != stopped['id']
            or data.get('run_id') != stop.get('run_id')
            or data.get('claim_lock') != stop.get('claim_lock')):
        return False
    request = conn.execute("SELECT * FROM task_events WHERE id=? AND task_id=? "
                           "AND kind='block_termination_requested' AND id<?",
                           (data.get('request_event_id'), stopped['task_id'], stopped['id'])).fetchone()
    return bool(request and _valid_request(request, _data(request['payload']))
                and request['run_id'] == settlement['run_id'] == data['run_id']
                and _data(request['payload'])['claim_lock'] == data['claim_lock']
                and _data(request['payload'])['workers'] == data.get('workers'))


def reconcile_pending(conn):
    for row in conn.execute("SELECT DISTINCT task_id FROM task_events WHERE kind='block_termination_requested'").fetchall():
        for request in pending_requests(conn, row['task_id']):
            settle_request(conn, row['task_id'], request['id'])
