# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_memory_create_guard import CTX, MemoryFS
from test_memory_create_guard import schema as record_schema

from openviking.session.memory.dataclass import MemoryFile, ResolvedOperation, ResolvedOperations
from openviking.session.memory.experience.compiler import compile_dag
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.memory_type_registry import (
    MemoryTypeRegistry,
    resolve_memory_templates_dir,
)
from openviking.session.memory.memory_updater import ExtractContext, MemoryUpdater
from openviking.session.memory.merge_op import (
    DeleteBlock,
    FieldType,
    PatchOp,
    SearchReplaceBlock,
    StrPatch,
)
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking.session.memory.validation import content as content_gate

URI = "viking://user/alice/memories/experiences/case_a.md"
OTHER = "viking://user/alice/memories/records/a.md"
SOURCE = """dag = workflow("review")
condition = check("Current request applies")
reminder = tell("old reminder")
condition.then(reminder)"""


def registry():
    value = MemoryTypeRegistry(load_schemas=False)
    value.load_from_yaml(str(resolve_memory_templates_dir() / "experiences.yaml"))
    value.register(record_schema())
    return value


def file(source=SOURCE, uri=URI):
    return MemoryFile(
        uri=uri,
        memory_type="experiences",
        content=source,
        extra_fields={"experience_name": "case_a", "version": 1},
    )


def operation(content, old=None, uri=URI, memory_type="experiences", create_only=False):
    return ResolvedOperation(
        memory_type=memory_type,
        uris=[uri],
        page_id=1,
        memory_fields={"content": content, "experience_name": "case_a"},
        old_memory_file_content=old,
        create_only=create_only,
    )


def batch(*ops, deletes=()):
    return ResolvedOperations(
        upsert_operations=list(ops), delete_file_contents=list(deletes), errors=[]
    )


def patch(search="old reminder", replace="new reminder"):
    return StrPatch(blocks=[SearchReplaceBlock(search=search, replace=replace)])


class Store(MemoryFS):
    def __init__(self, files=None):
        super().__init__(files)
        self.writes = []
        self.deletes = []
        self.on_lock = None

    async def acquire(self, paths, **kwargs):
        lease = await super().acquire(paths, **kwargs)
        if self.on_lock:
            self.on_lock()
            self.on_lock = None
        return lease

    async def write_file(self, uri, content, **kwargs):
        await super().write_file(uri, content, **kwargs)
        self.writes.append(uri)

    async def rm(self, uri, **kwargs):
        assert self.lock.locked()
        self.deletes.append(uri)
        self.files.pop(uri, None)


def updater(fs):
    result = MemoryUpdater(registry=registry())
    result._viking_fs = fs
    result._vectorize_memories = AsyncMock(return_value=0)
    result.generate_overview = AsyncMock()
    result._sync_resource_refs_for_result = AsyncMock()
    return result


def loop():
    value = object.__new__(ExtractLoop)
    value.ctx = CTX
    value._extract_context = ExtractContext([])
    schemas = registry().list_all()
    value.context_provider = SimpleNamespace(get_memory_schemas=lambda _: schemas)
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_patch_is_materialized_before_gate_and_preserved_until_locked_apply(as_dict):
    old = file()
    update = patch()
    update = update.model_dump() if as_dict else update
    op = operation(update, old)
    ops = batch(op)
    assert await loop()._validate_content_gates(ops) == []
    assert op.memory_fields["content"] is update  # Never flatten a patch before the lock.
    fs = Store({URI: MemoryFileUtils.write(old)})
    result = await updater(fs).apply_operations(ops, CTX)
    assert not result.errors
    stored = MemoryFileUtils.read(fs.files[URI], uri=URI)
    assert stored.content == SOURCE.replace("old reminder", "new reminder")
    assert stored.extra_fields["version"] == 2
    assert compile_dag(stored.content)
    assert not fs.lock.locked()


@pytest.mark.asyncio
async def test_latest_locked_content_preserves_concurrent_unrelated_change():
    old = file()
    latest = SOURCE.replace('"Current request applies"', '"Updated applicability"')
    fs = Store({URI: MemoryFileUtils.write(old)})
    fs.on_lock = lambda: fs.files.update({URI: MemoryFileUtils.write(file(latest))})
    result = await updater(fs).apply_operations(batch(operation(patch(), old)), CTX)
    assert not result.errors
    content = MemoryFileUtils.read(fs.files[URI], uri=URI).content
    assert "Updated applicability" in content and "new reminder" in content


@pytest.mark.asyncio
async def test_latest_content_gate_catches_invalid_graph_despite_valid_preview():
    original = 'dag = workflow("review")\ncondition = check("ready")\nreminder = tell("old")'
    update = patch('reminder = tell("old")', 'reminder = tell("new")\ncondition.then(reminder)')
    op = operation(update, file(original))
    assert await loop()._validate_content_gates(batch(op)) == []
    fs = Store({URI: MemoryFileUtils.write(file(original)), OTHER: "keep this"})
    latest = 'dag = workflow("review")\nreminder = tell("old")'
    fs.on_lock = lambda: fs.files.update({URI: MemoryFileUtils.write(file(latest))})
    result = await updater(fs).apply_operations(
        batch(
            operation("changed", memory_type="records", uri=OTHER),
            op,
            deletes=[MemoryFile(uri=OTHER)],
        ),
        CTX,
    )
    assert result.errors
    assert fs.writes == [] and fs.deletes == []
    assert MemoryFileUtils.read(fs.files[URI], uri=URI).content == latest
    assert not fs.lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "latest",
    [
        SOURCE.replace("old reminder", "almost old reminder!"),
        SOURCE + '\nother = tell("old reminder")',
    ],
)
async def test_stale_or_ambiguous_anchor_does_not_use_fuzzy_matching(latest):
    # Match the full declaration, so a changed string cannot match as a substring.
    update = patch('reminder = tell("old reminder")', 'reminder = tell("new reminder")')
    if "other =" in latest:
        update = patch("old reminder", "new reminder")
    fs = Store({URI: MemoryFileUtils.write(file(latest))})
    before = dict(fs.files)
    result = await updater(fs).apply_operations(batch(operation(update, file())), CTX)
    assert result.errors and "exactly once" in str(result.errors[0][1])
    assert fs.files == before and fs.writes == []
    assert not fs.lock.locked()


@pytest.mark.asyncio
async def test_invalid_candidate_blocks_unrelated_write_and_dependent_delete():
    fs = Store({URI: MemoryFileUtils.write(file()), OTHER: "keep"})
    result = await updater(fs).apply_operations(
        batch(
            operation("changed", memory_type="records", uri=OTHER),
            operation('dag = workflow("broken")', file()),
            deletes=[file()],
        ),
        CTX,
    )
    assert result.errors
    assert not fs.writes and not fs.deletes


@pytest.mark.asyncio
async def test_delete_patch_checks_the_complete_result_and_allows_safe_node_removal():
    source = SOURCE + '\nextra = tell("Unrelated reminder")'
    good = StrPatch(blocks=[DeleteBlock(delete='extra = tell("Unrelated reminder")')])
    fs = Store({URI: MemoryFileUtils.write(file(source))})
    result = await updater(fs).apply_operations(batch(operation(good, file(source))), CTX)
    assert not result.errors
    assert MemoryFileUtils.read(fs.files[URI], uri=URI).content == SOURCE
    bad = StrPatch(blocks=[DeleteBlock(delete='reminder = tell("old reminder")')])
    result = await updater(fs).apply_operations(batch(operation(bad, file())), CTX)
    assert result.errors
    assert MemoryFileUtils.read(fs.files[URI], uri=URI).content == SOURCE


@pytest.mark.asyncio
async def test_sequential_patches_share_a_staged_view_before_any_write():
    fs = Store({URI: MemoryFileUtils.write(file())})
    result = await updater(fs).apply_operations(
        batch(
            operation(patch(replace="intermediate"), file()),
            operation(patch(search="intermediate", replace="final"), file()),
        ),
        CTX,
    )
    assert not result.errors
    assert MemoryFileUtils.read(fs.files[URI], uri=URI).content == SOURCE.replace(
        "old reminder", "final"
    )


@pytest.mark.asyncio
async def test_erased_content_is_not_validated_using_the_old_graph():
    content = StrPatch(blocks=[DeleteBlock(delete=SOURCE)])
    fs = Store({URI: MemoryFileUtils.write(file())})
    result = await updater(fs).apply_operations(batch(operation(content, file())), CTX)
    assert result.errors and not fs.writes


@pytest.mark.asyncio
async def test_missing_or_unreadable_latest_file_never_falls_back_to_prefetch():
    for error in [FileNotFoundError(URI), PermissionError("denied")]:
        fs = Store()
        fs.read_file = AsyncMock(side_effect=error)
        result = await updater(fs).apply_operations(batch(operation(patch(), file())), CTX)
        assert result.errors and not fs.writes
        assert not fs.lock.locked()


@pytest.mark.asyncio
async def test_new_memory_and_full_replacement_use_same_gate():
    fs = Store()
    result = await updater(fs).apply_operations(batch(operation(SOURCE, create_only=True)), CTX)
    assert not result.errors
    result = await updater(fs).apply_operations(
        batch(operation(SOURCE.replace("old", "new"), file())), CTX
    )
    assert not result.errors
    assert "new reminder" in MemoryFileUtils.read(fs.files[URI], uri=URI).content


@pytest.mark.asyncio
async def test_gate_dispatch_is_type_generic_and_independent_of_quality_gate(
    monkeypatch,
):
    calls = []

    def check(content):
        calls.append(content)
        if content != "safe":
            raise ValueError("record content rejected")
        return content

    monkeypatch.setitem(content_gate._CONTENT_GATES, "records", check)
    fs = Store({OTHER: "before"})
    result = await updater(fs).apply_operations(
        batch(operation("unsafe", uri=OTHER, memory_type="records")), CTX
    )
    assert calls == ["unsafe"] and result.errors and not fs.writes
    assert content_gate.get_content_gate("experiences") is not None


@pytest.mark.asyncio
async def test_ordinary_patch_keeps_existing_lenient_semantics():
    # Only the gated-content boundary opts into exact application.
    merge = PatchOp(FieldType.STRING)
    content = "unchanged"
    empty = StrPatch(blocks=[SearchReplaceBlock(search="", replace="ignored")])
    assert await merge.apply(content, empty) == content
    with pytest.raises(ValueError, match="exactly once"):
        await merge.apply_exact(content, empty)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", None])
async def test_empty_optional_patch_value_keeps_existing_merge_semantics(content):
    fs = Store({URI: MemoryFileUtils.write(file())})
    result = await updater(fs).apply_operations(batch(operation(content, file())), CTX)
    assert not result.errors
    assert MemoryFileUtils.read(fs.files[URI], uri=URI).content == SOURCE


@pytest.mark.asyncio
async def test_concurrent_disjoint_patches_preserve_both_updates():
    fs = Store({URI: MemoryFileUtils.write(file())})
    results = await asyncio.gather(
        updater(fs).apply_operations(batch(operation(patch(), file())), CTX),
        updater(fs).apply_operations(
            batch(operation(patch("Current request applies", "Updated condition"), file())), CTX
        ),
    )
    assert all(not result.errors for result in results)
    content = MemoryFileUtils.read(fs.files[URI], uri=URI).content
    assert "new reminder" in content and "Updated condition" in content
    assert not fs.lock.locked()


@pytest.mark.asyncio
async def test_extraction_repair_and_writer_share_the_content_gate(monkeypatch):
    from openviking.session.memory.experience.experience_context import (
        AgentExperienceContextProvider,
    )
    from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
    from openviking.session.train import Case, Rubric, Trajectory

    old = file()
    fs = Store({URI: MemoryFileUtils.write(old)})
    provider = AgentExperienceContextProvider(
        messages=[],
        case=Case("case_a", "signature", {}, Rubric("reward", "", [])),
        target_uri=URI,
        source_session_uri="viking://session/s/history/archive_001",
        memory_registry=registry(),
        trajectories=[
            Trajectory(
                name="observation",
                uri="viking://user/alice/memories/trajectories/observation.md",
                outcome="failure",
                content="The old reminder needs correction",
                metadata={
                    "case_name": "case_a",
                    "source_session_uri": "viking://session/s/history/archive_001",
                },
            )
        ],
    )
    provider.read_file_contents[URI] = old
    provider.get_extract_context().page_id_map.get_page_id(URI)
    provider._eval_memory = AsyncMock(
        return_value={
            "experience": {
                "uri": URI,
                "memory_type": "experiences",
                "experience_name": "case_a",
                "content": SOURCE,
            }
        }
    )
    isolation = MemoryIsolationHandler(
        CTX, provider.get_extract_context(), allowed_memory_types={"experiences"}
    )
    provider._isolation_handler = isolation
    bad = 'experience.content.drop(text="condition = check(\\"Current request applies\\")")\nsdk.commit()'
    good = 'experience.content.edit(search="old reminder", replace="new reminder")\nsdk.commit()'
    model = SimpleNamespace(model="mock", get_completion_async=AsyncMock(side_effect=[bad, good]))
    extraction = ExtractLoop(
        vlm=model,
        viking_fs=fs,
        ctx=CTX,
        context_provider=provider,
        isolation_handler=isolation,
        max_iterations=1,
    )
    config = SimpleNamespace(
        memory=SimpleNamespace(link_enabled=False, extraction_output_format="python")
    )
    monkeypatch.setattr(
        "openviking.session.memory.extract_loop.get_openviking_config", lambda: config
    )
    monkeypatch.setattr("openviking_cli.utils.config.get_openviking_config", lambda: config)
    result, _ = await extraction.run()
    assert model.get_completion_async.await_count == 2
    assert "Memory content validation failed" in str(
        model.get_completion_async.await_args.kwargs["messages"]
    )
    assert isinstance(result.upsert_operations[0].memory_fields["content"], StrPatch)
    applied = await updater(fs).apply_operations(result, CTX)
    assert not applied.errors
    assert MemoryFileUtils.read(fs.files[URI], uri=URI).content == SOURCE.replace(
        "old reminder", "new reminder"
    )
