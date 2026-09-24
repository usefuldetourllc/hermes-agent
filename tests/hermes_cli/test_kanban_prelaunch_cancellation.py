"""Real private ownership and adapter subprocesses; no model launch is allowed."""
import json
import os
from pathlib import Path
import sys

import pytest

from tests.hermes_cli.test_kanban_protected_authority import protected_board
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_execution_authority as protected, kanban_execution_scope as scopes
from hermes_cli import kanban_execution_cancellation as cancellation, kanban_execution_admission as admission
from hermes_cli import kanban_spawn_ownership as ownership


@pytest.mark.linux_only
@pytest.mark.parametrize('failure',['rejected','lost_cancel_reply','lost_projection'])
def test_rejected_preparation_revokes_activation_and_reconciles(protected_board,tmp_path,monkeypatch,failure):
    home,public,old,worker=protected_board
    adapter=tmp_path/'adapter.py'
    adapter.write_text('''import json,sys
from pathlib import Path
r=json.load(sys.stdin);p=Path(__file__).with_suffix('.calls')
with p.open('a') as f:f.write(r['phase']+'\\n')
if r['phase']=='prepare':raise SystemExit(2)
assert r['phase']=='cancel'
if sys.argv[1]=='lost_cancel_reply' and p.read_text().count('cancel')==1:raise SystemExit(2)
print(json.dumps({'cancelled':True}))
''')
    policy={**old.policy,'directory':str(tmp_path/'cancel-authority'),
            'admission_command':[str(Path(sys.executable).resolve()),str(adapter),failure]}
    authority=protected.Authority(policy,create=True)
    work=public/'work';work.mkdir()
    monkeypatch.setattr(dispatch,'_default_spawn',lambda *a,**kw:pytest.fail('rejected preparation launched a worker'))
    original_project=cancellation.project
    if failure=='lost_projection':
        monkeypatch.setattr(cancellation,'project',lambda *a:(_ for _ in ()).throw(RuntimeError('lost local projection')))
    with protected.using(authority),kbc.connect_closing() as conn:
        task_id=kb.create_task(conn,title='rejected admission',assignee='fixture',
                              workspace_kind='dir',workspace_path=str(work),max_runtime_seconds=60)
        dispatch.dispatch_once(conn,failure_limit=5)
        run=kb.list_runs(conn,task_id)[-1]
        original=authority.read(conn,run.id)
        assert original['activation_revoked'] is True
        assert not scopes.settled(conn,run.id)  # Never fabricate a child-cleanup receipt.
        assert not original.get('children_reaped') and not original.get('worker_pid')
        if failure=='lost_cancel_reply':
            assert authority.pending(conn)
            assert kb.get_task(conn,task_id).status=='running'
        monkeypatch.setattr(cancellation,'project',original_project)
        admission.reconcile(conn)
        ownership.reconcile(conn,task_id)
        assert not authority.pending(conn) and not ownership.pending(conn,task_id)
        assert kb.get_task(conn,task_id).status=='blocked'
        assert kb.get_task(conn,task_id).consecutive_failures==1
        assert kb.list_runs(conn,task_id)[-1].ended_at is not None
        with pytest.raises(RuntimeError):
            scopes.activate(conn,task_id,run.id,run.claim_lock,original['id'],os.getpid(),'late-supervisor')
        kb.gc_events(conn, older_than_seconds=-1)
        dispatch.dispatch_once(conn,failure_limit=5)
        assert len(kb.list_runs(conn,task_id))==1
        assert kb.get_task(conn,task_id).consecutive_failures==1


@pytest.mark.linux_only
def test_active_execution_cannot_take_prelaunch_path(protected_board,tmp_path):
    home,public,old,worker=protected_board
    policy={**old.policy,'directory':str(tmp_path/'other-authority'),
            'admission_command':[str(Path('/usr/bin/false').resolve())]}
    authority=protected.Authority(policy,create=True)
    with protected.using(authority),kbc.connect_closing() as conn:
        task_id=kb.create_task(conn,title='active owner',assignee='fixture',max_runtime_seconds=60)
        task=kb.claim_task(conn,task_id,claimer='original')
        ownership.begin(conn,task);scope=scopes.prepare(conn,task)
        scope=scopes.activate(conn,task_id,task.current_run_id,task.claim_lock,scope['id'],os.getpid(),'original-supervisor')
        assert cancellation.cancel(conn,task.current_run_id) is False
        assert authority.read(conn,task.current_run_id)==scope and authority.pending(conn)
        forged={**scope,'state':'cancelled','activation_revoked':True,'admission_cancellation_confirmed':True}
        forged.pop('supervisor_pid');forged.pop('supervisor_fingerprint')
        conn.execute("UPDATE task_runs SET execution_scope=?,spawn_state='settled' WHERE id=?",
                     (json.dumps(forged),task.current_run_id));conn.commit()
        assert ownership.pending(conn,task_id)  # A worker-writable cancellation cannot release the private owner.
