from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import httpx
import pytest
from loguru import logger as loguru_logger

from openviking.session.train.components import session_logging as routing
from openviking.session.train.components.dataset_service import (
    create_dataset_service_app,
    redact_sensitive,
)
from openviking.session.train.components.remote import RemoteRolloutExecutor
from openviking.session.train.components.rollout_artifact_recorder import RolloutArtifactRecorder
from openviking.session.train.context import ExecutionContext
from openviking.session.train.domain import Rollout, RubricEvaluation
from tests.session.train.test_dataset_service_cancellation import (
    WaitingExecutor,
    case,
    payload,
    policy,
    terminal,
)


def _loggers():
    return [logging.getLogger()] + [
        item
        for item in list(logging.Logger.manager.loggerDict.values())
        if isinstance(item, logging.Logger)
    ]


@pytest.fixture(autouse=True)
def restore_router():
    handlers = {logger: set(logger.handlers) for logger in _loggers()}
    sink_id = routing._loguru_sink_id
    yield
    if routing._loguru_sink_id != sink_id:
        loguru_logger.remove(routing._loguru_sink_id)
        routing._loguru_sink_id = sink_id
    for logger in _loggers():
        for handler in list(logger.handlers):
            if isinstance(handler, routing._SessionFileHandler) and handler not in handlers.get(
                logger, set()
            ):
                logger.removeHandler(handler)
                handler.close()


def test_routes_python_and_loguru_once_redacts_copies_and_preserves_aggregate(tmp_path):
    aggregate = io.StringIO()
    sink_id = loguru_logger.add(aggregate, format="{message}", diagnose=False)
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    isolated = logging.getLogger("test.rollout.nonpropagating")
    old_propagate = isolated.propagate
    isolated.propagate = False
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    isolated.addHandler(handler)
    try:
        routing.install_session_log_routing()
        routing.install_session_log_routing()
        log_path = tmp_path / "rollout.log"
        with routing.rollout_log_session(str(log_path), redact=redact_sensitive) as session:
            logging.getLogger("test.rollout.child").warning("root-marker api_key=private-root")
            isolated.warning("isolated-marker password=private-password")
            loguru_logger.debug("loguru-marker authorization=Bearer private-bearer")
            try:
                raise RuntimeError('token="private-exception"')
            except RuntimeError:
                isolated.exception("python-traceback")
                loguru_logger.exception("loguru-traceback")
            assert session is not None
        assert session.stream.closed
        loguru_logger.warning("outside-marker")
        text = log_path.read_text()
        for marker in ("root-marker", "isolated-marker", "loguru-marker"):
            assert text.count(marker) == 1
        assert "Traceback" in text and "<redacted>" in text
        for value in ("private-root", "private-password", "private-bearer", "private-exception"):
            assert value not in text
        assert "outside-marker" not in text
        assert "private-password" in stream.getvalue()
        assert "private-bearer" in aggregate.getvalue()
        assert "outside-marker" in aggregate.getvalue()
        assert all(handler in root.handlers for handler in old_handlers)
        assert log_path.stat().st_mode & 0o777 == 0o600
    finally:
        isolated.removeHandler(handler)
        isolated.propagate = old_propagate
        loguru_logger.remove(sink_id)


def test_nested_disabled_scope_restores_outer_and_late_thread_logs_are_dropped(tmp_path):
    routing.install_session_log_routing()
    old_path = tmp_path / "old.log"
    new_path = tmp_path / "new.log"
    with routing.rollout_log_session(str(old_path), redact=redact_sensitive):
        context = copy_context()
        loguru_logger.info("old-marker")
        with routing.rollout_log_session(None, redact=redact_sensitive):
            loguru_logger.info("disabled-marker")
        loguru_logger.info("restored-marker")
    with routing.rollout_log_session(str(new_path), redact=redact_sensitive):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(context.run, loguru_logger.info, "late-marker").result(timeout=2)
        loguru_logger.info("new-marker")
    assert "restored-marker" in old_path.read_text()
    assert "new-marker" not in old_path.read_text()
    assert "old-marker" not in new_path.read_text()
    for path in (old_path, new_path):
        assert "late-marker" not in path.read_text()
        assert "disabled-marker" not in path.read_text()


@pytest.mark.parametrize("kind", ["file", "file_symlink", "directory_symlink"])
def test_log_destination_never_overwrites_or_follows_symlinks(tmp_path, kind, capsys):
    target = tmp_path / "target"
    target.write_text("untouched")
    if kind == "directory_symlink":
        (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
        path = tmp_path / "link" / "new.log"
    elif kind == "file_symlink":
        path = tmp_path / "link.log"
        path.symlink_to(target)
    else:
        path = target
    with routing.rollout_log_session(str(path), redact=redact_sensitive) as session:
        assert session is None
    assert target.read_text() == "untouched"
    assert not (tmp_path / "new.log").exists()
    assert "execution continues" in capsys.readouterr().err


def test_safe_unique_bounded_names(tmp_path):
    paths = [
        Path(
            routing.rollout_session_log_path(
                tmp_path,
                execution_id=f"rollout_exec_{number:032d}",
                case_name="../../" + "a" * 400,
                metadata={"rollout_stage": "../..\\/\n", "epoch": "../../"},
            )
        )
        for number in range(2)
    ]
    assert paths[0] != paths[1]
    for path in paths:
        assert path.parent == tmp_path
        assert len(path.name.encode()) <= 255
        assert ".." not in path.name and "\n" not in path.name


def test_write_and_close_errors_are_nonfatal_and_do_not_print_sensitive_error(capsys):
    class BrokenStream:
        closed = False

        def write(self, text):
            raise OSError("secret-error-content")

        def close(self):
            raise OSError("secret-error-content")

    session = routing._SessionLog("unused", BrokenStream(), redact_sensitive)
    session.write("token=private")
    session.close()
    stderr = capsys.readouterr().err
    assert "execution continues" in stderr
    assert "secret-error-content" not in stderr and "private" not in stderr


def make_app(tmp_path, executor, workers=None, enabled=True):
    return create_dataset_service_app(
        service_name="offline_logs",
        make_case_loader=lambda *args: None,
        make_rollout_executor=lambda options: executor,
        max_rollout_concurrency=2,
        rollout_thread_workers=workers,
        rollout_log_dir=tmp_path / "logs" if enabled else None,
    )


class LoggingExecutor:
    async def execute(self, cases, policy_set, context):
        name = cases[0].name
        logging.getLogger("test.rollout.executor").warning("async-marker-%s", name)
        await asyncio.sleep(0.005)
        await asyncio.to_thread(loguru_logger.info, "inner-marker-{}", name)
        return [
            Rollout(
                case=cases[0],
                messages=[],
                policy_snapshot_id=context.policy_snapshot_id,
                evaluation=RubricEvaluation(True, 1.0, [], []),
            )
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [None, 1, 2])
async def test_concurrent_http_rollouts_isolate_outer_inner_threads_and_persist_paths(
    tmp_path, workers
):
    app = make_app(tmp_path, LoggingExecutor(), workers)
    requests = []
    for name in ("first", "second", "first"):
        body = payload()
        body["case"]["name"] = name
        body["execution_context"]["metadata"] = {"epoch": 2, "rollout_stage": "train_rollout"}
        body["options"] = {"session_log_root": str(tmp_path / "client-controlled")}
        requests.append(body)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            responses = await asyncio.gather(
                *(client.post("/v1/rollouts/execute", json=body) for body in requests)
            )
            paths = []
            for response, request in zip(responses, requests, strict=True):
                data = response.json()
                result = await terminal(client, data["execution_id"])
                assert result["status"] == "completed"
                path = Path(data["rollout_log_path"])
                paths.append(path)
                assert result["rollout_log_path"] == str(path)
                assert result["rollout"]["metadata"]["rollout_log_path"] == str(path)
                assert path.parent == tmp_path / "logs"
                assert "train_rollout-epoch_2" in path.name
                text = path.read_text()
                own = request["case"]["name"]
                other = "second" if own == "first" else "first"
                assert f"async-marker-{own}" in text and f"inner-marker-{own}" in text
                assert f"marker-{other}" not in text
                assert "rollout started" in text and "rollout completed" in text
            assert len(set(paths)) == 3
            assert not (tmp_path / "client-controlled").exists()
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [None, 1])
@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_and_deadline_keep_log_path_and_cleanup_before_terminal(
    tmp_path, workers, cancel
):
    class CleanupLoggingExecutor(WaitingExecutor):
        async def execute(self, cases, policy_set, context):
            try:
                return await super().execute(cases, policy_set, context)
            finally:
                loguru_logger.warning("cleanup-marker password=private-cleanup")

    executor = CleanupLoggingExecutor()
    app = make_app(tmp_path, executor, workers)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            result = (
                await client.post("/v1/rollouts/execute", json=payload(5 if cancel else 0.1))
            ).json()
            while not executor.started.is_set():
                await asyncio.sleep(0.002)
            if cancel:
                await client.post(f"/v1/rollouts/executions/{result['execution_id']}/cancel")
            terminal_result = await terminal(client, result["execution_id"])
            assert terminal_result["status"] == "failed" and executor.cleaned.is_set()
            assert terminal_result["rollout_log_path"] == result["rollout_log_path"]
            text = Path(result["rollout_log_path"]).read_text()
            assert "cleanup-marker" in text and "private-cleanup" not in text
            assert ("CancelledError" if cancel else "TimeoutError") in text
            assert not app.state.rollout_tasks and not app.state.rollout_controls
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_disabled_or_unwritable_log_directory_does_not_fail_execution(tmp_path, disabled):
    (tmp_path / "logs").write_text("not a directory")
    app = make_app(tmp_path, LoggingExecutor(), enabled=not disabled)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            result = (await client.post("/v1/rollouts/execute", json=payload())).json()
            result = await terminal(client, result["execution_id"])
            assert result["status"] == "completed"
            if disabled:
                assert result["rollout_log_path"] is None
                assert "rollout_log_path" not in result["rollout"]["metadata"]
            assert (tmp_path / "logs").read_text() == "not a directory"
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_remote_success_and_artifacts_reference_server_log_without_copying(tmp_path):
    app = make_app(tmp_path, LoggingExecutor(), 1)
    context = ExecutionContext("test", metadata={"training": True, "epoch": 0})
    remote = RemoteRolloutExecutor("http://test", poll_interval_seconds=0.001)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            rollout = await remote._execute_one(client, case(), policy(), context)
            path = rollout.metadata["rollout_log_path"]
            assert Path(path).is_file()
            artifact_dir = tmp_path / "artifacts"
            recorder = RolloutArtifactRecorder(run_dir=artifact_dir)
            recorder.record_rollout_completion(rollout=rollout, index=0, context=context)
            statuses = list(artifact_dir.rglob("status.json"))
            assert statuses and json.loads(statuses[0].read_text())["rollout_log_path"] == path
            indexes = list(artifact_dir.rglob("rollouts_index.json"))
            assert indexes and path in indexes[0].read_text()
            assert not list(artifact_dir.rglob("*.log"))
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_remote_failure_still_raises_and_includes_log_path(tmp_path):
    class FailureExecutor:
        async def execute(self, *args):
            raise RuntimeError("api_key=private-failure")

    app = make_app(tmp_path, FailureExecutor(), 1)
    remote = RemoteRolloutExecutor("http://test", poll_interval_seconds=0.001)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            with pytest.raises(RuntimeError) as exc:
                await remote._execute_one(client, case(), policy(), ExecutionContext("test"))
            assert "rollout_log_path=" in str(exc.value)
            assert "private-failure" not in str(exc.value)
            path = next((tmp_path / "logs").glob("*.log"))
            assert str(path) in str(exc.value)
            assert "<redacted>" in path.read_text()
        finally:
            await app.router.shutdown()


@pytest.mark.asyncio
async def test_remote_local_timeout_includes_accepted_path_and_cancels_server():
    cancelled = threading.Event()

    async def handle(request):
        if request.url.path == "/v1/rollouts/execute":
            return httpx.Response(
                200, json={"execution_id": "exec", "rollout_log_path": "/logs/exec.log"}
            )
        if request.url.path.endswith("/cancel"):
            cancelled.set()
            return httpx.Response(200, json={"status": "running"})
        await asyncio.Event().wait()

    remote = RemoteRolloutExecutor("http://test", execution_timeout_seconds=0.01)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://test"
    ) as client:
        with pytest.raises(TimeoutError, match="rollout_log_path=/logs/exec.log"):
            await remote._execute_one(client, case(), policy(), ExecutionContext("test"))
    assert cancelled.is_set()


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_scope_closes_file_and_resets_context_on_error(tmp_path, error_type):
    with pytest.raises(error_type):
        with routing.rollout_log_session(
            str(tmp_path / "failed.log"), redact=redact_sensitive
        ) as session:
            assert session is not None
            raise error_type()
    assert session.stream.closed
    assert routing._current_log.get() is None


def test_real_tau2_executor_logger_is_routed_without_changing_level_or_handlers(tmp_path):
    from benchmark.tau2.train.rollout_executor_vikingbot import logger

    old_level, old_propagate, old_handlers = logger.level, logger.propagate, list(logger.handlers)
    routing.install_session_log_routing()
    path = tmp_path / "tau2.log"
    with routing.rollout_log_session(str(path), redact=redact_sensitive):
        logger.warning("real-tau2-logger-marker api_key=private-tau2")
    text = path.read_text()
    assert text.count("real-tau2-logger-marker") == 1 and "private-tau2" not in text
    assert (logger.level, logger.propagate) == (old_level, old_propagate)
    assert all(handler in logger.handlers for handler in old_handlers)


@pytest.mark.asyncio
async def test_shutdown_keeps_cleanup_logs_and_closes_session(tmp_path):
    sessions = []

    class Executor(WaitingExecutor):
        async def execute(self, *args):
            sessions.append(routing._current_log.get())
            try:
                await super().execute(*args)
            finally:
                loguru_logger.warning("shutdown-cleanup")

    executor = Executor()
    app = make_app(tmp_path, executor, 1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        try:
            result = (await client.post("/v1/rollouts/execute", json=payload(5))).json()
            while not executor.started.is_set():
                await asyncio.sleep(0.002)
        finally:
            await app.router.shutdown()
        result = await terminal(client, result["execution_id"])
        assert result["status"] == "failed" and "shutdown" in result["error"]
        assert sessions[0].stream.closed
        assert "shutdown-cleanup" in Path(result["rollout_log_path"]).read_text()
