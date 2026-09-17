# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Concurrency primitives used by the task tracker."""

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Generic, TypeVar

from openviking.concurrency import AsyncSemaphore
from openviking_cli.utils.logger import get_logger

_KeyT = TypeVar("_KeyT")
_ResultT = TypeVar("_ResultT")

logger = get_logger(__name__)


async def run_to_completion(
    factory: Callable[[], Awaitable[_ResultT]],
) -> _ResultT:
    """Finish started work before propagating caller cancellation.

    ``asyncio.to_thread`` cancellation cannot stop the underlying synchronous
    call. Waiting for the work to settle keeps lock and concurrency accounting
    aligned with the physical I/O lifetime.
    """
    work: asyncio.Future[_ResultT] = asyncio.ensure_future(factory())
    caller = asyncio.current_task()
    cancellation: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as exc:
            if work.cancelled():
                raise
            cancellation = exc
        except BaseException:
            # A completion exception can race with delivery of caller
            # cancellation. Honour the already-requested cancellation below.
            caller_with_cancellation_count: Any = caller
            if (
                caller is None
                or not hasattr(caller, "cancelling")
                or caller_with_cancellation_count.cancelling() == 0
            ):
                raise
            cancellation = asyncio.CancelledError()

    if cancellation is not None:
        if not work.cancelled():
            # Caller cancellation wins once it has been observed. Consume a
            # later work failure so asyncio does not report it as unhandled.
            work.exception()
        raise cancellation
    return work.result()


class StoreIOLimiter:
    """Bound task-store concurrency and expose low-cardinality diagnostics."""

    def __init__(self, max_concurrent: int, slow_threshold_seconds: float = 1.0) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self._semaphore = AsyncSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self._slow_threshold_seconds = slow_threshold_seconds
        self._inflight = 0
        self._max_observed_inflight = 0

    @property
    def inflight(self) -> int:
        with self._lock:
            return self._inflight

    @property
    def max_observed_inflight(self) -> int:
        with self._lock:
            return self._max_observed_inflight

    async def run(
        self,
        operation: str,
        factory: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        started_at = time.monotonic()
        async with self._semaphore:
            with self._lock:
                self._inflight += 1
                self._max_observed_inflight = max(
                    self._max_observed_inflight,
                    self._inflight,
                )
            try:
                return await factory()
            finally:
                with self._lock:
                    self._inflight -= 1
                duration_seconds = time.monotonic() - started_at
                if duration_seconds >= self._slow_threshold_seconds:
                    logger.warning(
                        "Slow task store operation: operation=%s duration_ms=%.1f",
                        operation,
                        duration_seconds * 1000,
                    )


@dataclass
class _LockEntry:
    lock: AsyncSemaphore
    users: int = 0


class KeyedAsyncLockPool(Generic[_KeyT]):
    """Serialize work by key without retaining idle lock objects."""

    def __init__(self) -> None:
        self._entries: dict[_KeyT, _LockEntry] = {}
        self._lock = threading.Lock()

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @asynccontextmanager
    async def acquire(self, key: _KeyT) -> AsyncIterator[None]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(lock=AsyncSemaphore())
                self._entries[key] = entry
            entry.users += 1

        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._lock:
                entry.users -= 1
                if entry.users == 0:
                    del self._entries[key]
