from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.session.train import batch_runner as runner

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER = REPO_ROOT / "openviking/session/train/auto_commit.sh"
LAUNCHER = REPO_ROOT / "benchmark/tau2/train/restart_vikingbot_train_eval.sh"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Provenance Test")
    git(repo, "config", "user.email", "provenance@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text("ignored/\nresult/\n")
    (repo / "main.py").write_text("original\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "test: initial state")
    monkeypatch.setattr(runner, "_repo_root", lambda: repo)
    return repo


def test_git_metadata_tracks_working_content_not_just_head(git_repo):
    clean = runner._git_metadata()
    assert clean["commit"] == git(git_repo, "rev-parse", "HEAD")
    assert clean["branch"] == git(git_repo, "rev-parse", "--abbrev-ref", "HEAD")
    assert clean["dirty"] is False
    assert clean["capture_scope"] == "launcher_checkout"
    assert len(clean["worktree_fingerprint"]) == 64
    assert runner._git_metadata() == clean

    (git_repo / "main.py").write_text("changed once\n")
    first = runner._git_metadata()
    (git_repo / "main.py").write_text("changed twice\n")
    second = runner._git_metadata()
    assert first["dirty"] is second["dirty"] is True
    assert first["commit"] == second["commit"] == clean["commit"]
    assert first["status_porcelain"] == second["status_porcelain"]
    assert len({m["worktree_fingerprint"] for m in (clean, first, second)}) == 3
    git(git_repo, "add", "main.py")
    assert runner._git_metadata()["worktree_fingerprint"] == second["worktree_fingerprint"]


def test_git_metadata_hashes_untracked_files_and_ignores_ignored_files(git_repo):
    original = runner._git_metadata()
    untracked = git_repo / "new file\nname.py"
    untracked.write_text("first\n")
    first = runner._git_metadata()
    untracked.write_text("second\n")
    second = runner._git_metadata()
    assert first["dirty"] is True
    assert len({m["worktree_fingerprint"] for m in (original, first, second)}) == 3
    ignored = git_repo / "ignored"
    ignored.mkdir()
    (ignored / "secret").write_text("not part of the code fingerprint")
    assert runner._git_metadata() == second
    assert git(git_repo, "diff", "--cached", "--name-only") == ""


def test_git_metadata_hashes_symlink_target_without_reading_external_file(git_repo, tmp_path):
    external = tmp_path / "external"
    external.write_text("private outside repository")
    (git_repo / "linked.py").symlink_to(external)
    first = runner._git_metadata()
    external.write_text("changed external content")
    assert runner._git_metadata() == first
    (git_repo / "linked.py").unlink()
    (git_repo / "linked.py").symlink_to(tmp_path / "missing")
    assert runner._git_metadata()["worktree_fingerprint"] != first["worktree_fingerprint"]


def test_git_metadata_unavailable_is_not_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_repo_root", lambda: tmp_path)
    metadata = runner._git_metadata()
    assert metadata["dirty"] is None
    assert metadata["commit"] is None
    assert metadata["worktree_fingerprint"] is None
    assert metadata["error"]


def test_run_metadata_uses_a_parameter_allowlist(git_repo, tmp_path):
    config = runner.BatchTrainEvalConfig(
        dataset="tau2",
        domain="airline",
        train_index="1,3",
        api_key="PRIVATE_API_KEY",
        server_url="https://user:PRIVATE_PASSWORD@example.invalid",
        config_path="PRIVATE_CONFIG_PATH",
    )
    metadata = runner._git_metadata()
    path = runner._write_run_metadata(tmp_path / "run", config, metadata)
    text = path.read_text()
    record = json.loads(text)
    assert path.name == "run_metadata.json"
    assert record["git"] == metadata
    assert record["dataset"] == "tau2"
    assert record["parameters"]["train_index"] == [1, 3]
    assert record["parameters"]["trials"] == config.trials
    for secret in ("PRIVATE_API_KEY", "PRIVATE_PASSWORD", "PRIVATE_CONFIG_PATH"):
        assert secret not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_initialize", [False, True])
async def test_batch_runner_correlates_metadata_events_and_report(
    git_repo, tmp_path, monkeypatch, fail_initialize
):
    client = SimpleNamespace(
        initialize=AsyncMock(), close=AsyncMock(), _url="http://test", _api_key="PRIVATE_KEY"
    )
    result = SimpleNamespace(
        apply_result=SimpleNamespace(updated_policy_set=None), metadata={"train_reports": []}
    )
    pipeline = SimpleNamespace(train=AsyncMock(return_value=result), rollout_executor=None)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(runner, "_configure_openviking_config", lambda _: None)
    monkeypatch.setattr(runner, "_build_http_client", lambda _: client)
    monkeypatch.setattr(runner, "_run_output_dir", lambda _: run_dir)
    monkeypatch.setattr(runner, "_result_base_dir", lambda _: tmp_path)
    monkeypatch.setattr(runner, "_build_pipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr(runner, "_case_loader", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "emit_run_summary", AsyncMock())
    config = runner.BatchTrainEvalConfig(
        dataset="tau2", domain="airline", epochs=0, eval_split=None, clean_result=False
    )

    if fail_initialize:
        client.initialize.side_effect = RuntimeError("server unavailable")
        with pytest.raises(RuntimeError, match="server unavailable"):
            await runner.run_batch_train_eval(config)
        metadata = json.loads((run_dir / "run_metadata.json").read_text())
        assert metadata["git"]["commit"] == git(git_repo, "rev-parse", "HEAD")
        pipeline.train.assert_not_awaited()
        return

    report = await runner.run_batch_train_eval(config)

    metadata = json.loads((run_dir / "run_metadata.json").read_text())
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    saved_report = json.loads((run_dir / "report.json").read_text())
    assert report.git == metadata["git"] == saved_report["git"]
    assert report.run_metadata_path == str(run_dir / "run_metadata.json")
    for event in events:
        if event.get("event") in ("run_start", "run_result"):
            assert event["git"] == metadata["git"]
            assert event["run_metadata_path"] == report.run_metadata_path
    assert {event["event"] for event in events} >= {"run_start", "run_result"}
    client.close.assert_awaited_once()


def run_auto_commit(repo, *, enabled):
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; AUTO_COMMIT="$3"; openviking_train_auto_commit "$2" "tau2 train eval"',
            "test",
            str(HELPER),
            str(repo),
            "true" if enabled else "false",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_auto_commit_disabled_does_not_stage_or_commit(git_repo):
    before = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "main.py").write_text("pending\n")
    result = run_auto_commit(git_repo, enabled=False)
    assert result.returncode == 0, result.stderr
    assert git(git_repo, "rev-parse", "HEAD") == before
    assert git(git_repo, "diff", "--cached", "--name-only") == ""
    assert git(git_repo, "status", "--porcelain")


def test_auto_commit_stages_all_visible_changes_and_respects_ignore(git_repo):
    before = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "main.py").unlink()
    (git_repo / "new.py").write_text("new\n")
    ignored = git_repo / "ignored"
    ignored.mkdir()
    (ignored / "result.json").write_text("{}")
    result = run_auto_commit(git_repo, enabled=True)
    assert result.returncode == 0, result.stderr
    assert git(git_repo, "rev-parse", "HEAD") != before
    assert git(git_repo, "status", "--porcelain") == ""
    assert git(git_repo, "show", "HEAD:new.py") == "new"
    assert "ignored/result.json" not in git(git_repo, "ls-files")
    assert git(git_repo, "log", "-1", "--format=%s").startswith("train: checkpoint before")
    assert git(git_repo, "for-each-ref", "--format=%(refname)", "refs/notes") == ""


def test_auto_commit_clean_tree_does_not_create_empty_commit(git_repo):
    before = git(git_repo, "rev-parse", "HEAD")
    result = run_auto_commit(git_repo, enabled=True)
    assert result.returncode == 0, result.stderr
    assert git(git_repo, "rev-parse", "HEAD") == before


def test_auto_commit_preserves_hooks_and_propagates_failure(git_repo):
    before = git(git_repo, "rev-parse", "HEAD")
    hook = git_repo / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho rejected-by-test-hook >&2\nexit 7\n")
    hook.chmod(0o755)
    (git_repo / "main.py").write_text("pending\n")
    result = run_auto_commit(git_repo, enabled=True)
    assert result.returncode != 0
    assert "rejected-by-test-hook" in result.stderr
    assert git(git_repo, "rev-parse", "HEAD") == before


def test_auto_commit_outside_git_is_an_error(tmp_path):
    result = run_auto_commit(tmp_path, enabled=True)
    assert result.returncode != 0


@pytest.mark.parametrize("enabled,reject_commit", [(False, False), (True, False), (True, True)])
def test_launcher_commits_before_services_without_forwarding_flag(
    git_repo, tmp_path, enabled, reject_commit
):
    relative = LAUNCHER.relative_to(REPO_ROOT)
    launcher = git_repo / relative
    launcher.parent.mkdir(parents=True)
    source = LAUNCHER.read_text()
    assert source.endswith("\nmain\n")
    launcher.write_text(source.removesuffix("\nmain\n"))
    helper = git_repo / HELPER.relative_to(REPO_ROOT)
    helper.parent.mkdir(parents=True)
    shutil.copyfile(HELPER, helper)
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "test: install launcher")
    before = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "main.py").write_text("pending\n")
    if reject_commit:
        hook = git_repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    events = tmp_path / "events"
    arguments = tmp_path / "arguments"
    script = r"""
source "$1" "${@:2}"
start_openviking_server() { git -C "$REPO_ROOT" rev-parse HEAD > "$TEST_EVENTS"; }
enable_agent_evolution() { :; }
start_tau2_service() { :; }
run_train_eval() { printf '%s\n' "$@" > "$TEST_ARGS"; }
main
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "test",
            str(launcher),
            *(["--auto-commit"] if enabled else []),
            "--epochs",
            "0",
        ],
        env={
            **os.environ,
            "TEST_EVENTS": str(events),
            "TEST_ARGS": str(arguments),
            "LOG_DIR": str(tmp_path / "logs"),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    if reject_commit:
        assert result.returncode != 0
        assert not events.exists() and not arguments.exists()
        assert git(git_repo, "rev-parse", "HEAD") == before
        return
    assert result.returncode == 0, result.stderr
    after = git(git_repo, "rev-parse", "HEAD")
    assert (after != before) is enabled
    assert events.read_text().strip() == after
    assert arguments.read_text().splitlines() == ["--epochs", "0"]
