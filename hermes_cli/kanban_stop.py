"""Task-set execution stop, independent of chat supervision and task outcome.

The existing event ledger is the durable fence. No status edit, dependency
completion, or recovery sweep grants permission to cross it; only an explicit
resume matching the stop event does. The short spawn gate orders stop against
OS process creation/publication, not against a whole worker run.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path


def stop_event(conn, task_id):
    row = conn.execute(
        "SELECT id, kind, payload FROM task_events WHERE task_id=? "
        "AND kind IN ('execution_stopped','execution_resumed') ORDER BY id DESC LIMIT 1",
        (task_id,)).fetchone()
    return row if row and row['kind'] == 'execution_stopped' else None


def execution_stopped(conn, task_id):
    return stop_event(conn, task_id) is not None


def execution_fenced(conn, task_id):
    ancestors = conn.execute(
        'WITH RECURSIVE ancestors(id) AS (SELECT ? UNION SELECT l.parent_id '
        'FROM task_links l JOIN ancestors a ON l.child_id=a.id) SELECT id FROM ancestors',
        (task_id,)).fetchall()
    return any(execution_stopped(conn, row['id']) for row in ancestors)


@contextmanager
def spawn_gate(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise ValueError('Execution stop requires a durable board database')
    with Path(path + '.execution.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _targets(conn, task_ids, reason):
    if not isinstance(task_ids, list) or not task_ids or not all(isinstance(t, str) and t for t in task_ids):
        raise ValueError('task_ids must be a nonempty exact task set')
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('An explicit reason is required')
    ids = sorted(set(task_ids))
    rows = [conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone() for tid in ids]
    if any(row is None for row in rows):
        raise ValueError('Unknown task in stop/resume set; nothing changed')
    return rows


def _worker_snapshots(conn, row):
    workers = {}
    if row['worker_pid']:
        workers[row['worker_pid']] = {'pid': row['worker_pid'], 'started_at': row['worker_started_at']}
    if row['current_run_id']:
        for event in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? "
                                  "AND kind='worker_bound' ORDER BY id", (row['id'], row['current_run_id'])):
            bound = json.loads(event['payload'] or '{}')
            if bound.get('pid') and bound.get('claim_lock') == row['claim_lock']:
                workers[bound['pid']] = {'pid': bound['pid'], 'started_at': bound.get('started_at')}
    return list(workers.values())


def _finish_stop(conn, task_id, event, *, signal_fn=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _terminate_reclaimed_worker, UNVERIFIED_WORKER_FINGERPRINT
    payload = json.loads(event['payload'])
    results = []
    for worker in payload.get('workers', []):
        if worker['pid'] == os.getpid():
            result = {'prev_pid': worker['pid'], 'terminated': False, 'signal_refused': True}
        else:
            result = _terminate_reclaimed_worker(worker['pid'], payload.get('claim_lock'), signal_fn=signal_fn,
                started_at=worker.get('started_at') or UNVERIFIED_WORKER_FINGERPRINT)
        results.append(result)
    termination = {'stop_event_id': event['id'], 'workers': results,
                   'terminated': all(item.get('terminated') for item in results)}
    with kb.write_txn(conn):
        kb._append_event(conn, task_id, 'stop_worker_termination', termination, run_id=payload.get('run_id'))
        if termination['terminated']:
            conn.execute('UPDATE tasks SET worker_pid=NULL, worker_started_at=NULL WHERE id=? AND current_run_id IS NULL', (task_id,))
    return termination


def reap_stopped_workers(conn, *, signal_fn=None):
    """Finish a committed stop after interruption, from the existing dispatcher reaper."""
    reaped = []
    rows = conn.execute("SELECT DISTINCT task_id FROM task_events WHERE kind='execution_stopped'").fetchall()
    if not rows:
        return reaped
    with spawn_gate(conn):
        for row in rows:
            event = stop_event(conn, row['task_id'])
            if event is None:
                continue
            previous = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='stop_worker_termination' "
                                    "AND id>? ORDER BY id DESC LIMIT 1", (row['task_id'], event['id'])).fetchone()
            if previous and json.loads(previous['payload'] or '{}').get('terminated'):
                continue
            if _finish_stop(conn, row['task_id'], event, signal_fn=signal_fn)['terminated']:
                reaped.append(row['task_id'])
    return reaped


def stop_tasks(conn, task_ids, *, reason, author):
    from hermes_cli import kanban_db as kb
    stopped = []
    with spawn_gate(conn):
        with kb.write_txn(conn):
            rows = _targets(conn, task_ids, reason)
            selected = set(task_ids)
            for row in rows:
                descendants = conn.execute(
                    'WITH RECURSIVE descendants(id) AS (SELECT child_id FROM task_links WHERE parent_id=? '
                    'UNION SELECT l.child_id FROM task_links l JOIN descendants d ON l.parent_id=d.id) '
                    'SELECT t.* FROM tasks t JOIN descendants d ON t.id=d.id', (row['id'],)).fetchall()
                active = [d['id'] for d in descendants if d['id'] not in selected
                          and (d['status'] == 'running' or d['claim_lock'] or d['worker_pid'])]
                if active:
                    raise ValueError('Confirm an exact set including active descendants before stopping: ' + ', '.join(active))
            for row in rows:
                prior = stop_event(conn, row['id'])
                if prior:
                    stopped.append({'task_id': row['id'], 'event_id': prior['id']})
                    continue
                payload = {'reason': reason, 'author': author, 'task_ids': sorted(set(task_ids)),
                           'prior_status': row['status'], 'run_id': row['current_run_id'],
                           'resume_status': (kb._retry_status_for_run(conn, row['id'], row['current_run_id'])
                                             if row['status'] == 'running' else kb._resume_status_from_events(conn, row['id'])),
                           'worker_pid': row['worker_pid'], 'worker_started_at': row['worker_started_at'],
                           'claim_lock': row['claim_lock'], 'workers': _worker_snapshots(conn, row)}
                kb._append_event(conn, row['id'], 'execution_stopped', payload, run_id=row['current_run_id'])
                event = stop_event(conn, row['id'])
                stopped.append({'task_id': row['id'], 'event_id': event['id']})
                # Never archive: that would release successors. Keep original
                # authorization and all failure counters, including review budgets.
                if row['status'] not in ('done', 'archived'):
                    run = conn.execute('SELECT summary, error, metadata FROM task_runs WHERE id=?',
                                       (row['current_run_id'],)).fetchone()
                    kb._end_run(conn, row['id'], outcome='stopped', status='blocked',
                                summary=(run['summary'] or reason) if run else reason,
                                error=run['error'] if run else None,
                                metadata=kb._json_dict(run['metadata']) if run else None)
                    conn.execute("UPDATE tasks SET status='blocked', current_run_id=NULL, "
                                 "claim_lock=NULL, claim_expires=NULL WHERE id=?", (row['id'],))
        # Stop is committed BEFORE signalling. A crash here leaves the retained
        # run PID/fingerprint for the existing terminal-worker reaper.
        for item in stopped:
            item['termination'] = _finish_stop(conn, item['task_id'], stop_event(conn, item['task_id']))
    return stopped


def resume_tasks(conn, task_ids, *, expected_stop_events, reason, author):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _worker_alive
    with spawn_gate(conn), kb.write_txn(conn):
        rows = _targets(conn, task_ids, reason)
        snapshots = []
        for row in rows:
            event = stop_event(conn, row['id'])
            if event is None or expected_stop_events.get(row['id']) != event['id']:
                raise ValueError('Stop event changed; read the exact target set before resuming')
            payload = json.loads(event['payload'])
            expected_status = payload['prior_status'] if payload['prior_status'] in ('done', 'archived') else 'blocked'
            newer = conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? AND id>? AND kind NOT IN "
                "('late_result','worker_result','stop_worker_termination','terminal_worker_reaped','commented','heartbeat',"
                "'claim_rejected','respawn_guarded','skipped_nonspawnable') LIMIT 1",
                (row['id'], event['id'])).fetchone()
            if row['status'] != expected_status or newer:
                raise ValueError('Task state changed after stop; resume must not overwrite a newer decision')
            limit = row['max_retries'] if row['max_retries'] is not None else kb.DEFAULT_FAILURE_LIMIT
            if row['consecutive_failures'] >= limit:
                raise ValueError('Original failure budget is exhausted; resume does not replenish it')
            if payload.get('workers') and not str(payload.get('claim_lock') or '').startswith(kb._host_prefix()):
                raise ValueError('Foreign worker termination cannot be proven on this host; resume refused')
            if any(_worker_alive(worker['pid'], worker.get('started_at')) for worker in payload.get('workers', [])):
                raise ValueError('Stopped worker termination is not yet proven; resume refused')
            snapshots.append((row, event, payload))
        for row, event, payload in snapshots:
            prior = payload['prior_status']
            # Triage stays triage: a resumed old specification is not a new idea.
            # Runtime/human triage still needs the native event-CAS decision path.
            status = payload['resume_status'] if prior == 'running' else prior
            if status == 'ready':
                status = 'todo'
            conn.execute('UPDATE tasks SET status=?, worker_pid=NULL, worker_started_at=NULL WHERE id=?',
                         (status, row['id']))
            kb._append_event(conn, row['id'], 'execution_resumed', {
                'stop_event_id': event['id'], 'reason': reason, 'author': author, 'status': status,
                'resume_status': payload['resume_status']})
    kb.recompute_ready(conn)
    return list(sorted(set(task_ids)))
