"""Shared runtime for the local text-classification models.

Two guards run a local model — the prompt-injection classifier and the
offensive-input classifier — and they need exactly the same machinery around it:
a lazy import of `transformers`, one pipeline per model kept in memory, a memory
of the loads that already failed, and a fail-open contract. That machinery lives
here so it exists once.

The caches are keyed by model name and shared by every caller, which is the
correct behaviour rather than a side effect: a model is loaded once per process,
whoever asks for it. It also keeps `classifier_load_error()` a single function,
so a caller that needs to know whether a model is usable asks one question
regardless of which guard it belongs to.

This module imports nothing from `cat` beyond the logger, and `transformers` only
inside `get_pipeline()`. Nothing here decides whether a message is blocked: that
belongs to the classifier modules, because the decision rule differs between
them — one label against a threshold for prompt injection, a sum of labels for
offensive input.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

try:
    from cat.log import log as runtime_log
except Exception:  # pragma: no cover - available only with the core importable
    runtime_log = logging.getLogger(__name__)


_CLASSIFIER_PIPELINES: dict[str, Any] = {}

# A cold model load can involve disk I/O and a Hugging Face download. Only one
# request may perform that work for a given model, while different models remain
# independent. Waiting is bounded so a stuck third-party load cannot hold every
# later request indefinitely; callers turn the timeout into the normal fail-open
# classifier behaviour.
CLASSIFIER_LOAD_WAIT_SECONDS = 5.0
_CLASSIFIER_LOAD_LOCKS: dict[str, Any] = {}
_CLASSIFIER_LOAD_LOCKS_GUARD = threading.Lock()

# Models whose load already failed, with the reason. This is a negative cache and
# it exists for cost, not for tidiness: without it every message retries the
# load, and `transformers` re-resolves the repository on the Hub each time, so a
# gated model with no token costs a network round trip inside `fast_reply`, the
# hook that runs before everything else.
#
# Retrying cannot help anyway: the token comes from the settings or from the
# environment, and neither changes without the plugin reloading, which clears
# this dict along with the successful pipelines above.
_FAILED_CLASSIFIER_MODELS: dict[str, str] = {}


class ClassifierUnavailable(RuntimeError):
    """A model that already failed to load and is not being retried."""


REDACTED = "***redacted***"

# Any token-shaped string. Needed on top of replacing the token we were handed,
# because a credential can reach an exception text from somewhere we never saw it:
# the library's own cache file, or an environment variable read by
# `huggingface_hub` rather than by us.
_TOKEN_SHAPED = re.compile(r"hf_[A-Za-z0-9]{8,}")


def redact_secrets(text: str, token: str | None = None) -> str:
    """Remove anything credential-shaped from text on its way to the log.

    This exists because the plugin interpolates **third-party exception messages**
    into the warnings it writes, and their content is not ours to control: an HTTP
    error from the Hub can carry a request URL or an authorization header. Auditing
    every version of every dependency for what it puts in an exception is not a
    strategy; redacting on the way out is.

    Two passes, and both are needed. The exact value catches a token that does not
    look like one — `HF_TOKEN` can hold anything. The pattern catches one we were
    never given.
    """
    if token:
        text = text.replace(token, REDACTED)
    return _TOKEN_SHAPED.sub(f"hf_{REDACTED}", text)


# What a load failure looks like when the cause is authorisation rather than a
# broken installation. Matched against the message text because `transformers`
# wraps several different exception types from `huggingface_hub` and the type
# alone does not distinguish «you have no access» from «the disk is full».
_ACCESS_ERROR_MARKERS = (
    "401",
    "403",
    "gated",
    "awaiting a review",
    "not authorized",
    "restricted",
    "authenticated",
    "access to model",
)


def access_remediation(model_name: str, error: Exception) -> str:
    """Instructions for a load that failed because of missing authorisation.

    Empty when the failure looks like anything else: a guess about the cause is
    worse than silence, because it sends whoever reads the log after the wrong
    problem.

    This exists because the failure is otherwise a dead end for the reader. Some
    supported models are gated — access granted manually by their publisher — so
    the fix is administrative and not technical, and no amount of restarting will
    produce it. The two steps below are the whole fix.
    """
    if not any(marker in str(error).lower() for marker in _ACCESS_ERROR_MARKERS):
        return ""

    return (
        f" This model needs authorised access, so the fix is not technical: "
        f"1) accept the model terms at https://huggingface.co/{model_name} and wait "
        f"for approval, which for the Meta models is granted manually and is not "
        f"immediate; "
        f"2) set the HF_TOKEN environment variable to a Hugging Face read token, or "
        f"fill in the token field in the plugin settings, then restart the container "
        f"— the failure is remembered and not retried until the plugin reloads. "
        f"A model that needs no authentication can be selected instead from the "
        f"plugin settings, and takes effect immediately."
    )


def classifier_load_error(model_name: str) -> str | None:
    """Why this model is unavailable, or None if it has not failed.

    Lets callers keep their own reporting honest — a check that cannot run must
    not be listed among the ones covering a turn.
    """
    return _FAILED_CLASSIFIER_MODELS.get(model_name)


def _classifier_load_lock(model_name: str):
    """Return the single load lock assigned to `model_name`."""
    with _CLASSIFIER_LOAD_LOCKS_GUARD:
        return _CLASSIFIER_LOAD_LOCKS.setdefault(model_name, threading.Lock())


def get_pipeline(model_name: str, token: str | None = None, **pipeline_kwargs):
    """Return the cached text-classification pipeline for `model_name`.

    Raises `ClassifierUnavailable` for a model whose load already failed, and
    whatever `transformers` raises the first time a load fails. Both are errors
    the callers turn into fail-open behaviour: a classifier that cannot run must
    leave the message alone, never take the turn down.

    `pipeline_kwargs` reaches `transformers.pipeline()` and is part of the cache
    identity only through the model name, which is deliberate: the two guards
    pass different arguments — `top_k=None` for the one that needs every score —
    and a model configured one way must not be silently reused with the other
    configuration. Callers therefore must not vary these arguments for the same
    model, and today none does: each model belongs to one guard.
    """
    pipeline = _CLASSIFIER_PIPELINES.get(model_name)
    if pipeline is not None:
        # INFO for v1, deliberately, even though this fires on every message
        # that reaches a classifier: while the feature is being evaluated,
        # seeing the pipeline being reused is worth the volume. It is the one
        # line this plugin writes per message at the default level — reconsider
        # demoting it to DEBUG once real traffic shows whether it is noise.
        # See DOC/SecurityGuards.md, section *Logging and measurement*.
        runtime_log.info(
            "[rag-guardrails] classifier pipeline cache hit "
            f"for model {model_name}"
        )
        return pipeline

    previous_error = _FAILED_CLASSIFIER_MODELS.get(model_name)
    if previous_error is not None:
        # Nothing is logged here: the failure was reported when it happened, and
        # repeating it once per message is the flood this cache removes.
        raise ClassifierUnavailable(previous_error)

    load_lock = _classifier_load_lock(model_name)
    if not load_lock.acquire(timeout=CLASSIFIER_LOAD_WAIT_SECONDS):
        # The loading request may have completed exactly as the timeout fired.
        # Re-read both caches before degrading this caller to fail-open.
        pipeline = _CLASSIFIER_PIPELINES.get(model_name)
        if pipeline is not None:
            return pipeline
        previous_error = _FAILED_CLASSIFIER_MODELS.get(model_name)
        if previous_error is not None:
            raise ClassifierUnavailable(previous_error)
        raise ClassifierUnavailable(
            "timed out waiting for another request to load classifier model "
            f"{model_name} after {CLASSIFIER_LOAD_WAIT_SECONDS:g} seconds"
        )

    try:
        # Another request may have populated either cache while this one waited.
        pipeline = _CLASSIFIER_PIPELINES.get(model_name)
        if pipeline is not None:
            runtime_log.info(
                "[rag-guardrails] classifier pipeline cache hit "
                f"for model {model_name}"
            )
            return pipeline

        previous_error = _FAILED_CLASSIFIER_MODELS.get(model_name)
        if previous_error is not None:
            raise ClassifierUnavailable(previous_error)

        from transformers import pipeline as transformers_pipeline

        runtime_log.info(
            "[rag-guardrails] loading classifier model "
            f"{model_name} into memory; Transformers will use the local Hugging Face "
            "cache when available and download missing files if needed"
        )
        try:
            pipeline = transformers_pipeline(
                "text-classification",
                model=model_name,
                token=token,
                **pipeline_kwargs,
            )
        except Exception as error:
            # Redacted before it is stored, not only before it is logged: the reason is
            # kept in the negative cache and handed to callers by
            # `classifier_load_error()`, which is another way for it to reach a log.
            reason = redact_secrets(str(error), token)
            _FAILED_CLASSIFIER_MODELS[model_name] = reason
            runtime_log.warning(
                "[rag-guardrails] failed to load classifier "
                f"model {model_name}: {reason}; it will not be retried until the "
                f"plugin reloads.{access_remediation(model_name, error)}"
            )
            raise

        runtime_log.info(
            "[rag-guardrails] classifier model "
            f"{model_name} loaded and cached in memory"
        )
        _CLASSIFIER_PIPELINES[model_name] = pipeline
        return pipeline
    finally:
        load_lock.release()


def model_labels(pipeline) -> tuple[str, ...]:
    """The labels a loaded model can actually return, in index order.

    Read from the model configuration rather than assumed, because that is the
    only place the truth is: `pipeline()` returns whatever `id2label` says, and
    for these models it says `LABEL_0`, `LABEL_1`, not the readable class names
    the model cards describe.

    Returns an empty tuple when the configuration cannot be read, so a caller
    validating its label mapping degrades into *not verifying* rather than into
    a failure.
    """
    try:
        id2label = pipeline.model.config.id2label
    except AttributeError:  # pragma: no cover - defensive, all models carry it
        return ()
    return tuple(str(id2label[index]) for index in sorted(id2label))

