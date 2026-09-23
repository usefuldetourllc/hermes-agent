"""Actual protected dispatch must provide usable workspaces without root Git."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.hermes_cli.test_kanban_protected_authority import protected_board
from tests.hermes_cli.test_kanban_execution_scope import wait_for
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_execution_scope as scopes


def worker_git(worker, home, path, *args):
    return subprocess.run(['git', '-C', str(path), *args], check=True,
                          capture_output=True, text=True,
                          user=worker.pw_uid, group=worker.pw_gid, extra_groups=[],
                          env={'PATH': '/usr/bin:/bin', 'HOME': str(home)})


@pytest.mark.linux_only
@pytest.mark.parametrize('variant', ['scratch', 'scratch_home', 'explicit_scratch', 'dir', 'existing_dir',
                                     'worktree', 'board_worktree', 'existing_worktree'])
def test_protected_workspace_write_and_git(protected_board, monkeypatch, variant):
    home, public, authority, worker = protected_board
    worker_parent = public/'worker-data'
    worker_parent.mkdir(mode=0o700)
    os.chown(worker_parent, worker.pw_uid, worker.pw_gid)
    profile = authority.worker_profile('fixture')
    kind = 'worktree' if 'worktree' in variant else 'dir' if 'dir' in variant else 'scratch'
    if variant == 'scratch_home': monkeypatch.delenv('HERMES_KANBAN_WORKSPACES_ROOT')
    path = None if variant in ('scratch', 'scratch_home') else worker_parent/'new'/'workspace'
    if kind == 'worktree':
        repo = worker_parent/'repo'
        repo.mkdir();os.chown(repo, worker.pw_uid, worker.pw_gid)
        worker_git(worker, profile, repo, 'init', '-q')
        worker_git(worker, profile, repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                   'commit', '-qm', 'seed', '--allow-empty')
        path = repo
        if variant == 'board_worktree':
            monkeypatch.setattr(kb, 'read_board_metadata', lambda *a: {'default_workdir': str(repo)})
            path = None
        if variant == 'existing_worktree':
            path = repo/'.worktrees'/'existing'
            worker_git(worker, profile, repo, 'worktree', 'add', '-b', 'fixture-existing', str(path))
    elif variant == 'existing_dir':
        path = worker_parent/'existing';path.mkdir();os.chown(path, worker.pw_uid, worker.pw_gid)
    observed = public/'workspace-observed.json'
    source = '''
import json,os,sys,subprocess
from pathlib import Path
cwd=Path.cwd()
(cwd/'worker-output.txt').write_text('ordinary task output')
data={'uid':os.geteuid(),'gid':os.getegid(),'cwd':str(cwd),'owner':cwd.stat().st_uid,
      'terminal_cwd':os.environ['TERMINAL_CWD'],'workspace':os.environ['HERMES_KANBAN_WORKSPACE']}
if sys.argv[2]=='worktree':
 subprocess.run(['git','add','worker-output.txt'],check=True)
 subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','worker output'],check=True)
 data['branch']=subprocess.check_output(['git','branch','--show-current'],text=True).strip()
Path(sys.argv[1]).write_text(json.dumps(data))
'''
    monkeypatch.setattr(dispatch, '_worker_argv', lambda *a, **kw: [sys.executable, '-c', source, str(observed), kind])
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title=variant, assignee='fixture', workspace_kind=kind,
                                 workspace_path=str(path) if path else None,
                                 branch_name='fixture-existing' if variant=='existing_worktree' else None,
                                 max_runtime_seconds=20)
        result = dispatch.dispatch_once(conn, max_in_progress=1)
        assert result.spawned
        task = kb.get_task(conn, task_id)
        try:
            wait_for(lambda: scopes.settled(conn, task.current_run_id), timeout=15)
            assert observed.exists(), kb.read_worker_log(task_id, tail_bytes=12000)
            data = json.loads(observed.read_text())
            task = kb.get_task(conn, task_id)
            assert data['uid'] == worker.pw_uid and data['gid'] == worker.pw_gid
            assert data['owner'] == worker.pw_uid
            assert data['cwd'] == data['terminal_cwd'] == data['workspace'] == task.workspace_path
            if kind == 'worktree': assert data['branch'] == task.branch_name
            assert not authority.pending(conn, task_id)
            assert authority.directory.stat().st_uid == 0
            assert authority.directory.stat().st_mode & 0o777 == 0o700
            print('WORKSPACE_VARIANT', variant, data)
        finally:
            scopes.request_task_stop(conn, task_id)
            wait_for(lambda: scopes.settled(conn, task.current_run_id), timeout=5)


@pytest.mark.linux_only
@pytest.mark.parametrize('variant', ['root_directory', 'private_authority', 'scratch_symlink', 'existing_root_scratch'])
def test_existing_protected_paths_are_not_reowned(protected_board, monkeypatch, variant):
    home, public, authority, worker = protected_board
    target = authority.directory if variant=='private_authority' else public/'root-owned'
    if target != authority.directory: target.mkdir(mode=0o755)
    before = (target.stat().st_uid, target.stat().st_gid, target.stat().st_mode)
    observed = public/'must-not-launch'
    monkeypatch.setattr(dispatch, '_worker_argv', lambda *a, **kw:
                        [sys.executable, '-c', 'from pathlib import Path;import sys;Path(sys.argv[1]).touch()', str(observed)])
    with kbc.connect_closing() as conn:
        scratch = variant in ('scratch_symlink', 'existing_root_scratch')
        task_id = kb.create_task(conn, title=variant, assignee='fixture',
                                 workspace_kind='scratch' if scratch else 'dir',
                                 workspace_path=None if scratch else str(target), max_runtime_seconds=20)
        if scratch:
            parent=public/'workspaces';parent.mkdir()
            if variant=='scratch_symlink':(parent/task_id).symlink_to(target, target_is_directory=True)
            else:
                (parent/task_id).mkdir(mode=0o755)
                target=parent/task_id
                before=(target.stat().st_uid,target.stat().st_gid,target.stat().st_mode)
        result = dispatch.dispatch_once(conn, max_in_progress=1)
        task = kb.get_task(conn, task_id)
        if result.spawned:
            wait_for(lambda: scopes.settled(conn, task.current_run_id), timeout=15)
        else:
            assert 'workspace:' in task.last_failure_error
        assert not observed.exists()
        assert not authority.pending(conn, task_id)
        assert before == (target.stat().st_uid, target.stat().st_gid, target.stat().st_mode)
