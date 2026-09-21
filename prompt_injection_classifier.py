"""Local classifier for prompt-injection attempts.

Only the decision rule lives here: each model's raw labels are translated into
the shared `BENIGN` / `MALICIOUS` classes, then the malicious score is compared
against a threshold. The machinery around it — pipeline cache, negative cache
on failed loads, lazy `transformers` import, fail-open contract — is in
`classifier_runtime.py`, shared with the offensive-input classifier.

The plugin must keep working when the dependency is missing or the model cannot
load: this classifier is fail-open by design in v1.
"""

from __future__ import annotations

try:
    from .classifier_runtime import (
        ClassifierUnavailable,
        classifier_load_error,
        get_pipeline,
        model_labels,
        normalize_scores,
        runtime_log,
    )
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from classifier_runtime import (
        ClassifierUnavailable,
        classifier_load_error,
        get_pipeline,
        model_labels,
        normalize_scores,
        runtime_log,
    )

# Re-exported so callers that already import them from here keep working, and so
# this module reads as the whole prompt-injection story in one place.
__all__ = [
    "ClassifierUnavailable",
    "DEFAULT_PROMPT_INJECTION_CLASSIFIER_MODEL",
    "PROMPT_INJECTION_CLASSIFIER_CLASSES",
    "PROMPT_INJECTION_CLASSIFIER_LABELS",
    "classifier_load_error",
    "classify_prompt_injection",
    "supported_prompt_injection_classifier_models",
]


DEFAULT_PROMPT_INJECTION_CLASSIFIER_MODEL = "meta-llama/Llama-Prompt-Guard-2-86M"

PROMPT_INJECTION_CLASSIFIER_CLASSES = {
    "meta-llama/Llama-Prompt-Guard-2-86M": {
        "LABEL_0": "BENIGN",
        "LABEL_1": "MALICIOUS",
    },
    "meta-llama/Llama-Prompt-Guard-2-22M": {
        "LABEL_0": "BENIGN",
        "LABEL_1": "MALICIOUS",
    },
    "deepset/deberta-v3-base-injection": {
        "LEGIT": "BENIGN",
        "INJECTION": "MALICIOUS",
    },
}

# Kept as the public table of raw labels that can block. Deriving it from the
# translation table gives settings enumeration, startup validation and the
# decision rule one source of truth.
PROMPT_INJECTION_CLASSIFIER_LABELS = {
    model_name: next(
        raw_label
        for raw_label, semantic_class in classes.items()
        if semantic_class == "MALICIOUS"
    )
    for model_name, classes in PROMPT_INJECTION_CLASSIFIER_CLASSES.items()
}

# Models whose declared labels have already been checked, so the verification
# runs once per model rather than once per message.
_VERIFIED_MODELS: set[str] = set()


def supported_prompt_injection_classifier_models() -> tuple[str, ...]:
    return tuple(PROMPT_INJECTION_CLASSIFIER_CLASSES)


def _warn_on_label_mismatch(model_name: str, pipeline) -> None:
    """Say out loud when a model cannot reach its expected blocking label.

    This is narrower than the offensive-input guard's check because the decision
    rule here is narrower too: one expected label per model, not a set of
    semantic classes. If the model does not declare that label at all, the check
    is enabled but can never block, and without this warning it looks exactly
    like a quiet classifier finding nothing.
    """
    if model_name in _VERIFIED_MODELS:
        return

    returned = tuple(label.upper() for label in model_labels(pipeline))
    if not returned:  # pragma: no cover - defensive, every supported model has it
        return

    _VERIFIED_MODELS.add(model_name)
    expected = PROMPT_INJECTION_CLASSIFIER_LABELS[model_name]
    if expected in returned:
        return

    runtime_log.warning(
        f"[rag-guardrails] prompt-injection classifier model {model_name} "
        f"returns labels {'+'.join(returned)}, not the expected blocking label "
        f"{expected}; the check is enabled but cannot block anything. Its label "
        "mapping in prompt_injection_classifier.py needs updating"
    )


def classify_prompt_injection(
    text: str,
    model_name: str = DEFAULT_PROMPT_INJECTION_CLASSIFIER_MODEL,
    threshold: float = 0.85,
    token: str | bool = False,
) -> dict[str, str | float | bool | None]:
    """Classify a message and decide whether it must be blocked.

    Returns a small dict so callers can log what happened without relying on the
    raw pipeline response shape.
    """
    if not text.strip():
        return {"triggered": False, "label": None, "score": 0.0}

    label_classes = PROMPT_INJECTION_CLASSIFIER_CLASSES[model_name]

    pipeline = get_pipeline(model_name, token=token)
    _warn_on_label_mismatch(model_name, pipeline)

    # `truncation=True` with no `max_length`, the same as the offensive-input
    # classifier: the bound is the tokenizer's own `model_max_length`, which is
    # the model's window and is therefore always the right number whichever
    # model is configured.
    #
    # This used to take a `max_length` the hook filled in from the Limits guard's
    # character limit — two different units, and an accidental coupling between
    # two guards an administrator configures separately. Setting that limit to
    # `0` to disable the length check requested no truncation at all, and raising
    # it past the model's window made inference fail, which this guard turns into
    # a silent fail-open. Neither is a thing the Limits guard should be able to do.
    #
    # The response goes through the shared normalizer rather than being indexed
    # directly. This used to be `result[0] if isinstance(result, list) else
    # result`, which reads the common shape and breaks on the other two
    # `transformers` produces: a list containing one list of dicts raised
    # `AttributeError`, an empty response raised `IndexError`. Both failed open,
    # correctly, but the guard then reported itself as *unavailable* — a load or
    # token problem — when what had actually changed was the library version.
    scores = normalize_scores(pipeline(text, truncation=True))
    if not scores:
        return {"triggered": False, "label": None, "score": 0.0}

    top = scores[0]
    raw_label = str(top.get("label", "")).strip().upper()
    label = label_classes.get(raw_label, raw_label)
    score = float(top.get("score", 0.0) or 0.0)
    return {
        "triggered": label == "MALICIOUS" and score >= threshold,
        "label": label,
        "score": score,
    }
