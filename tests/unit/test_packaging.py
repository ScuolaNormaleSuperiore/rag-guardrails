"""Tests for what the release package contains.

The release zip is built from an explicit list in `package-plugin.py`. A runtime
module missing from that list produces a package that installs and then fails:
the core imports every `.py` it finds in the plugin folder, so one absent module
makes the whole plugin unloadable. Nothing in the build catches it, because the
build only checks that the files it *does* list exist.

These tests need no Cheshire Cat: `package-plugin.py` imports nothing from
`cat`, which is what keeps them in `tests/unit`.
"""

import importlib.util
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement


REPO_ROOT = Path(__file__).resolve().parents[2]

# Scripts that run *around* the plugin rather than inside it. They are the only
# top-level Python files that legitimately stay out of the release package.
DEVELOPMENT_SCRIPTS = {"run-tests.py", "package-plugin.py"}


def load_packaging_module():
    """Import `package-plugin.py` by path.

    The hyphen in the filename makes it an invalid identifier, so a plain
    `import` cannot reach it. Loading by path also avoids putting the repository
    root on `sys.path` for a module that is only needed here.
    """
    path = REPO_ROOT / "package-plugin.py"
    spec = importlib.util.spec_from_file_location("package_plugin", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        pytest.fail(f"cannot load {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestReleasePackageContents:
    def test_every_runtime_module_is_shipped(self):
        # The invariant that turns a written convention into a failing test:
        # add a module to the plugin, and the suite fails until it is also added
        # to INCLUDED_FILES.
        shipped = set(load_packaging_module().INCLUDED_FILES)
        runtime_modules = {
            path.name
            for path in REPO_ROOT.glob("*.py")
            if path.name not in DEVELOPMENT_SCRIPTS
        }

        missing = runtime_modules - shipped
        assert not missing, (
            f"runtime modules absent from INCLUDED_FILES: {sorted(missing)}. "
            "The core imports every .py in the plugin folder, so the installed "
            "plugin would fail to load."
        )

    def test_requirements_are_shipped_when_they_exist(self):
        # The worst omission of all, and the one the check above cannot see
        # because it only looks at Python modules: without requirements.txt in
        # the package, the core installs nothing at activation and the plugin
        # fails on its first import.
        if not (REPO_ROOT / "requirements.txt").is_file():
            pytest.skip("the plugin declares no dependencies")

        assert "requirements.txt" in load_packaging_module().INCLUDED_FILES

    def test_every_listed_file_exists(self):
        # The mirror case: a renamed or moved file leaves a stale entry in the
        # list. The build raises on it, but only when someone runs the build.
        assert load_packaging_module().validate_included_files()

    def test_requirements_carry_nothing_the_core_cannot_parse(self):
        # Regression test. The core does not use pip to read this file: it calls
        # packaging's Requirement() on every line it finds, inside a try that
        # catches and abandons the whole loop. One comment or blank line
        # therefore does not skip that line, it skips **every dependency**, and
        # the only symptom is one ERROR in the log while activation continues.
        # The plugin then loads on a machine that happens to have the packages
        # already, and fails at import on a clean one.
        # Asked of `Requirement()` itself rather than of a list of shapes we
        # remembered to forbid: the core's condition is «does this parse», so
        # anything else is an approximation of it. Comments and blank lines are
        # the two that reach here by accident; a pip option line such as
        # `--extra-index-url`, the obvious way to ask for a lighter torch build,
        # is the one that reaches here on purpose and would pass a check written
        # against the other two.
        requirements = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

        offending = []
        for line in requirements.splitlines():
            try:
                Requirement(line)
            except InvalidRequirement:
                offending.append(line)

        assert not offending, (
            f"requirements.txt carries lines the core cannot parse: {offending}. "
            "Comments, blank lines and pip option lines are all valid for pip "
            "and fatal here: the core calls Requirement() on every line inside "
            "a try that abandons the whole loop, so one of them makes it "
            "install no dependency at all."
        )

    def test_no_development_material_is_shipped(self):
        # `DOC/` is internal documentation and `DEV/` is private: neither ships
        # unless that is an explicit decision, and tests never ship at all.
        shipped = set(load_packaging_module().INCLUDED_FILES)

        private = {
            name
            for name in shipped
            if name.startswith(("DEV/", "DOC/", "tests/", ".githooks/"))
        }
        assert not private, f"development material in the package: {sorted(private)}"
