from __future__ import annotations

import copy
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.session.compressor_v3 import _canonical_cases_from_update_result
from openviking.session.memory.dataclass import MemoryFile
from openviking.session.memory.extract_loop import ExtractLoop
from openviking.session.memory.memory_isolation_handler import MemoryIsolationHandler
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry, get_default_registry
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking.session.memory.streaming_memory_updater import (
    MemoryUpdateRequest,
    StreamingMemoryUpdater,
)
from openviking.session.memory.tools import add_tool_call_pair_to_messages
from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config import get_openviking_config


def case_memory(name, *, user="u", memory_type="cases"):
    return MemoryFile(
        uri=f"viking://user/{user}/memories/{memory_type}/{name}.md",
        memory_type=memory_type,
        content=f"Business domain {name}",
        extra_fields={
            "case_name": name,
            "task_signature": f"Domain {name}",
            "input": '{"summary":"service domain"}',
            "rubric": "[]",
            # Old on-disk data must not become Case learning context after field removal.
            "evidence": "OBSOLETE_CASE_EVIDENCE",
            "version": 1,
        },
    )


def selection_loop(monkeypatch, *, output_format, invalid=None):
    monkeypatch.setattr(get_openviking_config().memory, "extraction_output_format", output_format)
    registry = MemoryTypeRegistry(load_schemas=False)
    registry.register(get_default_registry().get("cases"))
    memories = [
        case_memory("first"),
        case_memory(
            "second",
            user="other" if invalid == "foreign" else "u",
            memory_type="entities" if invalid == "wrong_type" else "cases",
        ),
    ]
    files = {m.uri: MemoryFileUtils.write(m) for m in memories}
    fs = SimpleNamespace(
        read_file=AsyncMock(side_effect=lambda uri, **kwargs: files[uri]),
        exists=AsyncMock(side_effect=lambda uri, **kwargs: uri in files),
        write_file=AsyncMock(side_effect=AssertionError("selection must not write")),
    )
    ctx = RequestContext(user=UserIdentifier("default", "u"), role=Role.ROOT)
    provider = SessionExtractContextProvider(
        messages=[], ctx=ctx, viking_fs=fs, memory_registry=registry
    )
    extract_context = provider.get_extract_context()
    isolation = MemoryIsolationHandler(
        ctx, extract_context, allowed_memory_types={"cases"}, allow_self=invalid != "disabled"
    )
    isolation.prepare_messages()
    provider._isolation_handler = isolation
    payload = {}
    ids = []
    for name, memory in zip(["first", "second"], memories, strict=True):
        page_id = extract_context.page_id_map.get_page_id(memory.uri)
        ids.append(page_id)
        provider.read_file_contents[memory.uri] = memory
        payload[name] = {**memory.to_metadata(), "page_id": page_id}
    if invalid == "unread":
        del provider.read_file_contents[memories[1].uri]
    prefetch = []
    add_tool_call_pair_to_messages(
        prefetch,
        "prefetch",
        "eval",
        {
            "code": "\n".join(
                f"{name} = sdk.read(uri={memory.uri!r})"
                for name, memory in zip(["first", "second"], memories, strict=True)
            )
        },
        payload,
    )
    provider.prefetch = AsyncMock(return_value=prefetch)
    provider.get_tools = lambda: []
    provider.instruction = lambda: "Select the second business domain, not every search candidate."
    if output_format == "python":
        code = "sdk.commit(cases=[second, second])"
    else:
        result = {"selected_case_ids": [ids[1], ids[1]]}
        if invalid == "deleted":
            result["delete_ids"] = [{"delete_page_id": ids[1], "replacement_page_id": None}]
        code = json.dumps(result)
    model = SimpleNamespace(model="offline", get_completion_async=AsyncMock(return_value=code))
    loop = ExtractLoop(
        vlm=model,
        viking_fs=fs,
        ctx=ctx,
        context_provider=provider,
        isolation_handler=isolation,
        max_iterations=1,
    )
    return loop, model, files, fs, ctx, registry, memories


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["python", "json"])
async def test_selected_case_without_writes_reaches_compressor_and_does_not_select_all_candidates(
    monkeypatch, output_format
):
    loop, model, files, fs, ctx, registry, memories = selection_loop(
        monkeypatch, output_format=output_format
    )
    before = dict(files)
    operations, _ = await loop.run()
    assert operations.upsert_operations == [] and operations.delete_file_contents == []
    assert [ref.uris for ref in operations.case_references] == [[memories[1].uri]]
    model.get_completion_async.assert_awaited_once()
    updater = StreamingMemoryUpdater(registry=registry)
    monkeypatch.setattr(
        StreamingMemoryUpdater,
        "_apply_operations",
        AsyncMock(side_effect=AssertionError("no write batch")),
    )
    try:
        result = await updater.submit(
            MemoryUpdateRequest(
                operations=operations,
                messages=[],
                ctx=ctx,
                metadata={"source_extraction_id": "this_execution", "session_id": "s"},
            )
        )
    finally:
        await updater.close()
    assert result.apply_result.written_uris == [] and result.apply_result.edited_uris == []
    assert updater._group_batchers == {}
    cases = await _canonical_cases_from_update_result(
        operations=result.operations,
        requested_operations=operations,
        result=result.apply_result,
        viking_fs=fs,
        ctx=ctx,
    )
    assert [case.name for case in cases] == ["second"]
    assert "OBSOLETE_CASE_EVIDENCE" not in json.dumps(asdict(cases[0]))
    assert "evidence" not in cases[0].metadata
    assert files == before
    fs.write_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["foreign", "wrong_type", "unread", "deleted"])
async def test_case_selection_rejects_invalid_or_out_of_scope_page_ids(monkeypatch, invalid):
    loop, _model, files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format="json", invalid=invalid
    )
    before = copy.deepcopy(files)
    with pytest.raises(ValueError, match="Selected Case"):
        await loop.run()
    assert files == before
    fs.write_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_case_schema_does_not_offer_or_accept_selection(monkeypatch):
    loop, model, files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format="json", invalid="disabled"
    )
    before = dict(files)
    result, _ = await loop.run()
    assert result is None or not result.case_references
    assert "selected_case_ids" not in loop._operations_model.model_fields
    assert "sdk.commit(cases=" not in loop._output_protocol.render_contract(loop._output_context)
    fs.write_file.assert_not_awaited()
    assert files == before


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["python", "json"])
async def test_reading_candidates_without_selection_does_not_associate_them(
    monkeypatch, output_format
):
    loop, model, files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format=output_format
    )
    model.get_completion_async.return_value = "sdk.commit()" if output_format == "python" else "{}"
    before = dict(files)
    result, _ = await loop.run()
    assert result is None or (not result.case_references and not result.upsert_operations)
    assert files == before
    fs.write_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "program",
    [
        "sdk.commit(cases=second)",
        "sdk.commit(cases=[1])",
        "sdk.commit(cases=['viking://user/u/memories/cases/second.md'])",
        "second.delete()\nsdk.commit(cases=[second])",
        "created = sdk.create_cases(case_name='new', task_signature='New domain', "
        "input='{}', rubric='[]')\nsdk.commit(cases=[created])",
    ],
)
async def test_python_case_selection_rejects_invalid_objects(monkeypatch, program):
    loop, model, files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format="python"
    )
    # Initialize the real output context and binding table with a valid selection.
    await loop.run()
    before = dict(files)
    operations, error = loop._output_protocol.parse(program, loop._output_context)
    assert operations is None
    assert error
    assert files == before
    fs.write_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("page_id", [True, "2", 2.5, {"page_id": 2}])
async def test_json_case_selection_does_not_coerce_invalid_page_ids(monkeypatch, page_id):
    loop, model, files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format="json"
    )
    await loop.run()
    before = dict(files)
    operations, error = loop._output_protocol.parse(
        json.dumps({"selected_case_ids": [page_id]}), loop._output_context
    )
    assert operations is None
    assert error
    assert files == before
    fs.write_file.assert_not_awaited()
