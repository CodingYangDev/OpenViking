from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "benchmark/tau2/train/run_service.sh"


@pytest.mark.parametrize("backend", ["native", "vikingbot"])
@pytest.mark.parametrize("source", ["default", "env", "cli", "cli_over_env", "empty_cli"])
def test_service_launcher_forwards_rollout_log_directory(tmp_path, monkeypatch, backend, source):
    from benchmark.tau2.train import service_app

    data_root = tmp_path / "tau2 data"
    (data_root / "domains").mkdir(parents=True)
    config = tmp_path / "ov.conf"
    config.write_text("{}")
    captured = tmp_path / "launch.json"
    python = tmp_path / "fake-python"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:] == ['-']:\n"
        "    sys.stdin.read()\n"
        "    raise SystemExit(0)\n"
        "assert Path(sys.argv[1]).name == 'service_app.py', sys.argv\n"
        "Path(os.environ['CAPTURE_PATH']).write_text(json.dumps({\n"
        "    'argv': sys.argv[1:],\n"
        "    'log_dir': os.environ.get('TAU2_ROLLOUT_LOG_DIR'),\n"
        "}))\n"
    )
    python.chmod(0o755)
    env = {
        **os.environ,
        "OPENVIKING_ENV_FILE": "/dev/null",
        "PYTHON_BIN": str(python),
        "CAPTURE_PATH": str(captured),
    }
    env.pop("TAU2_ROLLOUT_LOG_DIR", None)
    env_dir = str(tmp_path / "env logs")
    cli_dir = "" if source == "empty_cli" else str(tmp_path / "slot logs" / "rollouts")
    if source in {"env", "cli_over_env", "empty_cli"}:
        env["TAU2_ROLLOUT_LOG_DIR"] = env_dir
    args = [
        "bash",
        str(SCRIPT),
        "--host",
        "127.0.0.1",
        "--port",
        "1944",
        "--data-root",
        str(data_root),
        "--config",
        str(config),
        "--rollout-backend",
        backend,
        "--max-rollout-concurrency",
        "150",
        "--rollout-thread-workers",
        "150",
        # Never stop listeners, install dependencies, or start a real service in this test.
        "--no-kill-existing",
        "--no-repair-vikingbot-gym",
    ]
    if source in {"cli", "cli_over_env", "empty_cli"}:
        args.extend(["--rollout-log-dir", cli_dir])
    result = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    launch = json.loads(captured.read_text())
    monkeypatch.delenv("TAU2_ROLLOUT_LOG_DIR", raising=False)
    if launch["log_dir"] is not None:
        monkeypatch.setenv("TAU2_ROLLOUT_LOG_DIR", launch["log_dir"])
    monkeypatch.setattr(sys, "argv", launch["argv"])
    parsed = service_app.parse_args()
    expected = (
        cli_dir
        if source in {"cli", "cli_over_env", "empty_cli"}
        else env_dir
        if source == "env"
        else service_app.DEFAULT_ROLLOUT_LOG_DIR
    )
    assert parsed.rollout_log_dir == expected
    assert parsed.max_rollout_concurrency == parsed.rollout_thread_workers == 150
    assert parsed.rollout_backend == backend
    assert parsed.config == str(config)


def test_restart_launcher_service_options_are_exposed_by_service_shell():
    launcher = (ROOT / "benchmark/tau2/train/restart_vikingbot_train_eval.sh").read_text()
    start_service = launcher.split("start_tau2_service() {", 1)[1].split("\n}\n", 1)[0]
    options = re.findall(r"(?m)^\s+(--[\w-]+)\s", start_service)
    assert "--rollout-log-dir" in options
    help_result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        cwd=ROOT,
        env={**os.environ, "OPENVIKING_ENV_FILE": "/dev/null"},
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert all(option in help_result.stdout for option in options)
