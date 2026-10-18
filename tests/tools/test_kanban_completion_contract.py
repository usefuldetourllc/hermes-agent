"""Model schema and actual completion dispatch agree on a usable handoff."""
import json

import pytest
from jsonschema import Draft202012Validator
from tests.tools.test_kanban_tools import worker_env


@pytest.mark.parametrize('field', ['summary', 'result'])
def test_missing_handoff_is_rejected_and_corrected_call_completes(worker_env, field):
    from tools import kanban_tools
    from tools.registry import registry
    from tools.kanban_tools_schemas import KANBAN_COMPLETE_SCHEMA
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    validate = Draft202012Validator(KANBAN_COMPLETE_SCHEMA['parameters'])
    for args in [{}, {'metadata': {}}, {'metadata_json': '{}'},
                 {'summary': '  ', 'result': '\n'}]:
        assert list(validate.iter_errors(args))
        assert 'error' in json.loads(registry.dispatch('kanban_complete', args))
        with kbc.connect() as conn:
            assert kb.get_task(conn, worker_env).status == 'running'
    corrected = {field: 'Complete advice with its original evidence.', 'metadata_json': '{"source":"original"}'}
    validate.validate(corrected)
    assert json.loads(registry.dispatch('kanban_complete', corrected))['ok']
    with kbc.connect() as conn:
        assert kb.latest_run(conn, worker_env).metadata == {'source': 'original'}
