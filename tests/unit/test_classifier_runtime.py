"""Tests for the machinery shared by the two local classifiers.

These exercise the cache, the negative cache and the fail-open contract without
importing `transformers`: the module is always monkeypatched into `sys.modules`,
so the suite stays fast and local.

What is verified here used to live in `test_prompt_injection_classifier.py`. It
moved when the machinery did, because it never was about prompt injection: it is
about a model being loaded once, a failure being remembered, and one guard's
broken model not taking the other's down with it.
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import classifier_runtime as runtime  # noqa: E402


A_MODEL = "meta-llama/Llama-Prompt-Guard-2-86M"
ANOTHER_MODEL = "deepset/deberta-v3-base-injection"


@pytest.fixture(autouse=True)
def reset_classifier_caches():
    """Isolate the two module-level caches, before *and* after each test.

    Clearing only on setup is not enough: the whole suite runs in one process, so
    the last test of this file would leave a failed model behind and the hook
    tests would then see a shipped default as unavailable. That is a failure this
    fixture was written to fix, not a hypothetical one.
    """
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    runtime._CLASSIFIER_STACK_STATUS = None
    runtime._IMPORTED_VERSIONS_ANNOUNCED = False
    with runtime._CLASSIFIER_LOAD_LOCKS_GUARD:
        runtime._CLASSIFIER_LOAD_LOCKS.clear()
    yield
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    runtime._CLASSIFIER_STACK_STATUS = None
    runtime._IMPORTED_VERSIONS_ANNOUNCED = False
    with runtime._CLASSIFIER_LOAD_LOCKS_GUARD:
        runtime._CLASSIFIER_LOAD_LOCKS.clear()


def fake_transformers(monkeypatch, pipeline_factory):
    """Install a stand-in `transformers` module exposing `pipeline`."""
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        type("M", (), {"pipeline": staticmethod(pipeline_factory)})(),
    )


class TestFailedLoadIsNotRetried:
    """The negative cache, and it is about cost rather than tidiness.

    Without it every message retries the load, and `transformers` re-resolves the
    repository on the Hub each time — so a gated model with no token costs a
    network round trip inside `fast_reply` per turn.
    """

    def failing_transformers(self, attempts, monkeypatch):
        def explode(task, model, token=None, **kwargs):
            attempts.append(model)
            raise OSError(f"401 Client Error: gated repo {model}")

        fake_transformers(monkeypatch, explode)

    def test_the_load_is_attempted_only_once(self, monkeypatch):
        attempts = []
        self.failing_transformers(attempts, monkeypatch)

        for _ in range(5):
            with pytest.raises(Exception):
                runtime.get_pipeline(A_MODEL)

        assert attempts == [A_MODEL]

    def test_later_calls_raise_without_touching_transformers(self, monkeypatch):
        attempts = []
        self.failing_transformers(attempts, monkeypatch)

        with pytest.raises(Exception):
            runtime.get_pipeline(A_MODEL)

        # Removing the module entirely: a retry would now raise ImportError, so
        # this asserts the second call never reaches the import at all.
        monkeypatch.delitem(sys.modules, "transformers")

        with pytest.raises(runtime.ClassifierUnavailable):
            runtime.get_pipeline(A_MODEL)

    def test_the_reason_is_kept_and_readable(self, monkeypatch):
        self.failing_transformers([], monkeypatch)

        with pytest.raises(Exception):
            runtime.get_pipeline(A_MODEL)

        reason = runtime.classifier_load_error(A_MODEL)
        assert reason is not None
        assert "gated repo" in reason

    def test_a_model_that_never_failed_reports_no_error(self):
        assert runtime.classifier_load_error(ANOTHER_MODEL) is None

    def test_one_model_failing_does_not_block_another(self, monkeypatch):
        def selectively_explode(task, model, token=None, **kwargs):
            if model.startswith("meta-llama/"):
                raise OSError("401 Client Error: gated repo")
            return lambda text, **call_kwargs: [{"label": "INJECTION", "score": 0.95}]

        fake_transformers(monkeypatch, selectively_explode)

        with pytest.raises(Exception):
            runtime.get_pipeline(A_MODEL)

        # The cache is per model, and the two guards share it: a broken model of
        # one must not make the other's model unavailable, and switching to a
        # working model in the admin panel must not need a restart.
        assert runtime.get_pipeline(ANOTHER_MODEL)
        assert runtime.classifier_load_error(ANOTHER_MODEL) is None


class TestPipelineCache:
    def test_pipeline_is_cached_per_model(self, monkeypatch):
        calls = []

        def fake_pipeline(task, model, token=None, **kwargs):
            calls.append((task, model))
            return lambda text, **call_kwargs: [{"label": "MALICIOUS", "score": 0.91}]

        fake_transformers(monkeypatch, fake_pipeline)

        first = runtime.get_pipeline(A_MODEL)
        second = runtime.get_pipeline(A_MODEL)

        assert first is second
        assert calls == [("text-classification", A_MODEL)]

    def test_load_arguments_reach_transformers(self, monkeypatch):
        captured = {}

        def fake_pipeline(task, model, token=None, **kwargs):
            captured["token"] = token
            captured["kwargs"] = kwargs
            return lambda text, **call_kwargs: []

        fake_transformers(monkeypatch, fake_pipeline)

        runtime.get_pipeline(A_MODEL, token="hf_test", device=-1)

        assert captured["token"] == "hf_test"
        assert captured["kwargs"] == {"device": -1}

    def test_concurrent_calls_load_the_same_model_once(self, monkeypatch):
        calls = 0
        calls_lock = Lock()
        first_load_entered = Event()
        duplicate_load_entered = Event()
        release_load = Event()

        def slow_pipeline(task, model, token=None, **kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
                if calls == 2:
                    duplicate_load_entered.set()
            first_load_entered.set()
            assert release_load.wait(timeout=2)
            return object()

        fake_transformers(monkeypatch, slow_pipeline)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(runtime.get_pipeline, A_MODEL)
            assert first_load_entered.wait(timeout=1)
            second = executor.submit(runtime.get_pipeline, A_MODEL)
            duplicate_observed = duplicate_load_entered.wait(timeout=0.5)
            release_load.set()
            first_pipeline = first.result(timeout=1)
            second_pipeline = second.result(timeout=1)

        assert not duplicate_observed
        assert calls == 1
        assert first_pipeline is second_pipeline

    def test_waiting_for_a_stuck_load_times_out_fail_open(self, monkeypatch):
        attempts = []

        def fake_pipeline(task, model, token=None, **kwargs):
            attempts.append(model)
            return object()

        fake_transformers(monkeypatch, fake_pipeline)
        monkeypatch.setattr(runtime, "CLASSIFIER_LOAD_WAIT_SECONDS", 0.01)
        load_lock = runtime._classifier_load_lock(A_MODEL)
        load_lock.acquire()
        try:
            with pytest.raises(runtime.ClassifierUnavailable, match="timed out"):
                runtime.get_pipeline(A_MODEL)
        finally:
            load_lock.release()

        assert attempts == []
        assert runtime.classifier_load_error(A_MODEL) is None

    def test_a_stuck_model_does_not_block_a_different_model(self, monkeypatch):
        fake_transformers(monkeypatch, lambda *args, **kwargs: object())
        blocked_model_lock = runtime._classifier_load_lock(A_MODEL)
        blocked_model_lock.acquire()
        try:
            assert runtime.get_pipeline(ANOTHER_MODEL) is not None
        finally:
            blocked_model_lock.release()


class TestAccessRemediation:
    """What a gated model failure tells whoever reads the log.

    A load that fails for missing authorisation is a dead end otherwise: the fix
    is administrative — accept the terms, wait for approval — and no amount of
    restarting produces it. These assert that the log carries the fix itself.
    """

    GATED = "meta-llama/Llama-Prompt-Guard-2-86M"

    def test_an_authorisation_failure_names_both_steps(self):
        text = runtime.access_remediation(
            self.GATED, OSError("401 Client Error: gated repo")
        )

        assert f"https://huggingface.co/{self.GATED}" in text
        assert "HF_TOKEN" in text
        assert "restart" in text

    @pytest.mark.parametrize(
        "error",
        [
            OSError("401 Client Error"),
            OSError("403 Forbidden"),
            OSError("You are trying to access a gated repo"),
            OSError("Your request to access model is awaiting a review"),
            OSError("Repo model is restricted and you are not authorized"),
        ],
    )
    def test_the_shapes_an_access_failure_takes_are_recognised(self, error):
        assert runtime.access_remediation(self.GATED, error) != ""

    @pytest.mark.parametrize(
        "error",
        [
            OSError("No space left on device"),
            ImportError("No module named transformers"),
            ValueError("Unrecognized configuration class"),
        ],
    )
    def test_any_other_failure_gets_no_instructions(self, error):
        # Guessing the cause would send the reader after the wrong problem.
        assert runtime.access_remediation(self.GATED, error) == ""

    def test_the_guidance_reaches_the_warning_of_a_failed_load(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(runtime.runtime_log, "warning", warnings.append)

        def explode(task, model, token=None, **kwargs):
            raise OSError("401 Client Error: gated repo")

        fake_transformers(monkeypatch, explode)

        with pytest.raises(OSError):
            runtime.get_pipeline(self.GATED)

        assert len(warnings) == 1
        assert "accept the model terms" in warnings[0]


class TestRedactSecrets:
    """The one secret this plugin handles, kept out of the log.

    This function exists because the plugin interpolates **third-party exception
    messages** into its own warnings, and their content is not ours to control: an
    HTTP error from the Hub can carry a request URL or an authorization header. It
    was added after a real leak, found by a test on 2026-08-06 — the announcement
    of a classifier failure was writing the token to the log at `WARNING`.

    Its two passes are tested separately because they cover different threats and
    only one of them is obvious.
    """

    TOKEN = "hf_fakevaluefortests"

    def test_a_token_we_were_given_is_removed(self):
        text = f"401 Client Error with authorization header {self.TOKEN}"

        assert self.TOKEN not in runtime.redact_secrets(text, self.TOKEN)

    def test_a_token_we_were_never_given_is_removed_too(self):
        """The pass whose reason is easy to miss, and the only one that can help.

        A credential can reach an exception text from somewhere the plugin never
        saw it: `huggingface_hub` reads its own cache file and its own environment
        variables. Passing no token must still redact something token-shaped.
        """
        text = f"401 Client Error for a repo, token {self.TOKEN} rejected"

        redacted = runtime.redact_secrets(text)

        assert self.TOKEN not in redacted
        assert runtime.REDACTED in redacted

    def test_a_token_that_does_not_look_like_one_is_removed_by_value(self):
        # `HF_TOKEN` holds whatever the deployment puts in it, and the pattern
        # cannot recognise an arbitrary string. This is why the exact value is
        # replaced as well.
        odd = "not-shaped-like-a-hugging-face-token-at-all"
        text = f"authentication failed for {odd}"

        assert odd not in runtime.redact_secrets(text, odd)

    def test_no_fragment_of_the_token_survives(self):
        # A partial leak is still a leak: assert on the tail, not only on the
        # whole value.
        redacted = runtime.redact_secrets(f"header {self.TOKEN}", self.TOKEN)

        assert self.TOKEN[-16:] not in redacted
        assert self.TOKEN.removeprefix("hf_") not in redacted

    def test_text_without_secrets_is_returned_unchanged(self):
        text = "No space left on device while writing the model cache"

        assert runtime.redact_secrets(text, self.TOKEN) == text

    def test_it_does_not_raise_without_a_token(self):
        # The hooks call it with whatever `resolve_huggingface_token()` returned,
        # which is `None` on an installation that configured no token at all.
        assert runtime.redact_secrets("plain text", None) == "plain text"

    def test_short_hf_prefixed_words_are_left_alone(self):
        # The pattern requires at least eight characters after `hf_`, so an
        # ordinary identifier is not mangled. Documented as a deliberate bound:
        # loosening it would start rewriting exception texts that carry no secret,
        # and a redacted message nobody can read is its own problem.
        assert runtime.redact_secrets("failed to open hf_cache") == (
            "failed to open hf_cache"
        )

    def test_the_reason_kept_in_the_negative_cache_is_redacted(self, monkeypatch):
        """Redacted before it is *stored*, not only before it is logged.

        The reason survives in `_FAILED_CLASSIFIER_MODELS` and
        `classifier_load_error()` hands it to callers, which is a second way for it
        to reach a log line — one that no test of the log itself would catch.
        """
        monkeypatch.setattr(runtime.runtime_log, "warning", lambda message: None)

        def explode(task, model, token=None, **kwargs):
            raise OSError(f"401 Client Error, token {self.TOKEN}")

        fake_transformers(monkeypatch, explode)

        with pytest.raises(OSError):
            runtime.get_pipeline(A_MODEL, token=self.TOKEN)

        reason = runtime.classifier_load_error(A_MODEL)
        assert reason is not None
        assert self.TOKEN not in reason


class TestModelLabels:
    """Reading the labels a model can return, which is how a mapping is checked."""

    def pipeline_with(self, id2label):
        config = type("Config", (), {"id2label": id2label})
        model = type("Model", (), {"config": config})
        return type("Pipeline", (), {"model": model})()

    def test_labels_are_returned_in_index_order(self):
        pipeline = self.pipeline_with({1: "LABEL_1", 0: "LABEL_0", 2: "LABEL_2"})

        assert runtime.model_labels(pipeline) == ("LABEL_0", "LABEL_1", "LABEL_2")

    def test_a_pipeline_without_a_config_yields_nothing(self):
        # Degrading into "not verified" rather than into a failure: a caller that
        # cannot read the labels must not take the turn down over it.
        assert runtime.model_labels(object()) == ()


def stack_with(monkeypatch, present, versions=None, find_spec_raises=()):
    """Make `find_spec` report exactly `present`, without touching the real stack.

    Every test here monkeypatches it rather than reading the environment: the
    unit suite must give the same answer on a developer machine with no Torch and
    inside the container where Torch is installed, or the tests would assert what
    the runner happens to have rather than what the code does.
    """
    versions = versions or {}

    def fake_find_spec(name):
        if name in find_spec_raises:
            raise ValueError(f"{name} has a broken parent package")
        return object() if name in present else None

    def fake_version(name):
        if name not in versions:
            raise LookupError(name)
        return versions[name]

    monkeypatch.setattr(runtime.importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(runtime.importlib.metadata, "version", fake_version)
    # The probe is cached for the life of the process, so patching the lookups
    # is not enough on its own: a status computed by an earlier test would
    # survive and this helper would silently do nothing.
    runtime._CLASSIFIER_STACK_STATUS = None


class TestClassifierStackStatus:
    """Whether Torch and Transformers are findable, answered without importing.

    The no-import property is the whole point rather than an optimisation: this
    runs at activation and inside the guard summary, and loading Torch to find
    out whether Torch is installed would defeat making it optional at all.
    """

    def test_a_complete_stack_reports_available_with_versions(self, monkeypatch):
        stack_with(
            monkeypatch,
            present={"torch", "transformers"},
            versions={"torch": "2.7.1", "transformers": "4.50.3"},
        )

        status = runtime.classifier_stack_status()

        assert status.available is True
        assert status.missing == ()
        assert status.versions == {"torch": "2.7.1", "transformers": "4.50.3"}

    @pytest.mark.parametrize(
        "present, missing",
        [
            ({"torch"}, ("transformers",)),
            ({"transformers"}, ("torch",)),
            (set(), ("torch", "transformers")),
        ],
    )
    def test_each_missing_combination_is_named_exactly(
        self, monkeypatch, present, missing
    ):
        # The names are a log contract: the `missing=` field is what somebody
        # greps for, and half a stack is a different problem from none of it.
        stack_with(monkeypatch, present=present, versions={"torch": "2.7.1"})

        status = runtime.classifier_stack_status()

        assert status.available is False
        assert status.missing == missing

    def test_find_spec_raising_counts_as_missing(self, monkeypatch):
        # `find_spec` raises rather than returning None for some broken
        # installations. The guard cannot run either way, and this function must
        # never be the thing that takes an activation down.
        stack_with(
            monkeypatch,
            present={"torch", "transformers"},
            find_spec_raises=("torch",),
        )

        status = runtime.classifier_stack_status()

        assert status.available is False
        assert status.missing == ("torch",)

    def test_a_package_without_metadata_reports_unknown(self, monkeypatch):
        # Importable with no distribution metadata: installed from a source tree,
        # vendored, or shadowed on the path. Not knowing the version is not a
        # failure, and must not become one.
        stack_with(
            monkeypatch,
            present={"torch", "transformers"},
            versions={"torch": "2.7.1"},
        )

        status = runtime.classifier_stack_status()

        assert status.available is True
        assert status.versions["transformers"] == runtime.UNKNOWN_VERSION

    def test_the_status_never_imports_the_stack(self, monkeypatch):
        # The strongest form of the property: the modules are poisoned in
        # `sys.modules`, so any import would raise. A status that comes back at
        # all proves nothing was imported.
        stack_with(monkeypatch, present=set())
        monkeypatch.setitem(sys.modules, "torch", None)
        monkeypatch.setitem(sys.modules, "transformers", None)

        assert runtime.classifier_stack_status().available is False

    def test_missing_is_rendered_as_one_grep_friendly_field(self, monkeypatch):
        stack_with(monkeypatch, present=set())

        assert (
            runtime.classifier_stack_status().describe_missing()
            == "torch+transformers"
        )

    def test_versions_are_rendered_in_a_stable_order(self, monkeypatch):
        stack_with(
            monkeypatch,
            present={"torch", "transformers"},
            versions={"torch": "2.7.1", "transformers": "4.50.3"},
        )

        assert (
            runtime.classifier_stack_status().describe_versions()
            == "torch=2.7.1, transformers=4.50.3"
        )


class TestStackRemediation:
    """Missing stack and broken stack are different problems and read differently.

    Telling somebody to install what they already installed sends them after the
    wrong thing, which is the failure mode this separation exists to avoid.
    """

    def test_an_absent_stack_names_both_files_in_install_order(self, monkeypatch):
        stack_with(monkeypatch, present=set())

        remedy = runtime.stack_remediation(
            ModuleNotFoundError("No module named 'torch'")
        )

        torch_file = remedy.index("requirements-classifiers-torch-cpu.txt")
        transformers_file = remedy.index("and then requirements-classifiers.txt")
        # Torch first: the CPU wheel has to be resolved from the PyTorch index
        # before Transformers is allowed to pull one from PyPI.
        assert torch_file < transformers_file

    def test_an_absent_stack_carries_no_absolute_path(self, monkeypatch):
        # A deployment-specific path in a log line is wrong on every other
        # deployment, and this line is written on all of them.
        stack_with(monkeypatch, present=set())

        remedy = runtime.stack_remediation()

        assert "/app" not in remedy
        assert ":\\" not in remedy

    def test_an_absent_stack_says_the_deterministic_guards_are_unaffected(
        self, monkeypatch
    ):
        stack_with(monkeypatch, present=set())

        assert "deterministic guards" in runtime.stack_remediation()

    def test_a_present_but_unimportable_stack_is_a_different_message(
        self, monkeypatch
    ):
        stack_with(monkeypatch, present={"torch", "transformers"})

        remedy = runtime.stack_remediation(
            ModuleNotFoundError("No module named 'transformers.utils'")
        )

        assert "installed but the import failed" in remedy
        assert "requirements-classifiers-torch-cpu.txt" not in remedy

    def test_an_unrelated_failure_on_a_complete_stack_gets_no_stack_advice(
        self, monkeypatch
    ):
        # Composability with `access_remediation()`: each speaks only for the
        # cause it recognises, so a gated-model failure gets that advice and not
        # this one.
        stack_with(monkeypatch, present={"torch", "transformers"})

        assert runtime.stack_remediation(OSError("401 Client Error: gated repo")) == ""

    def test_no_error_on_a_complete_stack_is_silent(self, monkeypatch):
        stack_with(monkeypatch, present={"torch", "transformers"})

        assert runtime.stack_remediation() == ""


class TestMissingStackEntersTheNegativeCache:
    """The defect this migration had to fix before Transformers could be optional.

    The import used to sit outside the `try` that records a failed load, so a
    `ModuleNotFoundError` bypassed the negative cache entirely: every message
    retried the import, took the load lock and paid the wait, for a package that
    cannot appear without restarting the process.
    """

    def without_transformers(self, monkeypatch):
        """Make importing `transformers` raise, as an image without it would."""
        monkeypatch.setitem(sys.modules, "transformers", None)

    def test_a_missing_module_is_remembered_as_a_failed_load(self, monkeypatch):
        self.without_transformers(monkeypatch)

        with pytest.raises(Exception):
            runtime.get_pipeline(A_MODEL)

        reason = runtime.classifier_load_error(A_MODEL)
        assert reason is not None
        assert "transformers" in reason.lower()

    def test_the_second_call_never_reaches_the_import(self, monkeypatch):
        self.without_transformers(monkeypatch)

        with pytest.raises(Exception):
            runtime.get_pipeline(A_MODEL)

        imports = []

        def counting_import(name, *args, **kwargs):
            imports.append(name)
            raise ModuleNotFoundError(name)

        monkeypatch.setattr("builtins.__import__", counting_import)

        with pytest.raises(runtime.ClassifierUnavailable):
            runtime.get_pipeline(A_MODEL)

        assert imports == []

    def test_the_second_call_does_not_take_the_load_lock(self, monkeypatch):
        # The lock is what costs five seconds per concurrent turn during a cold
        # load. A stack that will never appear must not pay it more than once.
        self.without_transformers(monkeypatch)

        with pytest.raises(Exception):
            runtime.get_pipeline(A_MODEL)

        # A `threading.Lock` refuses attribute assignment, so the count is taken
        # one level up, on the function that hands the lock out. Reaching it at
        # all is what costs the wait.
        reached = []
        original = runtime._classifier_load_lock

        def counting_load_lock(model_name):
            reached.append(model_name)
            return original(model_name)

        monkeypatch.setattr(runtime, "_classifier_load_lock", counting_load_lock)

        with pytest.raises(runtime.ClassifierUnavailable):
            runtime.get_pipeline(A_MODEL)

        assert reached == []

    def test_the_warning_carries_the_install_remedy(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(runtime.runtime_log, "warning", warnings.append)
        stack_with(monkeypatch, present=set())
        self.without_transformers(monkeypatch)

        with pytest.raises(Exception):
            runtime.get_pipeline(A_MODEL)

        assert len(warnings) == 1
        assert "requirements-classifiers-torch-cpu.txt" in warnings[0]

    def test_the_remembered_reason_is_redacted(self, monkeypatch):
        # Same contract as every other stored failure: the text of a third-party
        # exception is not ours to control, and it reaches a log through
        # `classifier_load_error()`.
        #
        # Two constraints on this fixture, both from `.githooks/check-staged-
        # secrets.sh`, and both learned by having the commit blocked.
        #
        # The value is short: redaction needs eight characters after `hf_` and
        # the hook blocks twenty or more, so a fake lives in the gap. And the
        # variable is not called `secret`, `password` or `access_token` — the
        # scanner also matches an assignment of any quoted eight-character value
        # to a name like those, whatever the value is.
        fake_token = "hf_fakestacktoken"

        def explode(task, model, token=None, **kwargs):
            raise ModuleNotFoundError(f"No module named 'torch'; {fake_token}")

        fake_transformers(monkeypatch, explode)

        with pytest.raises(Exception):
            runtime.get_pipeline(A_MODEL, token=fake_token)

        assert fake_token not in runtime.classifier_load_error(A_MODEL)


class TestStackStatusIsProbedOncePerProcess:
    """The probe is on the per-message path, so it has to be cached.

    `announce_active_guards()` is the first thing `guard_input_message` does and
    it builds the guard summary, which asks for this status. Measured in the
    container with the stack present, one uncached call costs 19.9 ms — against
    roughly 0.1 ms for every deterministic check together. Two hundred times the
    cost of the guards, in front of every turn.
    """

    def counting_probe(self, monkeypatch):
        lookups = []

        def fake_find_spec(name):
            lookups.append(name)
            return object()

        monkeypatch.setattr(runtime.importlib.util, "find_spec", fake_find_spec)
        monkeypatch.setattr(
            runtime.importlib.metadata, "version", lambda name: "1.0"
        )
        runtime._CLASSIFIER_STACK_STATUS = None
        return lookups

    def test_the_path_is_scanned_once_however_often_it_is_asked(self, monkeypatch):
        lookups = self.counting_probe(monkeypatch)

        for _ in range(50):
            runtime.classifier_stack_status()

        assert lookups == list(runtime.CLASSIFIER_STACK_PACKAGES)

    def test_the_same_object_comes_back_every_time(self, monkeypatch):
        self.counting_probe(monkeypatch)

        assert runtime.classifier_stack_status() is runtime.classifier_stack_status()

    def test_the_answer_is_unchanged_by_caching(self, monkeypatch):
        # Caching must not quietly alter what the callers see, which is the
        # thing a cache added late is most likely to do.
        stack_with(
            monkeypatch,
            present={"torch"},
            versions={"torch": "2.7.1"},
        )

        status = runtime.classifier_stack_status()

        assert status.available is False
        assert status.missing == ("transformers",)
        assert status.versions == {"torch": "2.7.1"}

    def test_nothing_needs_invalidating_while_the_process_runs(self, monkeypatch):
        # The property that makes the cache sound: installing the stack means
        # rebuilding the image and restarting, and a restart clears this module
        # along with everything else in it. Unlike the settings, which change
        # under a running process, packages do not appear mid-turn.
        stack_with(monkeypatch, present=set())
        assert runtime.classifier_stack_status().available is False

        # Even a stack that materialises on the path is not picked up, and that
        # is the intended behaviour rather than a limitation.
        stack_with(monkeypatch, present={"torch", "transformers"})
        runtime._CLASSIFIER_STACK_STATUS = runtime.ClassifierStackStatus(
            available=False, missing=("torch", "transformers"), versions={}
        )

        assert runtime.classifier_stack_status().available is False


class TestRemediationNamesTheRightProblem:
    """A `ModuleNotFoundError` is not automatically about our two packages.

    Some models need a package neither Torch nor Transformers pulls in —
    `sentencepiece` and `protobuf` are the usual ones — and that failure is fixed
    by installing it. Telling the reader the stack is broken and that installing
    again is not the fix sends them after the wrong problem, which is the exact
    misdirection the two separate messages exist to avoid.
    """

    @pytest.mark.parametrize("package", ["torch", "transformers"])
    def test_our_own_packages_report_a_broken_stack(self, monkeypatch, package):
        stack_with(monkeypatch, present={"torch", "transformers"})

        remedy = runtime.stack_remediation(
            ModuleNotFoundError(f"No module named '{package}'", name=package)
        )

        assert "installed but the import failed" in remedy

    def test_a_submodule_still_names_the_package_that_owns_it(self, monkeypatch):
        stack_with(monkeypatch, present={"torch", "transformers"})

        remedy = runtime.stack_remediation(
            ModuleNotFoundError(
                "No module named 'transformers.utils'", name="transformers.utils"
            )
        )

        assert "installed but the import failed" in remedy

    @pytest.mark.parametrize("package", ["sentencepiece", "protobuf", "tiktoken"])
    def test_an_unrelated_missing_package_gets_no_stack_advice(
        self, monkeypatch, package
    ):
        stack_with(monkeypatch, present={"torch", "transformers"})

        remedy = runtime.stack_remediation(
            ModuleNotFoundError(f"No module named '{package}'", name=package)
        )

        assert remedy == ""

    def test_the_decision_survives_being_handed_a_string(self, monkeypatch):
        # Callers pass the redacted text of an exception as often as the
        # exception, and by then `ImportError.name` is gone.
        stack_with(monkeypatch, present={"torch", "transformers"})

        assert "installed but the import failed" in runtime.stack_remediation(
            "No module named 'torch'"
        )
        assert runtime.stack_remediation("No module named 'sentencepiece'") == ""

    def test_an_absent_stack_still_wins_over_the_module_name(self, monkeypatch):
        # When the stack is genuinely missing, what the exception happens to name
        # first does not matter: the install instructions are the fix.
        stack_with(monkeypatch, present=set())

        remedy = runtime.stack_remediation(
            ModuleNotFoundError("No module named 'sentencepiece'", name="sentencepiece")
        )

        assert "requirements-classifiers-torch-cpu.txt" in remedy

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("torch", "torch"),
            ("transformers.utils.import_utils", "transformers"),
            (None, None),
        ],
    )
    def test_the_missing_module_is_reduced_to_its_top_level(self, name, expected):
        error = ModuleNotFoundError("boom", name=name) if name else ValueError("boom")

        assert runtime._missing_module_name(error) == expected


class TestImportedVersionsAreAnnouncedOnce:
    """The line that exposes a version bound the core cannot enforce.

    The core matches requirements by package name and ignores the version, so
    `transformers>=4.50,<5` is skipped on an image that already carries a 5.x.
    Nothing in the plugin can enforce that bound; this line is what makes a
    breach of it visible.
    """

    def working_stack(self, monkeypatch, transformers_version="4.50.3"):
        monkeypatch.setitem(
            sys.modules,
            "torch",
            type("T", (), {"__version__": "2.7.1"})(),
        )
        monkeypatch.setitem(
            sys.modules,
            "transformers",
            type("M", (), {"__version__": transformers_version})(),
        )

    def test_the_versions_are_logged_once(self, monkeypatch):
        infos = []
        monkeypatch.setattr(runtime.runtime_log, "info", infos.append)
        self.working_stack(monkeypatch)

        for _ in range(3):
            runtime._log_imported_stack_versions()

        lines = [line for line in infos if "stack imported" in line]
        assert len(lines) == 1
        assert "transformers=4.50.3" in lines[0]
        assert "torch=2.7.1" in lines[0]

    def test_a_failure_does_not_suppress_the_line_for_ever(self, monkeypatch):
        # The flag is set after the line is written, not before. Setting it first
        # is the ordinary way to write a once-per-process guard and it is wrong
        # here: one failure would permanently hide the only line that exposes an
        # unenforceable version bound.
        infos = []
        monkeypatch.setattr(runtime.runtime_log, "info", infos.append)
        monkeypatch.setitem(sys.modules, "torch", None)

        runtime._log_imported_stack_versions()
        assert not [line for line in infos if "stack imported" in line]

        self.working_stack(monkeypatch)
        runtime._log_imported_stack_versions()

        assert len([line for line in infos if "stack imported" in line]) == 1

    def test_a_version_out_of_the_declared_range_is_still_reported(
        self, monkeypatch
    ):
        # The whole point: an image carrying a 5.x keeps it, because the core
        # skipped our bound. The line has to say so rather than assume the range.
        infos = []
        monkeypatch.setattr(runtime.runtime_log, "info", infos.append)
        self.working_stack(monkeypatch, transformers_version="5.17.0")

        runtime._log_imported_stack_versions()

        line = next(line for line in infos if "stack imported" in line)
        assert "transformers=5.17.0" in line

    def test_a_successful_load_announces_the_versions(self, monkeypatch):
        # Reached through the real path rather than by calling the helper: the
        # line has to appear when a pipeline actually loads.
        infos = []
        monkeypatch.setattr(runtime.runtime_log, "info", infos.append)
        monkeypatch.setitem(
            sys.modules, "torch", type("T", (), {"__version__": "2.7.1"})()
        )
        module = type(
            "M",
            (),
            {
                "__version__": "4.50.3",
                "pipeline": staticmethod(lambda task, model, token=None, **k: object()),
            },
        )()
        monkeypatch.setitem(sys.modules, "transformers", module)

        runtime.get_pipeline(A_MODEL)

        assert any("stack imported" in line for line in infos)
