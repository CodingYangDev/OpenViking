# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from openviking.telemetry import tracer


@pytest.fixture
def trace_capture(monkeypatch):
    module = importlib.import_module("openviking.telemetry.tracer")
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    console = Mock()
    monkeypatch.setattr(module, "_otel_tracer", provider.get_tracer("test"))
    monkeypatch.setattr(module, "logger", console)
    yield SimpleNamespace(exporter=exporter, console=console, module=module)
    provider.shutdown()


@pytest.mark.asyncio
async def test_scoped_errors_keep_trace_status_exception_and_stack(trace_capture):
    @tracer.errors_to_trace()
    @tracer("quiet.extraction")
    async def extract():
        tracer.error("Invalid Python output")
        raise ValueError("parse failed")

    with pytest.raises(ValueError, match="parse failed"):
        await extract()
    trace_capture.console.opt.assert_not_called()
    (span,) = trace_capture.exporter.get_finished_spans()
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "Invalid Python output" for event in span.events)
    assert any(
        "parse failed" in str(event.attributes.get("exception.message", ""))
        for event in span.events
    )
    assert any(
        "ValueError: parse failed" in str(event.attributes.get("exception.stacktrace", ""))
        for event in span.events
    )
    tracer.error("outside scope")
    trace_capture.console.opt.return_value.error.assert_called_once_with("outside scope")


@pytest.mark.asyncio
async def test_trace_only_scope_isolated_between_concurrent_tasks(trace_capture):
    entered, finish = asyncio.Event(), asyncio.Event()

    @tracer.errors_to_trace()
    async def quiet():
        entered.set()
        await finish.wait()
        tracer.error("quiet")

    async def noisy():
        await entered.wait()
        tracer.error("visible sibling")
        finish.set()

    await asyncio.gather(quiet(), noisy())
    trace_capture.console.opt.return_value.error.assert_called_once_with("visible sibling")


@pytest.mark.asyncio
async def test_nested_scope_restores_after_cancellation(trace_capture):
    async with tracer.errors_to_trace():
        with pytest.raises(asyncio.CancelledError):
            async with tracer.errors_to_trace():
                raise asyncio.CancelledError()
        tracer.error("outer scope remains quiet")
    trace_capture.console.opt.assert_not_called()
    tracer.error("outside")
    trace_capture.console.opt.return_value.error.assert_called_once_with("outside")


@pytest.mark.asyncio
async def test_scope_without_otel_still_suppresses_console_and_restores(trace_capture, monkeypatch):
    monkeypatch.setattr(trace_capture.module, "_otel_tracer", None)
    async with tracer.errors_to_trace():
        tracer.error("quiet")
    trace_capture.console.opt.assert_not_called()
    tracer.error("outside")
    trace_capture.console.opt.return_value.error.assert_called_once_with("outside")
