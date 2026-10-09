# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.skills import SkillsLoader
from vikingbot.config.schema import SessionKey
from vikingbot.hooks.builtins.openviking_hooks import OpenVikingPostCallHook


@pytest.fixture
def context(tmp_path):
    for name in ("always_other", "normal_other", "experience_loader", "task_case_experience"):
        folder = tmp_path / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} description\n"
            f"always: {str(name == 'always_other').lower()}\n---\n{name} instructions\n",
            encoding="utf-8",
        )
    builder = ContextBuilder(tmp_path, remote_skills_summary="REMOTE_SKILL_SENTINEL")
    builder._templates_ensured = True
    builder._get_identity = AsyncMock(return_value="Test agent")
    return builder


async def _build_messages(context, **kwargs):
    return await context.build_messages(
        history=[],
        current_message="Help with my request",
        session_key=SessionKey(type="cli", channel_id="default", chat_id="test"),
        ov_tools_enable=False,
        experience_recall_enable=False,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_exclusive_advice_loader_inlines_bundled_skill_without_other_skills(context):
    # An initialized workspace need not have a copy of the newly bundled Skill.
    assert not (context.workspace / "skills/advice_loader/SKILL.md").exists()
    messages = await _build_messages(context, only_skills=["advice_loader"])
    prompt = messages[0]["content"]

    assert "# Active Skills" in prompt
    assert "### Skill: advice_loader" in prompt
    assert "You MUST call `ask_for_advice` at least once per task" in prompt
    assert "even if you are confident" in prompt
    assert "before committing to a task-specific plan" in prompt
    assert "state-changing action" in prompt
    assert "refusal, handoff, or task-completion conclusion" in prompt
    assert "You may first clarify the request" in prompt
    assert "Consult again only when" in prompt
    assert "also satisfies the initial consultation requirement" in prompt
    assert "Do not wait until you are stuck" in prompt
    assert "Current policy remains authoritative" in prompt
    assert "Do not batch the advice call with the action" in prompt
    assert "no_match" in prompt
    assert "no_applicable_advice" in prompt
    assert "unavailable" in prompt
    for excluded in (
        "always_other",
        "normal_other",
        "experience_loader",
        "task_case_experience",
        "REMOTE_SKILL_SENTINEL",
        "## Local Skills",
        "## OpenViking Skills",
        "Required skill:",
    ):
        assert excluded not in prompt
    assert messages[-1] == {"role": "user", "content": "Help with my request"}


@pytest.mark.asyncio
async def test_explicit_empty_selection_loads_no_skills(context):
    prompt = (await _build_messages(context, only_skills=[]))[0]["content"]
    for excluded in ("# Active Skills", "# Skills", "ask_for_advice", "REMOTE_SKILL_SENTINEL"):
        assert excluded not in prompt


@pytest.mark.asyncio
async def test_default_skill_loading_is_unchanged(context):
    prompt = (await _build_messages(context))[0]["content"]
    assert "always_other instructions" in prompt
    assert "normal_other description" in prompt
    assert "normal_other instructions" not in prompt
    assert "REMOTE_SKILL_SENTINEL" in prompt
    assert "Required skill:" in prompt
    assert "skills/experience_loader/SKILL.md" in prompt


@pytest.mark.asyncio
async def test_missing_exclusive_skill_fails_instead_of_silently_omitting_guidance(context):
    with pytest.raises(FileNotFoundError, match="missing_selected_skill"):
        await _build_messages(context, only_skills=["advice_loader", "missing_selected_skill"])


def test_default_skill_loader_still_skips_missing_optional_skills(tmp_path):
    assert SkillsLoader(tmp_path).load_skills_for_context(["missing_optional_skill"]) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("skill_name", ["advice_loader", "experience_loader", "normal_skill"])
@pytest.mark.parametrize("tool_name", ["read_file", "openviking_multi_read"])
async def test_advice_loader_read_does_not_trigger_automatic_recall(skill_name, tool_name):
    hook = OpenVikingPostCallHook()
    hook._search_skill_experiences = AsyncMock(return_value="unrelated automatic experience")
    context = SimpleNamespace(workspace_id="test", config=None, openviking_connection=None)
    content = f"---\nname: {skill_name}\ndescription: Skill description\n---\nSkill instructions"
    params = (
        {"path": f"skills/{skill_name}/SKILL.md"}
        if tool_name == "read_file"
        else {"uris": [f"viking://agent/skills/{skill_name}/SKILL.md"]}
    )

    result = await hook.execute(context, tool_name, params, content)

    if skill_name == "normal_skill":
        hook._search_skill_experiences.assert_awaited_once()
        assert "## Related Experiences" in result["result"]
    else:
        hook._search_skill_experiences.assert_not_awaited()
        assert result == {"tool_name": tool_name, "params": params, "result": content}
