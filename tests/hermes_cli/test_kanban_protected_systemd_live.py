"""Actual user-systemd transport for the protected worker's sealed environment."""
import os
from pathlib import Path
import shutil

import pytest

from tests.hermes_cli.test_kanban_protected_authority import protected_board
from tests.hermes_cli.test_kanban_execution_scope import wait_for
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_execution_scope as scopes
from tools import process_registry as registry


@pytest.mark.linux_only
def test_protected_supervisor_receives_sealed_environment_through_real_systemd(protected_board, monkeypatch):
    if not shutil.which('systemd-run') or not Path('/run/user/0/bus').exists():
        pytest.skip('requires the dedicated root user-systemd qualification job')
    assert os.geteuid() == 0
    assert registry._systemd_run_user_scope_available(), 'real root user scope unavailable'
    home, public, authority, worker = protected_board
    workspace = public/'systemd-workspace';workspace.mkdir(mode=0o755)
    # Only select the gateway-origin branch; bus discovery, availability, argv,
    # Popen, scope creation, descriptor transfer and both execs remain real.
    monkeypatch.setenv('INVOCATION_ID','isolated-systemd-qualification')
    monkeypatch.setattr(registry,'_is_supervised_gateway_process',lambda:True)
    launched=[];real_popen=dispatch.subprocess.Popen
    def observe(argv,**kwargs):
        proc=real_popen(argv,**kwargs)
        if 'kanban_execution_supervisor.py' in str(argv):
            launched.append(proc)
            assert '--scope' in argv and '--user' in argv
            assert kwargs['pass_fds']
        return proc
    monkeypatch.setattr(dispatch.subprocess,'Popen',observe)
    with kbc.connect_closing() as conn:
        task_id=kb.create_task(conn,title='real protected systemd transport',assignee='fixture',
            workspace_kind='dir',workspace_path=str(workspace),
            skills=['--bootstrap-probe-invalid'],max_runtime_seconds=35)
        try:
            dispatch.dispatch_once(conn,max_in_progress=1)
            task=kb.get_task(conn,task_id)
            assert launched,task.last_failure_error
            wait_for(lambda:scopes.settled(conn,task.current_run_id),timeout=30)
            scope=authority.read(conn,task.current_run_id)
            output=kb.read_worker_log(task_id,tail_bytes=16000) or ''
            assert scope['returncode']==2,output
            assert 'expected one argument' in output,output
            assert scope['worker_pid'] and scope['children_reaped']
            assert not authority.pending(conn,task_id)
        finally:
            print('SYSTEMD_WORKER_OUTPUT='+(kb.read_worker_log(task_id,tail_bytes=16000) or ''),flush=True)
            print('SYSTEMD_PRIVATE_PENDING='+repr(authority.pending(conn,task_id)),flush=True)
            scopes.request_task_stop(conn,task_id)
            for proc in launched:
                if proc.poll() is None:proc.terminate()
                proc.wait(timeout=10)
