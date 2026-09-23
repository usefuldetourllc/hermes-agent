"""Real Linux seals and admission failure on unsupported runtimes."""
import errno
import os

import pytest

from tests.hermes_cli.test_kanban_protected_authority import protected_board
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_execution_authority as protected
from hermes_cli import kanban_spawn_ownership as ownership


@pytest.mark.linux_only
def test_environment_remains_immutable_without_python_seal_constants(monkeypatch):
    import fcntl
    for name in ('F_ADD_SEALS', 'F_GET_SEALS', 'F_SEAL_SEAL',
                 'F_SEAL_SHRINK', 'F_SEAL_GROW', 'F_SEAL_WRITE'):
        monkeypatch.delattr(fcntl, name, raising=False)
    payload = {'HERMES_PROFILE': 'fixture', 'UNICODE': 'sealed λ'}
    fd = protected.environment_fd(payload)
    try:
        original_size = os.fstat(fd).st_size
        for mutate in (lambda: os.write(fd, b'x'),
                       lambda: os.ftruncate(fd, 0),
                       lambda: os.ftruncate(fd, original_size + 1)):
            with pytest.raises(OSError) as error:
                mutate()
            assert error.value.errno == errno.EPERM
        assert protected.read_environment(os.dup(fd)) == payload
    finally:
        os.close(fd)


@pytest.mark.linux_only
def test_unavailable_sealing_never_reserves_an_execution(protected_board, monkeypatch):
    home, public, authority, worker = protected_board
    workspace = public / 'unsupported-runtime'
    workspace.mkdir()
    def unavailable(*args, **kwargs):
        raise OSError(errno.ENOSYS, 'memfd unavailable')
    monkeypatch.setattr(os, 'memfd_create', unavailable)
    monkeypatch.setattr(dispatch, '_default_spawn',
                        lambda *args, **kwargs: pytest.fail('spawn must not be entered'))
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title='unsupported sealing', assignee='fixture',
                                workspace_kind='dir', workspace_path=str(workspace))
        dispatch.dispatch_once(conn, max_in_progress=1, failure_limit=1)
        task = kb.get_task(conn, task_id)
        run = conn.execute('SELECT * FROM task_runs WHERE task_id=?', (task_id,)).fetchone()
        assert task.status == 'blocked'
        assert 'memfd unavailable' in task.last_failure_error
        assert run['ended_at'] is not None and run['spawn_state'] is None
        assert run['execution_scope'] is None and authority.read(conn, run['id']) is None
        assert not ownership.pending(conn, task_id)
        assert dispatch.count_running_tasks(conn) == 0
