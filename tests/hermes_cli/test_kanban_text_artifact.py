"""Completion cannot release dependants on an attachment-only adviser handoff."""
import copy
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


def contract(**changes):
    value = {"metadata_key": "advice_artifact", "assignee": "architecture",
        "identity": {"schema_version": 1, "repository": "owner/repo", "item_number": 70,
            "role": "architecture", "source_revision_key": "a" * 64, "snapshot_revision": "b" * 40},
        "max_characters": 4000, "evidence_source": "hermes_completion_metadata"}
    value.update(changes)
    return "text-artifact-v1:" + json.dumps(value, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("transport", ["metadata", "metadata_json"])
def test_tool_handoff_rejects_incomplete_or_mismatched_artifacts_then_releases_exact_text(tmp_path, monkeypatch, transport):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_PROFILE", "architecture")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from tools import kanban_tools  # register the actual tool handlers
    from tools.registry import registry

    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="Advice", assignee="architecture", completion_contract=contract())
        child = kb.create_task(conn, title="Planner", assignee="planner", parents=[tid])
        assert kb.claim_task(conn, tid)
        run_id = kb.get_task(conn, tid).current_run_id
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        artifact = {**json.loads(contract().split(":", 1)[1])["identity"], "task_id": tid,
            "complete": True, "truncated": False, "text": "é" * 3999 + "終"}
        attachment = tmp_path / "advice_final.md"
        attachment.write_text(artifact["text"])
        invalid = [None, {}, {"advice_artifact": {**artifact, "task_id": "t_00000000"}},
            {"advice_artifact": {**artifact, "source_revision_key": "c" * 64}},
            {"advice_artifact": {**artifact, "schema_version": True}},
            {"advice_artifact": {**artifact, "text": "x" * 4001}},
            {"advice_artifact": {**artifact, "text": "Short summary"}},
            {"advice_artifact": {**artifact, "text": " "}},
            {"advice_artifact": {**artifact, "complete": False}},
            {"advice_artifact": {**artifact, "truncated": True}},
            {"advice_artifact": {**artifact, "evidence_source": "invented"}}]
        for metadata in invalid:
            result = json.loads(registry.dispatch("kanban_complete", {
                "summary": "Short summary", transport: (json.dumps(metadata) if transport == "metadata_json" else copy.deepcopy(metadata)), "artifacts": [str(attachment)]}))
            expected = "metadata_json must encode an object" if transport == "metadata_json" and metadata is None else "Completion requires metadata.advice_artifact"
            assert expected in result["error"]
            task = kb.get_task(conn, tid)
            assert task.status == "running" and task.current_run_id == run_id
            assert kb.latest_run(conn, tid).ended_at is None
            assert kb.get_task(conn, child).status == "todo"
            assert attachment.read_text() == artifact["text"]
        result = json.loads(registry.dispatch("kanban_complete", {
            "summary": "Short summary", transport: (json.dumps({"advice_artifact": artifact}) if transport == "metadata_json" else {"advice_artifact": artifact})}))
        assert result["ok"]
        assert kb.get_task(conn, tid).status == "done"
        assert kb.get_task(conn, child).status == "ready"
        run = kb.latest_run(conn, tid)
        assert run.id == run_id and run.metadata["advice_artifact"] == artifact


def test_contract_cannot_be_substituted_or_bypassed_by_direct_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="Advice", assignee="architecture",
            idempotency_key="stable", completion_contract=contract())
        assert kb.create_task(conn, title="Advice", assignee="architecture",
            idempotency_key="stable", completion_contract=contract()) == tid
        for changed in [None, "local-only", contract(max_characters=5000)]:
            with pytest.raises(ValueError, match="different text artifact"):
                kb.create_task(conn, title="Advice", idempotency_key="stable", completion_contract=changed)
        for bad in [contract(max_characters=True), contract(identity={"task_id": "other"}),
                    "text-artifact-v1:{", contract(metadata_key=""), contract(identity={})]:
            with pytest.raises(ValueError):
                kb.create_task(conn, title="Malformed", completion_contract=bad)
        with pytest.raises(ValueError, match="active run is required"):
            kb.complete_task(conn, tid, summary="Implicit final", force=True)
        artifact = {**json.loads(contract().split(":", 1)[1])["identity"], "task_id": tid,
            "complete": True, "truncated": False, "text": "Full advice including a finding."}
        with pytest.raises(ValueError, match="active run is required"):
            kb.complete_task(conn, tid, summary="Summary", metadata={"advice_artifact": artifact}, force=True)
        assert kb.assign_task(conn, tid, "another-profile")
        with pytest.raises(ValueError, match="assignee differs"):
            kb.complete_task(conn, tid, summary="Summary", metadata={"advice_artifact": artifact})
        assert kb.get_task(conn, tid).status != "done"
        assert not kb.list_runs(conn, tid)
        legacy = kb.create_task(conn, title="Generic task", assignee="architecture")
        assert kb.complete_task(conn, legacy, summary="Legacy completion remains supported")


def test_model_wire_schema_is_task_bound_stable_and_enforces_complete_handoff(tmp_path, monkeypatch):
    import httpx
    import re
    from openai import OpenAI
    from jsonschema import Draft202012Validator
    from model_tools import get_tool_definitions
    from tools.registry import registry

    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "architecture")
    held = []
    for name, task_key, bound in (("a", "first", 30), ("a", "second", 40),
                                  ("b", "first", 40), ("a", "first", 30)):
        home = tmp_path / name
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
        monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
        kb.init_db()
        with connect() as conn:
            tid = kb.create_task(conn, title="Advice", assignee="architecture",
                idempotency_key=task_key, completion_contract=contract(max_characters=bound))
            if len(held) < 3:
                assert kb.claim_task(conn, tid)
            monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
            definitions = get_tool_definitions(enabled_toolsets=["kanban"], quiet_mode=True)
            wire = []
            def capture(request):
                wire.append(json.loads(request.content))
                return httpx.Response(200, json={"id":"fixture", "object":"chat.completion",
                    "created":0, "model":"fixture", "choices":[{"index":0,
                    "message":{"role":"assistant", "content":"Done"}, "finish_reason":"stop"}]})
            with OpenAI(api_key="synthetic", http_client=httpx.Client(transport=httpx.MockTransport(capture))) as client:
                client.chat.completions.create(model="fixture", messages=[{"role":"user","content":"Complete"}], tools=definitions)
            schema = next(t["function"]["parameters"] for t in wire[0]["tools"] if t["function"]["name"] == "kanban_complete")
            held.append((tid, copy.deepcopy(schema), definitions))
            if len(held) == 4:
                assert schema == held[0][1]
                assert held[0][2] == definitions
                assert schema != held[1][1] and schema != held[2][1]
                break
            validator = Draft202012Validator(schema)
            artifact = {**json.loads(contract().split(":",1)[1])["identity"], "task_id":tid,
                "complete":True,"truncated":False,"evidence_source":"hermes_completion_metadata",
                "text":"Complete advice.\nUnicode: café"}
            valid = {"task_id":tid,"summary":"Advice ready","metadata":{"advice_artifact":artifact}}
            invalid = [{}, {"summary":"Ready"}, {**valid,"summary":" "}, {**valid,"metadata":{}},
                {**valid,"metadata":{"advice_artifact":{}}}]
            for key, value in (("task_id","wrong"),("source_revision_key","wrong"),
                               ("text","x"*41),("text"," "),("truncated",True)):
                invalid.append({**valid,"metadata":{"advice_artifact":{**artifact,key:value}}})
            for args in invalid:
                assert list(validator.iter_errors(args)), args
                assert "error" in json.loads(registry.dispatch("kanban_complete",args))
                assert kb.get_task(conn, tid).status == "running"
            # Some constrained decoders compile JSON Schema patterns as whole-string
            # grammars. Both interpretations must allow the entire multiline prose.
            for field, text in [(schema["properties"]["summary"], valid["summary"]),
                                (schema["properties"]["metadata"]["properties"]["advice_artifact"]["properties"]["text"], artifact["text"])]:
                assert re.fullmatch(field["pattern"], text), text
            validator.validate(valid)
            assert json.loads(registry.dispatch("kanban_complete",valid))["ok"]
            assert kb.latest_run(conn, tid).metadata["advice_artifact"] == artifact


def test_generic_completion_schema_accepts_full_prose_without_blank_handoffs(tmp_path, monkeypatch):
    import re
    from jsonschema import Draft202012Validator
    from tools.kanban_tools_schemas import KANBAN_COMPLETE_SCHEMA
    from tools.registry import registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    kb.init_db()
    with connect() as conn:
        for field in ("summary", "result"):
            tid = kb.create_task(conn, title="Ordinary completion", assignee="architecture")
            assert kb.claim_task(conn, tid)
            monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
            schema = KANBAN_COMPLETE_SCHEMA["parameters"]
            validator = Draft202012Validator(schema)
            prose = "Completed the requested task.\nVerified Unicode: café 終.\n"
            args = {"task_id": tid, field: prose}
            pattern = next(option["properties"][field]["pattern"] for option in schema["anyOf"]
                           if field in option["properties"])
            assert re.fullmatch(pattern, prose)
            validator.validate(args)
            for blank in ("", " ", "\n\t"):
                assert list(validator.iter_errors({"task_id": tid, field: blank}))
            assert json.loads(registry.dispatch("kanban_complete", args))["ok"]
            assert kb.latest_run(conn, tid).summary == prose
