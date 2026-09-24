"""Optional root-controlled external admission at protected launch boundaries.

The command is a trusted operator-installed adapter, not worker/profile code.
Its configuration is latched with the private execution authority. Failures keep
original native ownership; they never fall back to an unadmitted launch.
"""
import json
import math
import subprocess
import time

from hermes_cli import kanban_execution_authority as protected


def configured(authority):
    return bool(authority and authority.policy.get('admission_command'))


def _call(authority, conn, run_id, phase):
    original = authority.original_execution(conn, run_id)
    if original is None:
        raise RuntimeError('original private admission execution required')
    request = {'contract': 'protected-execution-admission-v1', 'phase': phase,
               'board': original['board'], 'run_id': run_id, 'authority': authority.policy}
    result = subprocess.run(authority.policy['admission_command'], input=json.dumps(request),
                            text=True, capture_output=True,
                            timeout=max(.01, min(10, original['scope']['deadline']-time.time())) if phase in {'launch','check'} else 10,
                            env=protected.privileged_environment(), cwd=authority.directory)
    if result.returncode:
        # Adapter stderr can contain credentials or remote payloads. Keep it out
        # of the worker log and worker-writable board failure text.
        raise RuntimeError('trusted execution admission adapter rejected '+phase)
    if len(result.stdout) > 128*1024:
        raise RuntimeError('execution admission response exceeds bound')
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise RuntimeError('execution admission response must be an object')
    return value


def prepare(conn, run_id):
    authority = protected.current()
    if not configured(authority): return
    original = authority.read(conn, run_id)
    if original['state'] != 'prepared' or original.get('admission') is not None:
        raise RuntimeError('fresh protected admission preparation required')
    response = _call(authority, conn, run_id, 'prepare')
    cutoff = response.get('deadline')
    if (not isinstance(cutoff, (int,float)) or isinstance(cutoff, bool) or not math.isfinite(cutoff)
            or cutoff <= time.time() or original['deadline'] is None or cutoff > original['deadline']
            or not isinstance(response.get('receipt'), dict)):
        raise RuntimeError('original bounded admission receipt required')
    updated = {**original, 'deadline': cutoff, 'admission': response['receipt']}
    authority.update(conn, run_id, original, updated)


def authorize(conn, run_id):
    authority = protected.current()
    if not configured(authority): return True
    original = authority.read(conn, run_id)
    if (original['state'] != 'active' or not original.get('admission')
            or original.get('admission_launch_attempted') or time.time() >= original['deadline']):
        raise RuntimeError('original unconsumed admission required')
    # A crash/lost response after this write may not retry model launch.
    updated = {**original, 'admission_launch_attempted': True}
    authority.update(conn, run_id, original, updated)
    result = _call(authority, conn, run_id, 'launch')
    return result.get('launch_authorized') is True and time.time() < updated['deadline']


def cleanup(conn, run_id):
    authority = protected.current()
    if not configured(authority): return
    original = authority.read(conn, run_id)
    if original['state'] != 'settled' or not original.get('children_reaped'):
        raise RuntimeError('original protected cleanup receipt required')
    if original.get('admission_cleanup_confirmed'): return
    if _call(authority, conn, run_id, 'cleanup').get('settled') is not True:
        raise RuntimeError('external admission cleanup remains unresolved')
    authority.update(conn, run_id, original, {**original, 'admission_cleanup_confirmed': True})


def reconcile(conn):
    """Reconcile cleanup or revoke preparation; never retry a model launch."""
    authority = protected.current()
    if not configured(authority): return
    for row in authority.pending(conn):
        scope = json.loads(row['scope'])
        if scope.get('state') in {'prepared', 'cancelling'}:
            from hermes_cli.kanban_execution_cancellation import cancel
            cancel(conn, row['run_id'])
        if scope.get('admission') and scope.get('state') == 'settled':
            cleanup(conn, row['run_id'])


def check(conn, run_id):
    authority = protected.current()
    if not configured(authority): return True
    original = authority.read(conn, run_id)
    if (original['state'] != 'active' or not original.get('admission_launch_attempted')
            or original.get('admission_input_attempted') or time.time() >= original['deadline']):
        raise RuntimeError('original consumed input gate required')
    authority.update(conn, run_id, original, {**original, 'admission_input_attempted': True})
    return _call(authority, conn, run_id, 'check').get('launch_current') is True and time.time() < original['deadline']
