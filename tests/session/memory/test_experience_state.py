from __future__ import annotations

import copy

import pytest

from openviking.session.memory.experience.state import bound_session_state, serialize_state


def test_small_state_preserves_native_message_order_and_metadata():
    messages = [
        {"role": "system", "content": "Rules"},
        {"role": "user", "content": "Current request"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"id": 1}'},
                }
            ],
        },
        {"role": "tool", "name": "lookup", "tool_call_id": "call1", "content": "found"},
    ]
    result = bound_session_state(messages, len(serialize_state(messages)))
    assert result == messages
    result[2]["tool_calls"][0]["function"]["arguments"] = "changed"
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == '{"id": 1}'


def test_state_has_no_message_count_cap():
    messages = [{"role": "user", "content": str(i)} for i in range(300)]
    assert bound_session_state(messages, 32768) == messages


def test_budget_includes_serialization_and_keeps_recent_whole_messages():
    old = {"role": "user", "content": "old" * 1000}
    latest = {"role": "user", "content": 'new " \\\n消息'}
    assert bound_session_state([old, latest], len(serialize_state([latest]))) == [latest]


@pytest.mark.parametrize("content", ["x" * 10000, ('"\\\n汉字') * 10000])
def test_large_tool_result_keeps_identity_and_marks_truncation(content):
    messages = [{"role": "tool", "name": "query_order", "tool_call_id": "c1", "content": content}]
    original = copy.deepcopy(messages)
    result = bound_session_state(messages, 1024)
    assert len(serialize_state(result)) <= 1024
    assert result[0]["name"] == "query_order"
    assert result[0]["tool_call_id"] == "c1"
    assert result[0]["content"].startswith(content[:10])
    assert result[0]["content"].endswith(content[-10:])
    assert "...[truncated]..." in result[0]["content"]
    assert messages == original


def test_large_tool_call_keeps_identity_without_inventing_a_result():
    messages = [
        {
            "role": "assistant",
            "content": "intent",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "write_order", "arguments": "x" * 10000},
                }
            ],
        }
    ]
    result = bound_session_state(messages, 1024)
    assert len(serialize_state(result)) <= 1024
    call = result[0]["tool_calls"][0]
    assert call["id"] == "c1" and call["function"]["name"] == "write_order"
    assert "...[truncated]..." in call["function"]["arguments"]
    assert all(m["role"] != "tool" for m in result)


def test_structured_text_content_is_bounded_without_changing_part_type():
    state = [{"role": "user", "content": [{"type": "text", "text": "x" * 10000}]}]
    result = bound_session_state(state, 1024)
    assert result[0]["content"][0]["type"] == "text"
    assert "...[truncated]..." in result[0]["content"][0]["text"]
    assert len(serialize_state(result)) <= 1024


def test_oversized_identity_is_not_silently_rewritten():
    with pytest.raises(ValueError, match="metadata"):
        bound_session_state([{"role": "tool", "name": "x" * 10000, "content": "ok"}], 1024)
