"""Tests for the offensive-input classifier's decision rule.

The pipeline is always a stub, so nothing here downloads a model. What the stubs
return is not invented: the four-class score sets are the ones
`IMSyPP/hate_speech_multilingual` actually produced on the installed core on
2026-08-06, recorded in `DOC/ToneGuards.md`. Using measured numbers is
what makes the threshold tests mean something — with made-up scores they would
only assert that a comparison operator works.
"""

import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import classifier_runtime as runtime  # noqa: E402
import offensive_input_classifier as offensive  # noqa: E402


MODEL = "IMSyPP/hate_speech_multilingual"
BINARY_MODEL = "textdetox/bert-multilingual-toxicity-classifier"

# Measured on the installed core. LABEL_0 appropriate, LABEL_1 inappropriate,
# LABEL_2 offensive, LABEL_3 violent — an ordering confirmed by inference, not
# only by the model card.
MEASURED = {
    "legitimate_ict": {
        "LABEL_0": 0.9915, "LABEL_1": 0.0030, "LABEL_2": 0.0043, "LABEL_3": 0.0012,
    },
    "frustrated_user": {
        "LABEL_0": 0.4033, "LABEL_1": 0.1737, "LABEL_2": 0.4222, "LABEL_3": 0.0008,
    },
    "insult": {
        "LABEL_0": 0.0134, "LABEL_1": 0.0031, "LABEL_2": 0.9814, "LABEL_3": 0.0021,
    },
    "hate_speech": {
        "LABEL_0": 0.2151, "LABEL_1": 0.0031, "LABEL_2": 0.7785, "LABEL_3": 0.0033,
    },
    "threat": {
        "LABEL_0": 0.0007, "LABEL_1": 0.0005, "LABEL_2": 0.0018, "LABEL_3": 0.9970,
    },
}


@pytest.fixture(autouse=True)
def reset_state():
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    offensive._VERIFIED_MODELS.clear()
    yield
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    offensive._VERIFIED_MODELS.clear()


def install_pipeline(model_name, scores, id2label=None, nested=True):
    """Put a stub pipeline in the shared cache, so no model is ever loaded."""
    labels = id2label or {
        index: label for index, label in enumerate(sorted(scores))
    }

    class Config:
        pass

    config = Config()
    config.id2label = labels

    class Model:
        pass

    model = Model()
    model.config = config

    class Pipeline:
        def __init__(self):
            self.model = model
            self.calls = []

        def __call__(self, text, **kwargs):
            self.calls.append(kwargs)
            entries = [{"label": k, "score": v} for k, v in scores.items()]
            return [entries] if nested else entries

    pipeline = Pipeline()
    runtime._CLASSIFIER_PIPELINES[model_name] = pipeline
    return pipeline


class TestSupportedModels:
    def test_supported_models_match_the_blocking_label_mapping(self):
        assert offensive.supported_offensive_input_classifier_models() == tuple(
            offensive.OFFENSIVE_INPUT_CLASSIFIER_LABELS
        )

    def test_every_supported_model_declares_its_classes(self):
        # Without the translation table a model's labels cannot be read, and the
        # blocking set is unreachable — the check would be inert.
        for model in offensive.supported_offensive_input_classifier_models():
            assert offensive.OFFENSIVE_INPUT_CLASSIFIER_CLASSES.get(model), model

    def test_every_blocking_class_exists_among_the_declared_classes(self):
        # A blocking class that no label maps to is a typo that disables the
        # check silently.
        for model, blocking in offensive.OFFENSIVE_INPUT_CLASSIFIER_LABELS.items():
            declared = {
                name.upper()
                for name in offensive.OFFENSIVE_INPUT_CLASSIFIER_CLASSES[model].values()
            }
            for name in blocking:
                assert name.upper() in declared, (model, name)

    def test_inappropriate_is_not_a_blocking_class(self):
        # A decision, not an accident: an exasperated user swearing at a broken
        # service is a support request written badly, and refusing it is what gets
        # a guard switched off. See DOC/ToneGuards.md.
        for blocking in offensive.OFFENSIVE_INPUT_CLASSIFIER_LABELS.values():
            assert "INAPPROPRIATE" not in {name.upper() for name in blocking}


class TestTheSumOfBlockingClasses:
    def test_empty_text_never_triggers(self):
        assert offensive.classify_offensive_input("   ") == {
            "triggered": False,
            "label": None,
            "score": 0.0,
        }

    @pytest.mark.parametrize(
        "case, expected_score, blocked_at_default",
        [
            ("legitimate_ict", 0.0055, False),
            ("frustrated_user", 0.4230, False),
            ("insult", 0.9835, True),
            ("hate_speech", 0.7818, True),
            ("threat", 0.9988, True),
        ],
    )
    def test_measured_messages_land_on_the_right_side(
        self, case, expected_score, blocked_at_default
    ):
        install_pipeline(MODEL, MEASURED[case])

        result = offensive.classify_offensive_input(
            "any text",
            model_name=MODEL,
            threshold=offensive.DEFAULT_OFFENSIVE_INPUT_CLASSIFIER_THRESHOLD,
        )

        assert result["score"] == pytest.approx(expected_score, abs=0.001)
        assert result["triggered"] is blocked_at_default

    def test_the_shipped_threshold_catches_hate_speech_that_0_85_would_miss(self):
        # The reason the default is not inherited from the prompt-injection
        # classifier. This message is explicit hate speech and its blocking sum is
        # 0.78: at 0.85 it would have been delivered.
        install_pipeline(MODEL, MEASURED["hate_speech"])

        at_default = offensive.classify_offensive_input(
            "any", model_name=MODEL,
            threshold=offensive.DEFAULT_OFFENSIVE_INPUT_CLASSIFIER_THRESHOLD,
        )
        install_pipeline(MODEL, MEASURED["hate_speech"])
        at_injection_threshold = offensive.classify_offensive_input(
            "any", model_name=MODEL, threshold=0.85
        )

        assert at_default["triggered"] is True
        assert at_injection_threshold["triggered"] is False

    def test_probability_split_between_two_blocking_classes_still_blocks(self):
        # The case the highest-label rule lets through: the model is certain about
        # the set and undecided inside it. Neither label reaches 0.60 alone.
        install_pipeline(MODEL, {
            "LABEL_0": 0.10, "LABEL_1": 0.05, "LABEL_2": 0.45, "LABEL_3": 0.40,
        })

        result = offensive.classify_offensive_input(
            "any", model_name=MODEL, threshold=0.60
        )

        assert result["score"] == pytest.approx(0.85)
        assert result["triggered"] is True

    def test_non_blocking_classes_are_not_added_to_the_sum(self):
        # `inappropriate` is 0.90 here and must contribute nothing.
        install_pipeline(MODEL, {
            "LABEL_0": 0.05, "LABEL_1": 0.90, "LABEL_2": 0.03, "LABEL_3": 0.02,
        })

        result = offensive.classify_offensive_input(
            "any", model_name=MODEL, threshold=0.60
        )

        assert result["score"] == pytest.approx(0.05)
        assert result["triggered"] is False

    def test_the_reported_label_is_the_strongest_blocking_class(self):
        # A refusal has to name the behaviour recognised: the sum alone does not.
        install_pipeline(MODEL, MEASURED["threat"])

        result = offensive.classify_offensive_input("any", model_name=MODEL)

        assert result["label"] == "violent"

    def test_the_threshold_is_inclusive(self):
        install_pipeline(MODEL, {
            "LABEL_0": 0.40, "LABEL_1": 0.0, "LABEL_2": 0.60, "LABEL_3": 0.0,
        })

        result = offensive.classify_offensive_input(
            "any", model_name=MODEL, threshold=0.60
        )

        assert result["triggered"] is True

    def test_a_binary_model_blocks_on_its_single_positive_class(self):
        install_pipeline(BINARY_MODEL, {"LABEL_0": 0.08, "LABEL_1": 0.92})

        result = offensive.classify_offensive_input(
            "any", model_name=BINARY_MODEL, threshold=0.60
        )

        assert result["triggered"] is True
        assert result["label"] == "toxic"
        assert result["score"] == pytest.approx(0.92)


class TestHowThePipelineIsCalled:
    def test_all_scores_are_requested_and_the_input_is_truncated(self):
        # `top_k=None` is what makes the sum possible at all, and truncation
        # without `max_length` bounds the input in tokens through the tokenizer's
        # own limit — deliberately not derived from the character limit of the
        # length guard.
        pipeline = install_pipeline(MODEL, MEASURED["insult"])

        offensive.classify_offensive_input("any", model_name=MODEL)

        assert pipeline.calls == [{"top_k": None, "truncation": True}]

    @pytest.mark.parametrize("nested", [True, False])
    def test_both_response_shapes_are_understood(self, nested):
        # Transformers has returned a list of dicts and a list containing one list
        # of dicts across versions.
        install_pipeline(MODEL, MEASURED["insult"], nested=nested)

        result = offensive.classify_offensive_input("any", model_name=MODEL)

        assert result["triggered"] is True


class TestLabelMismatchIsLoud:
    """The failure this guard would otherwise hide.

    If the labels a model returns are not the ones the table maps, no blocking
    class is ever reached, every message passes, and nothing says so — a message
    that passes writes no verdict line.
    """

    def test_a_model_returning_unmapped_labels_is_reported_once(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(
            offensive.runtime_log, "warning", lambda message: warnings.append(message)
        )
        install_pipeline(
            MODEL,
            {"SAFE": 0.9, "UNSAFE": 0.1},
            id2label={0: "SAFE", 1: "UNSAFE"},
        )

        offensive.classify_offensive_input("any", model_name=MODEL)
        offensive.classify_offensive_input("any", model_name=MODEL)

        assert len(warnings) == 1
        assert "cannot block anything" in warnings[0]
        assert MODEL in warnings[0]

    def test_a_mapping_that_matches_warns_about_nothing(self, monkeypatch):
        warnings = []
        monkeypatch.setattr(
            offensive.runtime_log, "warning", lambda message: warnings.append(message)
        )
        install_pipeline(MODEL, MEASURED["insult"])

        offensive.classify_offensive_input("any", model_name=MODEL)

        assert warnings == []

    def test_an_unmapped_label_does_not_raise(self, monkeypatch):
        # Fail-open all the way: a mapping problem must not take down the hook
        # that runs before everything else.
        monkeypatch.setattr(offensive.runtime_log, "warning", lambda message: None)
        install_pipeline(
            MODEL, {"SAFE": 0.9, "UNSAFE": 0.1}, id2label={0: "SAFE", 1: "UNSAFE"}
        )

        result = offensive.classify_offensive_input("any", model_name=MODEL)

        assert result["triggered"] is False
        assert result["score"] == 0.0

    def test_an_unreadable_configuration_is_retried_rather_than_assumed_fine(
        self, monkeypatch
    ):
        # The record of «already verified» is written only once the labels have
        # actually been read. It used to be written first, so a model whose
        # configuration could not be parsed — `model_labels()` returning an empty
        # tuple, its documented degradation — was filed as verified without
        # anything having been checked, and the check was never attempted again.
        # The failure it exists to catch is a guard that is switched on, blocks
        # nothing, and says nothing.
        warnings = []
        monkeypatch.setattr(
            offensive.runtime_log, "warning", lambda message: warnings.append(message)
        )

        # No `model` attribute at all, which is what `model_labels()` reads.
        unreadable = type("Pipeline", (), {"__call__": lambda self, text, **kw: []})()
        offensive._warn_on_label_mismatch(MODEL, unreadable)

        assert MODEL not in offensive._VERIFIED_MODELS
        assert warnings == []

        # The next message carries a readable configuration, and the check that
        # was skipped now runs and reports.
        install_pipeline(
            MODEL,
            {"SAFE": 0.9, "UNSAFE": 0.1},
            id2label={0: "SAFE", 1: "UNSAFE"},
        )
        offensive.classify_offensive_input("any", model_name=MODEL)

        assert MODEL in offensive._VERIFIED_MODELS
        assert len(warnings) == 1
        assert "cannot block anything" in warnings[0]
