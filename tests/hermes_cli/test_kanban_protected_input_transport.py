"""Complete large tasks cross real Linux gates without argv/environment limits."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest

from tests.hermes_cli.test_kanban_protected_authority import protected_board
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_execution_authority as protected
from hermes_cli import kanban_execution_supervisor as supervisor


@pytest.mark.linux_only
@pytest.mark.parametrize('grant', [False, True])
def test_complete_task_is_delivered_only_after_final_gate(protected_board, grant):
    home, public, authority, worker = protected_board
    workspace = public/'large-input';workspace.mkdir(mode=0o777);workspace.chmod(0o777)
    # Many characters expand in nested JSON; actual input stays at 512 KiB.
    body = ('λ\\"\n' * (512*1024//5)) + 'x'*(512*1024%5)
    assert len(body.encode()) == 512*1024
    captured = workspace/'captured.json'
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title='full frozen input', body=body, assignee='fixture',
                                workspace_kind='dir', workspace_path=str(workspace), max_runtime_seconds=30)
        task = kb.claim_task(conn, task_id)
        argv = dispatch._worker_argv(task, 'fixture', str(authority.worker_profile('fixture')))
    assert max(len(a.encode()) for a in argv) < 4096
    env = {'HERMES_HOME': str(authority.worker_profile('fixture')), 'HERMES_KANBAN_DB': str(public/'kanban.db'),
           'HERMES_KANBAN_WORKSPACE_REQUEST': json.dumps({'task': asdict(task), 'default_workdir': None})}
    fd = protected.environment_fd(env)
    initial_r, initial_w = os.pipe();ready_r, ready_w = os.pipe();final_r, final_w = os.pipe()
    code = r'''
import json,os,sys
from hashlib import sha256
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hermes_cli import kanban_execution_supervisor as owner
# Only the provider/profile invocation is replaced with an input observer.
# Workspace resolution, sealed FD, credential drop and both gates are real.
def capture(task, profile, home):
 assert os.geteuid()!=0
 assert 'HERMES_KANBAN_WORKSPACE_REQUEST' not in os.environ
 Path(sys.argv[2]).write_text(json.dumps({'sha256':sha256(task['body'].encode()).hexdigest(),
   'bytes':len(task['body'].encode()),'task_id':task['id'],'profile':profile,'uid':os.geteuid()}))
 return 0
owner._profile_worker_entry=capture
raise SystemExit(owner._worker_entry(int(sys.argv[3]),float(sys.argv[4]),json.loads(sys.argv[5]),
 env_fd=int(sys.argv[6]),ready_fd=int(sys.argv[7]),final_fd=int(sys.argv[8])))
'''
    process = None
    try:
        process = subprocess.Popen([sys.executable, '-I', '-c', code, str(Path(kb.__file__).resolve().parents[1]),
            str(captured), str(initial_r), str(time.time()+30), json.dumps(argv), str(fd), str(ready_w), str(final_r)],
            pass_fds=(initial_r, fd, ready_w, final_r), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **supervisor._worker_credentials())
        for opened in (initial_r, fd, ready_w, final_r):os.close(opened)
        initial_r = fd = ready_w = final_r = None
        os.write(initial_w, b'1');os.close(initial_w);initial_w=None
        assert select.select([ready_r], [], [], 10)[0], 'worker did not reach final gate'
        assert os.read(ready_r, 1) == b'1'
        assert not captured.exists()
        if grant:os.write(final_w, b'1')
        os.close(final_w);final_w=None
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == (0 if grant else 1), (stdout, stderr)
        if grant:
            assert json.loads(captured.read_text()) == {'sha256':sha256(body.encode()).hexdigest(),
                'bytes':len(body.encode()),'task_id':task_id,'profile':'fixture','uid':worker.pw_uid}
        else:assert not captured.exists()
    finally:
        for opened in (initial_r, initial_w, fd, ready_r, ready_w, final_r, final_w):
            if opened is not None:os.close(opened)
        if process and process.poll() is None:
            process.kill();process.wait(timeout=5)
