"""Run the rag-guardrails test suite.

This is the single source of truth for test execution: the pre-commit hook, CI
and manual runs all go through this file. See `DOC/TestingCode.md`.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent
SERVICE = "cheshire-cat-core"
PLUGIN_IN_CONTAINER = "/app/cat/plugins/rag-guardrails"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the rag-guardrails test suite."
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "-u",
        "--unit",
        action="store_true",
        help="run only tests/unit with the current local Python interpreter",
    )
    scope.add_argument(
        "-i",
        "--integration",
        action="store_true",
        help="run only tests/integration in the Cheshire Cat container",
    )
    parser.add_argument(
        "-d",
        "--detailed",
        action="store_true",
        help="pass -v to pytest, listing every test name",
    )
    return parser.parse_args()


def pytest_args(detailed: bool, test_path: str | None = None) -> list[str]:
    args = [sys.executable, "-m", "pytest"]
    if test_path:
        args.append(test_path)
    if detailed:
        args.append("-v")
    return args


def run_local_unit_tests(detailed: bool) -> int:
    print("Unit tests (pure logic), local interpreter")

    probe = subprocess.run(
        [sys.executable, "-c", "import pytest"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        print("pytest is not installed in this interpreter:", file=sys.stderr)
        print(f"  {sys.executable}", file=sys.stderr)
        print(
            f"Install it once with:  {Path(sys.executable).name} -m pip install pytest",
            file=sys.stderr,
        )
        print("Or use the suite in the container:  python run-tests.py", file=sys.stderr)
        return 1

    result = subprocess.run(pytest_args(detailed, "tests/unit"), cwd=REPO_ROOT)
    return result.returncode


def compose_dir() -> Path:
    directory = (REPO_ROOT / "../../../..").resolve()
    if not (directory / "compose.yml").is_file():
        raise FileNotFoundError(
            "compose.yml not found where expected:\n"
            f"  {directory / 'compose.yml'}\n"
            "This script assumes the plugin lives in core/cat/plugins/ of the "
            "Stregatto project."
        )
    return directory


def detect_compose_command() -> list[str] | None:
    docker = shutil.which("docker")
    if docker:
        probe = subprocess.run(
            [docker, "compose", "version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return [docker, "compose"]

    docker_compose = shutil.which("docker-compose")
    if docker_compose:
        return [docker_compose]

    return None


def running_container_id(compose_cmd: list[str], cwd: Path) -> str:
    probe = subprocess.run(
        [*compose_cmd, "ps", "-q", SERVICE],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.stdout.strip()


def run_container_suite(detailed: bool, integration_only: bool = False) -> int:
    try:
        project_dir = compose_dir()
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 1

    compose_cmd = detect_compose_command()
    if compose_cmd is None:
        print("Neither 'docker compose' nor 'docker-compose' is available.", file=sys.stderr)
        print("Run only the unit tests instead:  python run-tests.py --unit", file=sys.stderr)
        return 1

    container_id = running_container_id(compose_cmd, project_dir)
    if not container_id:
        print(f"The '{SERVICE}' container is not running.", file=sys.stderr)
        print(
            f"Start it from {project_dir} with:  {' '.join(compose_cmd)} up -d",
            file=sys.stderr,
        )
        print("Or run only the unit tests:  python run-tests.py --unit", file=sys.stderr)
        return 1

    if integration_only:
        print(f"Integration tests (hook adapters) in the '{SERVICE}' container")
    else:
        print(f"Full suite (unit + integration) in the '{SERVICE}' container")

    command = [
        *compose_cmd,
        "exec",
        "-T",
        "-w",
        PLUGIN_IN_CONTAINER,
        SERVICE,
        "python",
        "-m",
        "pytest",
    ]
    if integration_only:
        command.append("tests/integration")
    if detailed:
        command.append("-v")

    env = os.environ.copy()
    env.setdefault("MSYS_NO_PATHCONV", "1")
    result = subprocess.run(command, cwd=project_dir, env=env, check=False)
    return result.returncode


def main() -> int:
    args = parse_args()
    if args.unit:
        return run_local_unit_tests(args.detailed)
    return run_container_suite(args.detailed, integration_only=args.integration)


if __name__ == "__main__":
    raise SystemExit(main())

