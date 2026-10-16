# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from test_content_gate import CTX, SOURCE, URI, Store, batch, file, operation, registry
from test_extraction_output_protocol import _bind, _context

from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.experience.feedback import (
    injected_experiences,
    record_experience_feedback_stats,
    validate_experience_effects,
)
from openviking.session.memory.extraction_output_protocol import create_extraction_output_protocol
from openviking.session.memory.memory_updater import MemoryUpdater
from openviking.session.memory.patch_merge_context_provider import _hide_system_fields
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils


def effects(label="positive", alias="E1"):
    result = {"positive_ids": [], "negative_ids": [], "weak_ids": []}
    if label != "neutral":
        result[f"{label}_ids"] = [alias]
    return result


def trajectory(name="t1", *, label="positive", sources=None):
    return MemoryFile(
        uri=f"viking://user/alice/memories/trajectories/case_a/{name}.md",
        memory_type="trajectories",
        extra_fields={
            "experience_effects": effects(label),
            "experience_effect_sources": {"E1": URI} if sources is None else sources,
        },
    )


@pytest.fixture
def fs(monkeypatch):
    # Keep the real writer, merge, validation and pathlock; omit unrelated index/overview work.
    monkeypatch.setattr(MemoryUpdater, "_vectorize_memories", AsyncMock(return_value=0))
    monkeypatch.setattr(MemoryUpdater, "generate_overview", AsyncMock())
    monkeypatch.setattr(MemoryUpdater, "_sync_resource_refs_for_result", AsyncMock())
    return Store({URI: MemoryFileUtils.write(file())})


async def record(fs, value):
    return await record_experience_feedback_stats(value, registry=registry(), viking_fs=fs, ctx=CTX)


def stats(fs):
    return MemoryFileUtils.read(fs.files[URI], uri=URI).extra_fields["feedback_stats"]


def test_aliases_require_actual_delivery_not_proposals_or_ack_values():
    execution = {
        URI: {
            "actions": [{"description": "do it"}],
            "executed_nodes": ["tell"],
            "slot_values": {"tell": True},
        },
        URI.replace("case_a", "case_b"): {
            "delivered_advice": [{"instruction": "check scope"}, {"instruction": "check scope"}]
        },
    }
    assert injected_experiences(execution) == [
        {
            "id": "E1",
            "experience_uri": URI.replace("case_a", "case_b"),
            "instructions": ["check scope"],
        }
    ]


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        [],
        {"positive_ids": []},
        {"positive_ids": ["E2"], "negative_ids": [], "weak_ids": []},
        {"positive_ids": ["E1", "E1"], "negative_ids": [], "weak_ids": []},
        {"positive_ids": ["E1"], "negative_ids": ["E1"], "weak_ids": []},
        {"positive_ids": "E1", "negative_ids": [], "weak_ids": []},
    ],
)
def test_invalid_model_labels_are_rejected(value):
    with pytest.raises(ValueError):
        validate_experience_effects(value, {"E1": URI})


def test_effects_can_be_unknown_without_implying_no_effect():
    assert validate_experience_effects(json.dumps(effects("neutral")), {"E1": URI}) == effects(
        "neutral"
    )


@pytest.mark.asyncio
async def test_statistics_use_normal_writer_and_are_idempotent(fs, monkeypatch):
    calls = []
    original = MemoryUpdater.apply_operations

    async def apply(updater, operations, ctx, *args, **kwargs):
        assert updater._transaction_handle is not None and fs.lock.locked()
        calls.append(operations)
        return await original(updater, operations, ctx, *args, **kwargs)

    monkeypatch.setattr(MemoryUpdater, "apply_operations", apply)
    before = MemoryFileUtils.read(fs.files[URI], uri=URI)
    assert await record(fs, trajectory()) == {"updated_uris": [URI], "skipped_uris": []}
    persisted = fs.files[URI]
    assert await record(fs, trajectory()) == {"updated_uris": [], "skipped_uris": [URI]}
    after = MemoryFileUtils.read(fs.files[URI], uri=URI)
    assert fs.files[URI] == persisted
    assert len(calls) == 1
    assert set(calls[0].upsert_operations[0].memory_fields) == {"feedback_stats"}
    assert stats(fs)["injected_count"] == stats(fs)["positive_count"] == 1
    assert stats(fs)["observed_trajectories"] == [trajectory().uri]
    assert after.content == before.content
    assert after.extra_fields["experience_name"] == before.extra_fields["experience_name"]
    assert after.extra_fields["version"] == before.extra_fields["version"] + 1
    assert not fs.lock.locked()


@pytest.mark.asyncio
async def test_concurrent_new_and_duplicate_feedback_does_not_lose_counts(fs):
    labels = ("positive", "negative", "weak", "neutral")
    values = [trajectory(f"t{i}", label=labels[i % 4]) for i in range(20)]
    await asyncio.gather(*(record(fs, value) for value in values * 2))
    counts = stats(fs)
    assert counts["injected_count"] == 20
    assert all(counts[f"{label}_count"] == 5 for label in labels)
    assert len(counts["observed_trajectories"]) == 20
    assert not fs.lock.locked()


@pytest.mark.asyncio
async def test_effects_for_same_experience_are_counted_once(fs):
    value = trajectory(sources={"E1": URI, "E2": URI})
    value.extra_fields["experience_effects"] = {
        "positive_ids": ["E1"],
        "negative_ids": ["E2"],
        "weak_ids": [],
    }
    await record(fs, value)
    assert stats(fs)["injected_count"] == stats(fs)["negative_count"] == 1
    assert stats(fs)["positive_count"] == 0


@pytest.mark.asyncio
async def test_feedback_and_dag_update_preserve_each_other(fs):
    updater = MemoryUpdater(registry=registry())
    updater._viking_fs = fs
    result, _ = await asyncio.gather(
        updater.apply_operations(batch(operation(SOURCE.replace("old", "new"))), CTX),
        record(fs, trajectory()),
    )
    assert not result.errors
    assert "new reminder" in MemoryFileUtils.read(fs.files[URI], uri=URI).content
    assert stats(fs)["positive_count"] == 1


@pytest.mark.asyncio
async def test_write_failure_is_retryable_without_losing_or_duplicating_feedback(fs, monkeypatch):
    original = fs.write_file
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("disk unavailable")
        await original(*args, **kwargs)

    monkeypatch.setattr(fs, "write_file", fail_once)
    with pytest.raises(RuntimeError, match="feedback update failed"):
        await record(fs, trajectory())
    assert not fs.lock.locked()
    await record(fs, trajectory())
    await record(fs, trajectory())
    assert stats(fs)["injected_count"] == 1


@pytest.mark.asyncio
async def test_missing_experience_is_not_recreated_and_unattributed_history_is_not_backfilled(fs):
    fs.files.clear()
    assert (await record(fs, trajectory()))["skipped_uris"] == [URI]
    assert not fs.files
    assert await record(fs, trajectory(sources={})) == {"updated_uris": [], "skipped_uris": []}


@pytest.mark.asyncio
async def test_corrupt_counters_are_not_reset(fs):
    value = file()
    value.extra_fields["feedback_stats"] = {"injected_count": "bad"}
    fs.files[URI] = MemoryFileUtils.write(value)
    before = fs.files[URI]
    with pytest.raises(ValueError, match="feedback_stats"):
        await record(fs, trajectory())
    assert fs.files[URI] == before and not fs.lock.locked()


def test_system_metadata_is_not_declared_or_exposed_as_model_writable_fields():
    schema = registry().get("experiences")
    assert "feedback_stats" not in {field.name for field in schema.fields}
    value = file()
    value.extra_fields.update(
        feedback_stats={"private_marker": 123}, experience_effect_sources={"E1": URI}
    )
    context = _context([schema], files=[value])
    protocol = create_extraction_output_protocol("python")
    rendered = _bind(protocol, context)
    assert "feedback_stats" not in rendered and "private_marker" not in rendered
    assert "experience_effect_sources" not in rendered
    operations, error = protocol.parse(
        "experiences_1.update(feedback_stats='tampered', experience_effect_sources='tampered')\nsdk.commit()",
        context,
    )
    assert error is None
    assert "feedback_stats" not in json.dumps(operations.model_dump())
    assert _hide_system_fields({"feedback_stats": 123, "content": "keep"}) == {"content": "keep"}


def test_json_output_cannot_write_system_feedback_metadata():
    schema = registry().get("experiences")
    context = _context([schema])
    protocol = create_extraction_output_protocol("json")
    operations, error = protocol.parse(
        json.dumps(
            {
                "experiences": [
                    {
                        "page_id": 1,
                        "experience_name": "case_a",
                        "content": SOURCE,
                        "feedback_stats": {"injected_count": 999},
                        "experience_effect_sources": {"E1": URI},
                    }
                ],
                "delete_ids": [],
            }
        ),
        context,
    )
    assert error is None
    result = operations.model_dump()["experiences"][0]
    assert "feedback_stats" not in result and "experience_effect_sources" not in result


@pytest.mark.asyncio
async def test_non_experience_target_and_invalid_saved_sources_cannot_write(fs):
    for sources in [{"E1": "viking://user/alice/memories/records/a.md"}, []]:
        with pytest.raises(ValueError, match="sources"):
            await record(fs, trajectory(sources=sources))
    assert not fs.writes and not fs.lock.locked()


@pytest.mark.asyncio
async def test_lost_write_ack_does_not_double_count_on_retry(fs, monkeypatch):
    original = fs.write_file

    async def written_but_ack_lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("write acknowledgement lost")

    monkeypatch.setattr(fs, "write_file", written_but_ack_lost)
    with pytest.raises(RuntimeError, match="feedback update failed"):
        await record(fs, trajectory())
    persisted = fs.files[URI]
    monkeypatch.setattr(fs, "write_file", original)
    assert (await record(fs, trajectory()))["skipped_uris"] == [URI]
    assert fs.files[URI] == persisted and stats(fs)["injected_count"] == 1


@pytest.mark.asyncio
async def test_cancelled_write_releases_lock_and_can_retry(fs, monkeypatch):
    original = fs.write_file
    monkeypatch.setattr(fs, "write_file", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await record(fs, trajectory())
    assert not fs.lock.locked()
    monkeypatch.setattr(fs, "write_file", original)
    await record(fs, trajectory())
    assert stats(fs)["injected_count"] == 1


@pytest.mark.asyncio
async def test_policy_snapshot_does_not_overwrite_newer_feedback(fs):
    from openviking.session.train.components.policy_updater import MemoryFilePolicyUpdater
    from openviking.session.train.domain import Policy, PolicyPlanItem, PolicySet, PolicyUpdatePlan

    await record(fs, trajectory("first"))
    before = MemoryFileUtils.read(fs.files[URI], uri=URI)
    snapshot = PolicySet(
        root_uri=URI.rsplit("/", 1)[0],
        policies=[
            Policy(
                name="case_a",
                uri=URI,
                status="production",
                content=before.content,
                version=before.extra_fields["version"],
                metadata=dict(before.extra_fields),
            )
        ],
    )
    await record(fs, trajectory("second"))
    plan = PolicyUpdatePlan(
        items=[
            PolicyPlanItem(
                kind="upsert",
                memory_type="experiences",
                target_name="case_a",
                target_uri=URI,
                before_content=before.content,
                after_content=before.content.replace("old reminder", "new reminder"),
            )
        ]
    )
    result = await MemoryFilePolicyUpdater(viking_fs=fs).apply(plan, snapshot, CTX)
    assert not result.errors
    assert "new reminder" in MemoryFileUtils.read(fs.files[URI], uri=URI).content
    assert stats(fs)["injected_count"] == 2
    assert set(stats(fs)["observed_trajectories"]) == {
        trajectory("first").uri,
        trajectory("second").uri,
    }
