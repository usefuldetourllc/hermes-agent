"""Protected admission uses the real profile guard and control-home allowlist."""
import json
import os

import pytest

from tests.hermes_cli.test_kanban_protected_authority import protected_board
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch, profiles


@pytest.mark.linux_only
@pytest.mark.parametrize('allowlist,admitted', [
    (None, True), ([], False), (['other'], False), (['FIXTURE'], True),
])
def test_protected_admission_preserves_dispatch_allowlist(protected_board, allowlist, admitted):
    home, public, authority, worker = protected_board
    config = {'execution_authority': authority.policy}
    if allowlist is not None:
        config['dispatch_profiles'] = allowlist
    (home / 'config.yaml').write_text(json.dumps({'kanban': config}))
    assert not profiles.profile_exists('fixture')
    assert authority.worker_profile('fixture').is_dir()
    workspace = public / 'admission-workspace'
    workspace.mkdir()
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title='protected admission', assignee='fixture',
                                workspace_kind='dir', workspace_path=str(workspace))
        assert dispatch.has_spawnable_ready(conn) is admitted
        result = dispatch.dispatch_once(conn, max_in_progress=1, dry_run=True)
        assert any(row[0] == task_id for row in result.spawned) is admitted
        assert (task_id in result.skipped_nonspawnable) is not admitted


@pytest.mark.linux_only
def test_protected_admission_rejects_invalid_profiles_without_legacy_fallback(protected_board):
    home, public, authority, worker = protected_board
    legacy = home / 'profiles' / 'fixture'
    legacy.mkdir(parents=True)
    (legacy / 'config.yaml').write_text('{}')
    assert profiles.profile_exists('fixture')
    authority.worker_profile('fixture').chmod(0o755)
    # A matching ordinary profile cannot excuse unsafe protected ownership/mode.
    predicate = dispatch._profile_exists_fn()
    assert not predicate('fixture')
    assert not predicate('missing') and not predicate('default')
    escaped = authority.profiles_directory.parent / 'escaped'
    escaped.mkdir(mode=0o700)
    os.chown(escaped, worker.pw_uid, worker.pw_gid)
    assert not predicate('../escaped')
    assert not predicate(str(escaped))
