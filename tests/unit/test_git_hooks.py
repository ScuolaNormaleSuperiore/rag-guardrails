"""Regression tests for the staged-secret scanner.

The hook is intentionally a small Bash script rather than a Python dependency.
These tests execute that real script in disposable Git repositories so a broken
regular expression cannot silently remove protection from future commits.

They therefore need **both Bash and Git**, and neither is guaranteed: the
Cheshire Cat container has Bash but no Git, so running the whole suite there
used to produce eleven failures that said nothing about the code. Each tool is
now checked at the point of use and the tests skip with a reason when it is
missing, which is the idiom this file already used for Bash.

A skip is safe here in a way it would not be elsewhere, and the argument is
worth stating because it is what makes the coverage real rather than nominal:
these tests protect a **Git hook**, and a Git hook is invoked by Git. Wherever
the protection matters — a developer committing, where `pre-commit` runs
`tests/unit` — Git is present by definition. The environment that skips them is
the one where the hook cannot run in the first place.
"""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_HOOK = REPO_ROOT / ".githooks" / "check-staged-secrets.sh"


def _bash_executable() -> str:
    """Return Git Bash on Windows and ordinary Bash elsewhere."""
    if os.name == "nt":
        git_executable = shutil.which("git")
        if git_executable:
            git_bash = Path(git_executable).resolve().parents[1] / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash)

    bash_executable = shutil.which("bash")
    if bash_executable:
        return bash_executable

    pytest.skip("Bash is unavailable; staged-secret hook tests cannot run")


def _require_git() -> None:
    """Skip when Git is unavailable, instead of failing on FileNotFoundError.

    The Cheshire Cat container is the case in point: it has no Git, so every
    test that stages a line raised a bare FileNotFoundError from `subprocess`
    and reported as a failure — eleven of them, none about this repository.
    """
    if shutil.which("git") is None:
        pytest.skip("Git is unavailable; staged-secret hook tests cannot run")


def _stage_line(
    tmp_path: Path,
    line: str,
    hook: Path = SECRET_HOOK,
) -> subprocess.CompletedProcess[str]:
    _require_git()

    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    fixture = tmp_path / "fixture.txt"
    fixture.write_text(f"{line}\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "fixture.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    return subprocess.run(
        [_bash_executable(), str(hook)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )


SECRET_SHAPES = (
    ("private-key", lambda: "-----BEGIN " + "PRIVATE " + "KEY-----"),
    ("aws", lambda: "AK" + "IA" + "A" * 16),
    ("github-classic", lambda: "gh" + "p_" + "A" * 36),
    ("github-fine-grained", lambda: "github_" + "pat_" + "A" * 20),
    ("hugging-face", lambda: "h" + "f_" + "A" * 20),
    ("slack", lambda: "xo" + "xb-" + "A" * 10),
    ("openai", lambda: "s" + "k-" + "A" * 20),
    (
        "credential-url",
        lambda: "https://" + "user:" + "password" + "@example.org/private",
    ),
    (
        "quoted-assignment",
        lambda: "api_" + "key = " + chr(34) + "A" * 20 + chr(34),
    ),
)


def test_every_declared_pattern_has_a_secret_shape_regression_case():
    source = SECRET_HOOK.read_text(encoding="utf-8")
    pattern_block = source.split("patterns=(", 1)[1].split("\n)", 1)[0]
    declared_patterns = [
        line
        for line in pattern_block.splitlines()
        if line.lstrip().startswith(("'", "$'"))
    ]

    assert len(declared_patterns) == len(SECRET_SHAPES), (
        "the secret scanner's pattern list changed without a matching "
        "SECRET_SHAPES regression case"
    )


@pytest.mark.parametrize(("name", "secret_factory"), SECRET_SHAPES)
def test_every_supported_secret_shape_blocks_commit(tmp_path, name, secret_factory):
    result = _stage_line(tmp_path, secret_factory())

    assert result.returncode == 1, (
        f"{name} was not detected; scanner output:\n{result.stderr}"
    )
    assert "Potential secret detected" in result.stderr


def test_safe_staged_text_passes(tmp_path):
    result = _stage_line(tmp_path, "ordinary documentation without credentials")

    assert result.returncode == 0
    assert "Potential secret detected" not in result.stderr


def test_scanner_error_blocks_commit_instead_of_passing_silently(tmp_path):
    broken_hook = tmp_path / "broken-secret-hook.sh"
    hook_source = SECRET_HOOK.read_text(encoding="utf-8")
    broken_hook.write_text(
        hook_source.replace("'(AKIA|ASIA)[A-Z0-9]{16}'", "'['", 1),
        encoding="utf-8",
    )

    result = _stage_line(
        tmp_path,
        "ordinary documentation without credentials",
        hook=broken_hook,
    )

    assert result.returncode == 2
    assert "Secret scan failed" in result.stderr
    assert "did not complete" in result.stderr
