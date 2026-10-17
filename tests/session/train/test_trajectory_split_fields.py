from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
import yaml
from test_case_experience_pipeline import observe
from test_case_experience_pipeline import setup_observation as setup_observation
from test_gradient_estimator_component import analysis

from openviking.session.compressor_v3 import SessionCompressorV3
from openviking.session.memory.experience.experience_context import _trajectory_model_content
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["python", "json"])
@pytest.mark.parametrize("case_name", ["case_a", 'case_"quoted"', "case_{{literal}}"])
async def test_four_extracted_fields_reach_canonical_storage_without_model_identity(
    setup_observation, monkeypatch, output_format, case_name
):
    from types import SimpleNamespace

    fs, ctx, model, analyzer, context, policies = setup_observation
    monkeypatch.setattr(
        "openviking.session.memory.extract_loop.get_openviking_config",
        lambda: SimpleNamespace(
            memory=SimpleNamespace(link_enabled=False, extraction_output_format=output_format)
        ),
    )
    original_resolve = MemoryIsolationHandler.calculate_memory_uris
    routed = []

    def resolve_with_server_identity(self, memory_type_schema, operation, extract_context):
        if memory_type_schema.memory_type == "trajectories":
            assert operation.memory_fields["case_name"] == case_name
            assert operation.memory_fields["trajectory_name"]
            assert operation.memory_fields["outcome"] == "failure"
            routed.append(operation.memory_fields["trajectory_name"])
        return original_resolve(self, memory_type_schema, operation, extract_context)

    monkeypatch.setattr(
        MemoryIsolationHandler, "calculate_memory_uris", resolve_with_server_identity
    )
    run = analysis(name=case_name, passed=False)
    run.metadata["case_uri"] = ""
    rules = '  Confirm "yes" before updates. {{ literal }}\n\nException: duplicates qualify.\n'
    steps = "1. `user` Annulez, s’il vous plaît.\n2. `tool:lookup` ID A-123, amount 50.00.\n"
    assessments = [{"id": "extra_check", "passed": False, "evidence": "The check was missed."}]
    fields = {
        "context_rules": rules,
        "steps": steps,
        "evaluation_criteria": yaml.safe_dump(assessments),
        "action_scores": "[]",
    }
    if output_format == "python":
        reply = (
            "sdk.create_trajectories("
            + ", ".join(f"{key}={value!r}" for key, value in fields.items())
            + ")\nsdk.commit()"
        )
    else:
        reply = json.dumps({"trajectories": [{"page_id": 100, **fields}]})
    model.get_completion_async = AsyncMock(return_value=reply)

    await observe(analyzer, run, policies, context)

    model.get_completion_async.assert_awaited_once()
    assert routed
    memory = MemoryFileUtils.read(fs.files[run.trajectories[0].uri], uri=run.trajectories[0].uri)
    assert "{{ case_name }}" not in memory.uri and "{{ trajectory_name }}" not in memory.uri
    assert memory.extra_fields["case_name"] == case_name
    assert memory.extra_fields["trajectory_name"] == run.trajectories[0].name
    assert memory.extra_fields["outcome"] == "failure"
    assert memory.extra_fields["trajectory_format"] == "ordered_markdown_v3"
    assert memory.extra_fields["evaluation"] == {"passed": False, "score": 0.0}
    assert memory.extra_fields["experience_update"]["state"] == "pending"
    assert memory.extra_fields["context_rules"] == rules
    assert memory.extra_fields["steps"] == steps
    assert "content" not in memory.extra_fields
    header, body = memory.content.split("\n```\n\n", 1)
    assert body == f"# Session Context\n\n{rules}\n\n# Trajectory\n\n{steps}".rstrip()
    record = yaml.safe_load(header.split("```yaml\n", 1)[1])
    assert record["case_name"] == case_name
    assert record["evaluation_criteria"] == assessments
    assert "context_rules" not in record and "steps" not in record
    assert memory.content.count("# Session Context") == memory.content.count("# Trajectory\n") == 1
    model_view = _trajectory_model_content(memory.content)
    assert model_view.count(rules) == 1 and model_view.count(steps.rstrip()) == 1

    # Phase 2 state updates must preserve both raw fields and the frozen rendered body.
    compressor = SessionCompressorV3(vikingdb=None, rollout_analyzer=analyzer)
    updated = await compressor._write_trajectory_update_state(
        viking_fs=fs, memory=memory, ctx=ctx, state="applied", batch_id="batch"
    )
    assert updated.content == memory.content
    assert updated.extra_fields["context_rules"] == rules
    assert updated.extra_fields["steps"] == steps
    assert updated.extra_fields["experience_update"]["state"] == "applied"
    model.get_completion_async.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rules,steps", [("", ""), ("Rule", "unnumbered text"), ("", "1. Only a step.")]
)
async def test_split_text_does_not_add_nonempty_or_markdown_validation(
    setup_observation, rules, steps
):
    from test_case_experience_pipeline import trajectory_program

    fs, _ctx, model, analyzer, context, policies = setup_observation
    model.get_completion_async.return_value = trajectory_program(steps, context_rules=rules)
    run = analysis(passed=True)
    await observe(analyzer, run, policies, context)
    memory = MemoryFileUtils.read(fs.files[run.trajectories[0].uri])
    assert memory.extra_fields["context_rules"] == rules
    assert memory.extra_fields["steps"] == steps
    model.get_completion_async.assert_awaited_once()
