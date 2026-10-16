from __future__ import annotations

import re

import pytest
from test_case_selection import selection_loop
from test_extraction_output_protocol import _context, _preference_schema

from openviking.session.memory.extraction_output_protocol import create_extraction_output_protocol
from openviking.session.memory.extraction_output_protocol.python_protocol import (
    _MAX_FORMAT_RETRY_EXCERPT_CHARS,
    _format_retry_source_excerpt,
)


def bad_program():
    lines = [f"# source-line-{index}" for index in range(1, 21)]
    lines[9] = "items = list(range(1, 45))"
    lines[-1] = "sdk.commit()"
    return "\n".join(lines)


def excerpt_rows(excerpt):
    return [
        (marker, int(number), text)
        for marker, number, text in re.findall(r"^([ >])\s+(\d+) \| (.*)$", excerpt, re.MULTILINE)
    ]


@pytest.mark.parametrize("wrapper", ["plain", "fenced", "unclosed", "preamble", "leading_blank"])
def test_retry_excerpt_uses_actual_parser_line_numbers(wrapper):
    code = bad_program()
    error_line = 10
    if wrapper == "plain":
        content = code
    elif wrapper == "fenced":
        content = f"```python\n{code}\n```"
    elif wrapper == "unclosed":
        content = f"Explanation\n```python\n{code}"
    elif wrapper == "preamble":
        content = f"Explanation\n```python\n{code}\n```"
    else:
        content = f"```python\n\n{code}\n```"
        error_line += 1
    protocol = create_extraction_output_protocol("python")
    operations, error = protocol.parse(content, _context([_preference_schema()]))
    assert operations is None  # This change does not enable range().
    assert error.startswith(f"Line {error_line}:") and "'range' is not allowed" in error

    retry = protocol.render_format_retry(error, failed_content=content)
    rows = excerpt_rows(retry)
    assert [row[1] for row in rows] == list(range(error_line - 3, error_line + 4))
    assert [(number, text) for marker, number, text in rows if marker == ">"] == [
        (error_line, "items = list(range(1, 45))")
    ]
    assert "# source-line-1\n" not in retry and "# source-line-19\n" not in retry
    assert "Explanation" not in retry
    assert "preserve all other memory" in retry
    assert "Case identities, merge decisions, and source associations" in retry
    assert "complete corrected program, not just a repair fragment" in retry


@pytest.mark.parametrize("line", [1, 7, 10])
def test_retry_excerpt_clips_window_at_program_boundaries(line):
    source = "\n".join(f"# line-{index}" for index in range(1, 11))
    excerpt = _format_retry_source_excerpt(source, f"Line {line}: error")
    rows = excerpt_rows(excerpt)
    assert [row[1] for row in rows] == list(range(max(1, line - 3), min(10, line + 3) + 1))
    assert [number for marker, number, _ in rows if marker == ">"] == [line]


def test_retry_excerpt_bounds_long_unicode_lines_without_losing_error_marker():
    source = "\n".join(
        f"head-{index}:" + "内容" * 10_000 + f":tail-{index}" for index in range(1, 8)
    )
    excerpt = _format_retry_source_excerpt(source, "Line 4: invalid Python syntax")
    assert len(excerpt) <= _MAX_FORMAT_RETRY_EXCERPT_CHARS
    rows = excerpt_rows(excerpt)
    assert len(rows) == 7
    for marker, number, text in rows:
        assert text.startswith(f"head-{number}:") and text.endswith(f":tail-{number}")
        assert "[truncated]" in text
        assert (marker == ">") == (number == 4)


@pytest.mark.parametrize("error", [None, "Invalid field", "Line 0: error", "Line 99: error"])
def test_retry_without_a_valid_source_line_does_not_dump_the_response(error):
    content = "# PRIVATE_FULL_RESPONSE\nsdk.commit()"
    protocol = create_extraction_output_protocol("python")
    retry = protocol.render_format_retry(error, failed_content=content)
    assert "Previous failed program excerpt" not in retry
    assert "PRIVATE_FULL_RESPONSE" not in retry


def test_retry_unreadable_fence_keeps_original_error_without_guessing_source_lines():
    content = "```python\nsdk.commit()\n```\n```python\nsdk.commit()\n```"
    assert _format_retry_source_excerpt(content, "Line 1: error") == ""


def test_json_retry_is_unchanged_and_does_not_echo_failed_content():
    protocol = create_extraction_output_protocol("json")
    assert protocol.render_format_retry("error", failed_content="PRIVATE_RESPONSE") == (
        protocol.render_format_retry("error")
    )


@pytest.mark.asyncio
async def test_extract_loop_forwards_failed_source_to_existing_retry(monkeypatch):
    loop, model, _files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format="python"
    )
    failed = f"```python\n{bad_program()}\n```"
    model.get_completion_async.side_effect = [failed, "sdk.commit(cases=[second])"]

    operations, _ = await loop.run()

    assert model.get_completion_async.await_count == 2
    retry_messages = model.get_completion_async.await_args_list[1].kwargs["messages"]
    retry = next(
        message["content"]
        for message in retry_messages
        if "Previous failed program excerpt" in str(message.get("content", ""))
    )
    assert [row[1] for row in excerpt_rows(retry)] == list(range(7, 14))
    assert "> 10 | items = list(range(1, 45))" in retry
    assert failed not in retry
    assert len(operations.case_references) == 1
    fs.write_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_excerpt_does_not_add_another_retry_attempt(monkeypatch):
    loop, model, _files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format="python"
    )
    model.get_completion_async.return_value = bad_program()
    with pytest.raises(RuntimeError, match="after 2 iterations"):
        await loop.run()
    assert model.get_completion_async.await_count == 2
    fs.write_file.assert_not_awaited()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_retry_context_matches_python_physical_lines(newline):
    code = newline.join(
        ["# Unicode separator stays on this line: \u2028 text", "items = range(2)", "sdk.commit()"]
    )
    protocol = create_extraction_output_protocol("python")
    _operations, error = protocol.parse(code, _context([_preference_schema()]))
    assert error.startswith("Line 2:")
    rows = excerpt_rows(protocol.render_format_retry(error, failed_content=code))
    assert [(number, text) for marker, number, text in rows if marker == ">"] == [
        (2, "items = range(2)")
    ]


def test_retry_context_also_covers_native_syntax_errors():
    code = "# context\nsdk.create_preferences(\n    topic='x',\n    content='unterminated\n)"
    protocol = create_extraction_output_protocol("python")
    _operations, error = protocol.parse(code, _context([_preference_schema()]))
    assert "Line 4: invalid Python syntax" in error
    rows = excerpt_rows(protocol.render_format_retry(error, failed_content=code))
    assert [(number, text) for marker, number, text in rows if marker == ">"] == [
        (4, "    content='unterminated")
    ]


@pytest.mark.asyncio
async def test_each_format_failure_refreshes_the_error_and_source(monkeypatch):
    loop, model, _files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format="python"
    )
    loop.max_iterations = 2
    first_failure = "# first_failure\nitems = list(range(1, 45))\nsdk.commit()"
    second_failure = "# second_failure\n\nvalue = 'unterminated"
    model.get_completion_async.side_effect = [
        first_failure,
        second_failure,
        "sdk.commit(cases=[second])",
    ]

    operations, _ = await loop.run()

    assert model.get_completion_async.await_count == 3
    first_retry = [
        message["content"]
        for message in model.get_completion_async.await_args_list[1].kwargs["messages"]
        if "Previous failed program excerpt" in str(message.get("content", ""))
    ]
    second_retry = [
        message["content"]
        for message in model.get_completion_async.await_args_list[2].kwargs["messages"]
        if "Previous failed program excerpt" in str(message.get("content", ""))
    ]
    assert len(first_retry) == 1 and "# first_failure" in first_retry[-1]
    assert len(second_retry) == 2
    assert "# second_failure" in second_retry[-1]
    assert "unterminated string literal" in second_retry[-1]
    assert "# first_failure" not in second_retry[-1]
    assert first_retry[-1] != second_retry[-1]
    assert loop._format_retry_count == 1
    assert len(operations.case_references) == 1
    fs.write_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["python", "json"])
@pytest.mark.parametrize("max_iterations", [1, 3])
async def test_feedback_refresh_does_not_extend_budget_or_render_terminal_failure(
    monkeypatch, output_format, max_iterations
):
    loop, model, _files, fs, _ctx, _registry, _memories = selection_loop(
        monkeypatch, output_format=output_format
    )
    loop.max_iterations = max_iterations
    failures = [f"# failure_{index}\nvalue = 'unterminated" for index in range(max_iterations + 1)]
    model.get_completion_async.side_effect = failures
    feedback_sources = []
    add_feedback = loop._add_format_error_message

    def record_feedback(messages):
        feedback_sources.append(loop._last_llm_failure_content)
        add_feedback(messages)

    monkeypatch.setattr(loop, "_add_format_error_message", record_feedback)

    with pytest.raises(RuntimeError, match=f"after {max_iterations + 1} iterations"):
        await loop.run()

    assert model.get_completion_async.await_count == max_iterations + 1
    assert loop._format_retry_count == 1
    assert feedback_sources == failures[:-1]
    fs.write_file.assert_not_awaited()
