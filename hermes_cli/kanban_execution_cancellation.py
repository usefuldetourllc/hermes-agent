"""Durably revoke activation before reconciling an external preparation.

A cancelled scope is not a child-reaping receipt. CAS against the private
prepared scope prevents a late supervisor from activating or creating a worker.
An active scope must retain its supervisor and normal cleanup path.
"""
import json
import time

from hermes_cli import kanban_execution_authority as protected


def cancelled(scope):
    return bool(scope and scope.get('contract') == protected.CONTRACT
                and scope.get('state') == 'cancelled'
                and scope.get('activation_revoked') is True
                and scope.get('admission_cancellation_confirmed') is True
                and not any(scope.get(k) for k in
                            ('supervisor_pid', 'worker_pid', 'admission_launch_attempted')))


def blocks_automatic_retry(conn, task_id):
    authority = protected.current()
    if authority is None:
        return False
    with authority.transaction() as db:
        row = db.execute('''SELECT scope FROM executions WHERE board=? AND task_id=?
            ORDER BY run_id DESC LIMIT 1''', (protected.board_path(conn), task_id)).fetchone()
    return bool(row and cancelled(json.loads(row['scope'])))


def project(conn, run_id):
    """Idempotent board projection after the private cancellation is confirmed."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_dispatch import _record_task_failure
    authority = protected.current()
    if authority is None:
        raise RuntimeError('private cancellation authority required')
    original = authority.original_execution(conn, run_id)
    if not original or not cancelled(original['scope']):
        raise RuntimeError('confirmed original cancellation required')
    with kb.write_txn(conn, allow_nested=True):
        row = conn.execute('''SELECT r.*,t.current_run_id,t.claim_lock task_lock
            FROM task_runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=?''', (run_id,)).fetchone()
        if not row or row['task_id'] != original['task_id'] or row['claim_lock'] != original['claim_lock']:
            raise RuntimeError('original cancellation projection identity changed')
        conn.execute("UPDATE task_runs SET spawn_state='settled',execution_scope=? WHERE id=?",
                     (json.dumps(original['scope']), run_id))
        if row['ended_at'] is None:
            if row['current_run_id'] != run_id or row['task_lock'] != original['claim_lock']:
                raise RuntimeError('original cancellation claim changed')
            _record_task_failure(conn, original['task_id'], 'native activation cancelled before worker launch',
                                 outcome='spawn_failed', force_trip=True, release_claim=True, end_run=True,
                                 allow_nested=True)
            kb._append_event(conn, original['task_id'], 'blocked',
                {'reason':'native activation cancelled; a new authorized task is required',
                 'scope_id':original['scope']['id']}, run_id=run_id)


def cancel(conn, run_id):
    from hermes_cli import kanban_execution_admission as admission
    authority = protected.current()
    if not admission.configured(authority):
        return False
    original = authority.read(conn, run_id)
    if cancelled(original):
        project(conn, run_id)
        return True
    if (not original or original.get('state') not in {'prepared', 'cancelling'}
            or any(original.get(k) for k in ('supervisor_pid', 'worker_pid', 'admission_launch_attempted'))):
        return False
    if original['state'] == 'prepared':
        revoked = {**original, 'state': 'cancelling', 'activation_revoked': True}
        # A racing supervisor either activates first or fails its own CAS. Never both.
        authority.update(conn, run_id, original, revoked)
        original = revoked
    reply = admission._call(authority, conn, run_id, 'cancel')
    if reply.get('cancelled') is not True:
        raise RuntimeError('external prelaunch cancellation remains unresolved')
    authority.update(conn, run_id, original, {**original, 'state': 'cancelled',
        'admission_cancellation_confirmed': True, 'finished_at': time.time()})
    project(conn, run_id)
    return True
