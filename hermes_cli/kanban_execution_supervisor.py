"""Linux native worker owner: adopt/reap detached children before settling a run.

The supervisor stays alive through cleanup. SIGKILL/loss without its durable
receipt leaves capacity occupied; a process scan cannot manufacture that proof.
"""
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def _subreaper():
    if sys.platform != 'linux':
        raise RuntimeError('execution supervision requires Linux child subreaping')
    libc = ctypes.CDLL(None, use_errno=True)
    enabled = ctypes.c_int()
    if libc.prctl(36, 1, 0, 0, 0) or libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) or enabled.value != 1:
        raise RuntimeError('kernel child subreaping unavailable')


def _reap():
    """ECHILD is proof; WNOHANG=0 means at least one live child still exists."""
    exited = {}
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return exited, True
        if pid == 0:
            return exited, False
        exited[pid] = os.waitstatus_to_exitcode(status)


def _signal_children(sig):
    # Single-threaded owner: no reaping between this kernel child list and
    # signalling. Exited children remain zombies, so these PIDs cannot be reused.
    path = Path(f'/proc/self/task/{os.getpid()}/children')
    for value in path.read_text().split():
        try:
            os.kill(int(value), sig)
        except ProcessLookupError:
            pass


def _launch_worker(conn, run_id, scope, pid, fingerprint, argv, may_launch, worker_env=None):
    from hermes_cli import kanban_execution_scope as scopes
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    read_fd, write_fd = os.pipe()
    env_fd = None
    if worker_env is not None:
        from hermes_cli.kanban_execution_authority import environment_fd
        env_fd = environment_fd(worker_env)
    try:
        # exec preserves this child's PID/start fingerprint. The pipe prevents
        # even an immediate provider rejection racing its durable identity.
        proc = subprocess.Popen(
            [sys.executable, *(['-I'] if worker_env is not None else []), str(Path(__file__).resolve()), '--worker', str(read_fd),
             str(scope['deadline']), *([] if env_fd is None else ['--environment-fd', str(env_fd)]), *argv],
            stdin=subprocess.DEVNULL, pass_fds=(read_fd,) if env_fd is None else (read_fd,env_fd),
            **_worker_credentials())
        scopes.bind_worker(conn, run_id, scope['id'], pid, fingerprint,
                           proc.pid, _process_fingerprint(proc.pid))
        if may_launch():
            os.write(write_fd, b'1')
        return proc
    finally:
        os.close(read_fd)
        os.close(write_fd)
        if env_fd is not None: os.close(env_fd)


def _worker_credentials():
    from hermes_cli.kanban_execution_authority import current
    authority = current()
    if authority is None: return {}
    # Inherit this across the credential drop and every later worker exec.
    # A setuid binary or file capability must not restore dispatcher privilege.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(39, 0, 0, 0, 0) != 1:
        raise RuntimeError('worker no-new-privileges isolation unavailable')
    return {'user': authority.policy['worker_uid'], 'group': authority.policy['worker_gid'], 'extra_groups': []}


def _worker_entry(read_fd, deadline, argv, env_fd=None):
    try:
        granted = os.read(read_fd, 1) == b'1'
    finally:
        os.close(read_fd)
    # DB/pipe startup waits cannot extend the original absolute cutoff.
    if not granted or (deadline is not None and time.time() >= deadline):
        return 1
    env = os.environ
    if env_fd is not None:
        from hermes_cli.kanban_execution_authority import read_environment
        env = read_environment(env_fd)
        # Workspace Git/config/filesystem operations belong to the supervised,
        # dropped-UID child, including any checkout filters or hooks it starts.
        os.environ.clear()
        os.environ.update(env)
        request = os.environ.pop('HERMES_KANBAN_WORKSPACE_REQUEST', None)
        if request is not None:
            os.chdir(os.environ['HERMES_HOME'])
            from hermes_cli.kanban_protected_workspace import resolve_request
            resolve_request(request)
        else:
            workspace = env.get('TERMINAL_CWD')
            if workspace and Path(workspace).is_dir(): os.chdir(workspace)
        env = os.environ
    os.execvpe(argv[0], argv, env)


def _profile_worker_entry(task_json, profile, home):
    """Only the dropped-UID process may load its writable runtime profile."""
    import json
    if os.geteuid() == 0:
        raise RuntimeError('worker profile loading requires dropped privilege')
    from hermes_cli.kanban_db import Task
    from hermes_cli.kanban_db_dispatch import _worker_argv
    command = _worker_argv(Task(**json.loads(task_json)), profile, home, isolated_source=True)
    os.execvpe(command[0], command, os.environ)


def _cli_worker_entry(arguments):
    """Keep the verified source root across exec, independent of cwd/install state."""
    import runpy
    if os.geteuid() == 0:
        raise RuntimeError('worker CLI loading requires dropped privilege')
    sys.argv = [str(Path(__file__).with_name('main.py')), *arguments]
    runpy.run_module('hermes_cli.main', run_name='__main__')


def supervise(db_path, task_id, run_id, claim_lock, scope_id, argv, worker_env=None):
    from hermes_cli import kanban_db_connect as kbc, kanban_execution_scope as scopes
    from hermes_cli.kanban_db_dispatch import _process_fingerprint
    _subreaper()
    stopping = False
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, stop)
    pid = os.getpid()
    fingerprint = _process_fingerprint(pid)
    with kbc.connect_closing(Path(db_path)) as conn:
        scope = scopes.activate(conn, task_id, run_id, claim_lock, scope_id, pid, fingerprint)
        deadline = scope['deadline']
        monotonic_cutoff = None if deadline is None else time.monotonic()+max(0, deadline-time.time())
        def expired():
            return deadline is not None and (time.time() >= deadline or time.monotonic() >= monotonic_cutoff)
        proc, root_code, stopping_at = None, None, None
        reason = 'root_exit'
        try:
            # The immutable cutoff is checked at the actual process boundary,
            # after startup/DB waits, not only when the dispatcher planned it.
            if stopping or expired():
                reason = 'stopped_before_launch' if stopping else 'deadline_before_launch'
            else:
                proc = _launch_worker(conn, run_id, scope, pid, fingerprint, argv,
                                      lambda: not stopping and not expired(), worker_env=worker_env)
                while True:
                    exits, empty = _reap()
                    if proc.pid in exits:
                        root_code = exits[proc.pid]
                        proc.returncode = root_code
                    if empty:
                        break
                    if expired():
                        reason, stopping = 'deadline', True
                    if stopping or root_code is not None:
                        if stopping_at is None:
                            stopping_at = time.monotonic()
                            if stopping and reason == 'root_exit':
                                reason = 'signal'
                        _signal_children(signal.SIGKILL if time.monotonic()-stopping_at >= 2 else signal.SIGTERM)
                    time.sleep(.02)
            # Even failed Popen/prelaunch must establish absence, not assume it.
            _, empty = _reap()
            if not empty:
                raise RuntimeError('execution still owns children')
            scopes.finish(conn, run_id, scope_id, pid, fingerprint, reason=reason, returncode=root_code)
            return root_code if root_code is not None and root_code >= 0 else 1
        finally:
            # Unexpected failures must not publish cleanup. Attempt to kill
            # owned children; a lost/failed supervisor remains unresolved.
            _, empty = _reap()
            if not empty:
                _signal_children(signal.SIGKILL)


if __name__ == '__main__':
    # Script execution works from an arbitrary native workspace without relying
    # on PYTHONPATH or a globally installed copy of Hermes.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    if sys.argv[1] == '--cli-worker':
        raise SystemExit(_cli_worker_entry(sys.argv[2:]))
    if sys.argv[1] == '--profile-worker':
        raise SystemExit(_profile_worker_entry(*sys.argv[2:]))
    if sys.argv[1] == '--worker':
        _, read_fd, deadline, *command = sys.argv[1:]
        env_fd = None
        if command[0] == '--environment-fd':
            env_fd = int(command[1]);command = command[2:]
        raise SystemExit(_worker_entry(int(read_fd), None if deadline == 'None' else float(deadline), command, env_fd=env_fd))
    arguments = sys.argv[1:]
    authority = None
    worker_env = None
    if arguments[0] == '--authority':
        import json
        from hermes_cli.kanban_execution_authority import Authority
        authority = Authority(json.loads(arguments[1]))
        arguments = arguments[2:]
        if arguments[0] != '--environment-fd':
            raise SystemExit('protected worker environment descriptor required')
        from hermes_cli.kanban_execution_authority import read_environment
        worker_env = read_environment(int(arguments[1]));arguments = arguments[2:]
    db_path, task_id, run_id, claim_lock, scope_id, *command = arguments
    if not command:
        raise SystemExit('native command is required')
    from hermes_cli.kanban_execution_authority import using
    with using(authority):
        raise SystemExit(supervise(db_path, task_id, int(run_id), claim_lock, scope_id, command, worker_env=worker_env))
