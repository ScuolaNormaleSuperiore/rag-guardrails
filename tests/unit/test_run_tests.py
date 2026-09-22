"""Tests for the test runner's command construction."""

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "run-tests.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_tests", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pytest_args_targets_only_the_requested_suite():
    runner = load_runner()

    assert runner.pytest_args(False, "tests/unit")[-1] == "tests/unit"
    assert runner.pytest_args(False, "tests/integration")[-1] == "tests/integration"


def test_pytest_args_without_a_target_runs_the_full_suite_in_detail():
    runner = load_runner()

    assert runner.pytest_args(True) == [runner.sys.executable, "-m", "pytest", "-v"]
