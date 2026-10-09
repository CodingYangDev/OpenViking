# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import json

import pytest

from openviking.message import Message, TextPart
from openviking.session.train import Case, ExecutionContext, Rollout, Rubric, RubricEvaluation
from openviking.session.train.components import rollout_artifact_recorder as artifacts
from openviking.session.train.components.dataset_service import rollout_from_dict, rollout_to_dict


@pytest.mark.parametrize("capture", [False, True])
def test_recorder_persists_initial_request_once_without_reconstructing_missing_capture(
    tmp_path, monkeypatch, capture
):
    request = {
        "capture_boundary": "agent_provider",
        "messages": [{"role": "system", "content": "### Skill: advice_loader"}],
        "tools": [{"type": "function", "function": {"name": "ask_for_advice"}}],
    }
    rollout = Rollout(
        case=Case("case", "signature", {}, Rubric("reward", "", [])),
        messages=[Message(id="user", role="user", parts=[TextPart(text="hello")])],
        policy_snapshot_id="snapshot",
        evaluation=RubricEvaluation(passed=True, score=1.0, criterion_results=[], feedback=[]),
        metadata={"system_prompt": "business policy only", "train_rollout_cache_hit": True},
    )
    if capture:
        rollout.metadata["initial_llm_request"] = request
    # The capture must survive the remote execution response/cache serialization.
    rollout = rollout_from_dict(rollout_to_dict(rollout))
    recorder = artifacts.RolloutArtifactRecorder(run_dir=tmp_path)
    context = ExecutionContext(
        policy_snapshot_id="snapshot", metadata={"training": True, "epoch": 0}
    )
    writes = []
    original = artifacts._write_json

    def record_write(path, value):
        if path.name == "initial_llm_request.json":
            writes.append(path)
        original(path, value)

    monkeypatch.setattr(artifacts, "_write_json", record_write)
    for _ in range(2):
        recorder.record_rollout_completion(rollout=rollout, index=0, context=context)

    files = list(tmp_path.rglob("initial_llm_request.json"))
    assert len(files) == int(capture)
    assert len(writes) == int(capture)
    if capture:
        assert json.loads(files[0].read_text()) == request
        # Dedicated artifact only: do not duplicate full requests in rollout.json or training input.
        assert (
            "initial_llm_request"
            not in json.loads((files[0].parent / "rollout.json").read_text())["metadata"]
        )
        assert (
            "### Skill: advice_loader" not in (files[0].parent / "commit_messages.json").read_text()
        )


def test_tau2_request_capture_failure_does_not_leak_input_or_change_execution(monkeypatch):
    import benchmark.tau2.train.rollout_executor_vikingbot as module

    def fail_redaction(_value):
        raise ValueError("secret input must not be persisted")

    monkeypatch.setattr(module, "redact_sensitive", fail_redaction)
    output = {}
    module._capture_initial_llm_request(output, [{"content": "secret input"}], [])
    assert output == {"capture_error": "ValueError"}


def test_capture_redacts_credentials_and_inline_images_without_mutating_request():
    import copy

    import benchmark.tau2.train.rollout_executor_vikingbot as module

    messages = [
        {
            "role": "system",
            "content": "### Skill: advice_loader\n" + "Full task rules. " * 1000,
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": 'credentials: {"api_key": "private-value"}'},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,cHJpdmF0ZQ=="}},
            ],
        },
    ]
    tools = [
        {
            "function": {
                "name": "ask_for_advice",
                "description": "Authorization: Bearer private-value",
            }
        }
    ]
    original = copy.deepcopy({"messages": messages, "tools": tools})
    output = {}
    module._capture_initial_llm_request(output, messages, tools)

    assert {"messages": messages, "tools": tools} == original
    assert output["messages"][0] == messages[0]
    assert output["capture_boundary"] == "agent_provider"
    assert "private-value" not in json.dumps(output)
    assert "cHJpdmF0ZQ==" not in json.dumps(output)
    messages[0]["content"] = "mutated"
    tools[0]["function"]["name"] = "mutated"
    assert output["messages"][0] == original["messages"][0]
    assert output["tools"][0]["function"]["name"] == "ask_for_advice"


def test_optional_initial_request_file_failure_does_not_abort_recording(tmp_path, monkeypatch):
    recorder = artifacts.RolloutArtifactRecorder(run_dir=tmp_path)
    rollout = Rollout(
        case=Case("case", "signature", {}, Rubric("reward", "", [])),
        messages=[],
        policy_snapshot_id="snapshot",
        evaluation=RubricEvaluation(passed=True, score=1.0, criterion_results=[], feedback=[]),
        metadata={"initial_llm_request": {"messages": [], "tools": []}},
    )
    original = artifacts._write_json

    def fail_optional_write(path, value):
        if path.name == "initial_llm_request.json":
            raise OSError("secret exception details")
        original(path, value)

    monkeypatch.setattr(artifacts, "_write_json", fail_optional_write)
    warnings = []
    monkeypatch.setattr(artifacts.logger, "warning", lambda *args: warnings.append(args))
    recorder.record_rollout_completion(
        rollout=rollout,
        index=0,
        context=ExecutionContext(policy_snapshot_id="snapshot", metadata={"training": True}),
    )
    assert list(tmp_path.rglob("rollout.json"))
    assert (tmp_path / "rollouts_index.json").exists()
    assert len(warnings) == 1
    assert "secret exception details" not in str(warnings)
