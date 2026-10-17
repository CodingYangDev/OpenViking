# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Context-local copies of Python/Loguru logs; aggregate sinks are left intact."""

from __future__ import annotations

import copy
import logging
import os
import re
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from loguru import logger as loguru_logger


@dataclass(slots=True)
class _SessionLog:
    path: str
    stream: TextIO
    redact: Callable[[str], str]
    lock: threading.RLock = field(default_factory=threading.RLock)

    def write(self, message: str) -> None:
        with self.lock:
            # A cancelled to_thread call can still finish after its rollout closes.
            if self.stream.closed:
                return
            try:
                self.stream.write(self.redact(message.rstrip("\n")) + "\n")
                self.stream.flush()
            except Exception:
                _warn_log_failure()

    def close(self) -> None:
        with self.lock:
            try:
                self.stream.close()
            except OSError:
                _warn_log_failure()


_current_log: ContextVar[_SessionLog | None] = ContextVar("rollout_session_log", default=None)
_install_lock = threading.RLock()
_loguru_sink_id: int | None = None


def _warn_log_failure() -> None:
    # Avoid recursive logging and potentially sensitive exception/path contents.
    print(
        "[rollout-log] Could not write the per-rollout log; execution continues.", file=sys.stderr
    )


def rollout_session_log_path(
    root: Path, *, execution_id: str, case_name: str, metadata: dict
) -> str:
    def label(value: object) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value)).strip("_-")[:60] or "unknown"

    # Flat, sanitized names cannot introduce subdirectories; the server-generated
    # execution ID prevents repeated trials/truncated labels from sharing a file.
    name = "-".join(
        (
            label(metadata.get("rollout_stage") or "rollout"),
            "epoch_" + label(metadata.get("epoch", 0)),
            label(case_name),
            label(execution_id),
        )
    )
    return str(root / f"{name}.log")


class _SessionFileHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        session = _current_log.get()
        if session is not None:
            try:
                # Formatter caches exc_text; do not mutate records seen by other sinks.
                session.write(self.format(copy.copy(record)))
            except Exception:
                _warn_log_failure()


def _loguru_sink(message: object) -> None:
    session = _current_log.get()
    if session is not None:
        session.write(str(message))


def install_session_log_routing() -> None:
    global _loguru_sink_id
    with _install_lock:
        # OpenViking's managed roots and benchmark loggers do not propagate to
        # the Python root. Attach at each existing propagation boundary only,
        # so one record is copied once without modifying aggregate handlers.
        roots = [logging.getLogger()] + [
            item
            for item in list(logging.Logger.manager.loggerDict.values())
            if isinstance(item, logging.Logger) and not item.propagate
        ]
        for root in roots:
            if not any(isinstance(handler, _SessionFileHandler) for handler in root.handlers):
                handler = _SessionFileHandler()
                handler.setFormatter(
                    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
                )
                root.addHandler(handler)
        if _loguru_sink_id is None:
            # Synchronous dispatch is required: queued sinks lose the caller's context.
            _loguru_sink_id = loguru_logger.add(
                _loguru_sink, level="DEBUG", enqueue=False, diagnose=False, backtrace=False
            )


@contextmanager
def rollout_log_session(
    path: str | None, *, redact: Callable[[str], str]
) -> Iterator[_SessionLog | None]:
    session = None
    if path is not None:
        try:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                # Never follow a file symlink, append to an old execution, or overwrite a file.
                fd = os.open(
                    target.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
            finally:
                os.close(directory_fd)
            try:
                stream = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                os.close(fd)
                raise
            session = _SessionLog(path, stream, redact)
        except OSError:
            _warn_log_failure()
    token = _current_log.set(session)
    try:
        yield session
    finally:
        _current_log.reset(token)
        if session is not None:
            session.close()
