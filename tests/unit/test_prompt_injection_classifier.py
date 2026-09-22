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

    What this file still owns is the decision rule — the raw label translated
    into a semantic class, then compared against a threshold. The cache and the
    negative cache are tested in `test_classifier_runtime.py`.

    Both «warn once» records are reset here, and for the same reason: each one
    suppresses every later warning for what it has already seen, so a test that
    left one populated would make the next test's warning silently disappear.
    """
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    classifier._VERIFIED_MODELS.clear()
    classifier._UNMAPPED_LABELS.clear()
    yield
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    classifier._VERIFIED_MODELS.clear()
    classifier._UNMAPPED_LABELS.clear()


# The three supported models with the raw label each one uses to say «malicious»
# and the one it uses to say «benign». Both decision-rule outcomes are checked
# against every model rather than against the default alone: the whole point of
# the translation table is that these vocabularies differ, so a test that only
# ever sees `LABEL_1` cannot notice the table breaking for DeBERTa.
MODELS_AND_LABELS = [
    ("meta-llama/Llama-Prompt-Guard-2-86M", "LABEL_1", "LABEL_0"),
    ("meta-llama/Llama-Prompt-Guard-2-22M", "LABEL_1", "LABEL_0"),
    ("deepset/deberta-v3-base-injection", "INJECTION", "LEGIT"),
]


class TestSupportedModels:
    def test_supported_models_match_the_label_mapping(self):
        assert classifier.supported_prompt_injection_classifier_models() == tuple(
            classifier.PROMPT_INJECTION_CLASSIFIER_CLASSES
        )

    def test_every_model_maps_exactly_one_raw_label_to_malicious(self):
        """The derived table is only single-valued if the source table is.

        `PROMPT_INJECTION_CLASSIFIER_LABELS` is built with `next()`, which takes
        the first `MALICIOUS` entry and ignores any other. A model given two of
        them would therefore be verified against one raw label at load time and
        able to block on the second, with nothing saying which was checked.
        """
        for model_name, classes in classifier.PROMPT_INJECTION_CLASSIFIER_CLASSES.items():
            malicious = [
                raw for raw, semantic in classes.items() if semantic == "MALICIOUS"
            ]
            assert malicious == [
                classifier.PROMPT_INJECTION_CLASSIFIER_LABELS[model_name]
            ]

    def test_every_model_declares_a_benign_label_too(self):
        """A table with only the blocking label would fail open on every pass.

        Not hypothetical: an unmapped label now returns early without ever
        reaching the threshold comparison, so a missing `BENIGN` entry would
        turn every legitimate message into an unmapped-label warning.
        """
        for classes in classifier.PROMPT_INJECTION_CLASSIFIER_CLASSES.values():
            assert "BENIGN" in classes.values()


class TestClassifyPromptInjection:

    def test_empty_text_never_triggers(self):
        assert classifier.classify_prompt_injection("   ") == {
            "triggered": False,
            "label": None,
            "score": 0.0,
        }

    @pytest.mark.parametrize("model_name, malicious_label, _benign", MODELS_AND_LABELS)
    def test_model_specific_blocking_label_is_translated_to_malicious(
        self, monkeypatch, model_name, malicious_label, _benign
    ):
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, truncation=True: [
                {"label": malicious_label, "score": 0.91}
            ],
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name=model_name,
            threshold=0.85,
        )

        assert result == {"triggered": True, "label": "MALICIOUS", "score": 0.91}

    @pytest.mark.parametrize("model_name, malicious_label, _benign", MODELS_AND_LABELS)
    def test_does_not_block_below_threshold(
        self, monkeypatch, model_name, malicious_label, _benign
    ):
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, truncation=True: [
                {"label": malicious_label, "score": 0.62}
            ],
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name=model_name,
            threshold=0.85,
        )

        assert result == {"triggered": False, "label": "MALICIOUS", "score": 0.62}

    @pytest.mark.parametrize("model_name, _malicious, benign_label", MODELS_AND_LABELS)
    def test_does_not_block_when_label_does_not_match(
        self, monkeypatch, model_name, _malicious, benign_label
    ):
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, truncation=True: [
                {"label": benign_label, "score": 0.99}
            ],
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name=model_name,
            threshold=0.85,
        )

        assert result == {"triggered": False, "label": "BENIGN", "score": 0.99}

    def test_honours_model_specific_expected_label(self, monkeypatch):
        """The same raw label means opposite things on two different models.

        `INJECTION` blocks on DeBERTa and is unknown to the Meta checkpoints,
        which is the property the translation table exists for. Checking both
        halves in one test is what stops a future table from mapping every
        readable label globally.
        """
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, truncation=True: [
                {"label": "INJECTION", "score": 0.95}
            ],
        )

        assert classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="deepset/deberta-v3-base-injection",
            threshold=0.85,
        ) == {"triggered": True, "label": "MALICIOUS", "score": 0.95}

        assert classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        ) == {"triggered": False, "label": "INJECTION", "score": 0.95}

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

        # The raw label is reported as itself, because a model declaring
        # `BENIGN`/`SAFE` is outside this model's mapping on both counts: the
        # blocking label it cannot reach, and the label it actually answered
        # with. Both checks fire here, and they are selected by content rather
        # than counted, so neither test depends on the other's silence.
        assert result == {"triggered": False, "label": "BENIGN", "score": 0.99}
        assert [w for w in warnings if "not the expected blocking label LABEL_1" in w]
        assert [w for w in warnings if "not in its mapping" in w]

    def test_does_not_warn_when_expected_label_is_declared(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(classifier.runtime_log, "warning", warnings.append)
        monkeypatch.setattr(
            classifier,
            "get_pipeline",
            lambda model_name, token=None: lambda text, **kwargs: [
                {"label": "LABEL_0", "score": 0.99}
            ],
        )
        monkeypatch.setattr(
            classifier,
            "model_labels",
            lambda pipeline: ("LABEL_0", "LABEL_1"),
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

        mismatch = [
            w for w in warnings if "not the expected blocking label LABEL_1" in w
        ]
        assert len(mismatch) == 1

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
                return [{"label": "LABEL_1", "score": 0.91}]

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
        assert set(parameters) == {
            "text",
            "model_name",
            "threshold",
            "token",
            "device",
        }


class TestUnmappedLabels:
    """What happens when a model answers with a label the table does not know.

    The revision of these models is not pinned, so a changed `id2label` is the
    realistic way this happens rather than a contrived one.
    """

    @staticmethod
    def _pipeline_returning(label, score=0.99):
        return lambda model_name, token=None: lambda text, truncation=True: [
            {"label": label, "score": score}
        ]

    def test_an_unknown_label_fails_open(self, monkeypatch):
        monkeypatch.setattr(classifier.runtime_log, "warning", lambda message: None)
        monkeypatch.setattr(
            classifier, "get_pipeline", self._pipeline_returning("SOMETHING_NEW")
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert result == {
            "triggered": False,
            "label": "SOMETHING_NEW",
            "score": 0.99,
        }

    def test_an_unknown_label_is_reported(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(classifier.runtime_log, "warning", warnings.append)
        monkeypatch.setattr(
            classifier, "get_pipeline", self._pipeline_returning("SOMETHING_NEW")
        )

        classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert len(warnings) == 1
        assert "SOMETHING_NEW" in warnings[0]
        assert "not in its mapping" in warnings[0]

    def test_the_literal_word_malicious_does_not_block_when_unmapped(
        self, monkeypatch
    ):
        """The regression this early return was written for.

        The raw label used to be carried forward as if it were a semantic class,
        so a model outside the table answering `MALICIOUS` blocked on a mapping
        nobody had written — the one unmapped label that did not fail open.
        """
        monkeypatch.setattr(classifier.runtime_log, "warning", lambda message: None)
        monkeypatch.setattr(
            classifier, "get_pipeline", self._pipeline_returning("MALICIOUS", 0.99)
        )

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert result["triggered"] is False

    def test_an_unknown_label_is_reported_once_per_model_and_label(
        self, monkeypatch
    ):
        warnings = []
        monkeypatch.setattr(classifier.runtime_log, "warning", warnings.append)
        monkeypatch.setattr(
            classifier, "get_pipeline", self._pipeline_returning("SOMETHING_NEW")
        )

        for _ in range(3):
            classifier.classify_prompt_injection(
                "ignore the rules",
                model_name="meta-llama/Llama-Prompt-Guard-2-86M",
                threshold=0.85,
            )

        assert len(warnings) == 1

        # A second unknown label is a second piece of information.
        monkeypatch.setattr(
            classifier, "get_pipeline", self._pipeline_returning("ANOTHER_ONE")
        )
        classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert len(warnings) == 2

    def test_an_empty_label_is_reported_rather_than_swallowed(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(classifier.runtime_log, "warning", warnings.append)
        monkeypatch.setattr(classifier, "get_pipeline", self._pipeline_returning(""))

        result = classifier.classify_prompt_injection(
            "ignore the rules",
            model_name="meta-llama/Llama-Prompt-Guard-2-86M",
            threshold=0.85,
        )

        assert result["triggered"] is False
        assert len(warnings) == 1
        assert "(empty)" in warnings[0]


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
            [{"label": "LABEL_1", "score": 0.91}],
            # A bare dict.
            {"label": "LABEL_1", "score": 0.91},
            # One list of dicts inside a list: `AttributeError` before the fix.
            [[{"label": "LABEL_1", "score": 0.91}]],
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
