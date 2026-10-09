# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
import warnings
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from openviking.session.memory.dataclass import MemoryField, MemoryTypeSchema
from openviking.session.memory.memory_type_registry import MemoryTypeRegistry
from openviking.session.memory.merge_op.base import FieldType
from openviking.session.memory_policy import MemoryPolicy
from openviking.session.session import Session, SessionMeta, _apply_agent_evolution_setting
from openviking_cli.exceptions import InvalidArgumentError


def test_memory_policy_defaults_to_self_and_peer():
    policy = MemoryPolicy.from_dict(None)

    assert policy.self_enabled is True
    assert policy.peer_enabled is True
    assert policy.memory_types is None
    assert policy.working_memory_enabled is True
    assert policy.extract_max_iterations == 3
    assert policy.extract_thinking is False


@pytest.mark.parametrize("thinking", [False, True])
def test_memory_policy_extract_thinking_round_trip(thinking):
    policy = MemoryPolicy.from_dict({"extract_thinking": thinking})

    assert policy.extract_thinking is thinking
    assert MemoryPolicy.from_dict(policy.to_dict()).extract_thinking is thinking
    if thinking:
        assert policy.to_dict()["extract_thinking"] is True
    else:
        assert "extract_thinking" not in policy.to_dict()


@pytest.mark.parametrize("thinking", [None, 0, 1, "true", "false", [], {}])
def test_memory_policy_rejects_nonboolean_extract_thinking(thinking):
    with pytest.raises(InvalidArgumentError, match="extract_thinking must be a boolean"):
        MemoryPolicy.from_dict({"extract_thinking": thinking})
    with pytest.raises(InvalidArgumentError, match="extract_thinking must be a boolean"):
        MemoryPolicy(extract_thinking=thinking)


def test_memory_policy_thinking_survives_disabled_agent_evolution():
    policy = MemoryPolicy.from_dict({"memory_types": ["profile"], "extract_thinking": True})

    effective = _apply_agent_evolution_setting(policy, agent_evolution_enabled=False)

    assert effective.extract_thinking is True
    assert effective.memory_types == {"profile"}


@pytest.mark.asyncio
async def test_session_thinking_uses_commit_session_user_precedence():
    provider = AsyncMock(return_value={"extract_thinking": True})
    session = Session(viking_fs=None, memory_policy_provider=provider)
    assert (await session._resolve_memory_policy()).extract_thinking is True

    session.meta.memory_policy = MemoryPolicy.from_dict({"extract_thinking": True}).to_dict()
    restored = SessionMeta.from_dict(session.meta.to_dict())
    assert restored.memory_policy["extract_thinking"] is True
    assert (await session._resolve_memory_policy()).extract_thinking is True
    assert (
        await session._resolve_memory_policy({"extract_thinking": False})
    ).extract_thinking is False

    session.meta.memory_policy = MemoryPolicy.default().to_dict()
    assert (await session._resolve_memory_policy()).extract_thinking is False
    provider.assert_awaited_once_with()


@pytest.mark.parametrize("iterations", [1, 3, 6, 20])
def test_memory_policy_extract_max_iterations_round_trip(iterations):
    policy = MemoryPolicy.from_dict({"extract_max_iterations": iterations})

    assert policy.extract_max_iterations == iterations
    assert MemoryPolicy.from_dict(policy.to_dict()).extract_max_iterations == iterations
    if iterations == 3:
        assert "extract_max_iterations" not in policy.to_dict()
    else:
        assert policy.to_dict()["extract_max_iterations"] == iterations


@pytest.mark.parametrize("iterations", [None, True, False, 0, -1, 1.5, "6", [], {}])
def test_memory_policy_rejects_invalid_extract_max_iterations(iterations):
    with pytest.raises(
        InvalidArgumentError, match="extract_max_iterations must be a positive integer"
    ):
        MemoryPolicy.from_dict({"extract_max_iterations": iterations})
    with pytest.raises(
        InvalidArgumentError, match="extract_max_iterations must be a positive integer"
    ):
        MemoryPolicy(extract_max_iterations=iterations)


def test_memory_policy_iteration_budget_survives_disabled_agent_evolution():
    policy = MemoryPolicy.from_dict({"memory_types": ["profile"], "extract_max_iterations": 7})

    effective = _apply_agent_evolution_setting(policy, agent_evolution_enabled=False)

    assert effective.extract_max_iterations == 7
    assert effective.memory_types == {"profile"}


@pytest.mark.asyncio
async def test_session_iteration_budget_uses_commit_session_user_precedence():
    provider = AsyncMock(return_value={"extract_max_iterations": 6})
    session = Session(viking_fs=None, memory_policy_provider=provider)
    assert (await session._resolve_memory_policy()).extract_max_iterations == 6

    session.meta.memory_policy = MemoryPolicy.from_dict({"extract_max_iterations": 8}).to_dict()
    assert (await session._resolve_memory_policy()).extract_max_iterations == 8
    assert (
        await session._resolve_memory_policy({"extract_max_iterations": 10})
    ).extract_max_iterations == 10
    restored = SessionMeta.from_dict(session.meta.to_dict())
    assert restored.memory_policy["extract_max_iterations"] == 8
    provider.assert_awaited_once_with()


def test_memory_policy_can_disable_peer_memory():
    policy = MemoryPolicy.from_dict({"peer": {"enabled": False}})

    assert policy.self_enabled is True
    assert policy.peer_enabled is False


def test_memory_policy_uses_top_level_memory_types():
    policy = MemoryPolicy.from_dict(
        {
            "self": {"enabled": False},
            "peer": {"enabled": True},
            "memory_types": ["profile", "events"],
        }
    )

    assert policy.self_enabled is False
    assert policy.peer_enabled is True
    assert policy.memory_types == {"profile", "events"}
    assert policy.to_dict() == {
        "self": {"enabled": False},
        "peer": {"enabled": True},
        "memory_types": ["events", "profile"],
    }


def test_memory_policy_can_disable_working_memory():
    policy = MemoryPolicy.from_dict({"working_memory": {"enabled": False}})

    assert policy.self_enabled is True
    assert policy.peer_enabled is True
    assert policy.working_memory_enabled is False
    assert policy.to_dict() == {
        "self": {"enabled": True},
        "peer": {"enabled": True},
        "working_memory": {"enabled": False},
    }


def test_memory_policy_rejects_invalid_working_memory_shape():
    with pytest.raises(InvalidArgumentError, match="working_memory must be an object"):
        MemoryPolicy.from_dict({"working_memory": False})

    with pytest.raises(InvalidArgumentError, match="working_memory supports only: enabled"):
        MemoryPolicy.from_dict({"working_memory": {"enabled": True, "mode": "summary"}})


def test_memory_policy_legacy_enabled_compatibility_fixture():
    fixture_path = Path(__file__).parent / "fixtures" / "memory_policy_compat.json"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))

    for case in cases:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            policy = MemoryPolicy.from_dict({"peer": {"enabled": case["value"]}})

        assert policy.peer_enabled is case["expected"]
        deprecations = [item for item in caught if item.category is FutureWarning]
        assert bool(deprecations) is case["deprecated"]


def test_memory_policy_legacy_enabled_warning_is_user_visible():
    with pytest.warns(FutureWarning, match="legacy coercion is deprecated"):
        MemoryPolicy.from_dict({"peer": {"enabled": "false"}})


def test_memory_policy_rejects_invalid_memory_types():
    policy = MemoryPolicy.from_dict({"memory_types": ["profile", "missing"]})

    with pytest.raises(InvalidArgumentError, match="missing"):
        policy.validate_memory_types({"profile"})

    evolution_policy = MemoryPolicy.from_dict({"memory_types": ["profile", "experiences"]})
    assert evolution_policy.memory_types == {
        "profile",
        "cases",
        "trajectories",
        "experiences",
    }

    incomplete_policy = MemoryPolicy.from_dict(
        {"memory_types": ["profile", "cases", "trajectories"]}
    )
    assert incomplete_policy.memory_types == {"profile", "cases", "trajectories"}

    trajectory_policy = MemoryPolicy.from_dict({"memory_types": ["trajectories"]})
    assert trajectory_policy.memory_types == {"cases", "trajectories"}


async def test_initialize_memory_files_respects_memory_type_filter(monkeypatch):
    class FakeVikingFS:
        def __init__(self):
            self.written_uris = []

        async def read_file(self, uri, ctx=None):
            raise FileNotFoundError(uri)

        async def write_file(self, uri, content, ctx=None):
            del content, ctx
            self.written_uris.append(uri)

    def schema(memory_type: str, filename: str) -> MemoryTypeSchema:
        return MemoryTypeSchema(
            memory_type=memory_type,
            directory="viking://user/{{ user_space }}/memories",
            filename_template=filename,
            content_template="{{ content }}",
            fields=[
                MemoryField(
                    name="content",
                    field_type=FieldType.STRING,
                    init_value=memory_type,
                )
            ],
        )

    fake_fs = FakeVikingFS()
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fake_fs)

    registry = MemoryTypeRegistry(load_schemas=False)
    registry.register(schema("identity", "identity.md"))
    registry.register(schema("profile", "profile.md"))

    ctx = type("Ctx", (), {"user": type("User", (), {"user_id": "alice"})()})()
    await registry.initialize_memory_files(ctx, allowed_memory_types={"profile"})

    assert fake_fs.written_uris == ["viking://user/alice/memories/profile.md"]


async def test_initialize_memory_files_renders_fields_without_init_value_as_empty(monkeypatch):
    """Fields lacking an explicit init_value must not leak ``{{ field }}`` placeholders.

    Regression test for the bug where ``identity.md`` was written to disk with
    literal Jinja placeholders (e.g. ``{{ vibe }}``) for every field that did not
    declare an ``init_value``. See issue #4207.
    """

    class FakeVikingFS:
        def __init__(self):
            self.written = {}

        async def read_file(self, uri, ctx=None):
            raise FileNotFoundError(uri)

        async def write_file(self, uri, content, ctx=None):
            del ctx
            self.written[uri] = content

    def schema(memory_type: str, filename: str) -> MemoryTypeSchema:
        return MemoryTypeSchema(
            memory_type=memory_type,
            directory="viking://user/{{ user_space }}/memories",
            filename_template=filename,
            content_template=(
                "- **HasInit:** {{ has_init }}\n"
                "- **NoInit:** {{ no_init }}\n"
                "- **AlsoNoInit:** {{ also_no_init }}\n"
            ),
            fields=[
                MemoryField(
                    name="has_init",
                    field_type=FieldType.STRING,
                    init_value="filled",
                ),
                MemoryField(name="no_init", field_type=FieldType.STRING),
                MemoryField(name="also_no_init", field_type=FieldType.STRING),
            ],
        )

    fake_fs = FakeVikingFS()
    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fake_fs)

    registry = MemoryTypeRegistry(load_schemas=False)
    registry.register(schema("identity", "identity.md"))

    ctx = type("Ctx", (), {"user": type("User", (), {"user_id": "bob"})()})()
    await registry.initialize_memory_files(ctx)

    content = fake_fs.written["viking://user/bob/memories/identity.md"]
    assert "{{" not in content, f"unrendered placeholder leaked: {content!r}"
    assert "{{ no_init }}" not in content
    assert "{{ also_no_init }}" not in content
    assert "filled" in content
    # Fields without init_value render as empty, not as the literal placeholder.
    assert "- **NoInit:** \n" in content
    assert "- **AlsoNoInit:** \n" in content
