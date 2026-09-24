"""Retirement closes private admission without rewriting execution history."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from tests.hermes_cli.test_kanban_protected_authority import protected_board
from hermes_cli import kanban_db as kb,kanban_db_connect as kbc
from hermes_cli import kanban_execution_authority as protected,kanban_execution_scope as scopes,kanban_spawn_ownership as ownership


@pytest.mark.linux_only
def test_retirement_replays_across_restart_and_refuses_new_preparation(protected_board):
    home,public,authority,worker=protected_board
    receipt=authority.retire('original-workflow')
    resumed=protected.Authority(authority.policy)
    assert resumed.retire('original-workflow')==receipt
    with pytest.raises(RuntimeError,match='original retirement'):
        resumed.retire('different-workflow')
    with protected.using(resumed),kbc.connect_closing() as conn:
        task_id=kb.create_task(conn,title='late old work',assignee='fixture',max_runtime_seconds=60)
        task=kb.claim_task(conn,task_id,claimer='late-dispatcher');ownership.begin(conn,task)
        with pytest.raises(RuntimeError,match='retired'):scopes.prepare(conn,task)
        assert not resumed.pending() and resumed.retire('original-workflow')==receipt


@pytest.mark.linux_only
def test_retirement_serializes_against_private_preparation(protected_board):
    home,public,authority,worker=protected_board
    with kbc.connect_closing() as conn:
        task_id=kb.create_task(conn,title='racing preparation',assignee='fixture',max_runtime_seconds=60)
        task=kb.claim_task(conn,task_id,claimer='original');ownership.begin(conn,task)
    barrier=Barrier(2)
    def attempt(retire):
        with protected.using(authority),kbc.connect_closing() as conn:
            barrier.wait(5)
            try:
                if retire:return ('retired',authority.retire('original-workflow'))
                return ('prepared',scopes.prepare(conn,task))
            except RuntimeError:return None
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(attempt,[False,True]))
    successful=[x for x in results if x]
    assert len(successful)==1
    if successful[0][0]=='prepared':
        with pytest.raises(RuntimeError,match='occupied'):authority.retire('original-workflow')
        assert authority.pending()
    else:assert not authority.pending()
