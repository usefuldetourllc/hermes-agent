"""Original-run ownership survives both terminal transitions and launch errors.

A terminal work result and verified process cleanup are different facts. Events
hold the launch fence on the run even after the task's current-run pointer clears.
"""
import time

_EVENTS = ('spawn_attempted', 'spawned', 'spawn_returned', 'spawn_uncertain', 'spawn_cleanup_verified')


def pending_runs(conn, task_id=None):
    """Unreceipted launches and receipted terminal workers still own capacity."""
    return conn.execute('''SELECT r.*, t.current_run_id, t.status AS task_status,
        t.worker_pid AS task_pid, t.worker_started_at AS task_fingerprint,
        t.claim_lock AS task_lock, e.kind
        FROM task_runs r JOIN tasks t ON t.id=r.task_id
        JOIN task_events e ON e.id=(SELECT MAX(last.id) FROM task_events last
            WHERE last.task_id=r.task_id AND last.run_id=r.id AND last.kind IN (?,?,?,?,?))
        WHERE EXISTS (SELECT 1 FROM task_events first
            WHERE first.task_id=r.task_id AND first.run_id=r.id AND first.kind='spawn_attempted')
        AND (e.kind IN ('spawn_attempted','spawn_uncertain')
             OR (e.kind='spawned' AND r.ended_at IS NOT NULL))
        AND (? IS NULL OR r.task_id=?)''', (*_EVENTS, task_id, task_id)).fetchall()


def pending(conn, task_id):
    return bool(pending_runs(conn, task_id))


def extra_occupancy(conn):
    """Owners not already included in the running task count, by original profile."""
    return [row for row in pending_runs(conn)
            if row['task_status'] != 'running' or row['current_run_id'] != row['id']]


def require_resolved(conn, task_id, *, terminal_run_id=None):
    for row in pending_runs(conn, task_id):
        # Existing worker APIs verify expected_run_id in their transition CAS.
        # Permit that run's result, without resolving its process ownership.
        if (terminal_run_id is not None and row['id'] == terminal_run_id
                and row['current_run_id'] == terminal_run_id and row['ended_at'] is None):
            continue
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


def record_receipt(conn, task_id, run_id, claim_lock, pid, fingerprint):
    """Caller holds the write txn; never attach a late receipt to a successor."""
    row = conn.execute('SELECT task_id,claim_lock FROM task_runs WHERE id=?', (run_id,)).fetchone()
    if not row or row['task_id'] != task_id or row['claim_lock'] != claim_lock:
        raise RuntimeError('original spawn receipt ownership changed')
    conn.execute('UPDATE task_runs SET worker_pid=?,worker_started_at=? WHERE id=?', (pid, fingerprint, run_id))
    conn.execute('''UPDATE tasks SET worker_pid=?,worker_started_at=?
        WHERE id=? AND current_run_id=? AND claim_lock IS ?''', (pid, fingerprint, task_id, run_id, claim_lock))


def cleanup_verified(conn, task_id, run_id, pid, fingerprint):
    """Record verified process exit before a reaper clears the identity columns."""
    from hermes_cli import kanban_db as kb
    kb._append_event(conn, task_id, 'spawn_cleanup_verified', {
        'pid': pid, 'fingerprint': fingerprint, 'observed_at': int(time.time())}, run_id=run_id)


def reconcile(conn, task_id):
    """Only original process identity on the owning host can prove an exit."""
    from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
    with kb.write_txn(conn, allow_nested=True):
        for row in pending_runs(conn, task_id):
            if (not row['worker_pid'] or row['worker_started_at'] is None
                    or not str(row['claim_lock'] or '').startswith(kb._host_prefix())):
                continue
            if row['ended_at'] is None and (
                    row['current_run_id'] != row['id'] or row['worker_pid'] != row['task_pid']
                    or row['worker_started_at'] != row['task_fingerprint'] or row['claim_lock'] != row['task_lock']):
                continue
            if dispatch._worker_identity(row['worker_pid'], row['worker_started_at']) in {'gone', 'foreign'}:
                cleanup_verified(conn, task_id, row['id'], row['worker_pid'], row['worker_started_at'])
        return not pending(conn, task_id)
