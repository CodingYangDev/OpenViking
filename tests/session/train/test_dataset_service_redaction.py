from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from openviking.session.train import Case, CriterionResult, Rollout, Rubric, RubricEvaluation
from openviking.session.train.components import dataset_service
from openviking.session.train.components.dataset_service import (
    RolloutExecuteRequest,
    RolloutExecutionStore,
    case_to_dict,
    execution_to_dict,
    redact_sensitive,
    rollout_from_dict,
    rollout_to_dict,
)


def test_redact_sensitive_recursively_masks_openviking_api_key():
    payload = {
        "policy_set": {
            "metadata": {
                "openviking_api_key": "secret-key",
                "openviking_url": "http://127.0.0.1:1933",
                "nested": [{"api_key": "other-secret"}],
            }
        },
        "case": "keep-me",
    }

    redacted = redact_sensitive(payload)

    assert redacted["policy_set"]["metadata"]["openviking_api_key"] == "<redacted>"
    assert redacted["policy_set"]["metadata"]["nested"][0]["api_key"] == "<redacted>"
    assert redacted["policy_set"]["metadata"]["openviking_url"] == "http://127.0.0.1:1933"
    assert payload["policy_set"]["metadata"]["openviking_api_key"] == "secret-key"


def test_redact_sensitive_masks_key_value_pairs_in_strings():
    text = "failed with openviking_api_key='secret-key', api_key=other-secret token: bearer"

    redacted = redact_sensitive(text)

    assert "secret-key" not in redacted
    assert "other-secret" not in redacted
    assert "bearer" not in redacted
    assert "openviking_api_key='<redacted>'" in redacted
    assert "api_key=<redacted>" in redacted
    assert "token: <redacted>" in redacted


def test_rollout_round_trip_keeps_nested_dag_runtime_events():
    rollout = Rollout(
        case=Case(
            name="case",
            task_signature="task",
            input={},
            rubric=Rubric(name="rubric", description="", criteria=[]),
        ),
        messages=[],
        policy_snapshot_id="snapshot",
        evaluation=RubricEvaluation(
            passed=True,
            score=1.0,
            criterion_results=[
                CriterionResult(
                    criterion_name="done", passed=True, score=1.0, feedback=[], evidence=[]
                )
            ],
            feedback=[],
        ),
        metadata={
            "dag_runtime": {
                "session_id": "tau2_dag_1",
                "events": [{"revision": 2, "actions": [{"node_id": 3}]}],
            }
        },
    )

    restored = rollout_from_dict(rollout_to_dict(rollout))

    assert restored.metadata == rollout.metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("with_semaphore", [False, True])
@pytest.mark.parametrize(
    ("exception_type", "message", "expected_error"),
    [
        (TimeoutError, "", "TimeoutError: "),
        (RuntimeError, "unavailable", "RuntimeError: unavailable"),
        (RuntimeError, "api_key=secret-key", "RuntimeError: api_key=<redacted>"),
    ],
)
async def test_rollout_execution_retains_exception_type_and_redacts_credentials(
    tmp_path, monkeypatch, with_semaphore, exception_type, message, expected_error
):
    store = RolloutExecutionStore(spool_dir=tmp_path)
    case = Case(
        name="case-1",
        task_signature="task",
        input={},
        rubric=Rubric(name="rubric", description="", criteria=[]),
    )
    execution = await store.create(case_name=case.name)
    app = FastAPI()
    app.state.rollout_executions = store
    app.state.rollout_semaphore = asyncio.Semaphore(1) if with_semaphore else None
    executor = AsyncMock(side_effect=exception_type(message))
    monkeypatch.setattr(dataset_service, "_execute_rollout_request_hosted", executor)
    request = RolloutExecuteRequest(case=case_to_dict(case), policy_set={}, execution_context={})

    app.state.rollout_tasks = {}
    app.state.rollout_controls = {}
    control = dataset_service._RolloutControl(time.monotonic() + 60)
    await dataset_service._run_rollout_execution(app, execution.execution_id, request, control)

    failed = await store.get(execution.execution_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.error == expected_error
    assert failed.execution_id == execution.execution_id
    assert failed.case_name == case.name
    assert failed.created_at == execution.created_at
    assert failed.finished_at is not None
    assert execution_to_dict(failed)["error"] == expected_error
    persisted = json.loads((tmp_path / "failed" / f"{execution.execution_id}.json").read_text())
    assert persisted["error"] == expected_error
    assert "secret-key" not in json.dumps(persisted)
    assert await store.count_by_status() == {"running": 0, "completed": 0, "failed": 1}
    executor.assert_awaited_once()


@pytest.mark.parametrize(
    "text",
    [
        '{"api_key": "private-value"}',
        "Authorization: Bearer private-value",
        "Authorization: Basic private-value",
        "password='private-value'",
        "access_token=private-value",
        "Cookie: session=private-value",
        "use sk-private-value-123456",
    ],
)
def test_redact_sensitive_covers_credentials_in_model_prompt(text):
    assert "private-value" not in redact_sensitive(text)


def test_redact_sensitive_preserves_full_prompt_and_returns_independent_copy():
    prompt = "Task requirements and advice_loader instructions. " * 1000
    payload = {"messages": [{"role": "system", "content": prompt}], "tools": []}
    redacted = redact_sensitive(payload)
    assert redacted == payload
    payload["messages"][0]["content"] = "later mutation"
    assert redacted["messages"][0]["content"] == prompt


def test_prompt_redaction_does_not_mistake_basic_economy_for_basic_auth():
    text = "Basic economy cannot be modified. Query basic flight information first."
    assert redact_sensitive(text) == text
