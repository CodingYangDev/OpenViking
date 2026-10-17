# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.session.train import batch_runner as runner
from openviking.session.train.batch_runner import (
    BatchTrainEvalConfig,
    _baseline_cache_key,
    _case_loader,
)


def test_case_loader_uses_multi_value_train_and_eval_index_filters():
    config = BatchTrainEvalConfig(
        dataset="tau2",
        domain="airline",
        train_index=[1, 5, 5, 6],
        eval_index="10,14,18",
        benchmark_service_url="http://127.0.0.1:1944",
    )

    train_loader = _case_loader(
        config,
        split="train",
        sample_index=config.train_index,
    )
    eval_loader = _case_loader(
        config,
        split="train",
        sample_index=config.eval_index,
    )

    assert config.train_index == [1, 5, 6]
    assert config.eval_index == [10, 14, 18]
    assert train_loader.filters == {"task_indices": [1, 5, 6]}
    assert eval_loader.filters == {"task_indices": [10, 14, 18]}


def test_baseline_cache_key_depends_on_multi_eval_index():
    base = BatchTrainEvalConfig(
        dataset="tau2",
        domain="airline",
        eval_index=[1, 5, 6],
        trials=8,
        benchmark_service_url="http://127.0.0.1:1944",
    )

    assert _baseline_cache_key(base) == _baseline_cache_key(
        BatchTrainEvalConfig(
            dataset="tau2",
            domain="airline",
            eval_index="1,5,6",
            trials=8,
            benchmark_service_url="http://127.0.0.1:1944",
        )
    )
    assert _baseline_cache_key(base) != _baseline_cache_key(
        BatchTrainEvalConfig(
            dataset="tau2",
            domain="airline",
            eval_index=[1, 5],
            trials=8,
            benchmark_service_url="http://127.0.0.1:1944",
        )
    )


def test_empty_index_filter_is_invalid():
    import pytest

    with pytest.raises(ValueError, match="train_index must not be empty"):
        BatchTrainEvalConfig(
            dataset="tau2",
            domain="airline",
            train_index="",
            benchmark_service_url="http://127.0.0.1:1944",
        )


def test_train_trials_defaults_to_one_and_validates_positive():
    import pytest

    config = BatchTrainEvalConfig(
        dataset="tau2",
        domain="airline",
        benchmark_service_url="http://127.0.0.1:1944",
    )

    assert config.train_trials == 1
    with pytest.raises(ValueError, match="train_trials must be > 0"):
        BatchTrainEvalConfig(
            dataset="tau2",
            domain="airline",
            train_trials=0,
            benchmark_service_url="http://127.0.0.1:1944",
        )


@pytest.mark.parametrize(
    "split,train_index,eval_index,expected",
    [
        ("train", [5, 6], None, [5, 6]),
        ("train", 0, None, [0]),
        ("train", None, None, None),
        ("train", [5, 6], [10], [10]),
        ("train", [5, 6], 0, [0]),
        ("test", [5, 6], None, None),
        ("test", [5, 6], [10], [10]),
        (None, [5, 6], None, None),
    ],
)
def test_effective_eval_index_inherits_only_for_train_split(
    split, train_index, eval_index, expected
):
    config = BatchTrainEvalConfig(
        dataset="tau2",
        domain="airline",
        train_index=train_index,
        eval_index=eval_index,
        eval_split=split,
        benchmark_service_url="http://test",
    )
    requested = config.eval_index
    actual = runner._effective_eval_index(config)
    assert actual == expected
    assert config.eval_index == requested
    if split is not None:
        loader = _case_loader(config, split=split, sample_index=actual)
        assert loader.filters == ({} if expected is None else {"task_indices": expected})


def test_train_subset_cache_keys_use_actual_evaluation_subset(tmp_path):
    def config(train_index=None, eval_index=None, eval_split="train"):
        return BatchTrainEvalConfig(
            dataset="tau2",
            domain="airline",
            train_index=train_index,
            eval_index=eval_index,
            eval_split=eval_split,
        )

    implicit = config(train_index=[5, 6])
    explicit = config(eval_index=[5, 6])
    assert _baseline_cache_key(implicit) == _baseline_cache_key(explicit)
    assert _baseline_cache_key(implicit) != _baseline_cache_key(config(train_index=[5]))
    assert _baseline_cache_key(implicit) != _baseline_cache_key(config())
    assert _baseline_cache_key(config([1], [10])) == _baseline_cache_key(config([2], [10]))
    assert _baseline_cache_key(config([1], eval_split="test")) == _baseline_cache_key(
        config([2], eval_split="test")
    )
    cache = tmp_path / "cache.json"
    runner._write_baseline_cache(cache, {"accuracy": 0.5}, config=implicit)
    payload = json.loads(cache.read_text())
    assert payload["eval_index"] == [5, 6]
    assert payload["cache_key"] == _baseline_cache_key(implicit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "split,explicit_index,expected_indices,expected_payload",
    [
        ("train", None, [5, 6], [5, 6]),
        ("train", 10, [10], 10),
        ("test", None, None, None),
        ("test", 10, [10], 10),
    ],
)
async def test_eval_loaders_cache_events_and_report_share_effective_indices(
    tmp_path, monkeypatch, split, explicit_index, expected_indices, expected_payload
):
    client = SimpleNamespace(
        initialize=AsyncMock(), close=AsyncMock(), _url="http://test", _api_key=None
    )
    seen = []
    real_case_loader = runner._case_loader

    def make_loader(config, *, split, sample_index):
        loader = real_case_loader(config, split=split, sample_index=sample_index)
        return SimpleNamespace(filters=loader.filters, split_exists=AsyncMock(return_value=True))

    async def evaluate(*, case_loader, policy_set, context):
        del policy_set
        seen.append((context.execution_metadata["rollout_stage"], case_loader.filters))
        return SimpleNamespace(
            metadata={"report": {"accuracy": 1.0, "case_count": 2, "passed_count": 2}},
            analyses=[],
        )

    async def train(*, case_loader, policy_set, context):
        assert case_loader.filters == {"task_indices": [5, 6]}
        seen.append(("epoch", context.eval_each_epoch_case_loader.filters))
        return SimpleNamespace(
            metadata={"train_reports": []},
            apply_result=SimpleNamespace(updated_policy_set=policy_set),
        )

    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_configure_openviking_config", lambda _: None)
    monkeypatch.setattr(runner, "_build_http_client", lambda _: client)
    monkeypatch.setattr(runner, "_git_metadata", lambda: {"capture_scope": "test"})
    monkeypatch.setattr(runner, "_case_loader", make_loader)
    monkeypatch.setattr(
        runner,
        "_build_pipeline",
        lambda *args, **kwargs: SimpleNamespace(train=train, eval=evaluate, rollout_executor=None),
    )
    monkeypatch.setattr(runner, "emit_run_summary", AsyncMock())
    config = BatchTrainEvalConfig(
        dataset="tau2",
        domain="airline",
        epochs=1,
        train_index=[5, 6],
        eval_index=explicit_index,
        eval_split=split,
        eval_each_epoch=True,
        clean_result=False,
        benchmark_service_url="http://test",
        output_path=str(tmp_path / "run/report.json"),
    )
    requested_index = config.eval_index

    result = await runner.run_batch_train_eval(config)

    expected_filters = {} if expected_indices is None else {"task_indices": expected_indices}
    assert seen == [
        (f"baseline_{split}_rollout", expected_filters),
        ("epoch", expected_filters),
        (f"final_{split}_rollout", expected_filters),
    ]
    assert result.eval_index == expected_payload
    saved = json.loads((tmp_path / "run/report.json").read_text())
    assert saved["eval_index"] == expected_payload
    metadata = json.loads((tmp_path / "run/run_metadata.json").read_text())
    assert metadata["parameters"]["eval_index"] == requested_index
    assert metadata["effective_eval_index"] == expected_payload
    events = [json.loads(line) for line in (tmp_path / "run/events.jsonl").read_text().splitlines()]
    start = next(event for event in events if event["event"] == "run_start")
    assert start["eval_index"] == explicit_index
    assert start["effective_eval_index"] == expected_payload
    finish = next(event for event in events if event["event"] == "run_result")
    assert finish["effective_eval_index"] == expected_payload
    cache = json.loads(runner._baseline_cache_path(config).read_text())
    assert cache["eval_index"] == expected_payload
    assert config.eval_index == requested_index
    client.close.assert_awaited_once()
