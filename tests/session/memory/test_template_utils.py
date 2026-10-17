# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from types import SimpleNamespace

from openviking.session.memory.utils.template_utils import TemplateUtils


class TestTemplateUtils:
    def test_render_supports_extract_context(self):
        extract_context = SimpleNamespace(get_year=lambda ranges: "2026")

        rendered = TemplateUtils.render(
            "  {{ title }} {{ extract_context.get_year(ranges) }}  ",
            {"title": "Trip", "ranges": "0-1"},
            extract_context=extract_context,
        )

        assert rendered == "Trip 2026"

    def test_find_missing_variables_ignores_extract_context(self):
        missing = TemplateUtils.find_missing_variables(
            "{{ missing_field }} {{ extract_context.get_year(ranges) }} {{ content }}",
            {"content": "Trip summary", "ranges": "0-1"},
        )

        assert missing == {"missing_field"}

    def test_render_plain_text_without_template_syntax(self):
        rendered = TemplateUtils.render("plain text", {"unused": "value"})

        assert rendered == "plain text"


def test_to_yaml_preserves_json_values_quotes_and_unicode():
    import copy
    from datetime import datetime, timezone

    import yaml

    value = {
        "text": '用户说 "yes"，但路径为 C:\\tmp\\x。\n第二行含 \'\'\' 和 """。\n',
        "values": [False, True, None, 0, "false", "null", "2026-10-16"],
        "empty": [],
        "time": datetime(2026, 10, 16, tzinfo=timezone.utc),
    }
    before = copy.deepcopy(value)
    rendered = TemplateUtils.render("{{ value | to_yaml }}", {"value": value})
    assert yaml.safe_load(rendered) == {**value, "time": str(value["time"])}
    assert value == before
    assert "用户说" in rendered and "!!python" not in rendered


def test_to_yaml_works_with_exact_false_template_selection():
    import yaml

    criteria = [
        {"id": "failed", "passed": False},
        {"id": "passed", "passed": True},
        {"id": "unknown", "passed": None},
        {"id": "string", "passed": "false"},
        {"id": "number", "passed": 0},
        {"id": "missing"},
    ]
    rendered = TemplateUtils.render(
        "{{ criteria | selectattr('passed', 'sameas', false) | list | to_yaml }}",
        {"criteria": criteria},
    )
    assert yaml.safe_load(rendered) == [criteria[0]]


def test_content_template_preserves_interpolated_body_whitespace():
    from openviking.session.memory.dataclass import MemoryFile
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    body = '\n  user: "yes"  \n\n'
    memory = MemoryFile(content=body, memory_type="events")
    rendered = MemoryFileUtils.write(memory, content_template="HEADER\n{{ content }}")
    assert rendered.startswith("HEADER\n" + body + "\n\n<!-- MEMORY_FIELDS")
    assert memory.content == body


def test_content_template_errors_do_not_silently_drop_the_template():
    import pytest
    from jinja2 import TemplateError

    from openviking.session.memory.dataclass import MemoryFile
    from openviking.session.memory.utils.memory_file_utils import MemoryFileUtils

    memory = MemoryFile(content="original body", memory_type="events")
    with pytest.raises(TemplateError):
        MemoryFileUtils.write(memory, content_template="{{ content | missing_filter }}")
    assert memory.content == "original body"


def test_yaml_filter_does_not_expand_account_template_permissions():
    import pytest

    from openviking.session.memory.utils.content_template import (
        ContentTemplateError,
        validate_content_template,
    )

    with pytest.raises(ContentTemplateError, match="unsupported_filter"):
        validate_content_template("{{ core_truths | to_yaml }}", "soul")


def test_yaml_filter_is_available_during_template_variable_analysis():
    template = "{{ present | to_yaml }} {{ missing | to_yaml }}"
    assert TemplateUtils.find_missing_variables(template, {"present": {}}) == {"missing"}
    assert TemplateUtils.referenced_variables(template) == {"present", "missing"}
