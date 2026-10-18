"""Selected workflows must not claim older cards on the same board."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_dispatch as dispatch
from pathlib import Path
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as connection:
        yield connection


@pytest.mark.parametrize('allowed', ['selected', 'empty', 'unset'])
def test_scope_filters_before_claim_and_preserves_board_order(conn, all_assignees_spawnable, allowed):
    foreign = kb.create_task(conn, title='unrelated older card', assignee='worker', priority=100)
    selected = kb.create_task(conn, title='selected card', assignee='worker', priority=0)
    calls = []
    kwargs = {} if allowed == 'unset' else {'allowed_task_ids': {selected} if allowed == 'selected' else set()}
    result = dispatch.dispatch_once(conn, max_spawn=1, max_in_progress=1,
        spawn_fn=lambda task, workspace: calls.append(task.id), **kwargs)
    expected = [foreign] if allowed == 'unset' else [selected] if allowed == 'selected' else []
    assert calls == expected
    assert [r[0] for r in result.spawned] == expected
    excluded = foreign if allowed != 'unset' else selected
    assert kb.get_task(conn, excluded).current_run_id is None
    assert kb.list_runs(conn, excluded) == []


def test_foreign_review_does_not_reserve_selected_ready_slot(conn, all_assignees_spawnable):
    foreign = kb.create_task(conn, title='foreign review', assignee='worker')
    # Set up a review-lane card without spawning a model.
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (foreign,));conn.commit()
    selected = kb.create_task(conn, title='selected ready', assignee='worker')
    result = dispatch.dispatch_once(conn, dry_run=True, max_spawn=1, allowed_task_ids={selected})
    assert [r[0] for r in result.spawned] == [selected]
    result = dispatch.dispatch_once(conn, dry_run=True, max_spawn=1, allowed_task_ids={foreign})
    assert [r[0] for r in result.spawned] == [foreign]


@pytest.mark.parametrize('value', ['task-string', [None], [1], ['']])
def test_invalid_scope_rejected_before_reconciliation(conn, monkeypatch, value):
    from hermes_cli import kanban_execution_admission as admission
    calls=[];monkeypatch.setattr(admission,'reconcile',lambda conn:calls.append(True))
    with pytest.raises(ValueError, match='allowed_task_ids'):
        dispatch.dispatch_once(conn, allowed_task_ids=value)
    assert calls == []


def test_empty_scope_still_reconciles_existing_ownership(conn, monkeypatch):
    from hermes_cli import kanban_execution_admission as admission
    calls=[];monkeypatch.setattr(admission,'reconcile',lambda conn:calls.append(True))
    result=dispatch.dispatch_once(conn, allowed_task_ids=set())
    assert calls == [True] and result.spawned == []
