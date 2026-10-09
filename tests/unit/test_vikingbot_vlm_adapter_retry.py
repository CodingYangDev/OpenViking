import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpcore
import httpx
import pytest
import vikingbot.providers.vlm_adapter as vlm_adapter
from vikingbot.providers.vlm_adapter import VLMProviderAdapter
from volcenginesdkarkruntime._exceptions import (
    ArkAPIConnectionError,
    ArkAPIError,
    ArkAPITimeoutError,
    ArkBadRequestError,
    ArkRateLimitError,
)

from openviking.models.vlm.backends.openai_vlm import OpenAIVLM
from openviking.models.vlm.backends.volcengine_vlm import (
    VOLCENGINE_CLIENT_REQUEST_ID,
    VOLCENGINE_CLIENT_REQUEST_ID_HEADER,
    VolcEngineVLM,
    build_volcengine_request_headers,
)
from openviking.models.vlm.base import MultiCredentialVLM
from openviking.utils.exceptions import AllCredentialsFailedError
from openviking.utils.model_retry import is_retryable_rate_limit_error


class _DisabledLangfuse:
    enabled = False
    _client = None


class _FakeVLM:
    def __init__(self, failures: list[Exception], result: str = "ok"):
        self.failures = list(failures)
        self.result = result
        self.calls = 0

    async def get_completion_async(self, **_kwargs):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.result


def _raw_chat_completions(create):
    async def raw_create(**kwargs):
        response = await create(**kwargs)

        async def parse():
            return response

        return SimpleNamespace(parse=parse, headers=httpx.Headers())

    return SimpleNamespace(create=create, with_raw_response=SimpleNamespace(create=raw_create))


class _FakeVolcEngineFailoverVLM(_FakeVLM):
    provider = "volcengine"
    model = "primary-model"
    thinking = False


class _AsyncChunks:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


class _FakeStreamingCompletions:
    def __init__(self, failures: list[Exception], chunks):
        self.failures = list(failures)
        self.chunks = chunks
        self.calls = 0
        self.kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        return _AsyncChunks(self.chunks)


class _FakeStreamingVLM:
    provider = "volcengine"
    model = "test-model"
    thinking = False
    temperature = 0.7
    max_tokens = None
    max_retries = 0
    timeout = 6000

    def __init__(self, completions: _FakeStreamingCompletions):
        self._client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
        )

    def get_async_client(self):
        return self._client


def _streaming_adapter(completions, with_credentials):
    vlm = _FakeStreamingVLM(completions)
    if with_credentials:
        vlm = MultiCredentialVLM([vlm], ["test-credential"])
        # Stress retries without flooding the process-wide asynchronous log sink.
        vlm._logger = Mock()
    return VLMProviderAdapter(vlm, "test-model", langfuse_client=_DisabledLangfuse())


def _wrapped_connection_error(cause=None):
    request = httpx.Request("POST", "https://ark.example.test/api/v3/chat/completions")
    error = ArkAPIConnectionError(request=request, request_id="test-request")
    error.__cause__ = cause if cause is not None else httpx.ConnectError("Connection failed")
    return error


def _stream_chunk(**delta):
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(finish_reason=None, delta=SimpleNamespace(**delta))],
    )


class _CaptureLogger:
    def __init__(self):
        self.messages: list[str] = []
        self.warnings: list[str] = []

    def warning(self, message: str, *args):
        self.warnings.append(message.format(*args))

    def error(self, message: str, *args):
        self.messages.append(message.format(*args))


@pytest.mark.asyncio
async def test_chat_retries_rate_limit_until_success(monkeypatch):
    sleep_delays: list[float] = []

    async def _sleep(delay: float):
        sleep_delays.append(delay)

    monkeypatch.setattr(vlm_adapter, "rate_limit_retry_delay", lambda attempt: attempt)
    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", _sleep)

    fake_vlm = _FakeVLM(
        [
            RuntimeError("Error code: 429 - ModelAccountTpmRateLimitExceeded"),
            RuntimeError("TooManyRequests: rate limit"),
        ],
        result="done",
    )
    adapter = VLMProviderAdapter(fake_vlm, "test-model", langfuse_client=_DisabledLangfuse())

    response = await adapter.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "done"
    assert response.finish_reason == "stop"
    assert fake_vlm.calls == 3
    assert sleep_delays == [1, 2]


@pytest.mark.asyncio
async def test_chat_does_not_retry_errors_without_rate_limit_markers(monkeypatch):
    async def _sleep(_delay: float):
        raise AssertionError("non-retryable errors must not sleep/retry")

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", _sleep)

    fake_vlm = _FakeVLM([RuntimeError("AuthenticationError Unauthorized")])
    adapter = VLMProviderAdapter(fake_vlm, "test-model", langfuse_client=_DisabledLangfuse())

    response = await adapter.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.finish_reason == "error"
    assert "AuthenticationError" in response.content
    assert fake_vlm.calls == 1


@pytest.mark.asyncio
async def test_chat_accepts_string_response_from_openai_backend_with_tools(monkeypatch):
    async def create(**_kwargs):
        return "plain string response"

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    vlm = OpenAIVLM({"provider": "openai", "model": "gpt-5.6-terra"})
    monkeypatch.setattr(vlm, "get_async_client", lambda: client)
    adapter = VLMProviderAdapter(vlm, "gpt-5.6-terra", langfuse_client=_DisabledLangfuse())

    response = await adapter.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert response.content == "plain string response"
    assert response.tool_calls == []
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_without_tools_preserves_usage_from_openai_backend(monkeypatch):
    raw_response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="plain response", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=13,
            completion_tokens=5,
            total_tokens=18,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )

    async def create(**kwargs):
        assert "tools" not in kwargs
        return raw_response

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    vlm = OpenAIVLM({"provider": "openai", "model": "gpt-5.6-terra"})
    monkeypatch.setattr(vlm, "get_async_client", lambda: client)
    adapter = VLMProviderAdapter(vlm, "gpt-5.6-terra", langfuse_client=_DisabledLangfuse())

    response = await adapter.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.content == "plain response"
    assert response.tool_calls == []
    assert response.finish_reason == "stop"
    assert response.usage == {
        "prompt_tokens": 13,
        "completion_tokens": 5,
        "total_tokens": 18,
        "prompt_tokens_details": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
async def test_chat_stream_retries_rate_limit_until_success(monkeypatch, with_credentials):
    sleep_delays: list[float] = []

    async def _sleep(delay: float):
        sleep_delays.append(delay)

    monkeypatch.setattr(vlm_adapter, "rate_limit_retry_delay", lambda attempt: attempt)
    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", _sleep)

    chunk = SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(content="streamed", reasoning_content=None),
            )
        ],
    )
    completions = _FakeStreamingCompletions(
        [RuntimeError("Error code: 429 - ModelAccountTpmRateLimitExceeded")],
        [chunk],
    )
    adapter = _streaming_adapter(completions, with_credentials)

    events = [
        event
        async for event in adapter.chat_stream(
            messages=[{"role": "user", "content": "hello"}],
        )
    ]

    assert completions.calls == 2
    assert sleep_delays == [1]
    assert [event.type for event in events] == ["content_delta", "response"]
    assert events[0].content == "streamed"
    assert events[1].response.content == "streamed"
    assert events[1].response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_stream_routes_failover_wrapper_through_completion_api():
    fake_vlm = _FakeVolcEngineFailoverVLM([], result="fallback-safe")
    adapter = VLMProviderAdapter(
        fake_vlm,
        "primary-model",
        langfuse_client=_DisabledLangfuse(),
    )

    events = [
        event
        async for event in adapter.chat_stream(
            messages=[{"role": "user", "content": "hello"}],
        )
    ]

    assert fake_vlm.calls == 1
    assert [event.type for event in events] == ["response"]
    assert events[0].response.content == "fallback-safe"


@pytest.mark.asyncio
async def test_chat_stream_logs_timeout_cause_and_request_metadata(monkeypatch):
    request = httpx.Request(
        "POST",
        "https://ark.example.test/api/v3/chat/completions?api_key=must-not-leak",
    )
    cause = httpx.ReadTimeout("read deadline exceeded", request=request)
    error = ArkAPITimeoutError(request=request, request_id="request-123")
    error.__cause__ = cause
    completions = _FakeStreamingCompletions([error], [])
    adapter = VLMProviderAdapter(
        _FakeStreamingVLM(completions),
        "test-model",
        langfuse_client=_DisabledLangfuse(),
    )
    captured = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", captured)

    events = [
        event
        async for event in adapter.chat_stream(
            messages=[{"role": "user", "content": "hello"}],
        )
    ]

    assert events[-1].response.finish_reason == "error"
    assert len(captured.messages) == 1
    log_message = captured.messages[0]
    assert "operation=chat_stream" in log_message
    assert "error_type=ArkAPITimeoutError" in log_message
    assert 'client_request_id="request-123"' in log_message
    assert "server_request_id=-" in log_message
    assert "server_trace_id=-" in log_message
    assert "method=POST" in log_message
    assert "url=https://ark.example.test/api/v3/chat/completions" in log_message
    assert "cause_chain=ReadTimeout: read deadline exceeded" in log_message
    assert "must-not-leak" not in log_message


def test_exception_log_details_include_redacted_upstream_error_body():
    request = httpx.Request(
        "POST",
        "https://ark.example.test/api/v3/chat/completions?token=query-secret",
    )
    response = httpx.Response(
        400,
        request=request,
        headers={
            "X-Request-Id": "ark-server-request-456",
            "X-Trace-Id": "ark-server-trace-456",
        },
    )
    error = ArkBadRequestError(
        "invalid request",
        response=response,
        body={
            "code": "InvalidParameter",
            "message": "invalid max_tokens",
            "api_key": "body-secret",
            "authorization": "Bearer header-secret",
        },
        request_id="request-456",
    )

    details = vlm_adapter._exception_log_details(error)

    assert details == {
        "error_type": "ArkBadRequestError",
        "status": "400",
        "code": '"InvalidParameter"',
        "client_request_id": '"request-456"',
        "server_request_id": "ark-server-request-456",
        "server_trace_id": "ark-server-trace-456",
        "method": "POST",
        "url": "https://ark.example.test/api/v3/chat/completions",
        "response_body": (
            '{"code":"InvalidParameter","message":"invalid max_tokens",'
            '"api_key":"<redacted>","authorization":"<redacted>"}'
        ),
        "cause_chain": "-",
        "traceback": "-",
    }


@pytest.mark.asyncio
async def test_volcengine_stream_uses_unique_default_client_request_ids():
    completions = _FakeStreamingCompletions([], [])
    adapter = VLMProviderAdapter(
        _FakeStreamingVLM(completions),
        "test-model",
        langfuse_client=_DisabledLangfuse(),
    )

    for _ in range(2):
        _ = [
            event
            async for event in adapter.chat_stream(
                messages=[{"role": "user", "content": "hello"}],
            )
        ]

    request_ids = [
        kwargs["extra_headers"][VOLCENGINE_CLIENT_REQUEST_ID_HEADER]
        for kwargs in completions.kwargs
    ]
    assert len(set(request_ids)) == 2
    assert all(value.startswith(f"{VOLCENGINE_CLIENT_REQUEST_ID},") for value in request_ids)


@pytest.mark.asyncio
async def test_volcengine_non_stream_uses_unique_default_client_request_ids(monkeypatch):
    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    vlm = VolcEngineVLM(
        {
            "provider": "volcengine",
            "model": "test-model",
            "api_key": "test-key",
            "max_retries": 0,
        }
    )
    monkeypatch.setattr(
        vlm,
        "get_async_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=_raw_chat_completions(create))),
    )

    for _ in range(2):
        assert await vlm.get_completion_async(messages=[{"role": "user", "content": "hi"}]) == "ok"

    request_ids = [kwargs["extra_headers"][VOLCENGINE_CLIENT_REQUEST_ID_HEADER] for kwargs in calls]
    assert len(set(request_ids)) == 2
    assert all(value.startswith(f"{VOLCENGINE_CLIENT_REQUEST_ID},") for value in request_ids)


def test_volcengine_request_headers_preserve_custom_client_request_id():
    headers = build_volcengine_request_headers(
        {
            "x-client-request-id": "caller-owned-request-id",
            "X-Tenant": "tenant-a",
        }
    )

    assert headers == {
        "x-client-request-id": "caller-owned-request-id",
        "X-Tenant": "tenant-a",
    }


def test_rate_limit_classifier_handles_target_error():
    assert is_retryable_rate_limit_error(
        RuntimeError("Error code: 429 - ModelAccountTpmRateLimitExceeded")
    )
    assert is_retryable_rate_limit_error(
        RuntimeError(
            "Error code: 429 - {'error': {'code': 'ModelAccountTpmRateLimitExceeded', "
            "'message': 'TPM (Tokens Per Minute) limit of the model doubao-seed-2-0-pro "
            "is exceeded. Please try again later Request id: "
            "0217817720969006061aa40146dbf4d117b0497e84060d7ac9102', "
            "'param': '', 'type': 'TooManyRequests'}}, request_id: "
            "202606181641366ORRzhOSo5se81lzpolL"
        )
    )
    assert is_retryable_rate_limit_error(RuntimeError("Error code: 429 - busy"))
    assert not is_retryable_rate_limit_error(RuntimeError("Error code: 401 Unauthorized"))
    assert not is_retryable_rate_limit_error(RuntimeError("trace_id=abc429def unrelated"))
    assert not is_retryable_rate_limit_error(RuntimeError("request_id=abc429def unrelated"))


def test_rate_limit_classifier_handles_structured_sdk_errors():
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(429, request=request)
    exc = ArkRateLimitError(
        "Error code: 429 - rate limited",
        response=response,
        body={
            "code": "ModelAccountTpmRateLimitExceeded",
            "type": "TooManyRequests",
            "message": "TPM limit exceeded",
        },
        request_id="0217817720969006061aa40146dbf4d117b0497e84060d7ac9102",
    )

    assert is_retryable_rate_limit_error(exc)


@pytest.fixture(params=[httpx.ConnectError, httpx.ReadError])
def transport_error_factory(request):
    return lambda: _wrapped_connection_error(request.param("Transport failed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
@pytest.mark.parametrize("jitter_fraction", [0.0, 1.0])
async def test_stream_transport_retry_is_unlimited_capped_and_quiet(
    monkeypatch, with_credentials, jitter_fraction, transport_error_factory
):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    monkeypatch.setattr(
        vlm_adapter.random, "uniform", lambda low, high: low + (high - low) * jitter_fraction
    )
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    # Beyond typical retry budgets and the overflow point of unbounded 2**attempt.
    failures = [transport_error_factory() for _ in range(1600)]
    completions = _FakeStreamingCompletions(failures, [_stream_chunk(content="recovered")])
    adapter = _streaming_adapter(completions, with_credentials)
    messages = [{"role": "user", "content": "same request"}]

    events = [event async for event in adapter.chat_stream(messages=messages)]

    assert completions.calls == 1601
    assert len(delays) == 1600
    multiplier = 0.5 + 0.5 * jitter_fraction
    assert delays[:8] == [value * multiplier for value in (0.5, 1, 2, 4, 8, 16, 30, 30)]
    assert all(delay == 30 * multiplier for delay in delays[6:])
    assert all(call["messages"] == messages for call in completions.kwargs)
    assert [event.type for event in events] == ["content_delta", "response"]
    assert events[-1].response.content == "recovered"
    assert events[-1].response.finish_reason == "stop"
    assert logger.messages == []


@pytest.mark.parametrize(
    "cause_type", [httpx.ConnectError, httpcore.ConnectError, httpx.ReadError, httpcore.ReadError]
)
@pytest.mark.parametrize("chain_attribute", ["__cause__", "__context__"])
def test_stream_transport_classifier_recognizes_wrapped_transport_error(
    cause_type, chain_attribute
):
    cause = cause_type("All connection attempts failed")
    error = _wrapped_connection_error()
    error.__cause__ = None
    setattr(error, chain_attribute, cause)

    assert vlm_adapter._stream_transport_error_types(cause)
    assert vlm_adapter._stream_transport_error_types(error)


def test_stream_transport_classifier_terminates_on_exception_chain_cycle():
    error = RuntimeError("not a connection error")
    error.__cause__ = error
    assert not vlm_adapter._stream_transport_error_types(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        _wrapped_connection_error(httpx.WriteError("Write failed")),
        _wrapped_connection_error(httpx.RemoteProtocolError("Peer disconnected")),
        _wrapped_connection_error(httpx.ReadTimeout("Read deadline exceeded")),
        _wrapped_connection_error(httpx.ConnectTimeout("Connect deadline exceeded")),
        ArkAPIConnectionError(
            request=httpx.Request("POST", "https://ark.example.test"), request_id="unknown-cause"
        ),
        RuntimeError("ConnectError: text alone is not a transport type"),
        RuntimeError("ReadError: text alone is not a transport type"),
        RuntimeError("400 Invalid parameter"),
        RuntimeError("401 Unauthorized"),
        RuntimeError("403 Forbidden"),
    ],
)
async def test_stream_does_not_retry_other_errors(monkeypatch, with_credentials, error):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    completions = _FakeStreamingCompletions([error], [_stream_chunk(content="not reached")])
    adapter = _streaming_adapter(completions, with_credentials)

    events = [event async for event in adapter.chat_stream(messages=[])]

    assert completions.calls == 1
    assert delays == []
    assert len(events) == 1
    assert events[0].response.finish_reason == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
@pytest.mark.parametrize(
    ("delta", "event_type"),
    [
        ({"content": "partial"}, "content_delta"),
        ({"reasoning_content": "thinking"}, "reasoning_delta"),
    ],
)
async def test_stream_does_not_retry_transport_error_after_emission(
    monkeypatch, with_credentials, delta, event_type, transport_error_factory
):
    delays = []

    async def sleep(delay):
        pytest.fail("Must not retry after an event has been emitted")

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    completions = _FakeStreamingCompletions([], [_stream_chunk(**delta), transport_error_factory()])
    adapter = _streaming_adapter(completions, with_credentials)

    events = [event async for event in adapter.chat_stream(messages=[])]

    assert completions.calls == 1
    assert delays == []
    assert [event.type for event in events] == [event_type, "response"]
    assert events[-1].response.finish_reason == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
async def test_stream_retries_transport_error_while_awaiting_first_event(
    monkeypatch, with_credentials, transport_error_factory
):
    delays = []
    completions = _FakeStreamingCompletions([], [transport_error_factory()])

    async def sleep(delay):
        delays.append(delay)
        completions.chunks = [_stream_chunk(content="recovered")]

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    adapter = _streaming_adapter(completions, with_credentials)

    events = [event async for event in adapter.chat_stream(messages=[])]

    assert completions.calls == 2
    assert len(delays) == 1
    assert events[-1].response.content == "recovered"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
async def test_stream_transport_retry_preserves_cancellation(
    monkeypatch, with_credentials, transport_error_factory
):
    sleeping = asyncio.Event()
    never_resume = asyncio.Event()
    emitted = []

    async def sleep(delay):
        sleeping.set()
        await never_resume.wait()

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    completions = _FakeStreamingCompletions([transport_error_factory()], [])
    adapter = _streaming_adapter(completions, with_credentials)

    async def consume():
        async for event in adapter.chat_stream(messages=[]):
            emitted.append(event)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert completions.calls == 1
    assert emitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
async def test_stream_transport_retries_share_backoff_without_changing_rate_limits(
    monkeypatch, with_credentials
):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    monkeypatch.setattr(vlm_adapter.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(vlm_adapter, "rate_limit_retry_delay", lambda attempt: 100 + attempt)
    completions = _FakeStreamingCompletions(
        [
            _wrapped_connection_error(),
            RuntimeError("429 TooManyRequests"),
            _wrapped_connection_error(httpx.ReadError("Read failed")),
            RuntimeError("429 TooManyRequests"),
        ],
        [_stream_chunk(content="recovered")],
    )
    adapter = _streaming_adapter(completions, with_credentials)

    events = [event async for event in adapter.chat_stream(messages=[])]

    assert completions.calls == 5
    assert delays == [0.5, 101, 1.0, 102]
    assert events[-1].response.content == "recovered"


def _diagnostic_records(logger):
    prefix = "[LLM_STREAM_DIAGNOSTIC] "
    return [json.loads(line[len(prefix) :]) for line in logger.warnings if line.startswith(prefix)]


def test_stream_diagnostic_warning_schedule_and_cleanup(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(vlm_adapter.time, "perf_counter", lambda: clock["now"])
    loop = Mock()
    loop.call_later.side_effect = lambda delay, callback: Mock()
    monkeypatch.setattr(vlm_adapter.asyncio, "get_running_loop", lambda: loop)
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    diagnostics = vlm_adapter._ModelCallDiagnostics("session", "model")

    diagnostics.start()
    assert loop.call_later.call_args.args[0] == 60.0
    clock["now"] = 60.0
    loop.call_later.call_args.args[1]()
    assert loop.call_later.call_args.args[0] == 300.0
    clock["now"] = 360.0
    loop.call_later.call_args.args[1]()
    assert [call.args[0] for call in loop.call_later.call_args_list] == [60.0, 300.0, 300.0]
    timer = diagnostics._timer
    diagnostics.finish("success")

    timer.cancel.assert_called_once()
    records = _diagnostic_records(logger)
    assert [record["outcome"] for record in records] == ["pending", "pending", "success"]
    assert [record["elapsed_s"] for record in records] == [60.0, 360.0, 360.0]
    assert len({record["logical_request_id"] for record in records}) == 1


@pytest.mark.asyncio
async def test_fast_success_has_no_diagnostic_log(monkeypatch):
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    adapter = _streaming_adapter(
        _FakeStreamingCompletions([], [_stream_chunk(content="fast")]), False
    )
    events = [event async for event in adapter.chat_stream(messages=[], session_id="fast")]
    assert events[-1].response.content == "fast"
    assert _diagnostic_records(logger) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
async def test_slow_stream_summary_tracks_progress_without_logging_payload(
    monkeypatch, with_credentials
):
    clock = {"now": 0.0}
    monkeypatch.setattr(vlm_adapter.time, "perf_counter", lambda: clock["now"])
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    argument = '{"private_value":"never-log-this-argument"}'
    first = _stream_chunk(
        tool_calls=[
            SimpleNamespace(
                index=0, id="tool-1", function=SimpleNamespace(name="tool", arguments=argument[:12])
            )
        ]
    )
    final = _stream_chunk(
        tool_calls=[SimpleNamespace(index=0, function=SimpleNamespace(arguments=argument[12:]))]
    )
    final.choices[0].finish_reason = "tool_calls"

    class Chunks:
        async def __aiter__(self):
            clock["now"] = 2.0
            yield first
            clock["now"] = 5.0
            yield final
            clock["now"] = 75.0

    completions = _FakeStreamingCompletions([], [])

    async def create(**kwargs):
        completions.kwargs.append(kwargs)
        return Chunks()

    completions.create = create
    adapter = _streaming_adapter(completions, with_credentials)
    events = [
        event
        async for event in adapter.chat_stream(
            messages=[{"role": "user", "content": "never-log-this-prompt"}],
            session_id="case-for-diagnostics",
        )
    ]

    assert events[-1].response.tool_calls[0].arguments == json.loads(argument)
    records = _diagnostic_records(logger)
    assert len(records) == 1
    record = records[0]
    assert record["outcome"] == "success"
    assert record["phase"] == "completed"
    assert record["session_id"] == "case-for-diagnostics"
    assert record["attempt_count"] == 1
    assert record["elapsed_s"] == 75.0
    assert record["first_chunk_after_s"] == 2.0
    assert record["last_chunk_age_s"] == 70.0
    assert record["sdk_chunk_count"] == record["total_sdk_chunk_count"] == 2
    assert record["finish_reason"] == "tool_calls"
    assert record["tool_argument_chars"] == len(argument)
    assert (
        record["client_request_id"]
        == completions.kwargs[0]["extra_headers"][VOLCENGINE_CLIENT_REQUEST_ID_HEADER]
    )
    assert "never-log-this-prompt" not in logger.warnings[0]
    assert "never-log-this-argument" not in logger.warnings[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["waiting_response", "reading_stream", "waiting_stream_end", "backoff"]
)
async def test_live_slow_warning_reports_phase_and_stops_on_cancellation(monkeypatch, phase):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(vlm_adapter, "_STREAM_SLOW_SECONDS", 0.001)
    monkeypatch.setattr(vlm_adapter, "_STREAM_PROGRESS_INTERVAL_SECONDS", 0.01)
    warned = asyncio.Event()
    block = asyncio.Event()

    class Logger(_CaptureLogger):
        def warning(self, message, *args):
            super().warning(message, *args)
            if _diagnostic_records(self)[-1]["outcome"] == "pending":
                warned.set()

    logger = Logger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    completions = _FakeStreamingCompletions([], [])

    class PausedChunks:
        async def __aiter__(self):
            if phase == "waiting_stream_end":
                chunk = _stream_chunk(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="tool-1",
                            function=SimpleNamespace(name="tool", arguments='{"id":1}'),
                        )
                    ]
                )
                chunk.choices[0].finish_reason = "tool_calls"
                yield chunk
            await block.wait()

    async def create(**kwargs):
        if phase == "waiting_response":
            await block.wait()
        if phase == "backoff":
            raise _wrapped_connection_error()
        return PausedChunks()

    async def sleep(delay):
        await block.wait()

    completions.create = create
    if phase == "backoff":
        monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    adapter = _streaming_adapter(completions, False)
    events = []

    async def consume():
        async for event in adapter.chat_stream(messages=[], session_id="pending-case"):
            events.append(event)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(warned.wait(), timeout=1)
        pending = _diagnostic_records(logger)[0]
        assert pending["phase"] == phase
        assert not task.done()
        if phase == "waiting_stream_end":
            assert pending["finish_reason"] == "tool_calls"
            assert pending["tool_argument_chars"] == len('{"id":1}')
            assert pending["sdk_chunk_count"] == 1
        elif phase == "backoff":
            assert pending["retry_counts"]["connect"] == 1
            assert pending["retry_count"] == 1
            assert pending["backoff_s"] > 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    await real_sleep(0.03)
    records = _diagnostic_records(logger)
    assert records[-1]["outcome"] == "cancelled"
    assert records[-1]["phase"] == phase
    assert sum(record["outcome"] == "cancelled" for record in records) == 1
    assert records[-1]["logical_request_id"] == records[0]["logical_request_id"]
    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_credentials", [False, True])
async def test_diagnostics_count_retries_and_reset_current_attempt_progress(
    monkeypatch, with_credentials
):
    clock = {"now": 0.0}
    monkeypatch.setattr(vlm_adapter.time, "perf_counter", lambda: clock["now"])
    monkeypatch.setattr(vlm_adapter.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(vlm_adapter, "rate_limit_retry_delay", lambda attempt: 5.0)
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)

    async def sleep(delay):
        clock["now"] += delay

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    partial = _stream_chunk(
        tool_calls=[
            SimpleNamespace(index=0, function=SimpleNamespace(name="tool", arguments='{"old":'))
        ]
    )
    completions = _FakeStreamingCompletions([], [])

    async def create(**kwargs):
        completions.kwargs.append(kwargs)
        call = len(completions.kwargs)
        if call == 1:
            raise _wrapped_connection_error()
        if call == 2:
            return _AsyncChunks([partial, _wrapped_connection_error(httpx.ReadError("read"))])
        if call == 3:
            raise RuntimeError("429 TooManyRequests")
        clock["now"] = 75.0
        return _AsyncChunks([_stream_chunk(content="recovered")])

    completions.create = create
    adapter = _streaming_adapter(completions, with_credentials)
    events = [event async for event in adapter.chat_stream(messages=[])]

    assert events[-1].response.content == "recovered"
    records = _diagnostic_records(logger)
    assert len(records) == 1
    record = records[0]
    assert record["attempt_count"] == 4
    assert record["retry_count"] == 3
    assert record["retry_counts"] == {"connect": 1, "read": 1, "rate_limit": 1}
    assert record["backoff_s"] == 6.5
    assert record["sdk_chunk_count"] == 1
    assert record["total_sdk_chunk_count"] == 2
    assert record["tool_argument_chars"] == 0
    assert record["finish_reason"] is None
    assert (
        record["client_request_id"]
        == completions.kwargs[-1]["extra_headers"][VOLCENGINE_CLIENT_REQUEST_ID_HEADER]
    )
    assert (
        len({kw["extra_headers"][VOLCENGINE_CLIENT_REQUEST_ID_HEADER] for kw in completions.kwargs})
        == 4
    )
    assert sum("rate limited" in warning for warning in logger.warnings) == 1
    assert (
        len(logger.warnings) == 2
    )  # Existing 429 warning plus one final diagnostic, not every retry.


@pytest.mark.asyncio
async def test_failed_concurrent_streams_have_isolated_diagnostics(monkeypatch):
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    both_waiting = asyncio.Event()
    waiting_count = 0
    arguments = {"one": '{"id":1}', "two": '{"id":222,"other":333}'}

    async def create(**kwargs):
        session = kwargs["messages"][0]["content"]

        async def chunks():
            nonlocal waiting_count
            yield _stream_chunk(
                tool_calls=[
                    SimpleNamespace(
                        index=0,
                        function=SimpleNamespace(name="tool", arguments=arguments[session]),
                    )
                ]
            )
            waiting_count += 1
            if waiting_count == 2:
                both_waiting.set()
            await both_waiting.wait()
            raise httpx.RemoteProtocolError("private error body must not enter diagnostic")

        return chunks()

    completions = _FakeStreamingCompletions([], [])
    completions.create = create
    adapter = _streaming_adapter(completions, False)

    async def consume(session):
        return [
            event
            async for event in adapter.chat_stream(
                messages=[{"role": "user", "content": session}], session_id=session
            )
        ]

    results = await asyncio.wait_for(asyncio.gather(consume("one"), consume("two")), timeout=1)
    assert all(events[-1].response.finish_reason == "error" for events in results)
    records = _diagnostic_records(logger)
    assert len(records) == 2
    assert len({record["logical_request_id"] for record in records}) == 2
    for record in records:
        assert record["outcome"] == "failed"
        assert record["phase"] == "reading_stream"
        assert record["attempt_count"] == 1
        assert record["retry_count"] == 0
        assert record["sdk_chunk_count"] == 1
        assert record["last_error_type"] == "RemoteProtocolError"
        assert record["tool_argument_chars"] == len(arguments[record["session_id"]])
    assert all("private error body" not in warning for warning in logger.warnings)


def test_diagnostic_ids_are_redacted(monkeypatch):
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    diagnostics = vlm_adapter._ModelCallDiagnostics("api_key=must-not-leak", "model")
    diagnostics.begin_attempt("model", {"x-client-request-id": "Bearer must-not-leak-either"})
    diagnostics.server_request_id = "api_key=must-not-leak-server"
    diagnostics.log("failed")
    assert "must-not-leak" not in logger.warnings[0]
    assert "redacted" in logger.warnings[0]


def test_transport_retry_diagnostics_preserve_mixed_credential_causes_with_cycles():
    error = AllCredentialsFailedError(
        [
            ("one", "transient", httpx.ConnectError("connect"), 0),
            ("two", "transient", httpx.ReadError("read"), 1),
        ]
    )
    error.errors.append(("cycle", "transient", error, 2))
    assert vlm_adapter._stream_transport_error_types(error) == {"connect", "read"}


def _service_error(code="InternalServiceError"):
    return ArkAPIError(
        message="provider failed",
        request=httpx.Request("POST", "https://offline.invalid/chat/completions"),
        body={
            "code": code,
            "type": "TooManyRequests" if code == "ServerOverloaded" else "InternalServerError",
        },
        request_id="service-test",
    )


def _timeout_error():
    error = ArkAPITimeoutError(
        request=httpx.Request("POST", "https://offline.invalid/chat/completions"),
        request_id="offline-timeout",
    )
    error.__cause__ = httpx.ReadTimeout("")
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["connect", "read", "service", "rate_limit", "timeout"])
async def test_nonstream_chat_retries_transient_errors_without_limit(monkeypatch, kind):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    monkeypatch.setattr(vlm_adapter.random, "uniform", lambda low, high: high)
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    factories = {
        "connect": _wrapped_connection_error,
        "read": lambda: _wrapped_connection_error(httpx.ReadError("read")),
        "service": _service_error,
        "rate_limit": lambda: _service_error("ServerOverloaded"),
        "timeout": _timeout_error,
    }
    backend = _FakeVLM([factories[kind]() for _ in range(1200)], result="recovered")
    adapter = VLMProviderAdapter(backend, "test", langfuse_client=_DisabledLangfuse())

    response = await adapter.chat(messages=[{"role": "user", "content": "same request"}])

    assert response.content == "recovered"
    assert backend.calls == 1201
    if kind == "rate_limit":
        assert delays[:7] == [6, 12, 24, 48, 96, 144, 144]
        assert all(delay == 144 for delay in delays[5:])
    else:
        assert delays[:8] == [0.5, 1, 2, 4, 8, 16, 30, 30]
        assert all(delay == 30 for delay in delays[6:])
    assert logger.messages == []
    assert logger.warnings == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["service", "timeout"])
async def test_nonstream_chat_cancellation_interrupts_backoff(monkeypatch, kind):
    async def sleep(delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    backend = _FakeVLM([_timeout_error() if kind == "timeout" else _service_error()])
    adapter = VLMProviderAdapter(backend, "test", langfuse_client=_DisabledLangfuse())
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    with pytest.raises(asyncio.CancelledError):
        await adapter.chat(messages=[])
    assert backend.calls == 1
    record = json.loads(logger.warnings[-1].split("[LLM_CHAT_DIAGNOSTIC] ", 1)[1])
    assert record["outcome"] == "cancelled"
    assert record["retry_counts"][kind] == 1
    assert record["attempt_scope"] == "provider_call"
    assert "sdk_chunk_count" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404])
@pytest.mark.parametrize("code", ["InvalidRequest", "InternalServiceError"])
async def test_nonstream_chat_does_not_retry_permanent_errors(monkeypatch, status, code):
    class StatusError(Exception):
        status_code = status
        body = {"code": code}

    async def sleep(delay):
        pytest.fail("Permanent errors must not be retried")

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    error = StatusError("permanent error")
    error.__cause__ = _timeout_error()
    backend = _FakeVLM([error])
    adapter = VLMProviderAdapter(backend, "test", langfuse_client=_DisabledLangfuse())
    response = await adapter.chat(messages=[])
    assert response.finish_reason == "error"
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_volcengine_adapter_chat_uses_nonstream_requests_and_preserves_tools(monkeypatch):
    calls = []

    async def sleep(delay):
        return None

    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise _service_error("ServerOverloaded")
        if len(calls) == 2:
            raise _service_error()
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="done-call",
                                function=SimpleNamespace(name="done", arguments="{}"),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    backend = VolcEngineVLM({"model": "test", "api_key": "fake", "max_retries": 0})
    monkeypatch.setattr(
        backend,
        "get_async_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=_raw_chat_completions(create))),
    )
    adapter = VLMProviderAdapter(backend, "test", langfuse_client=_DisabledLangfuse())
    messages = [{"role": "user", "content": "do the task"}]
    tools = [{"type": "function", "function": {"name": "done", "parameters": {"type": "object"}}}]

    response = await adapter.chat(messages=messages, tools=tools)

    assert len(calls) == 3
    assert all(not call.get("stream", False) for call in calls)
    assert all("stream_options" not in call for call in calls)
    assert all(call["messages"] == messages and call["tools"] == tools for call in calls)
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "done"
    assert response.tool_calls[0].arguments == {}


@pytest.mark.asyncio
async def test_nonstream_slow_diagnostics_count_provider_calls_not_http_attempts(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(vlm_adapter.time, "perf_counter", lambda: clock["now"])
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    backend = _FakeVLM([])

    async def completion(**kwargs):
        clock["now"] = 75.0
        return "done"

    backend.get_completion_async = completion
    response = await VLMProviderAdapter(backend, "test", langfuse_client=_DisabledLangfuse()).chat(
        messages=[], session_id="quiet-session"
    )
    assert response.content == "done"
    records = [json.loads(line.split("[LLM_CHAT_DIAGNOSTIC] ", 1)[1]) for line in logger.warnings]
    assert len(records) == 1
    assert records[0]["outcome"] == "success"
    assert records[0]["elapsed_s"] == 75
    assert records[0]["attempt_count"] == 1
    assert records[0]["attempt_scope"] == "provider_call"
    assert records[0]["session_id"] == "quiet-session"
    assert "sdk_chunk_count" not in records[0]


def _chat_diagnostic_records(logger):
    prefix = "[LLM_CHAT_DIAGNOSTIC] "
    return [json.loads(line[len(prefix) :]) for line in logger.warnings if line.startswith(prefix)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools",
    [
        None,
        [],
        [
            {
                "type": "function",
                "function": {"name": "done", "parameters": {"type": "object", "properties": {}}},
            }
        ],
    ],
)
@pytest.mark.parametrize("reasoning_tokens", [None, 0, 900])
@pytest.mark.parametrize("server_id", [None, "upstream-request-123"])
async def test_slow_chat_logs_actual_usage_and_ark_headers_without_reasoning_text(
    monkeypatch, tools, reasoning_tokens, server_id
):
    from volcenginesdkarkruntime import AsyncArk

    clock = {"now": 0.0}
    monkeypatch.setattr(vlm_adapter.time, "perf_counter", lambda: clock["now"])
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    requests = []
    visible = "已完成"
    text = visible if tools else f"<think>PRIVATE_IN_CONTENT</think>  {visible}  "

    async def handle(request):
        requests.append(request)
        clock["now"] = 75.0
        usage = {"prompt_tokens": 120, "completion_tokens": 960, "total_tokens": 1080}
        if reasoning_tokens is not None:
            usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
        headers = {"x-client-request-id": request.headers["x-client-request-id"]}
        if server_id is not None:
            headers["x-request-id"] = server_id
        return httpx.Response(
            200,
            headers=headers,
            json={
                "id": "completion-id-is-not-request-id",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": text,
                            "reasoning_content": "PRIVATE_REASONING_MUST_NOT_BE_LOGGED",
                        },
                    }
                ],
                "usage": usage,
            },
        )

    client = AsyncArk(
        api_key="private-api-key",
        base_url="https://ark.test/api/v3",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        max_retries=0,
    )
    backend = VolcEngineVLM(
        {"model": "test-model", "thinking": True, "max_tokens": 2048, "temperature": 0.2}
    )
    monkeypatch.setattr(backend, "get_async_client", lambda: client)
    try:
        response = await VLMProviderAdapter(
            backend, "test-model", langfuse_client=_DisabledLangfuse()
        ).chat(
            messages=[{"role": "user", "content": "private user prompt"}],
            tools=tools,
            session_id="slow-case",
        )
    finally:
        await client.close()
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["thinking"] == {"type": "enabled"}
    assert body["max_tokens"] == 2048
    assert body["temperature"] == 0.2
    assert not body.get("stream")
    assert body.get("tools") == (tools or None)
    assert response.content == visible
    assert response.reasoning_content is None  # Do not newly forward hidden thinking to the Agent.
    assert response.usage.get("reasoning_tokens") == reasoning_tokens
    (record,) = _chat_diagnostic_records(logger)
    assert record["prompt_tokens"] == 120
    assert record["completion_tokens"] == 960
    assert record["reasoning_tokens"] == reasoning_tokens
    assert record["response_chars"] == len(visible)
    assert record["client_request_id"] == requests[0].headers["x-client-request-id"]
    assert record["server_request_id"] == server_id
    assert record["elapsed_s"] == 75
    assert record["attempt_count"] == 1
    assert record["retry_count"] == 0
    assert record["attempt_scope"] == "provider_call"
    assert "completion-id-is-not-request-id" not in str(record)
    for secret in [
        "PRIVATE_REASONING",
        "PRIVATE_IN_CONTENT",
        "private user prompt",
        "private-api-key",
    ]:
        assert secret not in str(logger.warnings)


def test_chat_diagnostics_distinguish_unknown_zero_and_clear_new_attempt(monkeypatch):
    from vikingbot.providers.base import LLMResponse

    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    diag = vlm_adapter._ModelCallDiagnostics("case", "model", operation="chat")
    diag.begin_attempt("model", None)
    diag.log("pending")
    record = _chat_diagnostic_records(logger)[-1]
    for key in [
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "response_chars",
        "server_request_id",
    ]:
        assert record[key] is None
    diag.record_response(
        LLMResponse(
            content=None, usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
        )
    )
    diag.server_request_id = "previous-server"
    diag.log("success")
    record = _chat_diagnostic_records(logger)[-1]
    assert all(
        record[key] == 0
        for key in ["prompt_tokens", "completion_tokens", "reasoning_tokens", "response_chars"]
    )
    diag.begin_attempt("model", {"x-client-request-id": "next-client"})
    diag.log("pending")
    record = _chat_diagnostic_records(logger)[-1]
    assert record["client_request_id"] == "next-client"
    for key in [
        "prompt_tokens",
        "completion_tokens",
        "reasoning_tokens",
        "response_chars",
        "server_request_id",
    ]:
        assert record[key] is None


@pytest.mark.asyncio
async def test_slow_failed_chat_keeps_upstream_request_id_and_unknown_usage(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(vlm_adapter.time, "perf_counter", lambda: clock["now"])
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    request = httpx.Request(
        "POST", "https://ark.test/chat", headers={"x-client-request-id": "client-1"}
    )
    raw = httpx.Response(400, request=request, headers={"x-request-id": "server-1"})

    async def fail(**kwargs):
        clock["now"] = 75.0
        raise httpx.HTTPStatusError("bad request", request=request, response=raw)

    backend = _FakeVLM([])
    backend.get_completion_async = fail
    response = await VLMProviderAdapter(backend, "test", langfuse_client=_DisabledLangfuse()).chat(
        messages=[]
    )
    assert response.finish_reason == "error"
    (record,) = _chat_diagnostic_records(logger)
    assert record["client_request_id"] == "client-1"
    assert record["server_request_id"] == "server-1"
    assert record["prompt_tokens"] is None
    assert record["completion_tokens"] is None
    assert record["reasoning_tokens"] is None
    assert record["response_chars"] is None


@pytest.mark.parametrize("reasoning", [None, 0, 123])
def test_stream_usage_preserves_reported_zero_without_inventing_missing_reasoning(reasoning):
    raw = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    if reasoning is not None:
        raw["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    parsed = VLMProviderAdapter._parse_usage(raw)
    assert parsed.get("reasoning_tokens") == reasoning
    assert ("reasoning_tokens" in parsed) == (reasoning is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {}, "unrecognized metadata must not be logged"])
async def test_slow_diagnostics_do_not_break_completion_on_missing_usage(monkeypatch, usage):
    from openviking.models.vlm.base import VLMResponse

    clock = {"now": 0.0}
    monkeypatch.setattr(vlm_adapter.time, "perf_counter", lambda: clock["now"])
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    backend = _FakeVLM([])

    async def completion(**kwargs):
        clock["now"] = 75.0
        return VLMResponse(content="ok", usage=usage)

    backend.get_completion_async = completion
    response = await VLMProviderAdapter(backend, "test", langfuse_client=_DisabledLangfuse()).chat(
        messages=[]
    )
    assert response.content == "ok"
    (record,) = _chat_diagnostic_records(logger)
    assert record["outcome"] == "success"
    assert record["response_chars"] == 2
    assert all(
        record[key] is None for key in ["prompt_tokens", "completion_tokens", "reasoning_tokens"]
    )
    assert "unrecognized metadata" not in str(record)


@pytest.mark.asyncio
async def test_fast_nonstream_success_does_not_add_diagnostic_logs(monkeypatch):
    from openviking.models.vlm.base import VLMResponse

    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    backend = _FakeVLM(
        [],
        result=VLMResponse(
            content="ok", usage={"prompt_tokens": 2, "completion_tokens": 1, "reasoning_tokens": 0}
        ),
    )
    response = await VLMProviderAdapter(backend, "test", langfuse_client=_DisabledLangfuse()).chat(
        messages=[]
    )
    assert response.content == "ok"
    assert _chat_diagnostic_records(logger) == []


def test_stream_usage_does_not_invent_missing_prompt_or_completion_counts():
    assert VLMProviderAdapter._parse_usage({"total_tokens": 3}) == {"total_tokens": 3}


@pytest.mark.asyncio
async def test_real_ark_timeout_retries_through_vlm_and_adapter(monkeypatch):
    from volcenginesdkarkruntime import AsyncArk

    requests = []
    delays = []

    async def handle(request):
        requests.append(request)
        if len(requests) <= 5:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(
            200,
            json={
                "id": "offline-response",
                "model": "offline-model",
                "object": "chat.completion",
                "created": 0,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "recovered"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(vlm_adapter.asyncio, "sleep", sleep)
    monkeypatch.setattr(vlm_adapter.random, "uniform", lambda low, high: high)
    monkeypatch.setattr(vlm_adapter, "_STREAM_SLOW_SECONDS", 0)
    logger = _CaptureLogger()
    monkeypatch.setattr(vlm_adapter, "logger", logger)
    async with AsyncArk(
        api_key="offline-key",
        base_url="https://offline.invalid",
        timeout=600,
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    ) as client:
        backend = VolcEngineVLM(
            {"model": "offline-model", "api_key": "offline-key", "timeout": 600, "max_retries": 1}
        )
        monkeypatch.setattr(backend, "get_async_client", lambda: client)
        adapter = VLMProviderAdapter(backend, "offline-model", langfuse_client=_DisabledLangfuse())
        response = await adapter.chat(
            messages=[{"role": "user", "content": "retry unchanged input"}]
        )

    assert response.content == "recovered"
    assert len(requests) == 6
    assert all(request.content == requests[0].content for request in requests)
    assert all(request.extensions["timeout"]["read"] == 600 for request in requests)
    assert delays == [1, 0.5, 1, 1, 1]
    record = _chat_diagnostic_records(logger)[-1]
    assert record["outcome"] == "success"
    assert record["attempt_count"] == 3  # Each provider call gets one inner retry.
    assert record["retry_counts"]["timeout"] == 2


def test_chat_retry_detects_timeout_in_credential_aggregate_with_cycle():
    timeout = _timeout_error()
    error = AllCredentialsFailedError([("credential", "transient", timeout, 0)])
    timeout.__context__ = error
    assert vlm_adapter._chat_retry_error_types(error) == {"timeout"}
