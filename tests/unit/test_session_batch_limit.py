# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import pytest
from fastapi import HTTPException

from openviking.server.config import ServerConfig
from openviking.server.routers.sessions import (
    BatchAddMessageRequest,
    _check_batch_message_limit,
)


def test_server_config_sets_a_high_default_safety_cap():
    assert ServerConfig().max_batch_messages == 10_000


def test_default_config_accepts_a_120_message_batch():
    _check_batch_message_limit(120, ServerConfig().max_batch_messages)


def test_batch_request_model_no_longer_hard_caps_messages_at_100():
    request = BatchAddMessageRequest.model_validate(
        {"messages": [{"role": "user", "content": f"message-{index}"} for index in range(120)]}
    )

    assert len(request.messages) == 120


def test_batch_limit_accepts_a_batch_at_the_configured_limit():
    _check_batch_message_limit(100, 100)


def test_batch_limit_rejects_a_larger_batch_with_a_chunking_hint():
    with pytest.raises(HTTPException) as exc_info:
        _check_batch_message_limit(101, 100)

    assert exc_info.value.status_code == 400
    assert "at most 100" in exc_info.value.detail
    assert "server.max_batch_messages" in exc_info.value.detail
