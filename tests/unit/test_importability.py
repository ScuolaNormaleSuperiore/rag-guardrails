"""Every test file must import without the plugin folder on `sys.path`.

Cheshire Cat collects a plugin's modules with `glob("**/*.py", recursive=True)`
and imports each one — `cat/mad_hatter/plugin.py`. `tests/` is not excluded, and
what an installation receives is the repository, not the zip that
`package-plugin.py` builds, so every `.py` in here is imported on every
activation.

An import that raises is caught per file, so the plugin still loads and its
hooks still register. What the administrator gets instead is
`ERROR ... Unable to load plugin rag-guardrails`, once per activation, for a
plugin that is in fact running. On a guardrail component that line reads as *the
controls are not active*, which is false and impossible to act on. It is not
hypothetical: a sibling plugin in the same installation produced fourteen such
lines, every one of them from its own `tests/`.

The core never touches `sys.path` — there is no `sys.path` assignment anywhere in
`cat/`. The plugin folder is therefore not importable by itself, which is why the
existing test files insert the repository root before importing `checks`. That
convention is what this file enforces.

**What this proves, and what it does not.** The probe imports each file by path
in a clean interpreter, so it reproduces what pytest and the core have in common:
no plugin folder on the path. It does *not* reproduce the core's module naming
(`cat.plugins.rag-guardrails.<module>`), which is what makes the runtime modules'
relative imports work. So a failure is only counted when the traceback names one
of the plugin's own modules — the missing path fix, the regression this catches.
Everything else is tolerated on purpose:

- `cat` absent, because the core lives in the container, not here;
- `pytest.importorskip` raising `Skipped` outside a pytest run, which is what
  `tests/integration/test_hooks.py` does by design.

Runtime modules are out of scope: they import each other through the
relative-first pattern documented in `rag_guardrails.py`, which only resolves
under the core's own naming. Their equivalent of this check is that the plugin
activates at all.

One decision worth knowing before editing: the probe restores `sys.path` and
purges newly imported modules **between files**, inside one interpreter. Without
that, a path fix performed by the first file would stay in `sys.path` for every
file after it, and a missing fix would pass unnoticed — the check would prove the
opposite of what it claims. One interpreter instead of one per file keeps the
pre-commit gate fast; the isolation is what makes it honest.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

SEPARATOR = "\x00"

# Imports each file by path and executes its module body, restoring the import
# state in between so no file benefits from what the previous one did. Nothing
# else runs: a test file defines its cases, it does not run them.
#
# `BaseException`, not `Exception`: pytest's `Skipped` inherits from
# `BaseException`, and `test_hooks.py` raises it through `importorskip` when the
# core is absent.
PROBE = textwrap.dedent(
    """
    import importlib.util, sys, traceback

    failures = []
    for index, path in enumerate(sys.argv[1:]):
        saved_path = list(sys.path)
        saved_modules = set(sys.modules)
        try:
            spec = importlib.util.spec_from_file_location(f"probe_{index}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except BaseException:
            failures.append(path + "\\n" + traceback.format_exc())
        finally:
            sys.path[:] = saved_path
            for name in set(sys.modules) - saved_modules:
                del sys.modules[name]

    sys.stdout.write("\\x00".join(failures))
    """
)


def plugin_modules() -> set[str]:
    """Top-level module names a test file can only reach through the path fix."""
    return {
        path.stem
        for path in REPO_ROOT.glob("*.py")
        if path.stem.isidentifier() and not path.name.startswith("test_")
    }


def discover_test_files() -> list[Path]:
    return sorted(
        path
        for path in TESTS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def run_probe(paths: list[Path]) -> dict[str, str]:
    # A neutral working directory and no PYTHONPATH: with the repository root
    # reachable, `import checks` would succeed for the wrong reason and the
    # check would pass on a file the core cannot import.
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}

    with tempfile.TemporaryDirectory() as neutral_cwd:
        result = subprocess.run(
            [sys.executable, "-c", PROBE, *(str(p) for p in paths)],
            cwd=neutral_cwd,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, f"the probe itself failed: {result.stderr}"

    reports = [chunk for chunk in result.stdout.split(SEPARATOR) if chunk.strip()]
    return {chunk.splitlines()[0]: chunk for chunk in reports}


class TestEveryTestFileImportsOnItsOwn:
    def test_there_are_test_files_to_probe(self):
        # A broken glob would make the test below pass by having nothing to run,
        # which is the failure mode this catches.
        assert discover_test_files(), f"no test file found under {TESTS_ROOT}"

    def test_no_test_file_needs_the_plugin_folder_on_the_path(self):
        files = discover_test_files()
        failures = run_probe(files)
        modules = plugin_modules()

        offending = {
            path: report
            for path, report in failures.items()
            if any(f"No module named '{module}'" in report for module in modules)
        }

        assert not offending, (
            "these files do not import on their own, so the core will log "
            "'Unable to load plugin rag-guardrails' for each of them on every "
            "activation. Insert the repository root into sys.path before "
            "importing the plugin's modules, as the other test files do:\n\n"
            + "\n\n".join(offending.values())
        )
