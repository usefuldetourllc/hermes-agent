"""Retain original execution ownership across an ambiguous spawn callback.

The attempt is durable before entering external code. A callback exception or
interrupted dispatcher is not evidence that no worker exists. Existing run events
hold this fence, so old runs without an attempt retain their original semantics.
"""
import time

_EVENTS = ('spawn_attempted', 'spawned', 'spawn_returned', 'spawn_uncertain', 'spawn_cleanup_verified')


def pending(conn, task_id):
    row = conn.execute('''SELECT e.kind FROM task_events e JOIN tasks t
        ON t.id=e.task_id AND t.current_run_id=e.run_id
        WHERE e.task_id=? AND e.kind IN (?,?,?,?,?) ORDER BY e.id DESC LIMIT 1''',
        (task_id, *_EVENTS)).fetchone()
    return row is not None and row['kind'] in {'spawn_attempted', 'spawn_uncertain'}


def require_resolved(conn, task_id):
    if pending(conn, task_id):
        raise RuntimeError('spawn ownership is unresolved; verified worker cleanup is required')


def begin(conn, task):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        require_resolved(conn, task.id)
        row = conn.execute('SELECT current_run_id,claim_lock,status FROM tasks WHERE id=?', (task.id,)).fetchone()
        if (not row or row['status'] != 'running' or row['current_run_id'] != task.current_run_id
                or row['claim_lock'] != task.claim_lock):
            raise RuntimeError('original spawn ownership changed')
        kb._append_event(conn, task.id, 'spawn_attempted', {'claim_lock': task.claim_lock}, run_id=task.current_run_id)


def returned(conn, task):
    """Normal no-PID callbacks retain their existing ownership convention."""
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        kb._append_event(conn, task.id, 'spawn_returned', run_id=task.current_run_id)


def uncertain(conn, task, error):
    from hermes_cli import kanban_db as kb
    with kb.write_txn(conn):
        conn.execute('''UPDATE tasks SET last_failure_error=?
            WHERE id=? AND current_run_id=? AND claim_lock IS ?''',
            ('spawn cleanup unresolved: '+str(error)[:400], task.id, task.current_run_id, task.claim_lock))
        kb._append_event(conn, task.id, 'spawn_uncertain', {'error': str(error)[:500]}, run_id=task.current_run_id)


def reconcile(conn, task_id):
    """Only recorded original process identity can prove an uncertain exit.

    Without a PID/fingerprint, retain uncertainty. Task completion labels, TTL,
    host-local claim strings and an absent dispatcher prove no worker cleanup.
    """
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    with kb.write_txn(conn):
        if not pending(conn, task_id):
            return True
        row = conn.execute('''SELECT t.current_run_id,t.worker_pid,t.worker_started_at,t.claim_lock,
            r.worker_pid AS run_pid,r.worker_started_at AS run_fingerprint,r.claim_lock AS run_lock
            FROM tasks t JOIN task_runs r ON r.id=t.current_run_id WHERE t.id=?''', (task_id,)).fetchone()
        if (not row or not row['worker_pid'] or row['worker_started_at'] is None
                or row['worker_pid'] != row['run_pid'] or row['worker_started_at'] != row['run_fingerprint']
                or row['claim_lock'] != row['run_lock']
                or not str(row['claim_lock'] or '').startswith(kb._host_prefix())):
            return False
        if dispatch._worker_identity(row['worker_pid'], row['worker_started_at']) not in {'gone', 'foreign'}:
            return False
        kb._append_event(conn, task_id, 'spawn_cleanup_verified', {
            'pid': row['worker_pid'], 'fingerprint': row['worker_started_at'],
            'observed_at': int(time.time())}, run_id=row['current_run_id'])
        return True
