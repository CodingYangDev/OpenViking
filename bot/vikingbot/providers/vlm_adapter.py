"""VLM-to-LLMProvider adapter.

Wraps openviking VLM backends (VLMBase) to implement the vikingbot
LLMProvider interface, so that bot.agents.provider / bot.agents.model
configuration semantics are consistent with openviking server's vlm section.
"""

import asyncio
import json
import random
import re
import time
import traceback
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import httpcore
import httpx
from loguru import logger

from openviking.models.vlm.backends.volcengine_vlm import build_volcengine_request_headers
from openviking.utils.exceptions import AllCredentialsFailedError
from openviking.utils.model_retry import (
    is_retryable_rate_limit_error,
    is_retryable_timeout_error,
    rate_limit_retry_delay,
)
from openviking.utils.multimodal import redact_image_data_urls
from vikingbot.integrations.langfuse import LangfuseClient
from vikingbot.providers.base import (
    LLMProvider,
    LLMResponse,
    LLMStreamEvent,
    ToolCallRequest,
    build_stream_response,
    merge_stream_tool_call_delta,
    stream_delta_value,
)
from vikingbot.utils.tracing import get_current_response_id

_STREAM_SLOW_SECONDS = 60.0
_STREAM_PROGRESS_INTERVAL_SECONDS = 300.0

_MAX_ERROR_LOG_VALUE_CHARS = 4096
_MAX_ERROR_LOG_ITEMS = 50
_SENSITIVE_ERROR_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "credential",
        "credentials",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "session_token",
        "token",
    }
)
_SENSITIVE_ERROR_KEY_SUFFIXES = (
    "api_key",
    "password",
    "secret",
    "token",
)
_SENSITIVE_ERROR_KV_RE = re.compile(
    r"(?i)((?:api[_-]?key|authorization|credential|password|secret|token)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s,;\]\}\"']+"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)(bearer\s+)[^\s,;\]\}\"']+")
_API_KEY_TOKEN_RE = re.compile(r"\bsk-[a-zA-Z0-9._-]{8,}")
_SERVER_REQUEST_ID_HEADERS = ("x-request-id",)
_SERVER_TRACE_ID_HEADERS = (
    "x-trace-id",
    "trace-id",
    "x-tt-logid",
    "x-log-id",
)


def _safe_exception_attribute(error: BaseException, name: str) -> Any:
    try:
        return getattr(error, name, None)
    except BaseException:
        return None


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = _safe_exception_attribute(current, "__cause__") or _safe_exception_attribute(
            current, "__context__"
        )
    return chain


def _model_exception_chain(error: BaseException) -> list[BaseException]:
    pending = _exception_chain(error)
    seen: set[int] = set()
    chain: list[BaseException] = []
    while pending:
        exc = pending.pop()
        if id(exc) in seen:
            continue
        seen.add(id(exc))
        chain.append(exc)
        if isinstance(exc, AllCredentialsFailedError):
            for _, _, cause, _ in exc.errors:
                pending.extend(_exception_chain(cause))
    return chain


def _stream_transport_error_types(error: BaseException) -> set[str]:
    """Recognize the existing retryable connect/read causes, including failover errors."""
    kinds: set[str] = set()
    for exc in _model_exception_chain(error):
        if isinstance(exc, (httpx.ConnectError, httpcore.ConnectError)):
            kinds.add("connect")
        elif isinstance(exc, (httpx.ReadError, httpcore.ReadError)):
            kinds.add("read")
    return kinds


def _chat_retry_error_types(error: BaseException) -> set[str]:
    """Allow known transient failures when no partial response has been delivered."""
    status = _safe_exception_attribute(error, "status_code")
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        return set()
    kinds = _stream_transport_error_types(error)
    for exc in _model_exception_chain(error):
        if is_retryable_rate_limit_error(exc):
            kinds.add("rate_limit")
        if is_retryable_timeout_error(exc):
            kinds.add("timeout")
        body = _safe_exception_attribute(exc, "body")
        details = body.get("error", body) if isinstance(body, dict) else {}
        if not isinstance(details, dict):
            details = {}
        codes = (
            _safe_exception_attribute(exc, "code"),
            _safe_exception_attribute(exc, "type"),
            details.get("code"),
            details.get("type"),
        )
        if _safe_exception_attribute(exc, "status_code") == 500 or any(
            value in ("InternalServiceError", "InternalServerError") for value in codes
        ):
            kinds.add("service")
    return kinds


def _redact_error_text(value: str) -> str:
    value = redact_image_data_urls(value)
    value = _SENSITIVE_ERROR_KV_RE.sub(lambda match: f"{match.group(1)}<redacted>", value)
    value = _BEARER_TOKEN_RE.sub(lambda match: f"{match.group(1)}<redacted>", value)
    value = _API_KEY_TOKEN_RE.sub("<redacted>", value)
    if len(value) > _MAX_ERROR_LOG_VALUE_CHARS:
        return value[:_MAX_ERROR_LOG_VALUE_CHARS] + "...[truncated]"
    return value


def _sanitize_error_value(value: Any, *, depth: int = 0) -> Any:
    """Prepare an upstream error body for logs without exposing credentials or huge values."""
    if depth >= 5:
        return "<max-depth>"
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ERROR_LOG_ITEMS:
                sanitized["<truncated>"] = f"{len(value) - _MAX_ERROR_LOG_ITEMS} more items"
                break
            key_text = str(key)
            normalized_key = key_text.lower().replace("-", "_")
            if normalized_key in _SENSITIVE_ERROR_KEYS or any(
                normalized_key.endswith(f"_{suffix}") for suffix in _SENSITIVE_ERROR_KEY_SUFFIXES
            ):
                sanitized[key_text] = "<redacted>"
            else:
                sanitized[key_text] = _sanitize_error_value(item, depth=depth + 1)
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        sanitized_items = [
            _sanitize_error_value(item, depth=depth + 1) for item in items[:_MAX_ERROR_LOG_ITEMS]
        ]
        if len(items) > _MAX_ERROR_LOG_ITEMS:
            sanitized_items.append(f"<{len(items) - _MAX_ERROR_LOG_ITEMS} more items>")
        return sanitized_items
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return _redact_error_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        return _redact_error_text(str(value))
    except BaseException:
        return f"<{type(value).__name__}>"


def _render_error_log_value(value: Any) -> str:
    if value is None:
        return "-"
    sanitized = _sanitize_error_value(value)
    try:
        rendered = json.dumps(sanitized, ensure_ascii=False, default=str, separators=(",", ":"))
    except BaseException:
        rendered = _redact_error_text(str(sanitized))
    if len(rendered) > _MAX_ERROR_LOG_VALUE_CHARS:
        return rendered[:_MAX_ERROR_LOG_VALUE_CHARS] + "...[truncated]"
    return rendered


def _first_exception_attribute(chain: list[BaseException], name: str) -> Any:
    for item in chain:
        value = _safe_exception_attribute(item, name)
        if value is not None:
            return value
    return None


def _first_header_value(headers: Any, names: tuple[str, ...]) -> str | None:
    if headers is None:
        return None
    for name in names:
        try:
            value = headers.get(name)
        except BaseException:
            value = None
        if value:
            return _redact_error_text(str(value))
    return None


def _exception_log_details(error: BaseException) -> dict[str, str]:
    """Extract Ark/OpenAI-compatible exception details for one structured log line."""
    chain = _exception_chain(error)
    response = _first_exception_attribute(chain, "response")
    request = _first_exception_attribute(chain, "request")
    if request is None and response is not None:
        request = _safe_exception_attribute(response, "request")

    status = _first_exception_attribute(chain, "status_code")
    if status is None and response is not None:
        status = _safe_exception_attribute(response, "status_code")

    body = _first_exception_attribute(chain, "body")
    if body is None and response is not None:
        body = _safe_exception_attribute(response, "text")

    response_headers = (
        _safe_exception_attribute(response, "headers") if response is not None else None
    )
    request_headers = _safe_exception_attribute(request, "headers") if request is not None else None
    client_request_id = _first_exception_attribute(chain, "request_id")
    if client_request_id is None:
        client_request_id = _first_header_value(request_headers, ("x-client-request-id",))

    method = _safe_exception_attribute(request, "method") if request is not None else None
    url = _safe_exception_attribute(request, "url") if request is not None else None
    # Query strings can contain credentials. The Ark API endpoint path is sufficient for diagnosis.
    safe_url = str(url).split("?", 1)[0] if url is not None else "-"

    causes = [f"{type(item).__name__}: {_redact_error_text(str(item))}" for item in chain[1:]]
    stack_frames: list[str] = []
    for item in chain:
        item_traceback = _safe_exception_attribute(item, "__traceback__")
        if item_traceback is None:
            continue
        for frame in traceback.extract_tb(item_traceback)[-12:]:
            stack_frames.append(
                f"{type(item).__name__}@{frame.filename}:{frame.lineno} in {frame.name}"
            )
    return {
        "error_type": type(error).__name__,
        "status": str(status) if status is not None else "-",
        "code": _render_error_log_value(_first_exception_attribute(chain, "code")),
        "client_request_id": _render_error_log_value(client_request_id),
        "server_request_id": _first_header_value(response_headers, _SERVER_REQUEST_ID_HEADERS)
        or "-",
        "server_trace_id": _first_header_value(response_headers, _SERVER_TRACE_ID_HEADERS) or "-",
        "method": str(method) if method is not None else "-",
        "url": safe_url,
        "response_body": _render_error_log_value(body),
        "cause_chain": " <- ".join(causes) if causes else "-",
        "traceback": " <- ".join(stack_frames) if stack_frames else "-",
    }


@dataclass(slots=True)
class _ModelCallDiagnostics:
    """Request-local metadata only; never retain prompts, chunks, or argument contents."""

    session_id: str | None
    model: str
    operation: str = "chat_stream"
    logical_request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: float = field(default_factory=lambda: time.perf_counter())
    phase: str = "preparing"
    attempt_count: int = 0
    attempt_started_at: float | None = None
    client_request_id: str | None = None
    server_request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    response_chars: int | None = None
    response_received_at: float | None = None
    first_chunk_at: float | None = None
    last_chunk_at: float | None = None
    sdk_chunk_count: int = 0
    total_sdk_chunk_count: int = 0
    finish_reason: str | None = None
    tool_argument_chars: int = 0
    retry_count: int = 0
    retry_counts: dict[str, int] = field(
        default_factory=lambda: {"connect": 0, "read": 0, "rate_limit": 0}
    )
    backoff_seconds: float = 0.0
    backoff_started_at: float | None = None
    last_error_type: str | None = None
    _timer: asyncio.TimerHandle | None = None

    def start(self) -> None:
        self._timer = asyncio.get_running_loop().call_later(
            _STREAM_SLOW_SECONDS, self._report_pending
        )

    def _report_pending(self) -> None:
        self.log("pending")
        self._timer = asyncio.get_running_loop().call_later(
            _STREAM_PROGRESS_INTERVAL_SECONDS, self._report_pending
        )

    def finish(self, outcome: str) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if outcome != "success" or time.perf_counter() - self.started_at >= _STREAM_SLOW_SECONDS:
            self.log(outcome)

    def begin_attempt(self, model: str, headers: Any) -> None:
        self.attempt_count += 1
        self.attempt_started_at = time.perf_counter()
        self.model = model
        self.phase = "waiting_response"
        self.client_request_id = _first_header_value(
            httpx.Headers(headers or {}), ("x-client-request-id",)
        )
        self.server_request_id = None
        self.prompt_tokens = None
        self.completion_tokens = None
        self.reasoning_tokens = None
        self.response_chars = None
        self.response_received_at = None
        self.first_chunk_at = None
        self.last_chunk_at = None
        self.sdk_chunk_count = 0
        self.finish_reason = None
        self.tool_argument_chars = 0

    def record_response(self, response: LLMResponse) -> None:
        usage = response.usage if isinstance(response.usage, dict) else {}
        for name in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
            value = usage.get(name)
            # Missing usage is unknown, not zero; do not estimate or log arbitrary values.
            setattr(self, name, value if type(value) is int and value >= 0 else None)
        content = response.content
        self.response_chars = (
            len(content.strip()) if isinstance(content, str) else 0 if content is None else None
        )

    def received_chunk(self) -> None:
        now = time.perf_counter()
        if self.first_chunk_at is None:
            self.first_chunk_at = now
        self.last_chunk_at = now
        self.sdk_chunk_count += 1
        self.total_sdk_chunk_count += 1

    def log(self, outcome: str) -> None:
        now = time.perf_counter()
        attempt_start = self.attempt_started_at
        backoff = self.backoff_seconds
        if self.backoff_started_at is not None:
            backoff += now - self.backoff_started_at
        data = {
            "outcome": outcome,
            "session_id": self.session_id,
            "logical_request_id": self.logical_request_id,
            "client_request_id": self.client_request_id,
            "server_request_id": self.server_request_id,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "response_chars": self.response_chars,
            "model": self.model,
            "phase": self.phase,
            "elapsed_s": round(now - self.started_at, 3),
            "attempt_count": self.attempt_count,
            "attempt_elapsed_s": (
                round(now - attempt_start, 3) if attempt_start is not None else None
            ),
            "response_after_s": (
                round(self.response_received_at - attempt_start, 3)
                if self.response_received_at is not None and attempt_start is not None
                else None
            ),
            "first_chunk_after_s": (
                round(self.first_chunk_at - attempt_start, 3)
                if self.first_chunk_at is not None and attempt_start is not None
                else None
            ),
            "last_chunk_age_s": (
                round(now - self.last_chunk_at, 3) if self.last_chunk_at is not None else None
            ),
            "sdk_chunk_count": self.sdk_chunk_count,
            "total_sdk_chunk_count": self.total_sdk_chunk_count,
            "finish_reason": self.finish_reason,
            "tool_argument_chars": self.tool_argument_chars,
            "retry_count": self.retry_count,
            # Counts are retry cycles containing each cause; mixed credential causes can overlap.
            "retry_counts": self.retry_counts,
            "backoff_s": round(backoff, 3),
            "last_error_type": self.last_error_type,
        }
        if self.operation == "chat":
            # A provider call may have its own finite SDK/backend retries. Do not
            # label this count as HTTP attempts or invent streaming progress.
            data["attempt_scope"] = "provider_call"
            for key in (
                "first_chunk_after_s",
                "last_chunk_age_s",
                "sdk_chunk_count",
                "total_sdk_chunk_count",
                "tool_argument_chars",
            ):
                data.pop(key)
            logger.warning("[LLM_CHAT_DIAGNOSTIC] {}", _render_error_log_value(data))
        else:
            logger.warning("[LLM_STREAM_DIAGNOSTIC] {}", _render_error_log_value(data))


class VLMProviderAdapter(LLMProvider):
    """Adapter that wraps an openviking VLMBase instance as an LLMProvider.

    When bot.agents.provider is explicitly set, _make_provider() creates the
    appropriate VLM backend via VLMFactory.create() and wraps it with this
    adapter.  The VLM backend handles model name resolution internally (e.g.
    VolcEngineVLM passes the model verbatim, LiteLLMVLMProvider auto-detects
    the provider from model name keywords), so no manual prefixing is needed.
    """

    def __init__(
        self,
        vlm_instance,  # VLMBase
        default_model: str,
        langfuse_client: LangfuseClient | None = None,
    ):
        super().__init__(api_key=None, api_base=None)
        self._vlm = vlm_instance
        self._default_model = default_model
        self._langfuse = langfuse_client or LangfuseClient.get_instance()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        session_id: str | None = None,
    ) -> LLMResponse:
        effective_model = model or self._default_model
        diagnostics = _ModelCallDiagnostics(session_id, effective_model, operation="chat")
        diagnostics.retry_counts.update(service=0, timeout=0)
        diagnostics.start()
        outcome = "cancelled"

        # --- Langfuse: start observation ---
        langfuse_observation = None
        try:
            if self._langfuse.enabled and self._langfuse._client:
                metadata: dict[str, Any] = {"has_tools": tools is not None}
                response_id = get_current_response_id()
                if response_id:
                    metadata["response_id"] = response_id
                client = self._langfuse._client
                if hasattr(client, "start_observation"):
                    langfuse_observation = client.start_observation(
                        name="llm-chat",
                        as_type="generation",
                        model=effective_model,
                        input=redact_image_data_urls(messages),
                        metadata=metadata,
                    )
                    if response_id:
                        self._langfuse.register_generation(
                            response_id, langfuse_observation, metadata=metadata
                        )

            # --- Call VLM backend ---
            rate_limit_attempt = 1
            transient_delay = 0.5
            # An explicit empty list asks VLM backends for a structured response
            # without exposing tools, so per-response usage is preserved.
            response_tools = tools if tools is not None else []
            while True:
                diagnostics.begin_attempt(effective_model, None)
                try:
                    result = await self._vlm.get_completion_async(
                        messages=messages,
                        thinking=getattr(self._vlm, "thinking", None),
                        tools=response_tools,
                        tool_choice="auto" if response_tools else None,
                    )
                    break
                except Exception as e:
                    diagnostics.last_error_type = type(e).__name__
                    chain = _exception_chain(e)
                    request = _first_exception_attribute(chain, "request")
                    response = _first_exception_attribute(chain, "response")
                    diagnostics.client_request_id = _first_header_value(
                        _safe_exception_attribute(request, "headers"), ("x-client-request-id",)
                    )
                    diagnostics.server_request_id = _first_header_value(
                        _safe_exception_attribute(response, "headers"), _SERVER_REQUEST_ID_HEADERS
                    )
                    kinds = _chat_retry_error_types(e)
                    if not kinds:
                        raise
                    if "rate_limit" in kinds:
                        delay = rate_limit_retry_delay(rate_limit_attempt)
                        rate_limit_attempt += 1
                    else:
                        delay = random.uniform(transient_delay / 2, transient_delay)
                        transient_delay = min(30.0, transient_delay * 2)
                    diagnostics.retry_count += 1
                    for kind in kinds:
                        diagnostics.retry_counts[kind] += 1
                    diagnostics.phase = "backoff"
                    diagnostics.backoff_started_at = time.perf_counter()
                    try:
                        await asyncio.sleep(delay)
                    finally:
                        diagnostics.backoff_seconds += (
                            time.perf_counter() - diagnostics.backoff_started_at
                        )
                        diagnostics.backoff_started_at = None

            diagnostics.response_received_at = time.perf_counter()
            llm_response = self._convert_response(result)
            diagnostics.record_response(llm_response)
            diagnostics.client_request_id = getattr(result, "client_request_id", None)
            diagnostics.server_request_id = getattr(result, "server_request_id", None)
            diagnostics.finish_reason = llm_response.finish_reason
            diagnostics.phase = "completed"

            # --- Langfuse: end observation ---
            if langfuse_observation:
                self._end_langfuse_observation(langfuse_observation, llm_response)

            outcome = "failed" if llm_response.finish_reason == "error" else "success"
            return llm_response

        except Exception as e:
            outcome = "failed"
            diagnostics.last_error_type = type(e).__name__
            if langfuse_observation:
                self._end_langfuse_observation_error(langfuse_observation, e)
            self._log_request_exception("chat", e, model=effective_model)
            return LLMResponse(
                content=f"Error calling LLM in VLM Adapter: {_redact_error_text(str(e))}",
                finish_reason="error",
            )
        finally:
            diagnostics.finish(outcome)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        session_id: str | None = None,
    ) -> AsyncIterator[LLMStreamEvent]:
        if not self._supports_native_stream(self._vlm):
            async for event in super().chat_stream(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                session_id=session_id,
            ):
                yield event
            return

        diagnostics = _ModelCallDiagnostics(
            session_id=session_id, model=model or self._default_model
        )
        diagnostics.start()
        outcome = "cancelled"
        try:
            async for event in self._chat_stream_with_retry(
                self._vlm,
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                diagnostics=diagnostics,
            ):
                yield event
            outcome = "success"
        except Exception as exc:
            outcome = "failed"
            diagnostics.last_error_type = type(exc).__name__
            self._log_request_exception("chat_stream", exc, model=model or self._default_model)
            yield LLMStreamEvent(
                type="response",
                response=LLMResponse(
                    content=f"Error calling LLM in VLM Adapter stream: {str(exc)}",
                    finish_reason="error",
                ),
            )
        finally:
            diagnostics.finish(outcome)

    def _log_request_exception(
        self,
        operation: str,
        error: BaseException,
        *,
        model: str,
    ) -> None:
        details = _exception_log_details(error)
        logger.error(
            "VLM Adapter request failed: operation={} provider={} model={} timeout={}s "
            "error_type={} status={} code={} client_request_id={} "
            "server_request_id={} server_trace_id={} method={} url={} "
            "response_body={} cause_chain={} traceback={}",
            operation,
            getattr(self._vlm, "provider", None) or "unknown",
            model,
            getattr(self._vlm, "timeout", None) or "-",
            details["error_type"],
            details["status"],
            details["code"],
            details["client_request_id"],
            details["server_request_id"],
            details["server_trace_id"],
            details["method"],
            details["url"],
            details["response_body"],
            details["cause_chain"],
            details["traceback"],
        )

    @classmethod
    def _supports_native_stream(cls, vlm: Any) -> bool:
        instances = getattr(vlm, "_vlm_instances", None)
        if instances is not None:
            return bool(instances) and all(cls._supports_native_stream(item) for item in instances)

        primary = getattr(vlm, "primary", None)
        backup = getattr(vlm, "backup", None)
        if primary is not None and backup is not None:
            return cls._supports_native_stream(primary) and cls._supports_native_stream(backup)

        return getattr(vlm, "provider", None) in {"openai", "volcengine"} and callable(
            getattr(vlm, "get_async_client", None)
        )

    async def _chat_stream_with_retry(
        self,
        vlm: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
        temperature: float,
        diagnostics: _ModelCallDiagnostics,
    ) -> AsyncIterator[LLMStreamEvent]:
        stream_backend = partial(
            self._chat_stream_backend,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            diagnostics=diagnostics,
        )
        stream_with_failover = getattr(vlm, "stream_with_failover", None)
        use_failover = callable(stream_with_failover)
        rate_limit_attempt = 1
        transport_delay = 0.5
        while True:
            emitted = False
            try:
                stream = (
                    stream_with_failover(stream_backend) if use_failover else stream_backend(vlm)
                )
                async for event in stream:
                    emitted = True
                    yield event
                return
            except Exception as exc:
                diagnostics.last_error_type = type(exc).__name__
                if emitted:
                    raise
                retry_kinds = _stream_transport_error_types(exc)
                if retry_kinds:
                    # Capped state avoids exponent overflow during unlimited retries.
                    delay = random.uniform(transport_delay / 2, transport_delay)
                    transport_delay = min(30.0, transport_delay * 2)
                elif is_retryable_rate_limit_error(exc):
                    retry_kinds = {"rate_limit"}
                    delay = rate_limit_retry_delay(rate_limit_attempt)
                    logger.warning(
                        "VLM {} stream rate limited; retrying attempt={} delay={:.1f}s error={}",
                        "credential" if use_failover else "adapter",
                        rate_limit_attempt,
                        delay,
                        exc,
                    )
                    rate_limit_attempt += 1
                else:
                    raise
                diagnostics.retry_count += 1
                for kind in retry_kinds:
                    diagnostics.retry_counts[kind] += 1
                diagnostics.phase = "backoff"
                diagnostics.backoff_started_at = time.perf_counter()
                try:
                    await asyncio.sleep(delay)
                finally:
                    diagnostics.backoff_seconds += (
                        time.perf_counter() - diagnostics.backoff_started_at
                    )
                    diagnostics.backoff_started_at = None

    async def _chat_stream_backend(
        self,
        vlm: Any,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
        temperature: float,
        diagnostics: _ModelCallDiagnostics,
    ) -> AsyncIterator[LLMStreamEvent]:
        kwargs = self._build_stream_kwargs(
            vlm,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, int] = {}
        start_time = time.perf_counter()

        diagnostics.begin_attempt(kwargs["model"], kwargs.get("extra_headers"))
        client = vlm.get_async_client()
        response = await client.chat.completions.create(**kwargs)
        diagnostics.response_received_at = time.perf_counter()
        diagnostics.server_request_id = _first_header_value(
            _safe_exception_attribute(_safe_exception_attribute(response, "response"), "headers"),
            _SERVER_REQUEST_ID_HEADERS,
        )
        diagnostics.phase = "reading_stream"
        async for chunk in response:
            diagnostics.received_chunk()
            chunk_usage = self._parse_usage(getattr(chunk, "usage", None))
            if chunk_usage:
                usage = chunk_usage

            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason or finish_reason
                diagnostics.finish_reason = finish_reason
                diagnostics.phase = "waiting_stream_end"
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue

            reasoning_delta = stream_delta_value(delta, "reasoning_content")
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                diagnostics.phase = "delivering"
                yield LLMStreamEvent(type="reasoning_delta", content=reasoning_delta)
                diagnostics.phase = (
                    "waiting_stream_end" if diagnostics.finish_reason else "reading_stream"
                )

            content_delta = stream_delta_value(delta, "content")
            if content_delta:
                content_parts.append(content_delta)
                diagnostics.phase = "delivering"
                yield LLMStreamEvent(type="content_delta", content=content_delta)
                diagnostics.phase = (
                    "waiting_stream_end" if diagnostics.finish_reason else "reading_stream"
                )

            for fallback_index, delta_tool_call in enumerate(
                getattr(delta, "tool_calls", None) or []
            ):
                merge_stream_tool_call_delta(
                    tool_calls,
                    delta_tool_call,
                    fallback_index=fallback_index,
                )
                diagnostics.tool_argument_chars += len(
                    stream_delta_value(getattr(delta_tool_call, "function", None), "arguments")
                )

        if usage:
            self._record_vlm_usage(vlm, usage, time.perf_counter() - start_time)

        diagnostics.phase = "delivering"
        llm_response = build_stream_response(
            content="".join(content_parts),
            reasoning_content="".join(reasoning_parts),
            raw_tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )
        diagnostics.record_response(llm_response)
        yield LLMStreamEvent(type="response", response=llm_response)
        diagnostics.phase = "completed"

    def _build_stream_kwargs(
        self,
        vlm: Any,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
        temperature: float,
    ) -> dict[str, Any]:
        provider = getattr(vlm, "provider", None)
        configured_max_tokens = getattr(vlm, "max_tokens", None)
        effective_max_tokens = (
            configured_max_tokens if configured_max_tokens is not None else max_tokens
        )
        # A wrapped credential owns its model.  The model passed by AgentLoop is
        # the outer/default model and must not overwrite a backup credential.
        effective_model = (
            getattr(vlm, "model", None)
            if vlm is not self._vlm
            else model or getattr(vlm, "model", None)
        ) or self._default_model

        if provider == "volcengine":
            kwargs: dict[str, Any] = {
                "model": effective_model,
                "messages": messages,
                "temperature": getattr(vlm, "temperature", temperature),
                "thinking": {"type": "enabled" if getattr(vlm, "thinking", False) else "disabled"},
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if effective_max_tokens is not None:
                kwargs["max_tokens"] = effective_max_tokens
            kwargs["extra_headers"] = build_volcengine_request_headers(
                getattr(vlm, "extra_headers", None)
            )
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            return kwargs

        kwargs = vlm._build_text_kwargs(
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None,
            thinking=getattr(vlm, "thinking", None),
        )
        kwargs["model"] = effective_model
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        if effective_max_tokens is not None and not (
            "max_tokens" in kwargs or "max_completion_tokens" in kwargs
        ):
            token_key = "max_completion_tokens" if "reasoning_effort" in kwargs else "max_tokens"
            kwargs[token_key] = effective_max_tokens
        return kwargs

    @staticmethod
    def _usage_value(usage: Any, name: str) -> int:
        if isinstance(usage, dict):
            return int(usage.get(name, 0) or 0)
        return int(getattr(usage, name, 0) or 0)

    @classmethod
    def _parse_usage(cls, raw_usage: Any) -> dict[str, int]:
        if not raw_usage:
            return {}

        usage = {}
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = (
                raw_usage.get(name)
                if isinstance(raw_usage, dict)
                else getattr(raw_usage, name, None)
            )
            if value is not None:
                usage[name] = int(value)
        prompt_details = (
            raw_usage.get("prompt_tokens_details")
            if isinstance(raw_usage, dict)
            else getattr(raw_usage, "prompt_tokens_details", None)
        )
        completion_details = (
            raw_usage.get("completion_tokens_details")
            if isinstance(raw_usage, dict)
            else getattr(raw_usage, "completion_tokens_details", None)
        )
        cached = cls._usage_value(prompt_details, "cached_tokens") if prompt_details else 0
        reasoning = (
            completion_details.get("reasoning_tokens")
            if isinstance(completion_details, dict)
            else getattr(completion_details, "reasoning_tokens", None)
        )
        if cached:
            usage["cache_read_input_tokens"] = cached
        if reasoning is not None:
            usage["reasoning_tokens"] = int(reasoning)
        return usage

    def _record_vlm_usage(
        self,
        vlm: Any,
        usage: dict[str, int],
        duration_seconds: float,
    ) -> None:
        update_token_usage = getattr(vlm, "update_token_usage", None)
        if not callable(update_token_usage):
            return
        try:
            update_token_usage(
                model_name=getattr(vlm, "model", None) or self._default_model,
                provider=getattr(vlm, "provider", None) or "unknown",
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                duration_seconds=duration_seconds,
                prompt_cached_tokens=usage.get("cache_read_input_tokens", 0),
                completion_reasoning_tokens=usage.get("reasoning_tokens", 0),
            )
        except Exception as e:
            logger.debug(f"[VLM] Failed to record stream token usage: {e}")

    def _convert_response(self, result) -> LLMResponse:
        """Convert VLMResponse (or str) to LLMResponse."""
        if isinstance(result, str):
            return LLMResponse(content=result, finish_reason="stop")

        tool_calls = []
        for tc in result.tool_calls:
            tool_calls.append(
                ToolCallRequest(
                    id=tc.id,
                    name=tc.name,
                    arguments=tc.arguments,
                    tokens=0,
                )
            )

        return LLMResponse(
            content=result.content,
            tool_calls=tool_calls,
            finish_reason=result.finish_reason,
            usage=result.usage,
            reasoning_content=result.reasoning_content,
        )

    def get_default_model(self) -> str:
        return self._default_model

    def supports_tool_result_media(self, model: str | None = None) -> bool:
        """Return true only for backends with native rich tool-result support."""
        return self._backend_supports_tool_result_media(self._vlm)

    @classmethod
    def _backend_supports_tool_result_media(cls, vlm: Any) -> bool:
        instances = getattr(vlm, "_vlm_instances", None)
        if instances is not None:
            return bool(instances) and all(
                cls._backend_supports_tool_result_media(item) for item in instances
            )

        primary = getattr(vlm, "primary", None)
        backup = getattr(vlm, "backup", None)
        if primary is not None and backup is not None:
            return cls._backend_supports_tool_result_media(
                primary
            ) and cls._backend_supports_tool_result_media(backup)

        resolve_provider = getattr(vlm, "resolved_provider", None)
        provider = (
            resolve_provider() if callable(resolve_provider) else getattr(vlm, "provider", "")
        )
        provider = str(provider or "").lower()
        return provider in {"anthropic", "openai-codex"}

    # ------------------------------------------------------------------
    # Langfuse helpers
    # ------------------------------------------------------------------

    def _end_langfuse_observation(self, obs, llm_response: LLMResponse) -> None:
        try:
            output_text = llm_response.content or ""
            if llm_response.tool_calls:
                output_text = (
                    output_text or f"[Tool calls: {[tc.name for tc in llm_response.tool_calls]}]"
                )

            update_kwargs: dict[str, Any] = {
                "output": output_text,
                "metadata": {
                    "finish_reason": llm_response.finish_reason,
                    **(
                        {"response_id": get_current_response_id()}
                        if get_current_response_id()
                        else {}
                    ),
                },
            }

            if llm_response.usage:
                usage_details: dict[str, Any] = {
                    "input": llm_response.usage.get("prompt_tokens", 0),
                    "output": llm_response.usage.get("completion_tokens", 0),
                }
                cache_read_tokens = llm_response.usage.get(
                    "cache_read_input_tokens"
                ) or llm_response.usage.get("prompt_tokens_details", {}).get("cached_tokens")
                if cache_read_tokens:
                    usage_details["cache_read_input_tokens"] = cache_read_tokens
                update_kwargs["usage_details"] = usage_details

            response_id = get_current_response_id()
            if response_id:
                update_kwargs["metadata"] = self._langfuse.update_generation_metadata(
                    response_id,
                    update_kwargs.get("metadata", {}),
                )

            if hasattr(obs, "update"):
                try:
                    obs.update(**update_kwargs)
                except Exception as e:
                    logger.debug(f"[LANGFUSE] Failed to update observation: {e}")

            if hasattr(obs, "end"):
                try:
                    obs.end()
                except Exception as e:
                    logger.debug(f"[LANGFUSE] Failed to end observation: {e}")

            try:
                self._langfuse.flush()
            except Exception as e:
                logger.debug(f"[LANGFUSE] Failed to flush: {e}")
        except Exception:
            pass

    def _end_langfuse_observation_error(self, obs, error: Exception) -> None:
        try:
            if hasattr(obs, "update"):
                obs.update(
                    output=f"Error: {str(error)}",
                    metadata={
                        "error": str(error),
                        **(
                            {"response_id": get_current_response_id()}
                            if get_current_response_id()
                            else {}
                        ),
                    },
                )
            if hasattr(obs, "end"):
                obs.end()
            try:
                self._langfuse.flush()
            except Exception:
                pass
        except Exception:
            pass
