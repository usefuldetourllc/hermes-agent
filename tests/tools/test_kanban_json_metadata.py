"""JSON string handoffs preserve nested facts and reject ambiguous input."""
import json
import pytest
from tests.tools.test_kanban_tools import worker_env


@pytest.mark.parametrize('tool', ['kanban_complete', 'kanban_request_review'])
def test_json_metadata_keeps_nested_values_and_rejects_bad_input(worker_env,tool):
    from tools import kanban_tools
    from tools.registry import registry
    from hermes_cli import kanban_db as kb,kanban_db_connect as kbc
    metadata={'nested':{'text':'Full unicode advice 終', 'items':[1,False,{'source':'original'}]}}
    for extra in [dict(metadata_json='invalid'),dict(metadata_json='[]'),
                  dict(metadata_json=123),dict(metadata_json='null'),
                  dict(metadata_json=json.dumps(metadata),metadata={'conflict':True})]:
        assert 'error' in json.loads(registry.dispatch(tool,{'summary':'Summary',**extra}))
        with kbc.connect() as conn:assert kb.get_task(conn,worker_env).status=='running'
    result=json.loads(registry.dispatch(tool,{'summary':'Summary','metadata':{},'metadata_json':json.dumps(metadata)}))
    assert result['ok']
    with kbc.connect() as conn:
        assert kb.latest_run(conn,worker_env).metadata==metadata
