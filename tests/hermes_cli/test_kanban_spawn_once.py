"""A spawn error cannot authorize a second process under the same original run."""
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.mark.parametrize('error', [TypeError, ValueError])
def test_dispatch_does_not_repeat_callback_after_process_was_started(tmp_path, monkeypatch, all_assignees_spawnable, error):
    home = tmp_path/'hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACES_ROOT', str(tmp_path/'workspaces'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    kb.init_db()
    launches = []
    def spawn(task, workspace, *, board=None):
        # Exercise a real but harmless process launch; no agent/model/network.
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        try:
            assert child.wait(timeout=5) == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        launches.append((task.id, task.current_run_id, child.pid))
        raise error('post-spawn receipt failed')
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title='One-shot spawn probe', assignee='fixture')
        result = kbd.dispatch_once(conn, spawn_fn=spawn)
        runs = list(conn.execute('SELECT * FROM task_runs WHERE task_id=?', (task_id,)))
    assert not result.spawned
    assert len(runs) == 1
    assert len(launches) == 1, f'One original run launched multiple processes: {launches}'


@pytest.mark.parametrize('shape', ['legacy', 'board', 'opaque_type', 'opaque_value'])
def test_spawn_signature_compatibility_invokes_once(shape):
    calls = []
    task = object()
    def legacy(value, workspace):
        calls.append((value, workspace))
        return 123
    def board(value, workspace, *, board=None):
        calls.append((value, workspace, board))
        return 123
    class Opaque:
        @property
        def __signature__(self):
            raise (TypeError if shape == 'opaque_type' else ValueError)('signature unavailable')
        def __call__(self, value, workspace):
            return legacy(value, workspace)
    callback = legacy if shape == 'legacy' else board if shape == 'board' else Opaque()
    assert kbd._call_spawn_fn(callback, task, '/workspace', 'selected-board') == 123
    assert calls == [(task, '/workspace', 'selected-board') if shape == 'board' else (task, '/workspace')]
