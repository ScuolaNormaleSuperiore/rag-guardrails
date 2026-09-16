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

`torch` and `transformers` are **optional**: the core installs neither, and an
image built without them runs every deterministic guard unchanged. That is why
`classifier_stack_status()` exists and why it answers with `find_spec()` rather
than with an import — the callers are an activation and a guard summary, and
neither may pull hundreds of megabytes into the process just to report that the
stack is absent. The absence is a supported configuration, so it is reported and
never raised.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import logging
import re
import threading
from dataclasses import dataclass
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


# The two packages the classifier guards need and the deterministic guards do
# not. They are installed by the image rather than by the core, so their absence
# is a normal configuration and not a broken installation.
CLASSIFIER_STACK_PACKAGES = ("torch", "transformers")

UNKNOWN_VERSION = "unknown"


@dataclass(frozen=True)
class ClassifierStackStatus:
    """Which of the optional packages are importable, and at which version.

    Deliberately says *findable*, not *working*: it is built from
    `importlib.util.find_spec()`, which locates a module without executing it.
    A package that is present but broken looks available here and fails at the
    real import, which is why `get_pipeline()` keeps its own failure path.

    `versions` carries an entry only for a package that was found, and
    `UNKNOWN_VERSION` when the distribution metadata cannot be read.
    """

    available: bool
    missing: tuple[str, ...]
    versions: dict[str, str]

    def describe_missing(self) -> str:
        """The missing packages as one stable field value, or an empty string."""
        return "+".join(self.missing)

    def describe_versions(self) -> str:
        """The packages that were found, as `name=version` pairs."""
        return ", ".join(
            f"{name}={self.versions[name]}"
            for name in CLASSIFIER_STACK_PACKAGES
            if name in self.versions
        )


def _package_version(package: str) -> str:
    """The installed version of `package`, read without importing it.

    `importlib.metadata.version()` reads the distribution metadata from disk, so
    it costs nothing and — this is the point — does not pull Torch into the
    process. Reporting the version must never become a reason to load the very
    stack this module exists to keep optional.
    """
    try:
        return importlib.metadata.version(package)
    except Exception:
        # A package can be importable with no distribution metadata: installed
        # from a source tree, vendored, or shadowed by a directory on the path.
        # Not knowing the version is not a failure.
        return UNKNOWN_VERSION


# The probe result, computed once per process. See `classifier_stack_status()`
# for why caching it is not an optimisation but a correctness matter for the
# per-message path. Tests reset it the way they reset the pipeline caches.
_CLASSIFIER_STACK_STATUS: ClassifierStackStatus | None = None


def classifier_stack_status() -> ClassifierStackStatus:
    """Whether the optional classifier stack is present, without importing it.

    Called at activation and by the guard summary, and the guard summary runs on
    **every message**: `announce_active_guards()` is the first thing
    `guard_input_message` does. That is why the result is cached.

    An earlier version of this docstring claimed the per-message path never
    reached here and used that to argue against a cache. It was wrong in both
    halves. Measured in the container with the stack present, one uncached call
    costs **19.9 ms** — 3.7 ms for the two `find_spec()` lookups and 16.6 ms for
    the two `importlib.metadata.version()` reads — against roughly 0.1 ms for
    every deterministic check together and 0.27 ms for the settings read. That
    put two hundred times the cost of the guards themselves in front of every
    turn, inside the hook that runs before anything else.

    Caching needs no invalidation story, which is the other half of the earlier
    reasoning that does not survive contact with the facts: installing the stack
    means rebuilding the image and restarting the process, and a restart clears
    this along with everything else in the module. It is the settings that
    change under a running process, not the interpreter's packages.

    The import is still never performed: loading Torch costs hundreds of
    megabytes of resident memory, and doing it to find out whether Torch is
    installed would defeat the whole point of making it optional.
    """
    global _CLASSIFIER_STACK_STATUS

    if _CLASSIFIER_STACK_STATUS is None:
        _CLASSIFIER_STACK_STATUS = _probe_classifier_stack()
    return _CLASSIFIER_STACK_STATUS


def _probe_classifier_stack() -> ClassifierStackStatus:
    """Look the two packages up on the path. The uncached half of the above."""
    missing = []
    versions = {}
    for package in CLASSIFIER_STACK_PACKAGES:
        try:
            found = importlib.util.find_spec(package) is not None
        except Exception:
            # `find_spec()` raises rather than returning None for a package
            # whose parent cannot be imported, and for some broken
            # installations. Treated as missing: the guard cannot run either
            # way, and this function must not be the thing that raises during
            # activation.
            found = False
        if found:
            versions[package] = _package_version(package)
        else:
            missing.append(package)

    return ClassifierStackStatus(
        available=not missing,
        missing=tuple(missing),
        versions=versions,
    )


# What the log says when a classifier cannot run because the optional stack was
# never installed. Deliberately free of any absolute or deployment-specific
# path: the log says which state the process is in and names the two files, the
# README carries the commands.
STACK_INSTALL_REMEDIATION = (
    " The optional classifier stack is not installed. Install it into the image "
    "from the plugin directory, first "
    "requirements-classifiers-torch-cpu.txt and then "
    "requirements-classifiers.txt, and restart the core; see README.md, section "
    "*The optional classifier stack*. The deterministic guards do not need it "
    "and are unaffected."
)

# What the log says when both packages are findable and the import failed
# anyway. A different sentence on purpose: telling somebody to install what they
# already installed sends them after the wrong problem.
STACK_BROKEN_REMEDIATION = (
    " Both torch and transformers are installed but the import failed, so the "
    "stack is broken or mutually incompatible rather than absent. Check the "
    "versions against README.md, section *The optional classifier stack*, and "
    "rebuild the image; installing again over the current environment is not "
    "the fix."
)


def stack_remediation(error: Exception | str | None = None) -> str:
    """Instructions when a classifier failed because of the optional stack.

    Empty when the stack is complete and the failure looks like anything else,
    which keeps it composable with `access_remediation()`: each one speaks only
    for the cause it recognises, and a load that failed for a third reason gets
    neither instead of both.

    `error` is optional and only refines the wording. On a complete stack the
    question is not «was this an import error» but «was it an import error about
    *this* stack», and the difference is the whole value of the split: some
    models need a package neither of ours pulls in — `sentencepiece` and
    `protobuf` are the usual ones — and a `ModuleNotFoundError` naming one of
    those is fixed by installing it. Answering that with «the stack is broken,
    rebuild the image, installing again is not the fix» is precisely the
    wrong-problem misdirection these two messages exist to avoid.

    So the missing module is compared against the two packages we own, and
    anything else gets no stack advice at all rather than the wrong one.
    """
    status = classifier_stack_status()
    if not status.available:
        return STACK_INSTALL_REMEDIATION

    if error is None:
        return ""

    missing_module = _missing_module_name(error)
    if missing_module in CLASSIFIER_STACK_PACKAGES:
        return STACK_BROKEN_REMEDIATION

    return ""


# `No module named 'transformers.utils'` — the quoted name, dotted path and all.
_MISSING_MODULE = re.compile(r"no module named '([^']+)'", re.IGNORECASE)


def _missing_module_name(error: Exception | str) -> str | None:
    """The top-level package a `ModuleNotFoundError` is about, or None.

    `ImportError.name` is the reliable source and is read first. The text is
    parsed only as a fallback, because callers hand this function the redacted
    *string* of an exception as often as the exception itself, and by then the
    attribute is gone.

    The top level alone is what the caller compares, so `transformers.utils`
    answers `transformers`: a submodule that cannot be imported is a fact about
    the package that owns it.
    """
    name = getattr(error, "name", None)
    if not name:
        found = _MISSING_MODULE.search(str(error))
        name = found.group(1) if found else None

    return name.split(".")[0] if name else None


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


_IMPORTED_VERSIONS_ANNOUNCED = False


def _log_imported_stack_versions() -> None:
    """Report the versions actually imported, once per process.

    This is not the same fact as the one announced at activation, and the
    difference is the reason both lines exist. Activation reports what the
    *distribution metadata* says is installed, read without importing anything.
    This reports what the interpreter actually imported. They normally agree and
    can diverge — an installation overwritten by hand, a shadowing directory on
    the path — and when they do, only this line is the truth the classifier ran
    against.

    It also matters because of a core behaviour documented in
    `DEV/AGENTS/PROJECT.md`: requirements are matched by package *name* only, so
    the `transformers>=4.50,<5` upper bound in the optional requirements file is
    silently skipped on an image that already carries a 5.x. Nothing in the
    plugin can enforce that bound; this line is what makes a breach visible.

    Never raises and never blocks a load: a version that cannot be read is worth
    less than the pipeline that just loaded successfully.

    The flag is set **after** the line is written, not before. Setting it first
    is the ordinary way to write a once-per-process guard and it is wrong here:
    a single failure would then suppress the line for the life of the process,
    and this line is the only place where a `transformers` outside the range the
    optional requirements declare becomes visible. Announcing once means once
    *successfully*, so a later load gets another chance.
    """
    global _IMPORTED_VERSIONS_ANNOUNCED
    if _IMPORTED_VERSIONS_ANNOUNCED:
        return

    try:
        import torch
        import transformers

        runtime_log.info(
            "[rag-guardrails] optional classifier stack imported, "
            f"transformers={getattr(transformers, '__version__', UNKNOWN_VERSION)}, "
            f"torch={getattr(torch, '__version__', UNKNOWN_VERSION)}"
        )
    except Exception:
        # Reachable in practice through a stubbed pipeline, which is how the
        # tests load one: the fake module satisfies the pipeline call without
        # `torch` being importable at all.
        return

    _IMPORTED_VERSIONS_ANNOUNCED = True


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

        runtime_log.info(
            "[rag-guardrails] loading classifier model "
            f"{model_name} into memory; Transformers will use the local Hugging Face "
            "cache when available and download missing files if needed"
        )
        try:
            # The import is inside this `try`, deliberately, and it used to be
            # outside it. Transformers is an optional dependency now: on an image
            # built without the classifier stack this line raises
            # `ModuleNotFoundError`, and outside the `try` that failure bypassed
            # the negative cache entirely — so every single message retried the
            # import, took the load lock and paid the wait, for a package that
            # cannot appear without restarting the process. Inside it, the stack
            # is declared missing once and every later turn fails open
            # immediately, which is the same contract as a model that cannot be
            # downloaded.
            from transformers import pipeline as transformers_pipeline

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
                f"{stack_remediation(error)}"
            )
            raise

        _log_imported_stack_versions()
        runtime_log.info(
            "[rag-guardrails] classifier model "
            f"{model_name} loaded and cached in memory"
        )
        _CLASSIFIER_PIPELINES[model_name] = pipeline
        return pipeline
    finally:
        load_lock.release()


def normalize_scores(result) -> list[dict]:
    """Normalize what a pipeline returned into one flat list of score dicts.

    `transformers` has returned a dict, a list of dicts, and a list containing
    one list of dicts, across versions and arguments. Which of the three arrives
    depends on the installed version and on whether `top_k` was asked for, so the
    shape is not something this plugin can pin down, only something it has to
    absorb.

    The optional requirements declare `transformers>=4.50,<5`, and that upper
    bound does **not** make the shape predictable — it exists for the transitive
    dependencies, because the 5.x chain replaces `huggingface-hub` and
    `tokenizers`, which the core uses for its embedders. The three shapes above
    all occur inside the 4.x line, and the bound is in any case unenforceable:
    the core matches requirements by package name only, so an image that already
    carries a 5.x keeps it. This function stays necessary either way.

    It lives here rather than in either classifier because both need it and only
    one used to have it: the prompt-injection classifier indexed the raw result
    directly, so the list-of-lists shape raised `AttributeError` and an empty
    response raised `IndexError`. Neither reached the user — the hook catches
    everything and fails open — but the guard went quiet and reported itself as
    *unavailable*, which points whoever reads the log at a loading or token
    problem instead of at a library upgrade.

    An empty list is returned as an empty list, deliberately, and the callers
    read it as «decided nothing» rather than raising. A working model does not
    produce it.
    """
    if isinstance(result, dict):
        return [result]
    if result and isinstance(result[0], list):
        return list(result[0])
    return list(result)


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

