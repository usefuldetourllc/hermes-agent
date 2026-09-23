"""Workspace preparation across the protected dispatcher/worker boundary."""
import json
import os
from pathlib import Path
import tempfile
import sys


def default_root(board=None):
    """Keep shared worker data beside profiles, outside the private control home."""
    if sys.platform != 'linux' or os.geteuid() != 0:
        return None
    from hermes_cli.kanban_execution_authority import current
    from hermes_cli import kanban_db as kb
    authority = current()
    if authority is None:
        return None
    return authority.profiles_directory.parent / 'workspaces' / kb._slug_or_default(board)


def validate_task_id(task_id):
    """A board identifier must remain one filename component at root boundaries."""
    if (not isinstance(task_id, str) or not task_id or task_id in ('.', '..')
            or '/' in task_id or '\x00' in task_id):
        raise ValueError('protected task identifier must be one filename component')


def _create_scratch(root, task_id, uid, gid):
    """Grant only a newly created task directory, never chown existing paths.

    Descriptor-relative, no-follow operations keep a worker-controlled board
    directory from redirecting root's ownership changes through a symlink.
    """
    validate_task_id(task_id)
    root = Path(root).expanduser().absolute()
    if '..' in root.parts:
        raise ValueError('scratch root must not contain parent traversal')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = (*root.parts[1:], task_id)
        for index, name in enumerate(parts):
            leaf = index == len(parts) - 1
            if leaf:
                info = os.fstat(fd)
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise RuntimeError('managed scratch parent must be root-owned and not worker-writable')
            created = False
            try:
                os.mkdir(name, mode=0o755, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if leaf and created:
                os.fchown(fd, uid, gid)
                os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    return root / task_id


def prepare_request(task, board, authority):
    """Resolve root-owned metadata only; defer filesystem work to the worker."""
    from hermes_cli import kanban_db as kb
    validate_task_id(task.id)
    if (task.workspace_kind or 'scratch') == 'scratch' and not task.workspace_path:
        task.workspace_path = str(_create_scratch(
            kb.workspaces_root(board=board), task.id,
            authority.policy['worker_uid'], authority.policy['worker_gid']))
    elif task.workspace_kind == 'worktree' and not task.workspace_path:
        anchor = (kb.read_board_metadata(board or kb.get_current_board())
                  .get('default_workdir') or '').strip()
        if not anchor:
            raise ValueError('protected worktree task requires a board default_workdir or workspace_path')
        return anchor
    # This is a launch hint, not a resolved worktree path. The supervised worker
    # persists the actual path and branch before loading task/profile code.
    return task.workspace_path or ''


def resolve_request(request):
    """Materialize under the worker UID and publish for the original claim."""
    if os.geteuid() == 0:
        raise RuntimeError('workspace resolution requires dropped privilege')
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_cli import kanban_db_workspace as workspace
    payload = json.loads(request)
    task = kb.Task(**payload['task'])
    branch = task.branch_name
    if task.workspace_kind == 'worktree':
        path, branch = workspace._resolve_worktree_workspace(task, default_workdir=payload.get('default_workdir'))
    else:
        path = workspace.resolve_workspace(task)
    os.chdir(path)
    # Test the actual UID's access (including ACLs), without touching user files.
    with tempfile.TemporaryFile(dir=path):
        pass
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            changed = conn.execute(
                'UPDATE tasks SET workspace_path=?, branch_name=? '
                'WHERE id=? AND current_run_id=? AND claim_lock=?',
                (str(path), branch, task.id, task.current_run_id, task.claim_lock)).rowcount
            if changed != 1:
                raise RuntimeError('workspace launch claim changed')
    os.environ['TERMINAL_CWD'] = str(path)
    os.environ['HERMES_KANBAN_WORKSPACE'] = str(path)
    if branch:
        os.environ['HERMES_KANBAN_BRANCH'] = branch
