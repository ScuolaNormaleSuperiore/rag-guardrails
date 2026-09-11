"""Local classifier for offensive incoming messages.

The decision rule differs from the prompt-injection classifier's, and the
difference is the reason this is a separate module rather than a second entry in
that one: these models expose several classes that all mean «block», so the
threshold is compared against the **sum** of the blocking ones.

The shared machinery — pipeline cache, negative cache, lazy `transformers`
import, fail-open contract — lives in `classifier_runtime.py`.

Reference: DOC/ToneGuards.md, which describes this guard in detail.
"""

from __future__ import annotations

try:
    from .classifier_runtime import (
        get_pipeline,
        model_labels,
        normalize_scores,
        runtime_log,
    )
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from classifier_runtime import (
        get_pipeline,
        model_labels,
        normalize_scores,
        runtime_log,
    )


DEFAULT_OFFENSIVE_INPUT_CLASSIFIER_MODEL = "IMSyPP/hate_speech_multilingual"

# Measured, not inherited. On the seven-message probe recorded in
# DOC/ToneGuards.md the blocking sum was 0.42 for a frustrated user
# swearing at a broken service — which must pass — and 0.78 for explicit hate
# speech, which must not. The default sits in that gap, with margin on both
# sides.
#
# Do not raise this to the 0.85 of the prompt-injection classifier by analogy:
# there the threshold meets the score of a single label, here it meets a sum, so
# the same number is not the same statement. At 0.85 the measured hate-speech
# message was delivered unblocked.
DEFAULT_OFFENSIVE_INPUT_CLASSIFIER_THRESHOLD = 0.60

# What each label these models return actually means. This table is not
# decoration: all three of them expose `LABEL_0`, `LABEL_1`, … through
# `id2label`, so the readable class names of the model cards exist nowhere at
# runtime. Verified on the installed core on 2026-08-06, model by model, and the
# ordering of the four IMSyPP classes was confirmed by inference — a legitimate
# question scores 0.99 on `LABEL_0`, an insult 0.98 on `LABEL_2`, a threat
# 1.00 on `LABEL_3`.
#
# Keyed by what the pipeline returns, so a model that one day exposes readable
# labels needs only an identity mapping rather than a change of shape.
OFFENSIVE_INPUT_CLASSIFIER_CLASSES = {
    "IMSyPP/hate_speech_multilingual": {
        "LABEL_0": "appropriate",
        "LABEL_1": "inappropriate",
        "LABEL_2": "offensive",
        "LABEL_3": "violent",
    },
    "patriciacarla/HS-multilingual-DNR": {
        "LABEL_0": "acceptable",
        "LABEL_1": "inappropriate",
        "LABEL_2": "offensive",
        "LABEL_3": "violent",
    },
    "textdetox/bert-multilingual-toxicity-classifier": {
        "LABEL_0": "neutral",
        "LABEL_1": "toxic",
    },
}

# Which of those classes block, per model. Expressed in the readable names above
# and not in `LABEL_2`, because a reviewer has to be able to see what is being
# refused without counting indices.
#
# `inappropriate` is deliberately absent, and it is a decision rather than an
# omission: an help desk receives exasperated users, and a message that is
# rude about a broken service is a support request written badly. Refusing it is
# the kind of error that gets a guard switched off, which leaves no protection at
# all. See DOC/ToneGuards.md.
OFFENSIVE_INPUT_CLASSIFIER_LABELS = {
    "IMSyPP/hate_speech_multilingual": ("OFFENSIVE", "VIOLENT"),
    "patriciacarla/HS-multilingual-DNR": ("OFFENSIVE", "VIOLENT"),
    "textdetox/bert-multilingual-toxicity-classifier": ("TOXIC",),
}

# Models whose labels have already been checked against the table above, so the
# check costs one set lookup per message instead of a comparison.
_VERIFIED_MODELS: set[str] = set()


def supported_offensive_input_classifier_models() -> tuple[str, ...]:
    return tuple(OFFENSIVE_INPUT_CLASSIFIER_LABELS)


def blocking_classes(model_name: str) -> tuple[str, ...]:
    """The readable classes that block for this model, upper-cased."""
    return tuple(
        name.upper() for name in OFFENSIVE_INPUT_CLASSIFIER_LABELS.get(model_name, ())
    )


def _semantic_name(model_name: str, label: str) -> str:
    """Translate a label the pipeline returned into its readable class.

    Falls back to the raw label, so an unmapped label reaches the log as itself
    rather than disappearing — which is what makes the mismatch below diagnosable.
    """
    classes = OFFENSIVE_INPUT_CLASSIFIER_CLASSES.get(model_name, {})
    return classes.get(label, label)


def _warn_on_label_mismatch(model_name: str, pipeline) -> None:
    """Say out loud when a model cannot block anything.

    This is the failure this guard would otherwise hide. If the labels a model
    returns are not the ones the table maps, no blocking class is ever reached,
    every message passes, and nothing says so: a message that passes writes no
    verdict line, so a guard that is switched on and inert looks exactly like a
    guard that is finding nothing.

    Reported once per model, and never raised: a mapping problem must not take
    down the hook that runs before everything else.

    «Once per model» starts counting when the labels have actually been read.
    The record used to be written before the read, so a model whose
    configuration could not be parsed — the documented degradation of
    `model_labels()`, which then returns an empty tuple — was filed as verified
    without anything having been verified, and the check was never attempted
    again for the life of the plugin. An unreadable configuration now means «try
    again on the next message» rather than «assume it is fine forever», which is
    the only safe reading for a check whose whole purpose is to catch a guard
    that is switched on and cannot block.
    """
    if model_name in _VERIFIED_MODELS:
        return

    returned = model_labels(pipeline)
    if not returned:
        return

    _VERIFIED_MODELS.add(model_name)

    reachable = {
        _semantic_name(model_name, label).upper() for label in returned
    } & set(blocking_classes(model_name))

    if reachable:
        return

    runtime_log.warning(
        f"[rag-guardrails] offensive-input classifier model {model_name} "
        f"returns labels {'+'.join(returned)}, none of which maps to a blocking "
        f"class ({'+'.join(blocking_classes(model_name)) or 'none configured'}); "
        "the check is enabled but cannot block anything. Its label mapping in "
        "offensive_input_classifier.py needs updating"
    )


def classify_offensive_input(
    text: str,
    model_name: str = DEFAULT_OFFENSIVE_INPUT_CLASSIFIER_MODEL,
    threshold: float = DEFAULT_OFFENSIVE_INPUT_CLASSIFIER_THRESHOLD,
    token: str | None = None,
) -> dict[str, str | float | bool | None]:
    """Classify a message and decide whether it must be blocked.

    Blocks when the **sum** of the scores of the blocking classes reaches the
    threshold. The classes of these models are mutually exclusive — verified as
    `single_label_classification`, so the scores are a softmax and add up to one
    — which makes that sum the probability that the message belongs to the set we
    refuse. Comparing only the highest label instead would let through the case
    where the model is certain about the set and undecided inside it: 0.45
    `offensive` plus 0.40 `violent` is an 85% verdict that no single label
    reports.

    Returns the aggregate as `score` and the strongest blocking class as `label`,
    because a refusal has to say which behaviour was recognised, and the
    aggregate alone does not.
    """
    if not text.strip():
        return {"triggered": False, "label": None, "score": 0.0}

    blocking = set(blocking_classes(model_name))
    pipeline = get_pipeline(model_name, token=token)
    _warn_on_label_mismatch(model_name, pipeline)

    # truncation=True with no max_length: the bound is the tokenizer's own
    # `model_max_length`, which is the model's window in tokens. Deliberately not
    # derived from the message-length limit, which is a count of characters. The
    # prompt-injection classifier used to do exactly that and no longer does —
    # the two now agree, which is also what a common runner over both will need.
    scores = normalize_scores(pipeline(text, top_k=None, truncation=True))

    total = 0.0
    dominant_label: str | None = None
    dominant_score = 0.0
    for entry in scores:
        name = _semantic_name(model_name, str(entry.get("label", "")))
        if name.upper() not in blocking:
            continue
        score = float(entry.get("score", 0.0) or 0.0)
        total += score
        if score > dominant_score:
            dominant_score = score
            dominant_label = name

    return {
        "triggered": total >= threshold,
        "label": dominant_label,
        "score": total,
    }

