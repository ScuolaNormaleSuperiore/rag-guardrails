"""Guardrail pipeline for a website support chatbot: Cheshire Cat hooks.

This module is the adapter between Cheshire Cat and the pure decision logic in
`checks.py`. It reads from the Cat, loads the configuration, delegates every
decision, and writes back. No threshold and no rule live here.

One hook: `fast_reply`, for verdicts that depend on the incoming message alone.

A second hook on `agent_fast_reply` existed and was removed. It was the dispatch
point for verdicts that can only be formed after the recall — the evidence gate
of Fase 3 — and that gate is not planned: refusing on an empty recall duplicates
an instruction the deployed prompt already carries, while refusing greetings and
follow-up questions. With no check producing a post-recall verdict, the hook
registered a behaviour the plugin does not have. If such a check ever arrives,
`agent_fast_reply` is still the only place it can reply from, and the reasoning
is in `DEV/AGENTS/PROJECT.md`, section *Why input checks run on `fast_reply`*.

Input checks run on `fast_reply`, the earliest hook, for two reasons that both
matter more than the single dispatch point the design document assumes:

1. It is the only place where a refused message never reaches the vector
   database. On the `agent_fast_reply` path the core still stores the user
   message in episodic memory after the agent returns, so a message blocked for
   containing personal data would be persisted anyway — and the embedding is
   computed from the original text, which `before_cat_stores_episodic_memory`
   cannot undo.
2. It makes the check independent of any other plugin. Hooks with the same name
   are piped, not short-circuited, in descending priority order, and the last
   non-None return wins. A negative priority puts this plugin last, so its reply
   is the one delivered whatever the `Rate Limiter` plugin decided. Its side
   effects, such as progressive suspensions, are outside this plugin's reach.

A refused message therefore also skips the retrieval, the conversation history
and `before_cat_sends_message`. That is intentional: there is nothing to filter
in a reply we wrote ourselves, and an over-long message is better left out of
the history.

A second hook, `before_cat_sends_message`, filters the outgoing answer for
personal data before the user sees it and before the AI turn is written to the
conversation history.

"""

import os
import time

from cat.log import log
from cat.mad_hatter.decorators import hook, plugin
from pydantic import ValidationError

# The Cat imports plugin files as `cat.plugins.<folder>.<module>`, which makes
# the relative import the correct one at runtime. The absolute fallback is what
# lets the tests import this module directly, with the plugin folder on the path.
try:
    from .checks import (
        CATEGORY_LIMITS,
        CATEGORY_PRIVACY,
        CATEGORY_SECURITY,
        CATEGORY_TONE,
        STAGE_INPUT,
        STAGE_OUTPUT,
        VERDICT_MESSAGE_LENGTH,
        VERDICT_OFFENSIVE_INPUT,
        VERDICT_OUTPUT_PERSONAL_DATA,
        VERDICT_PERSONAL_DATA,
        VERDICT_PROMPT_INJECTION,
        category_of,
        check_output_personal_data,
        extract_text,
        matched_personal_data_kinds,
        matched_prompt_injection_pattern,
        parse_public_contacts,
        phone_number_types,
        run_input_checks,
        stage_of,
    )
    from .classifier_runtime import classifier_load_error, redact_secrets
    from .offensive_input_classifier import classify_offensive_input
    from .prompt_injection_classifier import classify_prompt_injection
    from .settings import RagGuardrailsSettings
except ImportError:  # pragma: no cover - depends on how the module is loaded
    from checks import (
        CATEGORY_LIMITS,
        CATEGORY_PRIVACY,
        CATEGORY_SECURITY,
        CATEGORY_TONE,
        STAGE_INPUT,
        STAGE_OUTPUT,
        VERDICT_MESSAGE_LENGTH,
        VERDICT_OFFENSIVE_INPUT,
        VERDICT_OUTPUT_PERSONAL_DATA,
        VERDICT_PERSONAL_DATA,
        VERDICT_PROMPT_INJECTION,
        category_of,
        check_output_personal_data,
        extract_text,
        matched_personal_data_kinds,
        matched_prompt_injection_pattern,
        parse_public_contacts,
        phone_number_types,
        run_input_checks,
        stage_of,
    )
    from classifier_runtime import classifier_load_error, redact_secrets
    from offensive_input_classifier import classify_offensive_input
    from prompt_injection_classifier import classify_prompt_injection
    from settings import RagGuardrailsSettings

# Where the verdict of the turn is recorded. `WorkingMemory` allows extra
# attributes and lives for the whole session, so it is reset at the beginning of
# every turn — a stale verdict would otherwise describe the wrong message.
#
# Written and never read inside this plugin, deliberately: it used to be the
# carrier between two hooks, and it is now the trace the telemetry module will
# read instead of parsing log lines.
VERDICT_ATTRIBUTE = "ict_guard_verdict"

# Runs after every other plugin on `fast_reply`. The Rate Limiter plugin uses
# the default priority of 1, and core plugin hooks use 0; a negative value is
# unambiguously last, and being last is what makes this plugin's reply win.
INPUT_GUARD_PRIORITY = -1

# Which categories ship with a guard switched on, and therefore the only ones
# that can be reported as *uncovered* in the announcement below.
#
# That field answers a narrower question than «is this category guarded»: it
# answers «was a protection this plugin provides by default switched off», which
# is a deviation worth a WARNING. `tone` ships disabled on purpose — a second
# model in memory, and a precision still to be measured — so listing it would put
# a WARNING on every fresh installation and teach everyone to ignore the line,
# including on the day privacy really is off. The summary still reports
# `tone(disabled)`, so the state stays visible; only the severity of the whole
# announcement does not follow it.
#
# When the tone guard's default flips to enabled, add it here in the same change.
CATEGORIES_ENABLED_BY_DEFAULT = (
    CATEGORY_LIMITS,
    CATEGORY_PRIVACY,
    CATEGORY_SECURITY,
)

# Which settings field holds the reply text of each verdict. Adding a check
# means adding an entry here and a field to the settings model; the tests fail
# if the two get out of step.
#
# The keys are verdicts and the values are settings field names, and the two
# namespaces are deliberately independent: a verdict may be renamed freely,
# because it is never persisted, while renaming a field silently discards the
# text an administrator edited in the admin panel. Two verdicts may also point
# at the same field, which is what Fase 3 and Fase 5 need when they share one
# insufficiency message.
REPLY_SETTING_BY_VERDICT = {
    VERDICT_MESSAGE_LENGTH: "message_too_long",
    VERDICT_PERSONAL_DATA: "personal_data_detected",
    VERDICT_OUTPUT_PERSONAL_DATA: "output_personal_data_detected",
    VERDICT_PROMPT_INJECTION: "prompt_injection_detected",
    VERDICT_OFFENSIVE_INPUT: "offensive_input_detected",
}

# The guard configuration as it was last announced. A guard disabled from the
# admin panel leaves no other trace, and this plugin exists to prevent silent
# gaps, so the whole configuration is announced once and then only when it
# changes — not at every turn, which would be noise on every conversation.
_ANNOUNCED_GUARD_SUMMARY: str | None = None

# The classifier failure already reported. The classifier is fail-open, so a
# model that cannot load leaves the message unblocked on every turn — reporting
# it once per turn would bury the log without adding information, since the state
# cannot change until the plugin reloads.
_ANNOUNCED_CLASSIFIER_FAILURE: str | None = None

# The same, for the offensive-input classifier. A separate name rather than a
# shared registry because what the two failures have to say differs: prompt
# injection still has its built-in patterns to fall back on, the tone guard has
# nothing else at all.
_ANNOUNCED_OFFENSIVE_CLASSIFIER_FAILURE: str | None = None


def load_settings(cat) -> RagGuardrailsSettings:
    """Return the plugin configuration, falling back to the model defaults.

    Never raises. A configuration problem must not take the guard down, and
    falling back to the defaults keeps the checks running. It also lets the
    hooks be called with a fake `cat` that has no plugin registry at all.

    The fallback is not only defensive: `load_settings()` returns settings.json
    verbatim, so an empty or partial file would otherwise yield no values.
    """
    try:
        stored = cat.mad_hatter.get_plugin().load_settings()
    except Exception as error:
        log.info(
            f"[rag-guardrails] settings unavailable ({error}), using defaults"
        )
        return RagGuardrailsSettings()

    try:
        return RagGuardrailsSettings.model_validate(stored or {})
    except ValidationError as error:
        log.warning(
            f"[rag-guardrails] invalid settings, using defaults: {error}"
        )
        return RagGuardrailsSettings()


def _render(template: str, settings: RagGuardrailsSettings) -> str:
    """Insert the configured values into a reply text.

    A plain replace, not str.format: the text is edited by hand in the admin
    panel, and any stray brace would make format() raise on a message that is
    only meant to be shown to a user.
    """
    return template.replace("{help_desk_email}", settings.help_desk_email)


def reply_for(verdict: str, settings: RagGuardrailsSettings) -> str | None:
    """Return the configured reply for a verdict, or None if there is none.

    A verdict with no reply is a bug, not a valid state: the caller answers
    normally rather than sending an empty message, and logs the gap.
    """
    setting_name = REPLY_SETTING_BY_VERDICT.get(verdict)
    if setting_name is None:
        log.warning(
            f"[rag-guardrails] no reply configured for verdict "
            f"'{verdict}', falling back to normal execution"
        )
        return None
    return _render(getattr(settings, setting_name), settings)


def enabled_privacy_detectors(
    settings: RagGuardrailsSettings,
) -> tuple[str, ...]:
    """The input-side personal-data detectors currently switched on."""
    return tuple(
        name
        for name, enabled in (
            ("email", settings.detect_input_email),
            ("codice_fiscale", settings.detect_input_codice_fiscale),
            ("iban", settings.detect_input_iban),
            ("phone", settings.detect_input_phone),
        )
        if enabled
    )


def output_privacy_checks_enabled(settings: RagGuardrailsSettings) -> tuple[str, ...]:
    """The output-side personal-data detectors currently switched on."""
    return tuple(
        name
        for name, enabled in (
            ("email", settings.detect_output_email),
            ("codice_fiscale", settings.detect_output_codice_fiscale),
            ("iban", settings.detect_output_iban),
            ("phone", settings.detect_output_phone),
        )
        if enabled
    )


def enabled_check_names(settings: RagGuardrailsSettings) -> tuple[str, ...]:
    """Which input checks are effectively active, in the order they run.

    A check whose configuration disables it — a non-positive length limit, all
    four privacy detectors off — is not listed, because it decides nothing.
    """
    names = []
    if settings.max_message_chars > 0:
        names.append("length")
    if settings.detect_prompt_injection_custom:
        names.append("injection_patterns")
    if enabled_privacy_detectors(settings):
        names.append("personal_data")
    if settings.detect_prompt_injection_classifier and not classifier_load_error(
        settings.prompt_injection_classifier_model.value
    ):
        # Last on purpose: it runs only when every deterministic check passed.
        # Enabled in the settings is not enough — a model that failed to load is
        # not covering anything, and listing it would make the line claim a
        # coverage the turn did not have.
        names.append("injection_classifier")
    if settings.detect_offensive_input_classifier and not classifier_load_error(
        settings.offensive_input_classifier_model.value
    ):
        # After the injection classifier, matching the order they run in. Same
        # rule as above: a model that failed to load is not covering anything.
        names.append("offensive_input")
    return tuple(names)


def active_guards_summary(
    settings: RagGuardrailsSettings,
) -> tuple[str, tuple[str, ...]]:
    """The guard configuration as one line, plus the categories left uncovered.

    One string per category, so the line reads as the answer to «what is
    actually protecting this instance» — the question the admin form answers
    only by being opened, field by field.
    """
    if settings.max_message_chars > 0:
        limits = f"{CATEGORY_LIMITS}(max {settings.max_message_chars} chars)"
    else:
        limits = f"{CATEGORY_LIMITS}(disabled)"

    input_detectors = enabled_privacy_detectors(settings)
    output_checks = output_privacy_checks_enabled(settings)
    if input_detectors or output_checks:
        parts = []
        if input_detectors:
            parts.append(f"input={'+'.join(input_detectors)}")
            parts.append(f"input_region={settings.input_phone_region}")
        else:
            parts.append("input=disabled")
        if output_checks:
            parts.append(f"output={'+'.join(output_checks)}")
            parts.append(f"output_region={settings.output_phone_region}")
        else:
            parts.append("output=disabled")
        privacy = f"{CATEGORY_PRIVACY}({', '.join(parts)})"
    else:
        privacy = f"{CATEGORY_PRIVACY}(disabled)"

    mechanisms = []
    if settings.detect_prompt_injection_custom:
        mechanisms.append("patterns")
    if settings.detect_prompt_injection_classifier:
        mechanisms.append(
            f"classifier {settings.prompt_injection_classifier_model.value}"
            f"@{settings.prompt_injection_classifier_threshold:.2f}"
        )
    if mechanisms:
        security = f"{CATEGORY_SECURITY}({'+'.join(mechanisms)})"
    else:
        security = f"{CATEGORY_SECURITY}(disabled)"

    if settings.detect_offensive_input_classifier:
        tone = (
            f"{CATEGORY_TONE}(classifier "
            f"{settings.offensive_input_classifier_model.value}"
            f"@{settings.offensive_input_classifier_threshold:.2f})"
        )
    else:
        tone = f"{CATEGORY_TONE}(disabled)"

    # Coverage is decided per stage+category pair, not per category, because the
    # two axes are orthogonal: `privacy` carries a verdict on `input` and another
    # on `output`, and switching off one stage leaves the other one working. A
    # single per-category flag reported an instance as covered while its whole
    # output stage was off, and the only hint was the absence of the `output=`
    # fragment from a line nobody reads that closely.
    #
    # The flag is carried explicitly rather than derived from the description.
    # `description.endswith("(disabled)")` decided the severity of this
    # announcement by pattern-matching text written for a human, so a phone
    # region or a model name ending in that word would have changed it.
    #
    # Both privacy stages collapse back into one entry when neither is on, so the
    # common «privacy is off» case still reads as `privacy` rather than as two
    # near-identical items.
    if input_detectors or output_checks:
        privacy_coverage = (
            (f"{CATEGORY_PRIVACY}(input)", CATEGORY_PRIVACY, bool(input_detectors)),
            (f"{CATEGORY_PRIVACY}(output)", CATEGORY_PRIVACY, bool(output_checks)),
        )
    else:
        privacy_coverage = ((CATEGORY_PRIVACY, CATEGORY_PRIVACY, False),)

    coverage = (
        (CATEGORY_LIMITS, CATEGORY_LIMITS, settings.max_message_chars > 0),
        *privacy_coverage,
        (CATEGORY_SECURITY, CATEGORY_SECURITY, bool(mechanisms)),
        (
            CATEGORY_TONE,
            CATEGORY_TONE,
            settings.detect_offensive_input_classifier,
        ),
    )

    uncovered = tuple(
        label
        for label, category, covered in coverage
        if not covered and category in CATEGORIES_ENABLED_BY_DEFAULT
    )

    return f"{limits}, {privacy}, {security}, {tone}", uncovered


def announce_active_guards(settings: RagGuardrailsSettings) -> None:
    """Log the guard configuration once, and again whenever it changes.

    Not at every turn: on a message that passes, the plugin writes nothing at
    `INFO`, and that silence used to be indistinguishable from the plugin not
    running at all. This line is what tells the two apart. Announced on change
    rather than per message so the cost stays at one tuple comparison and the
    log of a normal conversation stays readable.

    A category with nothing left enabled is a `WARNING`, because that is the
    state where the chatbot is unguarded and nothing else says so.
    """
    global _ANNOUNCED_GUARD_SUMMARY

    summary, uncovered = active_guards_summary(settings)
    if summary == _ANNOUNCED_GUARD_SUMMARY:
        return

    if uncovered:
        log.warning(
            f"[rag-guardrails] guards active: {summary}; "
            f"no guard covers: {', '.join(uncovered)}"
        )
    else:
        log.info(f"[rag-guardrails] guards active: {summary}")

    _ANNOUNCED_GUARD_SUMMARY = summary


def log_output_allowed(
    enabled_detectors: tuple[str, ...], started: float
) -> None:
    """Record that the output stage ran and let the answer through.

    The counterpart of the `input allowed` line, and it exists for the same
    reason: silence from a guard is indistinguishable from a guard that is not
    running. On the output stage that ambiguity was worse than on input, because
    three different situations produced no line at all — a clean answer, a turn
    refused earlier that never reached generation, and every output detector
    switched off.

    `checks` names the detectors that actually examined the answer, so an empty
    set prints `none` instead of implying a check that did not happen. The
    generated answer is never included, exactly as on the input side.
    """
    log.info(
        f"[rag-guardrails] output allowed, "
        f"stage='{STAGE_OUTPUT}', "
        f"checks={'+'.join(enabled_detectors) or 'none'}, "
        f"latency_ms={(time.perf_counter() - started) * 1000:.2f}"
    )


def blocked_detail(
    verdict: str, text: str, settings: RagGuardrailsSettings
) -> str:
    """Context for the log line, never the message itself.

    The refused text must not reach the logs: on the personal-data verdict that
    would defeat the point of the check. Only the shape of the violation is
    recorded — how long the message was, or which detector fired.
    """
    if verdict == VERDICT_MESSAGE_LENGTH:
        return f", length={len(text)} chars (limit {settings.max_message_chars})"

    if verdict in {VERDICT_PERSONAL_DATA, VERDICT_OUTPUT_PERSONAL_DATA}:
        if verdict == VERDICT_PERSONAL_DATA:
            detect_email = settings.detect_input_email
            detect_codice_fiscale = settings.detect_input_codice_fiscale
            detect_iban = settings.detect_input_iban
            detect_phone = settings.detect_input_phone
            phone_region = settings.input_phone_region
        else:
            detect_email = settings.detect_output_email
            detect_codice_fiscale = settings.detect_output_codice_fiscale
            detect_iban = settings.detect_output_iban
            detect_phone = settings.detect_output_phone
            phone_region = settings.output_phone_region

        public_contacts = parse_public_contacts(settings.public_service_contacts)

        kinds = matched_personal_data_kinds(
            text,
            detect_email=detect_email,
            detect_codice_fiscale=detect_codice_fiscale,
            detect_iban=detect_iban,
            detect_phone=detect_phone,
            allowed_email=settings.help_desk_email,
            phone_region=phone_region,
            public_contacts=public_contacts,
        )
        detail = f", detected={'+'.join(kinds)}"

        # The kind of number is useful to whoever reads the logs, and is not a
        # setting: see the note on `phone_number_types`. The public contacts go
        # in here too, so a block caused by a personal number does not report
        # the type of an exempt number sitting in the same sentence.
        if "phone" in kinds:
            types = phone_number_types(text, phone_region, public_contacts)
            detail += f" ({'+'.join(sorted(set(types)))})"

        return detail

    if verdict == VERDICT_PROMPT_INJECTION:
        # Only the pattern detector reaches this function: the classifier path
        # builds its own detail, because it has a score and a latency to report.
        pattern = matched_prompt_injection_pattern(
            text, settings.detect_prompt_injection_custom
        )
        if pattern is None:  # pragma: no cover - defensive, the check just fired
            return ", detector=custom"
        return f", detector=custom, pattern={pattern}"

    return ""


def replace_message_text(message, text: str):
    """Return `message` with its text replaced and its source metadata cleared.

    The output guard replaces the generated answer entirely, so the old `why`
    metadata must not survive into the static fallback. The hook receives a
    `CatMessage` on the live flow and may receive a dict in other contexts.
    """
    if isinstance(message, dict):
        updated = dict(message)
        updated["text"] = text
        updated["content"] = text
        updated["why"] = None
        return updated

    message.text = text
    if hasattr(message, "why"):
        message.why = None
    return message


def announce_classifier_failure(
    error: Exception, settings: RagGuardrailsSettings
) -> None:
    """Report a fail-open classifier once, naming what still covers the turn.

    Once per failure rather than once per message: the classifier cannot recover
    without the plugin reloading, so the state is constant and repeating it every
    turn buries the log exactly when a configuration problem needs diagnosing.

    What the line must say is *what is left*, because the announcement of the
    active guards was built from the settings and therefore claimed a classifier
    that turns out not to run. When the built-in patterns are off as well, this is
    the only line saying the security category covers nothing.
    """
    global _ANNOUNCED_CLASSIFIER_FAILURE

    # The exception text belongs to a third-party library: redact before it is
    # formatted into a line that goes to the log at WARNING.
    reported = (
        f"{settings.prompt_injection_classifier_model.value}: "
        f"{redact_secrets(str(error), resolve_huggingface_token(settings))}"
    )
    if reported == _ANNOUNCED_CLASSIFIER_FAILURE:
        return
    _ANNOUNCED_CLASSIFIER_FAILURE = reported

    if settings.detect_prompt_injection_custom:
        remaining = (
            f"the {CATEGORY_SECURITY} guard continues on its built-in patterns only"
        )
    else:
        remaining = (
            f"no guard covers: {CATEGORY_SECURITY} — the built-in patterns are "
            "disabled too"
        )

    log.warning(
        f"[rag-guardrails] prompt-injection classifier unavailable "
        f"({reported}), continuing without blocking; {remaining}. "
        "Not repeated until the plugin reloads"
    )


def detect_prompt_injection_with_classifier(
    text: str, settings: RagGuardrailsSettings
) -> dict[str, str | float] | None:
    """Run the local prompt-injection classifier, fail-open on any error."""
    if not settings.detect_prompt_injection_classifier:
        return None

    model_name = settings.prompt_injection_classifier_model.value
    token = resolve_huggingface_token(settings)

    started = time.perf_counter()
    try:
        result = classify_prompt_injection(
            text,
            model_name=model_name,
            threshold=settings.prompt_injection_classifier_threshold,
            max_length=settings.max_message_chars,
            token=token,
        )
    except Exception as error:
        announce_classifier_failure(error, settings)
        return None

    elapsed_ms = (time.perf_counter() - started) * 1000
    if not result["triggered"]:
        return None

    return {
        "detail": (
            ", detector=classifier"
            f", model={model_name}"
            f", label={result['label']}"
            f", score={result['score']:.3f}"
            f", threshold={settings.prompt_injection_classifier_threshold:.2f}"
            f", latency_ms={elapsed_ms:.1f}"
        ),
    }


def announce_offensive_classifier_failure(
    error: Exception, settings: RagGuardrailsSettings
) -> None:
    """Report a fail-open offensive-input classifier once, saying what is left.

    What is left is nothing, and that is the difference from the prompt-injection
    announcement: that guard falls back on its built-in patterns, this one has no
    deterministic half. When its model does not load, the `tone` category is
    uncovered, and this line is the only place that says so — the `guards active`
    announcement was built from the settings and therefore claimed a classifier
    that turns out not to run.
    """
    global _ANNOUNCED_OFFENSIVE_CLASSIFIER_FAILURE

    reported = (
        f"{settings.offensive_input_classifier_model.value}: "
        f"{redact_secrets(str(error), resolve_huggingface_token(settings))}"
    )
    if reported == _ANNOUNCED_OFFENSIVE_CLASSIFIER_FAILURE:
        return
    _ANNOUNCED_OFFENSIVE_CLASSIFIER_FAILURE = reported

    log.warning(
        f"[rag-guardrails] offensive-input classifier unavailable "
        f"({reported}), continuing without blocking; no guard covers: "
        f"{CATEGORY_TONE} — this check has no deterministic fallback. "
        "Not repeated until the plugin reloads"
    )


def detect_offensive_input(
    text: str, settings: RagGuardrailsSettings
) -> dict[str, str | float] | None:
    """Run the local offensive-input classifier, fail-open on any error.

    Last of the input checks, so it runs only on a message every other one let
    through. Fail-open for the same reason as the prompt-injection classifier: a
    model that cannot run must leave the message alone rather than take down the
    hook that runs before retrieval.
    """
    if not settings.detect_offensive_input_classifier:
        return None

    model_name = settings.offensive_input_classifier_model.value
    token = resolve_huggingface_token(settings)

    started = time.perf_counter()
    try:
        result = classify_offensive_input(
            text,
            model_name=model_name,
            threshold=settings.offensive_input_classifier_threshold,
            token=token,
        )
    except Exception as error:
        announce_offensive_classifier_failure(error, settings)
        return None

    elapsed_ms = (time.perf_counter() - started) * 1000
    if not result["triggered"]:
        return None

    # `label` is the strongest blocking class, `score` the sum of all of them:
    # without both, a refusal at 0.9 would not say whether the model saw an
    # insult or a threat, and the sum alone names no behaviour.
    return {
        "detail": (
            ", detector=classifier"
            f", model={model_name}"
            f", label={result['label']}"
            f", score={result['score']:.3f}"
            f", threshold={settings.offensive_input_classifier_threshold:.2f}"
            f", latency_ms={elapsed_ms:.1f}"
        ),
    }


# The environment variables Hugging Face itself honours, in its own order of
# precedence: `HF_TOKEN` is current, `HUGGING_FACE_HUB_TOKEN` is the legacy name
# `huggingface_hub` still reads. Both are checked here for one specific reason —
# passing `token=None` would let the library fall back to its own resolution and
# find them anyway, but the admin-panel field would then take precedence over an
# environment variable, which is the opposite of what this function promises.
HUGGINGFACE_TOKEN_VARIABLES = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


def resolve_huggingface_token(settings: RagGuardrailsSettings) -> str | None:
    """Return the Hugging Face token to use for gated models, if any.

    Environment variables take precedence over admin settings so deployments can
    keep secrets out of the plugin configuration when they want to.
    """
    for variable in HUGGINGFACE_TOKEN_VARIABLES:
        token = os.getenv(variable, "").strip()
        if token:
            return token

    token = settings.huggingface_token.strip()
    return token or None


@hook("fast_reply", priority=INPUT_GUARD_PRIORITY)
def guard_input_message(fast_reply, cat):
    """Run the input checks and answer immediately when one of them trips.

    Returning a dict containing an "output" key is what makes the core return
    straight away, before retrieval, before the agent, and before the message is
    stored in episodic memory. Returning the received dict untouched lets the
    flow continue — and preserves a reply another plugin may have already put
    there, so a rate-limit block from `Rate Limiter` still reaches the user.

    No generative call happens here: the checks are deterministic and cost
    computation, not tokens.
    """
    # The span covers everything this hook costs the turn, settings included:
    # the first message after an activation also pays the classifier model load,
    # and that is worth seeing rather than hiding.
    started = time.perf_counter()

    # Reset any verdict from the previous turn: working memory lives for the
    # whole session, and this is the earliest hook of the turn.
    setattr(cat.working_memory, VERDICT_ATTRIBUTE, None)

    settings = load_settings(cat)
    announce_active_guards(settings)

    text = extract_text(getattr(cat.working_memory, "user_message_json", None))
    verdict = run_input_checks(text, settings)
    detail = blocked_detail(verdict, text, settings) if verdict is not None else ""

    if verdict is None:
        classifier_result = detect_prompt_injection_with_classifier(text, settings)
        if classifier_result is not None:
            verdict = VERDICT_PROMPT_INJECTION
            detail = classifier_result["detail"]

    if verdict is None:
        # Last, and the order decides one thing worth knowing: a message that is
        # both offensive and an injection attempt is reported as
        # `prompt_injection`, because an attack on the assistant is the more
        # pertinent correction to give back.
        offensive_result = detect_offensive_input(text, settings)
        if offensive_result is not None:
            verdict = VERDICT_OFFENSIVE_INPUT
            detail = offensive_result["detail"]

    elapsed_ms = (time.perf_counter() - started) * 1000

    if verdict is None:
        # INFO deliberately: operators can keep the core at its normal INFO level
        # and still see that this plugin handled the turn and which checks covered
        # it. The message text itself is never included.
        # Two decimals, not one: the deterministic checks cost hundredths of a
        # millisecond, and `latency_ms=0.0` reads as a broken timer rather than
        # as a fast path. The classifier, when it runs, is three orders of
        # magnitude above that and stays readable either way.
        log.info(
            f"[rag-guardrails] input allowed, "
            f"stage='{STAGE_INPUT}', "
            f"checks={'+'.join(enabled_check_names(settings)) or 'none'}, "
            f"latency_ms={elapsed_ms:.2f}"
        )
        return fast_reply

    reply = reply_for(verdict, settings)
    if reply is None:
        return fast_reply

    # This hook answers on its own, so nothing in this plugin reads the verdict
    # back. It is kept as the trace of why the turn was refused, for the future
    # telemetry module and for anything else inspecting working memory: unlike a
    # hook registration, an attribute claims no behaviour the plugin lacks.
    setattr(cat.working_memory, VERDICT_ATTRIBUTE, verdict)

    log.info(
        f"[rag-guardrails] input blocked, "
        f"stage='{stage_of(verdict)}', "
        f"category='{category_of(verdict)}', verdict='{verdict}'"
        f"{detail}, latency_ms={elapsed_ms:.2f}; "
        f"no retrieval, no generation, nothing stored in memory"
    )
    return {"output": reply}


@hook("before_cat_sends_message")
def guard_output_message(message, cat):
    """Replace outgoing answers carrying personal data before delivery.

    Unlike the input guard, this path runs after generation: it cannot prevent
    the model call, but it still stops the answer from reaching the user and
    from being written to the AI side of the conversation history as generated.
    """
    # Timed like the input guard, and for the same reason: latency per stage is
    # only comparable between stages if both stages report it. The span covers the
    # settings read too, which on this path is the dominant cost — the detectors
    # work on an answer, not on an arbitrary user string.
    started = time.perf_counter()

    settings = load_settings(cat)
    enabled = output_privacy_checks_enabled(settings)
    if not enabled:
        # Logged, then returned: the detectors are skipped but the turn still
        # leaves a trace at this stage. Without it, «no output line» meant three
        # different things at once — the answer was clean, the turn never reached
        # generation, or every output detector was switched off — and the reader
        # had no way to tell them apart. `checks=none` names the third.
        log_output_allowed(enabled, started)
        return message

    text = extract_text(message)
    verdict = check_output_personal_data(
        text,
        detect_email=settings.detect_output_email,
        detect_codice_fiscale=settings.detect_output_codice_fiscale,
        detect_iban=settings.detect_output_iban,
        detect_phone=settings.detect_output_phone,
        allowed_email=settings.help_desk_email,
        phone_region=settings.output_phone_region,
        public_contacts=parse_public_contacts(settings.public_service_contacts),
    )
    if verdict is None:
        log_output_allowed(enabled, started)
        return message

    reply = reply_for(verdict, settings)
    if reply is None:
        return message

    setattr(cat.working_memory, VERDICT_ATTRIBUTE, verdict)

    log.info(
        f"[rag-guardrails] output blocked, "
        f"stage='{stage_of(verdict)}', "
        f"category='{category_of(verdict)}', verdict='{verdict}'"
        f"{blocked_detail(verdict, text, settings)}, "
        f"latency_ms={(time.perf_counter() - started) * 1000:.2f}; "
        "generated reply replaced before delivery"
    )
    return replace_message_text(message, reply)



@plugin
def activated(plugin):
    """Announce that the plugin loaded and which hooks it registered.

    The one thing no per-turn line can report: a plugin that fails to load
    registers no hooks, so every other line in this module is silent too, and
    silence is exactly what a broken activation and a quiet conversation have in
    common. This line turns «the guardrails are running» into a positive fact
    stated at a known moment.

    `activated` rather than `after_cat_bootstrap`, deliberately, and the two are
    not interchangeable: `after_cat_bootstrap` runs once when the core starts
    (`cat/looking_glass/cheshire_cat.py:106`), so it says nothing about a plugin
    switched on later from the admin panel — which is the case where a load
    failure actually happens, because that is when the code on disk is re-read.
    This override runs on every activation, toggles included
    (`cat/mad_hatter/plugin.py:90`).

    The guard configuration is deliberately *not* announced here. It belongs to
    `guards active`, which is driven by the settings and re-announced whenever
    they change; duplicating it at activation would report a configuration that
    can be edited a moment later, and would need `settings.json` to exist
    already.
    """
    log.info(
        "[rag-guardrails] plugin activated, guardrails registered: "
        f"fast_reply(priority={INPUT_GUARD_PRIORITY}) for the input stage, "
        "before_cat_sends_message for the output stage"
    )
