# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from copy import deepcopy

import pytest

from openviking.message import ContextPart, TextPart, ToolPart
from openviking.session import secret_masking as masking
from openviking.session.secret_masking import REDACTED, mask_message_specs, mask_text
from openviking_cli.exceptions import InvalidArgumentError

BODY = "abcd012345678901234567890123wxyz"


@pytest.mark.parametrize(
    "prefix",
    [
        "sk-",
        "sk-proj-",
        "sk-svcacct-",
        "sk-ant-api03-",
        "ak-",
        "AKLT",
        "AKTP",
        "ghp_",
        "github_pat_",
    ],
)
def test_supported_credentials_are_completely_masked_and_idempotent(prefix):
    text = f"凭据 {prefix}{BODY}；再次使用 {prefix}{BODY}"
    expected = f"凭据 {prefix}ab****yz；再次使用 {prefix}ab****yz"
    assert mask_text(text) == expected
    assert mask_text(expected) == expected


@pytest.mark.parametrize("prefix", ["AKIA", "ASIA"])
def test_aws_key_ids_are_fully_masked(prefix):
    assert mask_text(prefix + "ABCDEFGHIJKLMNOP") == REDACTED


@pytest.mark.parametrize(
    "syntax",
    [
        "ak={value}",
        "sk={value}",
        'secret_key="{value}"',
        '"clientSecret": "{value}"',
        "api key：'{value}'",
        'os.environ["AWS_SECRET_ACCESS_KEY"] = "{value}"',
        "VOLCENGINE_SECRET_ACCESS_KEY={value}",
    ],
)
def test_sensitive_assignments_mask_unprefixed_credentials(syntax):
    assert mask_text(syntax.format(value=BODY)) == syntax.format(value="ab****yz")


@pytest.mark.parametrize(
    "value", ["short", "two words", 'abc\\"secret', "p@ssword", "ab****yzMORE"]
)
def test_short_complex_and_noncanonical_values_are_fully_masked(value):
    assert mask_text(f'password="{value}"') == 'password="[REDACTED]"'


@pytest.mark.parametrize("prefix", ["", "sk-proj-"])
@pytest.mark.parametrize("length", [19, 20])
def test_partial_mask_uses_twenty_character_body_threshold(prefix, length):
    body = "ab" + "0" * (length - 4) + "yz"
    expected = prefix + "ab****yz" if length == 20 else REDACTED
    assert mask_text(f'api_key="{prefix}{body}"') == f'api_key="{expected}"'


@pytest.mark.parametrize(
    "name,value,expected",
    [
        ("Authorization", "Bearer abcdefghijklmnopqrstuvwx", "Bearer ab****wx"),
        ("proxy_authorization", "Basic " + BODY, "Basic ab****yz"),
        ("authorization", "Bearer short", "Bearer [REDACTED]"),
        ("Authorization", "Bearer ab****yz", "Bearer ab****yz"),
    ],
)
def test_authentication_text_and_tool_input_preserve_scheme(name, value, expected):
    assert mask_text(f'{name}: "{value}"') == f'{name}: "{expected}"'
    result = mask_message_specs(
        [{"role": "assistant", "parts": [ToolPart(tool_input={name: value})]}]
    )
    assert result[0]["parts"][0].tool_input == {name: expected}


@pytest.mark.parametrize("delimiter", ["；", "，"])
def test_mixed_configuration_keeps_normal_content(delimiter):
    text = f"sk={BODY}{delimiter}max_tokens=8192 token_count=4 request_id={BODY} sk-example"
    expected = f"sk=ab****yz{delimiter}max_tokens=8192 token_count=4 request_id={BODY} sk-example"
    assert mask_text(text) == expected


def test_all_content_fields_are_masked_in_independent_copies():
    parts = [
        TextPart("sk-" + BODY),
        ContextPart(uri="viking://resources/doc", abstract="sk=" + BODY),
        ToolPart(
            tool_input={"nested": [{"secretAccessKey": BODY, "max_tokens": 42}]},
            tool_output="api_key=" + BODY,
            tool_output_externalization_error="token=" + BODY,
        ),
    ]
    original = deepcopy(parts)
    result = mask_message_specs([{"role": "assistant", "parts": parts}])[0]["parts"]
    assert parts == original
    assert result[0].text == "sk-ab****yz"
    assert result[1].abstract == "sk=ab****yz"
    assert result[2].tool_input == {"nested": [{"secretAccessKey": "ab****yz", "max_tokens": 42}]}
    assert result[2].tool_output == "api_key=ab****yz"
    assert result[2].tool_output_externalization_error == "token=ab****yz"
    result[2].tool_input["nested"][0]["max_tokens"] = 99
    assert parts[2].tool_input["nested"][0]["max_tokens"] == 42


def test_sensitive_values_are_masked_without_traversing_containers():
    cycle = {}
    cycle["loop"] = cycle
    deep = {"leaf": BODY}
    for _ in range(50):
        deep = {"nested": deep}
    original = {
        "credentials": cycle,
        "secret": deep,
        "token": None,
        "ak": 123,
        "password": "Bearer " + BODY,
    }
    result = mask_message_specs([{"role": "assistant", "parts": [ToolPart(tool_input=original)]}])
    assert result[0]["parts"][0].tool_input == dict.fromkeys(original, REDACTED)


@pytest.mark.parametrize(
    "limit", ["MAX_TEXT_CHARS", "MAX_BATCH_CHARS", "MAX_MATCHES", "MAX_NODES", "MAX_DEPTH"]
)
def test_resource_limits_reject_without_echoing_credentials(monkeypatch, limit):
    monkeypatch.setattr(masking, limit, 0 if limit == "MAX_MATCHES" else 1)
    specs = [
        {
            "role": "user",
            "parts": [
                ToolPart(tool_input={"nested": {"nested": {"sk": BODY}}}, tool_output="sk=" + BODY)
            ],
        }
    ]
    with pytest.raises(InvalidArgumentError) as error:
        mask_message_specs(specs)
    assert BODY not in str(error.value)


def test_sensitive_strings_still_count_toward_text_budget(monkeypatch):
    monkeypatch.setattr(masking, "MAX_TEXT_CHARS", len(BODY) - 1)
    with pytest.raises(InvalidArgumentError, match="text limit"):
        mask_message_specs([{"role": "assistant", "parts": [ToolPart(tool_input={"sk": BODY})]}])


def test_invalid_tool_input_and_masked_key_collisions_reject():
    cycle = {}
    cycle["loop"] = cycle
    collision = {"sk-ab" + "1" * 30 + "yz": 1, "sk-ab" + "2" * 30 + "yz": 2}
    for value in [cycle, {1: BODY}, {"custom": object()}, collision]:
        with pytest.raises(InvalidArgumentError):
            mask_message_specs([{"role": "assistant", "parts": [ToolPart(tool_input=value)]}])
