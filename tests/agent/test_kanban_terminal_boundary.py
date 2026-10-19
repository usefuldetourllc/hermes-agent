"""A durable worker handoff ends inference without becoming cleanup evidence."""
import json

import pytest

from tests.tools.test_kanban_tools import worker_env


@pytest.mark.parametrize('terminal', ['kanban_complete', 'kanban_block'])
def test_only_original_committed_terminal_ends_worker_inference(worker_env, monkeypatch, terminal):
    from agent.delegation_context import delegated_child_context
    from agent.kanban_stop import owned_terminal_response
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from tools import kanban_tools
    from tools.registry import registry
    with kbc.connect_closing() as conn:
        run=kb.latest_run(conn,worker_env)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID',str(run.id))
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK',run.claim_lock)
    assert owned_terminal_response() is None
    assert 'error' in json.loads(registry.dispatch(terminal,{}))
    assert owned_terminal_response() is None
    args={'summary':'Original recorded handoff.'} if terminal=='kanban_complete' else {'reason':'Original blocker.'}
    assert json.loads(registry.dispatch(terminal,args))['ok']
    response=owned_terminal_response()
    assert response
    with delegated_child_context():assert owned_terminal_response() is None
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK','foreign-claim')
    assert owned_terminal_response() is None
    monkeypatch.setenv('HERMES_KANBAN_CLAIM_LOCK',run.claim_lock)
    assert owned_terminal_response()==response


def test_terminal_handoff_on_last_iteration_is_not_budget_failure(monkeypatch):
    from tests.agent.test_turn_finalizer_iteration_limit_exit import _LimitAgent, _finalize
    from agent import turn_finalizer
    def forbidden(*a,**k):raise AssertionError('completed handoff entered budget failure path')
    monkeypatch.setenv('HERMES_KANBAN_TASK','owned-task')
    monkeypatch.setattr(turn_finalizer,'_record_kanban_budget_exhausted',forbidden)
    agent=_LimitAgent()
    result=_finalize(agent,final_response='Original recorded handoff.',exit_reason='kanban_terminal')
    assert result['completed'] and not result['failed']
    assert not agent._handle_max_iterations_called
