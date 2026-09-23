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
        CLASSIFIER_MAX_INPUT_TOKENS,
        classifier_load_error,
        get_pipeline,
        model_labels,
        normalize_scores,
        runtime_log,
    )
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from classifier_runtime import (
        ClassifierUnavailable,
        CLASSIFIER_MAX_INPUT_TOKENS,
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

# The `(model, raw label)` pairs already reported as unmapped, so a model that
# returns an unknown label on every message writes one line instead of one per
# turn. Keyed by the pair and not by the model alone: a second unknown label is
# a second piece of information, and the first one must not hide it.
_UNMAPPED_LABELS: set[tuple[str, str]] = set()


def supported_prompt_injection_classifier_models() -> tuple[str, ...]:
    return tuple(PROMPT_INJECTION_CLASSIFIER_CLASSES)


def _warn_on_label_mismatch(model_name: str, pipeline) -> None:
    """Say out loud when a model cannot reach its blocking label.

    Both guards translate raw labels into semantic classes; what stays narrower
    here is the decision rule, which reads the top class only instead of summing
    a blocking set. So this check has one raw label to look for — the one
    `PROMPT_INJECTION_CLASSIFIER_CLASSES` maps to `MALICIOUS` — rather than a
    set. If the model does not declare that label at all, the check is enabled
    but can never block, and without this warning it looks exactly like a quiet
    classifier finding nothing.

    This inspects what the model *declares*, at load time. The companion check
    `_warn_on_unmapped_label` inspects what it *returns*, message by message:
    a configuration can declare the right labels and a later revision still
    answer with something the table does not know.
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


def _warn_on_unmapped_label(model_name: str, raw_label: str) -> None:
    """Say out loud when a model answers with a label the table does not know.

    The guard fails open on such a label, which is the right behaviour and the
    silent one: an unmapped label is indistinguishable from a benign verdict at
    every point downstream, so a model whose vocabulary changed under an
    unchanged name would stop blocking without a single line saying so. The
    revision of these models is not pinned, which is precisely how that happens.

    Reported once per `(model, label)` pair, and never raised: a mapping problem
    must not take down the hook that runs before everything else.
    """
    seen = (model_name, raw_label)
    if seen in _UNMAPPED_LABELS:
        return

    _UNMAPPED_LABELS.add(seen)
    known = "+".join(PROMPT_INJECTION_CLASSIFIER_CLASSES[model_name])
    runtime_log.warning(
        f"[rag-guardrails] prompt-injection classifier model {model_name} "
        f"returned the label {raw_label or '(empty)'}, which is not in its "
        f"mapping ({known}); the message was let through unclassified. Its label "
        "mapping in prompt_injection_classifier.py needs updating"
    )


def classify_prompt_injection(
    text: str,
    model_name: str = DEFAULT_PROMPT_INJECTION_CLASSIFIER_MODEL,
    threshold: float = 0.85,
    token: str | bool = False,
    device: int = -1,
) -> dict[str, str | float | bool | None]:
    """Classify a message and decide whether it must be blocked.

    Returns a small dict so callers can log what happened without relying on the
    raw pipeline response shape.
    """
    if not text.strip():
        return {"triggered": False, "label": None, "score": 0.0}

    label_classes = PROMPT_INJECTION_CLASSIFIER_CLASSES[model_name]

    pipeline_kwargs = {"token": token}
    if device >= 0:
        pipeline_kwargs["device"] = device
    pipeline = get_pipeline(model_name, **pipeline_kwargs)
    _warn_on_label_mismatch(model_name, pipeline)

    # `truncation=True` with no `max_length`, the same as the offensive-input
    # classifier: the bound asked for is the tokenizer's own `model_max_length`,
    # which is the model's window and is therefore the right number whichever
    # model is configured — *when the tokenizer declares one*.
    #
    # Neither Meta checkpoint does. Measured on 2026-09-21 in the container:
    # `model_max_length` is the sentinel `1e30` for both the 86M and the 22M, and
    # transformers says so out loud — «Asking to truncate to max_length but no
    # maximum length is provided and the model has no predefined maximum length.
    # Default to no truncation.» So on the two models this plugin can actually
    # run offline, nothing is truncated and the whole message reaches inference.
    #
    # That does not bring back the coupling removed below — a character limit is
    # still the wrong unit for a token window — but it does mean the upper bound
    # on what this classifier is asked to embed comes from the Limits guard's
    # character limit or from nowhere at all. What that costs when the limit is
    # disabled is an open issue in `DEV/AGENTS/ISSUES_TODO.md`.
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
    scores = normalize_scores(
        pipeline(text, truncation=True, max_length=CLASSIFIER_MAX_INPUT_TOKENS)
    )
    if not scores:
        return {"triggered": False, "label": None, "score": 0.0}

    top = scores[0]
    raw_label = str(top.get("label", "")).strip().upper()
    score = float(top.get("score", 0.0) or 0.0)

    # An unmapped label fails open explicitly rather than by falling through the
    # comparison. The raw label used to be carried forward as if it were a
    # semantic class, which was correct for every label except one: a model
    # answering with the literal string `MALICIOUS` while absent from the table
    # would have blocked on a mapping nobody wrote. It reaches the caller as
    # itself, so the log names what the model actually said.
    semantic_class = label_classes.get(raw_label)
    if semantic_class is None:
        _warn_on_unmapped_label(model_name, raw_label)
        return {"triggered": False, "label": raw_label, "score": score}

    return {
        "triggered": semantic_class == "MALICIOUS" and score >= threshold,
        "label": semantic_class,
        "score": score,
    }
