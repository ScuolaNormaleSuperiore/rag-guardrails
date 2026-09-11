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
    with runtime._CLASSIFIER_LOAD_LOCKS_GUARD:
        runtime._CLASSIFIER_LOAD_LOCKS.clear()
    yield
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
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
