"""Installed external workers retain native gates, complete input and cleanup."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from tests.hermes_cli.test_kanban_protected_authority import protected_board
from tests.hermes_cli.test_kanban_execution_scope import wait_for
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_execution_authority as private, kanban_execution_scope as scopes


@pytest.mark.linux_only
@pytest.mark.parametrize('case', ['accepted', 'revoked', 'deadline'])
def test_installed_external_runtime_retains_original_input_and_owner(protected_board, tmp_path, monkeypatch, case):
    old_home, public, old, worker = protected_board
    home=tmp_path/'external-control';home.mkdir(mode=0o755)
    monkeypatch.setenv('HERMES_HOME',str(home));monkeypatch.setenv('HERMES_KANBAN_HOME',str(home))
    monkeypatch.setattr(Path,'home',lambda:home)
    source=str(Path(kb.__file__).resolve().parents[1])
    adapter=tmp_path/'adapter.py'
    adapter.write_text("""
import json,sys,time
from pathlib import Path
request=json.load(sys.stdin)
root=Path(__file__).parent
phase=request['phase']
with (root/'phases').open('a') as log:log.write(phase+'\\n')
if phase=='prepare':result={'deadline':time.time()+4 if (root/'case').read_text()=='deadline' else time.time()+20,'receipt':{'fixture':True}}
elif phase=='launch':result={'launch_authorized':True}
elif phase=='check':result={'launch_current':(root/'case').read_text()!='revoked'}
else:result={'settled':True}
print(json.dumps(result))
""")
    (tmp_path/'case').write_text(case)
    program=tmp_path/'worker.py'
    program.write_text("""
import argparse,hashlib,json,os,sys,time,subprocess
from pathlib import Path
sys.path.insert(0,SOURCE)
from hermes_cli.kanban_external_worker import read_request,input_gate
p=argparse.ArgumentParser()
for name in ('request-fd','ready-fd','final-gate-fd'):p.add_argument('--'+name,type=int,required=True)
p.add_argument('--deadline',type=float,required=True);args=p.parse_args()
request=read_request(args.request_fd);root=Path(PUBLIC)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],start_new_session=True)
(root/'prepared').write_text(json.dumps({'uid':os.geteuid(),'child':child.pid,'request_sha':hashlib.sha256(request['task']['body'].encode()).hexdigest()}))
while not (root/'release').exists():time.sleep(.02)
if not input_gate(args.ready_fd,args.final_gate_fd,args.deadline):raise SystemExit(1)
assert all('FINAL-QUALIFICATION' not in value for value in os.environ.values())
assert b'FINAL-QUALIFICATION' not in Path('/proc/self/cmdline').read_bytes()
(root/'input').write_text(request['task']['body'])
""".replace('SOURCE',repr(source)).replace('PUBLIC',repr(str(public))))
    policy={**old.policy,'directory':str(tmp_path/'external-authority'),
            'admission_command':[str(Path(sys.executable).resolve()),str(adapter)],
            'worker_commands':{'fixture':[str(Path(sys.executable).resolve()),'-I',str(program)]}}
    (home/'config.yaml').write_text(json.dumps({'kanban':{'execution_authority':policy}}))
    authority=private.current()  # Real config loader and persisted authority, no argv mock.
    workspace=public/'external-work';workspace.mkdir();os.chown(workspace,worker.pw_uid,worker.pw_gid)
    prompt=('λ \\" \n'*70000)+'FINAL-QUALIFICATION'
    prompt += 'x'*(512*1024-len(prompt.encode()))
    assert len(prompt.encode())==512*1024
    with kbc.connect_closing() as conn:
        task_id=kb.create_task(conn,title='installed external worker',body=prompt,assignee='fixture',
                               workspace_kind='dir',workspace_path=str(workspace),max_runtime_seconds=30)
        assert dispatch.dispatch_once(conn,max_in_progress=1).spawned
        run=kb.get_task(conn,task_id).current_run_id
        try:
            wait_for(lambda:(public/'prepared').exists() or scopes.settled(conn,run))
            assert (public/'prepared').exists(),kb.read_worker_log(task_id,tail_bytes=12000)
            observed=json.loads((public/'prepared').read_text())
            assert observed['uid']==worker.pw_uid
            assert observed['request_sha']==hashlib.sha256(prompt.encode()).hexdigest()
            with kb.write_txn(conn):conn.execute('UPDATE tasks SET body=? WHERE id=?',('mutated public task',task_id))
            if case!='deadline':(public/'release').touch()
            wait_for(lambda:scopes.settled(conn,run) and not authority.pending(conn,task_id),timeout=15)
            final=authority.read(conn,run)
            assert final['children_reaped'] and not authority.pending(conn,task_id)
            assert not Path('/proc/'+str(observed['child'])).exists()
            assert (public/'input').exists()==(case=='accepted')
            if case=='accepted':assert (public/'input').read_text()==prompt
            assert final.get('admission_input_attempted',False)==(case!='deadline')
            phases=(tmp_path/'phases').read_text().splitlines()
            assert phases.count('launch')==1 and phases.count('check')==(case!='deadline')
            assert final['reason']==('deadline' if case=='deadline' else 'root_exit')
        except BaseException:
            print(kb.read_worker_log(task_id,tail_bytes=12000));raise
        finally:
            scopes.request_task_stop(conn,task_id)
            wait_for(lambda:scopes.settled(conn,run),timeout=10)


@pytest.mark.linux_only
@pytest.mark.parametrize('case',['no_gate','relative','writable','changed'])
def test_external_command_policy_cannot_select_untrusted_or_replaced_code(protected_board,tmp_path,case):
    home,public,old,worker=protected_board
    command=tmp_path/'installed';command.write_text('operator runtime')
    policy={**old.policy,'directory':str(tmp_path/'external-policy'),
            'admission_command':[str(Path(sys.executable).resolve())],
            'worker_commands':{'fixture':[str(command)]}}
    if case=='no_gate':policy.pop('admission_command')
    elif case=='relative':policy['worker_commands']['fixture']=['worker']
    elif case=='writable':command.chmod(0o666)
    else:
        private.Authority(policy,create=True)
        policy['worker_commands']['fixture'].append('--changed')
    with pytest.raises(RuntimeError):private.Authority(policy,create=True)
