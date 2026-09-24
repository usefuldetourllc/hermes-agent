"""A nonzero worker exit must respect the dispatcher's explicit failure limit."""

import subprocess
import sys

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.mark.linux_only
@pytest.mark.parametrize(
    "limit,override,attempts,expected",
    [(1, None, 1, "blocked"), (3, None, 2, "ready"),
     (3, None, 3, "blocked"), (1, 3, 1, "ready"), (3, 1, 1, "blocked")],
)
def test_dispatch_tick_accounts_real_worker_crash(
    tmp_path, monkeypatch, limit, override, attempts, expected
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kbc.init_db()
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="early worker failure", assignee="worker", max_retries=override
        )
        for _ in range(attempts):
            assert kb.claim_task(conn, task_id, claimer=kb._host_prefix() + "test")
            # Keep the real child alive until its identity has been recorded.
            with subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read(); raise SystemExit(2)"],
                stdin=subprocess.PIPE,
            ) as child:
                dispatch._set_worker_pid(conn, task_id, child.pid)
                child.communicate(timeout=10)
                assert child.returncode == 2
                dispatch._record_worker_exit(child.pid, child.returncode << 8)
            result = dispatch.dispatch_once(
                conn, failure_limit=limit, dry_run=True, reconcile_orphans=False,
            )
            assert task_id in result.crashed
        task = kb.get_task(conn, task_id)
        assert task.status == expected
        assert task.consecutive_failures == attempts
        if expected == "blocked":
            def unexpected_spawn(*args):
                pytest.fail("a blocked crash must not launch another worker")
            dispatch.dispatch_once(
                conn, failure_limit=limit, spawn_fn=unexpected_spawn,
                reconcile_orphans=False,
            )
        assert len(kb.list_runs(conn, task_id)) == attempts
