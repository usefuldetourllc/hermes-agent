"""Private execution ownership for a root dispatcher and unprivileged workers.

The board is a worker-writable projection, never the authority for this mode.
The private ledger remains occupied across board edits and dispatcher restarts.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

CONTRACT = 'linux-protected-execution-v1'
_override = ContextVar('kanban_execution_authority', default=None)


def protected_path(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise RuntimeError('execution authority path must not contain symlinks')
    for part in (path, *path.parents):
        info = part.stat()
        if info.st_uid != 0 or (info.st_mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX)):
            raise RuntimeError('execution authority ancestors must be root-controlled')
    return path


def board_path(conn):
    rows = conn.execute('PRAGMA database_list').fetchall()
    return str(Path(next(row[2] for row in rows if row[1] == 'main')).resolve())


class Authority:
    def __init__(self, policy, *, create=False):
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise RuntimeError('protected execution requires a Linux root dispatcher')
        if (not isinstance(policy, dict) or not {'directory', 'profiles_directory', 'worker_uid', 'worker_gid'} <= set(policy)
                or set(policy) - {'directory', 'profiles_directory', 'worker_uid', 'worker_gid', 'admission_command'}
                or any(type(policy[k]) is not int or policy[k] <= 0 for k in ('worker_uid', 'worker_gid'))
                or any(not isinstance(policy[k], str) or not Path(policy[k]).is_absolute()
                       for k in ('directory', 'profiles_directory'))):
            raise RuntimeError('explicit private/profile directories and non-root worker uid/gid required')
        command = policy.get('admission_command')
        if command is not None:
            if (not isinstance(command, list) or not command or len(command) > 16
                    or not all(isinstance(arg, str) and arg for arg in command)
                    or not Path(command[0]).is_absolute()):
                raise RuntimeError('trusted absolute admission command required')
            for arg in command:
                if Path(arg).is_absolute(): protected_path(arg)
        self.policy = dict(policy)
        self.profiles_directory = protected_path(policy['profiles_directory'])
        if self.profiles_directory.name != 'profiles' or not self.profiles_directory.is_dir():
            raise RuntimeError('worker profiles_directory must be a separate profiles directory')
        self.directory = Path(policy['directory'])
        if create and not self.directory.exists():
            protected_path(self.directory.parent)
            self.directory.mkdir(mode=0o700)
        protected_path(self.directory)
        if stat.S_IMODE(self.directory.stat().st_mode) != 0o700:
            raise RuntimeError('execution authority directory must be mode 0700')
        self.path = self.directory/'authority.sqlite3'
        if self.path.exists():
            protected_path(self.path)
        elif not create:
            raise RuntimeError('original execution authority ledger is missing')
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS policy(value TEXT NOT NULL)')
            row = db.execute('SELECT value FROM policy').fetchone()
            encoded = json.dumps(self.policy, sort_keys=True)
            if row is None:
                if not create: raise RuntimeError('original execution authority policy is missing')
                db.execute('INSERT INTO policy(value) VALUES(?)', (encoded,))
            elif row['value'] != encoded:
                raise RuntimeError('execution authority policy cannot be replaced')
            db.execute('''CREATE TABLE IF NOT EXISTS executions (
                board TEXT NOT NULL, run_id INTEGER NOT NULL, task_id TEXT NOT NULL,
                claim_lock TEXT NOT NULL, profile TEXT, scope TEXT NOT NULL,
                PRIMARY KEY(board,run_id))''')
        os.chmod(self.path, 0o600)

    def worker_profile(self, name):
        """Resolve worker state without importing or initializing it as root."""
        from hermes_cli.profiles import normalize_profile_name, validate_profile_name
        name = normalize_profile_name(name)
        validate_profile_name(name)
        if name == 'default':
            raise RuntimeError('protected execution requires a named worker profile')
        profile = self.profiles_directory / name
        if profile.is_symlink() or not profile.is_dir():
            raise RuntimeError('protected worker profile must be an installed directory')
        info = profile.stat()
        uid, gid = self.policy['worker_uid'], self.policy['worker_gid']
        if info.st_uid != uid or info.st_gid != gid or stat.S_IMODE(info.st_mode) != 0o700:
            raise RuntimeError('worker profile must be owned by its worker uid/gid with mode 0700')
        for parent in profile.parents:
            info = parent.stat()
            permission = 0o100 if info.st_uid == uid else 0o010 if info.st_gid == gid else 0o001
            if not info.st_mode & permission:
                raise RuntimeError('worker profile ancestors must be traversable by the worker')
        return profile

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def original_execution(self, conn, run_id):
        """Original identity from the private ledger, never the board projection."""
        with self.transaction() as db:
            row = db.execute('SELECT * FROM executions WHERE board=? AND run_id=?',
                             (board_path(conn), run_id)).fetchone()
        if row is None: return None
        return {**dict(row), 'scope': json.loads(row['scope'])}

    def read(self, conn, run_id):
        with self.transaction() as db:
            row = db.execute('SELECT scope FROM executions WHERE board=? AND run_id=?', (board_path(conn), run_id)).fetchone()
        return json.loads(row['scope']) if row else None

    def pending(self, conn=None, task_id=None):
        with self.transaction() as db:
            rows = db.execute('''SELECT * FROM executions WHERE
                (? IS NULL OR board=?) AND (? IS NULL OR task_id=?)''',
                (None if conn is None else board_path(conn), None if conn is None else board_path(conn), task_id, task_id)).fetchall()
        return [dict(row) for row in rows if not self.resolved(json.loads(row['scope']))]

    @staticmethod
    def resolved(scope):
        from hermes_cli.kanban_execution_cancellation import cancelled
        return cancelled(scope) or (scope.get('state') == 'settled'
            and (scope.get('admission') is None or scope.get('admission_cleanup_confirmed') is True))

    def prepare(self, conn, task, scope):
        from dataclasses import asdict
        scope = {**scope, 'contract': CONTRACT, 'prepared_task': asdict(task)}
        with self.transaction() as db:
            # This protected owner is intentionally serial across all its boards.
            if any(not self.resolved(json.loads(row['scope'])) for row in db.execute('SELECT scope FROM executions')):
                raise RuntimeError('protected execution capacity remains occupied')
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?)',
                (board_path(conn), task.current_run_id, task.id, task.claim_lock, task.assignee, json.dumps(scope)))
        return scope

    def update(self, conn, run_id, original, updated):
        with self.transaction() as db:
            row = db.execute('SELECT scope FROM executions WHERE board=? AND run_id=?', (board_path(conn),run_id)).fetchone()
            if not row or json.loads(row['scope']) != original:
                raise RuntimeError('original protected execution ownership changed')
            db.execute('UPDATE executions SET scope=? WHERE board=? AND run_id=?',
                       (json.dumps(updated),board_path(conn),run_id))


@contextmanager
def using(authority):
    token = _override.set(authority)
    try: yield authority
    finally: _override.reset(token)


def current():
    authority = _override.get()
    if authority is not None:
        return authority
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config_readonly
    home = get_hermes_home()
    anchor = home/'kanban-execution-authority.json'
    policy = (load_config_readonly() or {}).get('kanban', {}).get('execution_authority')
    if anchor.exists():
        protected_path(anchor)
        original = json.loads(anchor.read_text())
        if policy is not None and policy != original:
            raise RuntimeError('original execution authority configuration must be restored')
        return Authority(original)
    if policy is None:
        return None
    protected_path(home)
    protected_path(home/'config.yaml')
    authority = Authority(policy, create=True)
    # The root-controlled locator latches the owner even if configuration is removed.
    fd = os.open(anchor, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(policy, out, sort_keys=True);out.flush();os.fsync(out.fileno())
    parent = os.open(home, os.O_RDONLY)
    try: os.fsync(parent)
    finally: os.close(parent)
    return authority


def environment_fd(value):
    import fcntl
    if sys.platform != 'linux':
        raise RuntimeError('protected worker environments require Linux sealing')
    # Linux UAPI (linux/fcntl.h): these operation numbers and seal bits are
    # stable even when CPython was built with headers that omit the names.
    add_seals = getattr(fcntl, 'F_ADD_SEALS', 1033)
    get_seals = getattr(fcntl, 'F_GET_SEALS', 1034)
    seals = (getattr(fcntl, 'F_SEAL_SEAL', 0x0001)
             | getattr(fcntl, 'F_SEAL_SHRINK', 0x0002)
             | getattr(fcntl, 'F_SEAL_GROW', 0x0004)
             | getattr(fcntl, 'F_SEAL_WRITE', 0x0008))
    data = json.dumps(value).encode()
    if len(data) > 256 * 1024 or not isinstance(value, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in value.items()):
        raise RuntimeError('invalid protected worker environment')
    fd = os.memfd_create('kanban-worker-environment', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(fd,data);os.lseek(fd,0,os.SEEK_SET)
        fcntl.fcntl(fd, add_seals, seals)
        if fcntl.fcntl(fd, get_seals) & seals != seals:
            raise RuntimeError('kernel did not seal the protected worker environment')
        return fd
    except BaseException:
        os.close(fd);raise


def read_environment(fd):
    try:
        with os.fdopen(fd,'rb') as source:data=source.read(256*1024+1)
        value=json.loads(data)
        if len(data)>256*1024 or not isinstance(value,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in value.items()):
            raise RuntimeError('invalid protected worker environment')
        return value
    except (ValueError,TypeError):
        raise RuntimeError('invalid protected worker environment') from None


_verified_runtime = set()


def verify_runtime():
    """Before a worker exists, reject code/dependencies it could change for root."""
    for root in (Path(__file__).resolve().parents[1],Path(sys.prefix).resolve()):
        if root in _verified_runtime:continue
        protected_path(root)
        for base, directories, files in os.walk(root):
            for name in directories+files:
                entry=Path(base)/name
                info=entry.lstat()
                if info.st_uid != 0 or (not entry.is_symlink() and info.st_mode & 0o022):
                    raise RuntimeError('protected execution runtime must be root-controlled')
                if entry.is_symlink():protected_path(entry.resolve())
        _verified_runtime.add(root)
    protected_path(Path(sys.executable).resolve())


def privileged_environment():
    from hermes_constants import get_hermes_home
    home=protected_path(get_hermes_home())
    return {'PATH':'/usr/bin:/bin','HOME':str(home),'HERMES_HOME':str(home),
            'PYTHONNOUSERSITE':'1','PYTHONSAFEPATH':'1','LANG':'C.UTF-8'}
