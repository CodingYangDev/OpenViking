# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from benchmark.tau2.train.rollout_executor_native import _agent_token_usage_from_simulation
from openviking.session.train.components.dataset_service import rollout_from_dict, rollout_to_dict
from openviking.session.train.components.event_recorder import (
    JsonlEventRecorder,
    JsonlPipelineEventHook,
)
from openviking.session.train.components.report_builder import PipelineReportBuilder
from openviking.session.train.components.reporter import ConsolePipelineReporter
from openviking.session.train.context import ExecutionContext, PipelineContext
from openviking.session.train.domain import (
    Case,
    ExperienceSet,
    PipelineEpochResult,
    PipelineEvaluationResult,
    PolicyApplyResult,
    PolicyUpdatePlan,
    Rollout,
    RolloutAnalysis,
    Rubric,
    RubricEvaluation,
)


def _rollout(total, *, trial=0, cached=False, failed=False):
    return Rollout(
        case=Case(
            name=f"case-{trial}-{total}",
            task_signature="airline",
            input={"eval_trial": trial},
            rubric=Rubric(name="reward", description="reward", criteria=[]),
        ),
        messages=[],
        policy_snapshot_id="snapshot",
        evaluation=RubricEvaluation(
            passed=not failed, score=float(not failed), criterion_results=[], feedback=[]
        ),
        metadata={
            "token_usage": {"total_tokens": total},
            "train_rollout_cache_hit": cached,
            "user_token_usage": {"total_tokens": 1_000_000},
            **({"execution_error": {"source": "agent", "type": "timeout"}} if failed else {}),
        },
    )


def _analyses(rollouts):
    return [RolloutAnalysis(evaluation=r.evaluation, metadata={"rollout": r}) for r in rollouts]


def _epoch(rollouts):
    return PipelineEpochResult(
        epoch=0,
        analyses=_analyses(rollouts),
        gradients=[],
        plan=PolicyUpdatePlan(),
        apply_result=PolicyApplyResult(updated_policy_set=ExperienceSet(root_uri="", policies=[])),
        policy_snapshot_ids=["snapshot"],
    )


def test_native_usage_counts_only_assistant_generations():
    simulation = SimpleNamespace(
        messages=[
            SimpleNamespace(role="user", usage={"prompt_tokens": 9000, "completion_tokens": 8000}),
            SimpleNamespace(
                role="assistant", usage={"prompt_tokens": 100, "completion_tokens": 20}
            ),
            SimpleNamespace(role="tool", usage={"prompt_tokens": 7000, "completion_tokens": 6000}),
            SimpleNamespace(
                role="assistant",
                usage={"prompt_tokens": 200, "completion_tokens": 40, "reasoning_tokens": 30},
            ),
        ]
    )
    assert _agent_token_usage_from_simulation(simulation) == {
        "prompt_tokens": 300,
        "completion_tokens": 60,
        "total_tokens": 360,  # Reasoning is already part of completion, not added twice.
    }


@pytest.mark.parametrize(
    "missing", [None, {}, {"prompt_tokens": 12}, {"prompt_tokens": -1, "completion_tokens": 2}]
)
def test_native_partial_usage_is_unknown_not_a_partial_total(missing):
    simulation = SimpleNamespace(
        messages=[
            SimpleNamespace(
                role="assistant", usage={"prompt_tokens": 100, "completion_tokens": 20}
            ),
            SimpleNamespace(role="assistant", usage=missing),
        ]
    )
    assert _agent_token_usage_from_simulation(simulation) is None


def test_native_no_agent_usage_is_unknown_but_explicit_zero_is_valid():
    assert _agent_token_usage_from_simulation(SimpleNamespace(messages=[])) is None
    assert _agent_token_usage_from_simulation(
        SimpleNamespace(
            messages=[
                SimpleNamespace(
                    role="assistant", usage={"prompt_tokens": 0, "completion_tokens": 0}
                )
            ]
        )
    ) == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_report_uses_service_totals_including_failed_and_cached_attempts():
    rollouts = [_rollout(300, cached=True), _rollout(900, failed=True), _rollout(None)]
    # The existing service wire contract must carry usage unchanged; no side channel.
    rollouts = [rollout_from_dict(json.loads(json.dumps(rollout_to_dict(r)))) for r in rollouts]
    report = PipelineReportBuilder().train_rollout_report(
        epoch=0, rollouts=rollouts, snapshot_id="snapshot"
    )
    assert report["token_usage"] == {
        "rollout_count": 3,
        "reported_rollout_count": 2,
        "total_tokens": 1200,
        "average_tokens_per_rollout": 600.0,
    }
    assert report["execution_error_count"] == 1
    assert report["cache_hit_count"] == 1
    assert report["case_count"] == 3


@pytest.mark.parametrize("invalid", [None, -1, True, 2.5, float("nan"), float("inf"), "100"])
def test_missing_or_invalid_total_is_not_counted_as_zero(invalid):
    report = PipelineReportBuilder().evaluation_summary_from_analyses(
        _analyses([_rollout(invalid)])
    )
    assert report["token_usage"] == {
        "rollout_count": 1,
        "reported_rollout_count": 0,
        "total_tokens": None,
        "average_tokens_per_rollout": None,
    }


def test_zero_usage_counts_towards_the_average():
    report = PipelineReportBuilder().evaluation_summary_from_analyses(
        _analyses([_rollout(0), _rollout(100), _rollout(None)])
    )
    assert report["token_usage"]["average_tokens_per_rollout"] == 50.0
    assert report["token_usage"]["reported_rollout_count"] == 2


def test_empty_or_absent_metadata_does_not_fabricate_usage():
    builder = PipelineReportBuilder()
    assert builder.evaluation_summary_from_analyses([])["token_usage"] == {
        "rollout_count": 0,
        "reported_rollout_count": 0,
        "total_tokens": None,
        "average_tokens_per_rollout": None,
    }
    rollout = _rollout(100)
    rollout.metadata.pop("token_usage")
    assert (
        builder.evaluation_summary_from_analyses(_analyses([rollout]))["token_usage"][
            "reported_rollout_count"
        ]
        == 0
    )


def test_trial_report_averages_individual_attempts_not_trial_group_means():
    rollouts = [
        _rollout(100, trial=0),
        _rollout(300, trial=0),
        _rollout(1000, trial=1),
        _rollout(None, trial=1),
    ]
    report = PipelineReportBuilder(trial_index_key="eval_trial").trial_evaluation_report(
        PipelineEvaluationResult(epoch=0, analyses=_analyses(rollouts)), trial_count=2
    )
    assert report["token_usage"]["average_tokens_per_rollout"] == pytest.approx(1400 / 3)
    assert report["token_usage"]["reported_rollout_count"] == 3
    assert report["token_usage"]["rollout_count"] == 4
    assert report["trials"][0]["token_usage"]["average_tokens_per_rollout"] == 200
    assert report["trials"][1]["token_usage"]["average_tokens_per_rollout"] == 1000
    assert report["trials"][1]["token_usage"]["reported_rollout_count"] == 1


def test_train_epoch_keeps_original_rollout_usage_after_learning_filters():
    builder = PipelineReportBuilder()
    rollout_report = builder.train_rollout_report(
        epoch=0, rollouts=[_rollout(100), _rollout(300, failed=True)], snapshot_id="snapshot"
    )
    epoch = _epoch([_rollout(100)])
    report = builder.train_epoch_report(epoch, rollout_report=rollout_report)
    assert report["token_usage"] == rollout_report["token_usage"]
    assert report["train_eval"]["token_usage"]["total_tokens"] == 100
    assert builder.train_epoch_report(epoch)["token_usage"]["total_tokens"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("trial_count", [1, 2])
async def test_pipeline_prints_and_records_service_usage(tmp_path, capsys, trial_count):
    builder = PipelineReportBuilder(trial_index_key="eval_trial")
    rollouts = [_rollout(300), _rollout(900, trial=trial_count - 1), _rollout(None)]
    report = builder.train_rollout_report(epoch=0, rollouts=rollouts, snapshot_id="snapshot")
    train = builder.train_epoch_report(_epoch(rollouts), rollout_report=report)
    evaluation = PipelineEvaluationResult(
        epoch=0, analyses=_analyses(rollouts), metadata={"rollout_stage": "eval_train_rollout"}
    )
    eval_report = (
        builder.trial_evaluation_report(evaluation, trial_count=trial_count)
        if trial_count > 1
        else builder.evaluation_report(evaluation)
    )
    context = PipelineContext(eval_each_epoch_case_loader=object())
    reporter = ConsolePipelineReporter(use_rich=False)
    path = tmp_path / "events.jsonl"
    hook = JsonlPipelineEventHook(recorder=JsonlEventRecorder(path=path))
    reporter.on_train_rollout_report(report=report, context=context)
    await hook.on_train_rollout_report(report=report, context=context)
    reporter.on_train_report(report=train, context=context)
    await hook.on_train_report(report=train, context=context)
    reporter.on_eval_report(label="eval_train_rollout", report=eval_report, context=context)
    await hook.on_eval_report(label="eval_train_rollout", report=eval_report, context=context)
    output = capsys.readouterr().out
    assert output.count("avg_tokens=600.0 (usage 2/3)") == 5  # Three stages + two epoch lines.
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["event"] for record in records] == [
        "train_rollout",
        "train_result",
        "eval_train_rollout",
    ]
    assert all(record["token_usage"] == report["token_usage"] for record in records)
    if trial_count > 1:
        assert records[-1]["trials"][1]["token_usage"]["average_tokens_per_rollout"] == 900
    reporter._report_eval_line("final", eval_report)
    final_output = capsys.readouterr().out
    assert "avg_tokens=600.0 (usage 2/3)" in final_output
    assert "(3/3)" in final_output
    assert "±" not in final_output and "trials=" not in final_output
    if trial_count > 1:
        assert "accuracy_std" in records[-1]
        assert records[-1]["trial_count"] == trial_count


def test_console_shows_unknown_usage_without_zero(capsys):
    report = PipelineReportBuilder().train_rollout_report(
        epoch=0, rollouts=[_rollout(None)], snapshot_id="snapshot"
    )
    ConsolePipelineReporter(use_rich=False).on_train_rollout_report(
        report=report, context=PipelineContext()
    )
    assert "avg_tokens=n/a (usage 0/1)" in capsys.readouterr().out


def test_native_executor_returns_agent_usage_in_existing_service_payload(monkeypatch):
    import tau2.run
    from tau2.registry import registry

    from benchmark.tau2.train import rollout_executor_native as native

    simulation = SimpleNamespace(
        reward_info=SimpleNamespace(reward=1.0, info={}),
        termination_reason="agent_stop",
        messages=[
            SimpleNamespace(
                role="user",
                content="request",
                usage={"prompt_tokens": 9000, "completion_tokens": 9000},
            ),
            SimpleNamespace(
                role="assistant",
                content="answer",
                usage={"prompt_tokens": 120, "completion_tokens": 30},
            ),
        ],
    )
    monkeypatch.setattr(native, "_ensure_tau2_llm_api_bases", lambda: None)
    monkeypatch.setattr(native, "_install_tau2_litellm_rate_limit_retry", lambda: None)
    monkeypatch.setattr(native, "_resolve_llm_runtime_config", lambda _: ("test", {}, "test", {}))
    monkeypatch.setattr(registry, "get_tasks_loader", lambda _: lambda: [SimpleNamespace(id="1")])
    monkeypatch.setattr(tau2.run, "run_task", lambda **kwargs: simulation)
    case = _rollout(0).case
    case.input.update(domain="airline", task_id="1", task_no=0, data_split="train")
    rollout = native.NativeTau2RolloutExecutor(memory_enabled=False)._execute_one_sync(
        case, ExecutionContext("snapshot"), 0
    )
    payload = rollout_to_dict(rollout)
    assert payload["metadata"]["token_usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }
    restored = rollout_from_dict(json.loads(json.dumps(payload)))
    report = PipelineReportBuilder().train_rollout_report(
        epoch=0, rollouts=[restored], snapshot_id="snapshot"
    )
    assert report["token_usage"]["average_tokens_per_rollout"] == 150


@pytest.mark.parametrize("trial_count", [1, 8])
@pytest.mark.parametrize("use_rich", [False, True])
def test_train_eval_summaries_share_compact_accuracy_format(capsys, trial_count, use_rich):
    import copy
    import re

    if use_rich:
        pytest.importorskip("rich")
    reporter = ConsolePipelineReporter(use_rich=use_rich)
    context = PipelineContext(eval_each_epoch_case_loader=object())
    report = {
        "epoch": 0,
        "case_count": 56,
        "passed_count": 34,
        "accuracy": 34 / 56,
        "accuracy_mean": 34 / 56,
        "accuracy_std": 0.125,
        "trial_count": trial_count,
        "case_count_per_trial": 56 // trial_count,
        "total_rollout_count": 56,
        "token_usage": {
            "rollout_count": 56,
            "reported_rollout_count": 56,
            "total_tokens": 1_031_548,
            "average_tokens_per_rollout": 18_420.5,
        },
    }
    before = copy.deepcopy(report)
    reporter.on_train_rollout_report(report=report, context=context)
    reporter.on_eval_report(label="eval_train_rollout", report=report, context=context)
    output = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    metric = "accuracy: 60.71% (34/56)  avg_tokens=18,420.5"
    assert f"TRAIN {metric}" in output
    assert f"EVAL  {metric}" in output
    assert "±" not in output and "trials=" not in output
    assert "cases_per_trial=" not in output and "total_rollouts=" not in output
    assert report == before  # Presentation-only: structured statistics stay intact.
