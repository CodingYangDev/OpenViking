# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from openviking.parser_network import (
    ParserNetworkContext,
    ParserNetworkOperation,
    create_parser_httpx_client,
    open_parser_url,
    overlay_parser_subprocess_env,
    register_parser_network_adapter,
    reset_parser_network_adapter,
)


class _Adapter:
    def __init__(self, *, supported: bool = True) -> None:
        self.supported = supported
        self.transport = object()
        self.opener = Mock()
        self.opener.open.return_value = object()

    def supports(self, context: ParserNetworkContext) -> bool:
        return self.supported

    def create_httpx_transport(self, context: ParserNetworkContext):
        return self.transport

    def create_urllib_opener(self, context: ParserNetworkContext):
        return self.opener

    def subprocess_env_overlay(self, context: ParserNetworkContext):
        return {"HTTPS_PROXY": "http://proxy.example:8118"}


@pytest.fixture(autouse=True)
def _reset_adapter():
    reset_parser_network_adapter()
    yield
    reset_parser_network_adapter()


def _context(operation=ParserNetworkOperation.HTTP_RESOURCE):
    return ParserNetworkContext(operation, "https://example.com/resource")


def test_context_requires_typed_operation_and_non_empty_url() -> None:
    with pytest.raises(TypeError, match="ParserNetworkOperation"):
        ParserNetworkContext("http_resource", "https://example.com")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        ParserNetworkContext(ParserNetworkOperation.HTTP_RESOURCE, "  ")


def test_httpx_transport_is_injected_without_dropping_client_kwargs() -> None:
    adapter = _Adapter()
    register_parser_network_adapter(adapter)
    hooks = {"request": [object()]}

    client = create_parser_httpx_client(
        _context(),
        client_cls=lambda **kwargs: SimpleNamespace(kwargs=kwargs),  # type: ignore[arg-type]
        timeout=30,
        follow_redirects=True,
        event_hooks=hooks,
        trust_env=True,
    )

    assert client.kwargs["transport"] is adapter.transport
    assert client.kwargs["trust_env"] is False
    assert client.kwargs["event_hooks"] is hooks
    assert client.kwargs["timeout"] == 30
    assert client.kwargs["follow_redirects"] is True


def test_adapter_cannot_replace_explicit_transport() -> None:
    register_parser_network_adapter(_Adapter())

    with pytest.raises(RuntimeError, match="explicit transport"):
        create_parser_httpx_client(
            _context(),
            client_cls=lambda **kwargs: kwargs,  # type: ignore[arg-type]
            transport=object(),
        )


def test_urllib_opener_is_scoped_to_archive_operation() -> None:
    adapter = _Adapter()
    register_parser_network_adapter(adapter)
    context = _context(ParserNetworkOperation.GIT_ARCHIVE)
    request = object()

    result = open_parser_url(context, request, timeout=1800)  # type: ignore[arg-type]

    assert result is adapter.opener.open.return_value
    adapter.opener.open.assert_called_once_with(request, timeout=1800)


def test_subprocess_overlay_preserves_auth_env_without_mutating_it() -> None:
    adapter = _Adapter()
    register_parser_network_adapter(adapter)
    base = {"GIT_CONFIG_COUNT": "1", "SECRET": "kept"}

    result = overlay_parser_subprocess_env(
        _context(ParserNetworkOperation.GIT_COMMAND),
        base,
    )

    assert result == {
        "GIT_CONFIG_COUNT": "1",
        "SECRET": "kept",
        "HTTPS_PROXY": "http://proxy.example:8118",
    }
    assert base == {"GIT_CONFIG_COUNT": "1", "SECRET": "kept"}


def test_unsupported_adapter_preserves_default_behavior() -> None:
    register_parser_network_adapter(_Adapter(supported=False))
    base = {"EXISTING": "value"}
    client = create_parser_httpx_client(
        _context(),
        client_cls=lambda **kwargs: SimpleNamespace(kwargs=kwargs),  # type: ignore[arg-type]
        trust_env=False,
    )

    assert client.kwargs == {"trust_env": False}
    assert overlay_parser_subprocess_env(_context(ParserNetworkOperation.GIT_COMMAND), base) is base
    with patch("openviking.parser_network.urllib.request.urlopen", return_value="direct") as direct:
        assert (
            open_parser_url(
                _context(ParserNetworkOperation.GIT_ARCHIVE), "https://example.com/archive.zip"
            )
            == "direct"
        )
    direct.assert_called_once()


def test_registration_is_idempotent_but_replacement_is_rejected() -> None:
    adapter = _Adapter()
    register_parser_network_adapter(adapter)
    register_parser_network_adapter(adapter)

    with pytest.raises(RuntimeError, match="already registered"):
        register_parser_network_adapter(_Adapter())
