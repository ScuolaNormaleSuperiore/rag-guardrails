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


# The optional stack, installed by the image rather than by the core. Each file
# is a separate pip invocation on purpose, and the order between them is part of
# the contract: Torch has to come from the PyTorch CPU index before Transformers
# is allowed to resolve anything from PyPI.
TORCH_CPU_REQUIREMENTS = "requirements-classifiers-torch-cpu.txt"
CLASSIFIER_REQUIREMENTS = "requirements-classifiers.txt"


class TestAutomaticRequirements:
    """What the core installs by itself, which is now one package.

    `torch` and `transformers` moved out because the core installs requirements
    on every activation and cannot be told to use a different index: on a host
    with no GPU that pulled roughly 3 GB of CUDA wheels, and replaced
    `huggingface-hub` and `tokenizers`, which the core uses for its embedders.
    """

    def test_only_phonenumberslite_is_installed_automatically(self):
        lines = [
            line.strip()
            for line in (REPO_ROOT / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

        assert lines == ["phonenumberslite>=9"], (
            "requirements.txt is installed by the core on every activation and "
            "cannot carry the optional classifier stack: the core offers no way "
            "to choose an index, so torch resolves to a CUDA build."
        )

    def test_phonenumberslite_stays_mandatory(self):
        # `checks.py` imports it at module level, so it is not optional in any
        # sense: without it the plugin fails at import and no guard runs.
        assert "phonenumberslite" in (REPO_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        )

    def test_the_heavy_stack_is_not_installed_automatically(self):
        automatic = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")

        assert "torch" not in automatic
        assert "transformers" not in automatic


class TestOptionalRequirementFiles:
    """The two files pip reads and the core never does.

    They may carry pip options precisely because the core does not parse them:
    it opens the file named exactly `requirements.txt` and nothing else.
    """

    def read(self, name):
        return (REPO_ROOT / name).read_text(encoding="utf-8")

    def test_both_optional_files_exist(self):
        assert (REPO_ROOT / TORCH_CPU_REQUIREMENTS).is_file()
        assert (REPO_ROOT / CLASSIFIER_REQUIREMENTS).is_file()

    def test_both_optional_files_are_shipped(self):
        # The commands in README.md install them from the plugin directory, so
        # an image build has to find them where the plugin was unpacked.
        shipped = set(load_packaging_module().INCLUDED_FILES)

        assert TORCH_CPU_REQUIREMENTS in shipped
        assert CLASSIFIER_REQUIREMENTS in shipped

    def test_torch_comes_from_the_cpu_index(self):
        assert (
            "--index-url https://download.pytorch.org/whl/cpu"
            in self.read(TORCH_CPU_REQUIREMENTS)
        )

    def test_the_cpu_index_replaces_pypi_rather_than_competing_with_it(self):
        # `--extra-index-url` adds an index without ranking it, so pip may still
        # pick the CUDA wheel published on PyPI, and the configuration invites
        # dependency confusion. `--index-url` replaces PyPI for this invocation,
        # which is the only form that actually decides the outcome.
        assert "--extra-index-url" not in self.read(TORCH_CPU_REQUIREMENTS)

    def test_torch_is_bounded_below_the_next_major(self):
        assert "torch>=2,<3" in self.read(TORCH_CPU_REQUIREMENTS)

    def test_transformers_stays_on_the_verified_line(self):
        # The `<5` is about the transitive dependencies, not about the response
        # shape: the 5.x chain replaces `huggingface-hub` and `tokenizers`, which
        # the core uses for its embedders.
        assert "transformers>=4.50,<5" in self.read(CLASSIFIER_REQUIREMENTS)

    def test_the_two_stacks_stay_in_separate_files(self):
        # Two pip invocations, not one: `--index-url` applies to the whole
        # invocation, so installing Transformers under it would resolve it, and
        # everything it needs, from the PyTorch index too.
        assert "transformers" not in self.read(TORCH_CPU_REQUIREMENTS)
        assert "torch" not in self.read(CLASSIFIER_REQUIREMENTS)

    @pytest.mark.parametrize(
        "name", [TORCH_CPU_REQUIREMENTS, CLASSIFIER_REQUIREMENTS]
    )
    def test_no_comments_and_no_blank_lines(self, name):
        # The same hygiene as the automatic file even though the core never
        # reads these, for one reason: the day somebody merges a line back into
        # `requirements.txt`, a comment travelling with it makes the core
        # install nothing.
        lines = self.read(name).splitlines()

        assert lines == [line for line in lines if line.strip()]
        assert not [line for line in lines if line.lstrip().startswith("#")]

    @pytest.mark.parametrize(
        "name", [TORCH_CPU_REQUIREMENTS, CLASSIFIER_REQUIREMENTS]
    )
    def test_no_exact_pins(self, name):
        # The core compares requirements by package *name* and ignores the
        # version, so `==` protects nothing and breaks whoever installed first.
        assert "==" not in self.read(name)

    def test_the_core_parser_is_not_applied_to_the_optional_files(self):
        # Asserting the inverse of `test_requirements_carry_nothing_the_core
        # _cannot_parse`, so the two rules stay visibly different rather than
        # one drifting into the other: the option line is *correct* here and
        # fatal there, and the distinction is which file the core opens.
        first_line = self.read(TORCH_CPU_REQUIREMENTS).splitlines()[0]

        with pytest.raises(InvalidRequirement):
            Requirement(first_line)
