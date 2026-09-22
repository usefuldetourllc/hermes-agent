"""Protected root execution records with actual lower-privilege Linux workers."""
import json
import os
from pathlib import Path
import pwd
import sys
import time

import pytest

from tests.hermes_cli.test_kanban_execution_scope import wait_for
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, kanban_execution_scope as scopes
from hermes_cli import kanban_spawn_ownership as ownership, kanban_execution_authority as protected


@pytest.fixture
def protected_board(tmp_path, monkeypatch, all_assignees_spawnable):
    if os.geteuid() != 0:
        pytest.skip('requires isolated root test launcher for actual UID separation')
    worker = pwd.getpwnam('hermes')
    assert worker.pw_uid > 0 and worker.pw_gid > 0
    # Only test-owned ancestors under /tmp; let the unprivileged fixture traverse.
    for path in (tmp_path, *tmp_path.parents):
        if str(path) == '/tmp': break
        if path.stat().st_uid == 0: path.chmod(path.stat().st_mode | 0o011)
    home = tmp_path/'control';home.mkdir(mode=0o755)
    worker_root = tmp_path/'worker-root';worker_root.mkdir(mode=0o755)
    profiles = worker_root/'profiles';profiles.mkdir(mode=0o755)
    profile = profiles/'fixture';profile.mkdir(mode=0o700)
    (profile/'config.yaml').write_text(json.dumps({'platform_toolsets':{'cli':['clarify']}}))
    for path in (profile,profile/'config.yaml'):
        os.chown(path,worker.pw_uid,worker.pw_gid)
    public = tmp_path/'public';public.mkdir(mode=0o777);public.chmod(0o777)
    db_path = public/'kanban.db'
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME',str(home))
    monkeypatch.setenv('HERMES_KANBAN_DB',str(db_path))
    monkeypatch.setenv('HERMES_KANBAN_WORKSPACES_ROOT',str(public/'workspaces'))
    monkeypatch.setattr(Path,'home',lambda:home)
    policy = {'directory':str(tmp_path/'authority'),'profiles_directory':str(profiles),
              'worker_uid':worker.pw_uid,'worker_gid':worker.pw_gid}
    # The consuming readonly config loader, not an injected authority mock.
    (home/'config.yaml').write_text(json.dumps({'kanban':{'execution_authority':policy}}))
    kb.init_db(db_path)
    for path in public.glob('kanban.db*'):
        os.chown(path,worker.pw_uid,worker.pw_gid);path.chmod(0o660)
    authority = protected.current()
    assert authority.policy == policy
    return home,public,authority,worker


@pytest.mark.linux_only
@pytest.mark.parametrize('case',['ordinary','api_forgery','board_forgery','supervisor_loss','provider'])
def test_protected_cleanup_survives_worker_control_of_board(protected_board,tmp_path,monkeypatch,case):
    home,public,authority,worker = protected_board
    monkeypatch.setenv('OPENROUTER_API_KEY','root-only-fixture')
    work=public/'work';work.mkdir(mode=0o777);work.chmod(0o777)
    go,release,publication=[work/name for name in ('go','release','observed.json')]
    source = r'''
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from hermes_cli import kanban_db as kb,kanban_db_connect as kbc,kanban_execution_scope as scopes
from hermes_cli.config import load_config
from hermes_constants import get_hermes_home
from hermes_state import SessionDB
config=load_config()
assert config['platform_toolsets']['cli']==['clarify']
runtime_home=get_hermes_home()
(runtime_home/'sessions'/'fixture.txt').write_text('worker runtime is writable')
db=SessionDB()
try: db.create_session(session_id='protected-fixture',source='cli')
finally: db.close()
until=time.monotonic()+30
while not Path(sys.argv[2]).exists() and time.monotonic()<until:time.sleep(.02)
with kbc.connect_closing(Path(os.environ['HERMES_KANBAN_DB'])) as conn:
 task=kb.get_task(conn,os.environ['HERMES_KANBAN_TASK']);run=task.current_run_id;scope=scopes.read(conn,run)
 data={'uid':os.geteuid(),'gid':os.getegid(),'groups':os.getgroups(),'pid':os.getpid(),'blocked':False}
 status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
 data['no_new_privs']=status['NoNewPrivs'].strip()=='1'
 data['no_effective_caps']=int(status['CapEff'].strip(),16)==0
 from hermes_cli.kanban_db_dispatch import _worker_argv
 command=_worker_argv(task,'fixture',str(runtime_home))
 assert '--profile-worker' not in command
 assert command[command.index('--toolsets')+1]=='clarify'
 assert 'OPENROUTER_API_KEY' not in os.environ
 data['runtime_home']=str(runtime_home)
 try: Path(sys.argv[5]).joinpath('authority.sqlite3').read_bytes()
 except PermissionError:data['private_read_denied']=True
 try: Path(sys.argv[5]).joinpath('forgery').write_text('fake')
 except PermissionError:data['private_write_denied']=True
 if sys.argv[6]=='provider':
  from hermes_cli.kanban_worker_failure import report_provider_failure
  report_provider_failure({'failure_reason':'billing'})
 else: assert kb.complete_task(conn,task.id,expected_run_id=run)
 if sys.argv[6]=='api_forgery':
  try:scopes.finish(conn,run,scope['id'],scope['supervisor_pid'],scope['supervisor_fingerprint'],reason='root_exit',returncode=0)
  except RuntimeError:data['blocked']=True
 if sys.argv[6]=='board_forgery':
  # Deliberately corrupt the public projection while the real process lives.
  scope.update(contract='linux-child-subreaper-v1',state='settled',children_reaped=True)
  with kb.write_txn(conn):conn.execute("UPDATE task_runs SET execution_scope=?,spawn_state='settled' WHERE id=?",(json.dumps(scope),run))
 stage=Path(sys.argv[4]).with_suffix('.pending');stage.write_text(json.dumps(data));stage.replace(sys.argv[4])
until=time.monotonic()+30
while not Path(sys.argv[3]).exists() and time.monotonic()<until:time.sleep(.02)
'''
    cmd=[sys.executable,'-c',source,str(Path(kb.__file__).resolve().parents[1]),str(go),str(release),str(publication),str(authority.directory),case]
    real_worker_argv=dispatch._worker_argv
    def fixture_argv(task,profile,profile_home):
        deferred=real_worker_argv(task,profile,profile_home)
        assert '--profile-worker' in deferred
        return cmd
    monkeypatch.setattr(dispatch,'_worker_argv',fixture_argv)
    launched=[];worker_logs=[];real_popen=dispatch.subprocess.Popen
    def popen(argv,**kwargs):
        proc=real_popen(argv,**kwargs)
        if 'kanban_execution_supervisor.py' in str(argv):
            launched.append(proc)
            worker_logs.append(Path(kwargs['stdout'].name))
        return proc
    monkeypatch.setattr(dispatch.subprocess,'Popen',popen)
    worker_pid=worker_fingerprint=None
    try:
        with kbc.connect_closing() as conn:
            task_id=kb.create_task(conn,title='protected owner',assignee='fixture',workspace_kind='dir',workspace_path=str(work),max_runtime_seconds=35)
            dispatch.dispatch_once(conn,max_in_progress=1)
            task=kb.get_task(conn,task_id);assert launched,task.last_failure_error
            run=task.current_run_id
            wait_for(lambda:(authority.read(conn,run) or {}).get('worker_pid'))
            original=authority.read(conn,run);worker_pid=original['worker_pid'];worker_fingerprint=original['worker_fingerprint']
            for path in public.glob('kanban.db*'):
                os.chown(path,worker.pw_uid,worker.pw_gid);path.chmod(0o660)
            go.touch();wait_for(publication.exists)
            observed=json.loads(publication.read_text())
            assert Path(observed['runtime_home'])==authority.worker_profile('fixture')
            assert (Path(observed['runtime_home'])/'sessions'/'fixture.txt').read_text()=='worker runtime is writable'
            assert observed['uid']==worker.pw_uid and observed['gid']==worker.pw_gid and observed['groups']==[]
            assert observed['no_new_privs'] and observed['no_effective_caps']
            assert observed['private_read_denied'] and observed['private_write_denied']
            if case=='api_forgery':assert observed['blocked']
            assert authority.read(conn,run)['state']=='active'
            assert not scopes.settled(conn,run) and ownership.pending(conn,task_id)
            assert dispatch.count_running_tasks(conn)==1
            assert not ownership.reconcile(conn,task_id)
            # Simulated dispatcher restart/config removal recovers the latched owner.
            (home/'config.yaml').write_text('{}')
            assert protected.current().read(conn,run)==authority.read(conn,run)
            if case=='provider':
                from hermes_cli.kanban_worker_failure import provider_verdict
                verdict=provider_verdict(conn,task_id,original['supervisor_pid'])
                assert verdict and verdict['reason']=='billing' and verdict['pid']==worker_pid
            if case=='supervisor_loss':
                launched[0].kill();launched[0].wait(timeout=5)
                release.touch();wait_for(lambda:dispatch._worker_identity(worker_pid,worker_fingerprint)!='owned')
                assert not scopes.settled(conn,run) and ownership.pending(conn,task_id)
            else:
                release.touch();launched[0].wait(timeout=10)
                assert scopes.settled(conn,run),authority.read(conn,run)
                assert authority.read(conn,run)['deadline']==original['deadline']
                assert ownership.reconcile(conn,task_id)
                assert not authority.pending(conn,task_id)
    except BaseException:
        # The canonical runner deletes its per-file temp tree on exit. Emit
        # synthetic worker diagnostics now, without masking the original failure.
        try:
            with kbc.connect_closing() as conn:
                records = authority.pending(conn)
                scopes_at_failure = [dict(row) for row in conn.execute(
                    'SELECT id,execution_scope FROM task_runs')]
            print('PROTECTED_WORKER_DIAGNOSTICS=' + json.dumps({
                'case': case,
                'supervisors': [{'pid': proc.pid, 'returncode': proc.poll()} for proc in launched],
                'private_pending': records,
                'public_scopes': scopes_at_failure,
                'worker_logs': {str(path): path.read_text(errors='replace')[-16000:] for path in worker_logs},
            }), flush=True)
        except Exception as diagnostic_error:
            print(f'PROTECTED_WORKER_DIAGNOSTICS_ERROR={diagnostic_error!r}', flush=True)
        raise
    finally:
        go.touch();release.touch()
        for proc in launched:
            if proc.poll() is None:proc.terminate()
            proc.wait(timeout=10)
        if worker_pid:wait_for(lambda:dispatch._worker_identity(worker_pid,worker_fingerprint)!='owned',timeout=10)


@pytest.mark.linux_only
def test_protected_real_cli_bootstrap_uses_candidate_from_external_workspace(protected_board):
    home,public,authority,worker = protected_board
    workspace = public/'external-workspace';workspace.mkdir(mode=0o755)
    # A cwd-local package must not replace the root-verified Hermes source.
    decoy = workspace/'hermes_cli';decoy.mkdir()
    (decoy/'__init__.py').write_text("raise RuntimeError('untrusted workspace package imported')")
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn,title='real CLI bootstrap',assignee='fixture',
                                workspace_kind='dir',workspace_path=str(workspace),
                                skills=['--bootstrap-probe-invalid'],max_runtime_seconds=30)
        # No command/spawn replacement. The deliberately invalid skills option
        # reaches the actual CLI parser and exits before any agent/provider run.
        dispatch.dispatch_once(conn,max_in_progress=1)
        task = kb.get_task(conn,task_id)
        log_path = kb.worker_logs_dir()/f'{task_id}.log'
        try:
            wait_for(lambda:scopes.settled(conn,task.current_run_id),timeout=25)
            scope = authority.read(conn,task.current_run_id)
            output = log_path.read_text(errors='replace')
            assert scope['returncode']==2,output
            assert 'expected one argument' in output,output
            assert 'No module named' not in output and 'untrusted workspace package imported' not in output,output
            assert scope['children_reaped'] and not authority.pending(conn,task_id)
        finally:
            if log_path.exists():print('REAL_CLI_BOOTSTRAP_OUTPUT='+log_path.read_text(errors='replace'),flush=True)
            scopes.request_task_stop(conn,task_id)
            wait_for(lambda:scopes.settled(conn,task.current_run_id),timeout=10)


@pytest.mark.linux_only
def test_protected_owner_rejects_alternate_callback_and_config_change(protected_board):
    home,public,authority,worker=protected_board
    with kbc.connect_closing() as conn:
        with pytest.raises(RuntimeError,match='unowned spawn callback'):
            dispatch.dispatch_once(conn,spawn_fn=lambda *args:pytest.fail('unowned callback launched'))
    (home/'config.yaml').write_text(json.dumps({'kanban':{'execution_authority':{**authority.policy,'worker_uid':worker.pw_uid+1}}}))
    with pytest.raises(RuntimeError,match='configuration must be restored'):protected.current()
