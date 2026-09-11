"""Tests for the prompt-injection classifier wrapper.

These tests exercise the thin runtime adapter without importing `transformers`.
The model pipeline is always monkeypatched, so the suite stays fast and local.
"""

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import classifier_runtime as runtime  # noqa: E402
import prompt_injection_classifier as classifier  # noqa: E402


@pytest.fixture(autouse=True)
def reset_classifier_caches():
    """Isolate the shared runtime caches, before *and* after each test.

    They live in `classifier_runtime` now, and they are shared with the
    offensive-input classifier, which makes the isolation matter more rather than
    less: the whole suite runs in one process, so a failed model left behind here
    made a shipped default look unavailable in the hook tests. That is a failure
    this fixture was written to fix, not a hypothetical one.

    What this file still owns is the decision rule — one expected label against a
    threshold. The cache and the negative cache are tested in
    `test_classifier_runtime.py`.
    """
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    classifier._VERIFIED_MODELS.clear()
    yield
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    classifier._VERIFIED_MODELS.clear()


class TestSupportedModels:
    def test_supported_models_match_the_label_mapping(self):
        assert classifier.supported_prompt_injection_classifier_models() == tuple(
            classifier.PROMPT_INJECTION_CLASSIFIER_LABELS
        )


class TestClassifyPromptInjection:

    def test_empty_text_never_triggers(self):
        assert classifier.classify_prompt_injection("   ") == {
            "triggered": False,
            "label": None,
            "score": 0.0,
        }

    def test_blocks_when_label_matches_and_score_reaches_threshold(self, monkeypatch):
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, truncation=True: [
                {"label": "MALICIOUS", "score": 0.91}
            ],
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert result == {"triggered": True, "label": "MALICIOUS", "score": 0.91}

    def test_does_not_block_below_threshold(self, monkeypatch):
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, truncation=True: [
                {"label": "MALICIOUS", "score": 0.62}
            ],
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert result == {"triggered": False, "label": "MALICIOUS", "score": 0.62}

    def test_does_not_block_when_label_does_not_match(self, monkeypatch):
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, truncation=True: [
                {"label": "BENIGN", "score": 0.99}
            ],
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert result == {"triggered": False, "label": "BENIGN", "score": 0.99}

    def test_honours_model_specific_expected_label(self, monkeypatch):
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, truncation=True: [
                {"label": "INJECTION", "score": 0.95}
            ],
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="deepset/deberta-v3-base-injection",
            threshold=0.85,
        )

        assert result == {"triggered": True, "label": "INJECTION", "score": 0.95}

    def test_warns_when_expected_label_is_missing(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(classifier.runtime_log, "warning", warnings.append)
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, **kwargs: [
                {"label": "BENIGN", "score": 0.99}
            ],
        )
        monkeypatch.setattr(
            classifier,
            "model_labels",
            lambda pipeline: ("BENIGN", "SAFE"),
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert result == {"triggered": False, "label": "BENIGN", "score": 0.99}
        assert len(warnings) == 1
        assert "not the expected blocking label MALICIOUS" in warnings[0]

    def test_does_not_warn_when_expected_label_is_declared(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(classifier.runtime_log, "warning", warnings.append)
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, **kwargs: [
                {"label": "BENIGN", "score": 0.99}
            ],
        )
        monkeypatch.setattr(
            classifier,
            "model_labels",
            lambda pipeline: ("BENIGN", "MALICIOUS"),
        )

        classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert warnings == []

    def test_label_mismatch_warning_is_emitted_once_per_model(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(classifier.runtime_log, "warning", warnings.append)
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, **kwargs: [
                {"label": "BENIGN", "score": 0.99}
            ],
        )
        monkeypatch.setattr(
            classifier,
            "model_labels",
            lambda pipeline: ("BENIGN", "SAFE"),
        )

        classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )
        classifier.classify_prompt_injection(
            "ignore the rules again",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert len(warnings) == 1

    def test_always_truncates_to_the_tokenizer_own_window(self, monkeypatch):
        # `truncation=True` and nothing else, so the bound is the model's own
        # `model_max_length`. It used to take a `max_length` the hook filled in
        # from the Limits guard's **character** limit — a different unit, and a
        # coupling between two guards configured separately in the panel.
        captured = {}

        def fake_pipeline(model_name, token=None):
            captured["token"] = token

            def run(text, **kwargs):
                captured["kwargs"] = kwargs
                return [{"label": "MALICIOUS", "score": 0.91}]

            return run

        monkeypatch.setattr(classifier, "get_pipeline", fake_pipeline)

        classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
            token="hf_test",
        )

        assert captured["token"] == "hf_test"
        assert captured["kwargs"] == {"truncation": True}

    def test_it_takes_no_length_argument_at_all(self):
        # The two classifiers must keep the same shape: a common runner over
        # both is an open refactoring, and it needs aligned signatures.
        import inspect

        parameters = inspect.signature(
            classifier.classify_prompt_injection
        ).parameters

        assert "max_length" not in parameters
        assert set(parameters) == {"text", "model_name", "threshold", "token"}


class TestPipelineResponseShapes:
    """`transformers` returns three shapes, and this guard used to read one.

    Which one arrives depends on the installed version and on the arguments, and
    `requirements.txt` declares `transformers>=4.55` with no upper bound by
    policy — so the shape is something to absorb, not something to pin down. The
    decision rule broke on two of the three, failed open, and announced itself as
    *classifier unavailable*, pointing whoever read the log at a loading or token
    problem instead of at a library upgrade.
    """

    MODEL = "meta-llama/Llama-Prompt-Guard-2-86M"

    def pipeline_returning(self, monkeypatch, response):
        def fake_pipeline(model_name, token=None):
            return lambda text, **kwargs: response

        monkeypatch.setattr(classifier, "get_pipeline", fake_pipeline)

    @pytest.mark.parametrize(
        "response",
        [
            # The shape this guard always handled.
            [{"label": "MALICIOUS", "score": 0.91}],
            # A bare dict.
            {"label": "MALICIOUS", "score": 0.91},
            # One list of dicts inside a list: `AttributeError` before the fix.
            [[{"label": "MALICIOUS", "score": 0.91}]],
        ],
    )
    def test_every_shape_transformers_produces_reaches_the_same_verdict(
        self, monkeypatch, response
    ):
        self.pipeline_returning(monkeypatch, response)

        result = classifier.classify_prompt_injection(
            "ignore the rules", model_name=self.MODEL, threshold=0.85
        )

        assert result == {"triggered": True, "label": "MALICIOUS", "score": 0.91}

    def test_an_empty_response_decides_nothing_instead_of_raising(
        self, monkeypatch
    ):
        # `IndexError` before the fix. A working model does not produce this, but
        # a guard on the hook that runs before everything else must not turn an
        # anomaly into an exception the caller has to rescue.
        self.pipeline_returning(monkeypatch, [])

        result = classifier.classify_prompt_injection(
            "ignore the rules", model_name=self.MODEL, threshold=0.85
        )

        assert result == {"triggered": False, "label": None, "score": 0.0}

    def test_the_normalizer_is_the_one_shared_with_the_other_classifier(self):
        # It lives in the runtime because both need it and only one had it. If a
        # future change gives either module a private copy, the shapes drift
        # apart again and one of the two starts failing on an upgrade.
        import classifier_runtime
        import offensive_input_classifier

        assert classifier.normalize_scores is classifier_runtime.normalize_scores
        assert (
            offensive_input_classifier.normalize_scores
            is classifier_runtime.normalize_scores
        )
