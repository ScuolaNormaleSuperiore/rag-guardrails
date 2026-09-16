# Logging

How `RAG Guardrails` reports its activity in the logs.

This document is the detailed reference for guard-related log lines. The
taxonomy of `stage`, `category` and `verdict` is defined in
`DOC/GuardTaxonomy.md` and is not repeated here in full.

## Purpose

A guardrail that stops working raises no error: the chatbot simply keeps
answering unguarded. The log is what makes that visible, so it answers four
different questions:

- whether the plugin loaded at all
- which guards are active
- that a stage ran and let the turn through
- why a request or reply was blocked

The third one is easy to leave out and is what makes the others usable: without
it, a stage that never ran and a stage that found nothing look identical.

## Activation

One line when the plugin is activated, naming the hooks it registered:

```text
[rag-guardrails] plugin activated, guardrails registered: fast_reply(priority=-1) for the input stage, before_cat_sends_message for the output stage
```

This is the only line that survives the failure it reports. A plugin that fails
to load registers no hooks, so none of the lines below is ever written either —
and that silence is exactly what a broken activation shares with a quiet
instance.

It comes from the `activated` plugin override rather than the
`after_cat_bootstrap` hook, and the two are not interchangeable:
`after_cat_bootstrap` runs once when the core starts, so it says nothing about a
plugin switched on later from the admin panel, which is when the code on disk is
re-read and a load failure actually happens.

### Optional classifier stack

A second line follows the activation announcement, always, and it reports
whether `torch` and `transformers` are installed. They are not installed by the
core: a deployment puts them in its image only when it uses a classifier guard.

Complete stack, at `INFO`:

```text
[rag-guardrails] optional classifier stack installed, torch=2.7.1, transformers=4.50.3
```

It is not silent on success on purpose. The core matches requirements by package
*name* and ignores the version, so the version bound in the optional
requirements file cannot be enforced; this line is the only place where a stack
outside the verified range becomes visible without opening a shell on the host.

Stack absent and no classifier enabled — the shipped configuration — at `INFO`:

```text
[rag-guardrails] optional classifier stack not installed, missing=torch+transformers; classifier guards are disabled, deterministic guards remain available
```

Stack absent with a classifier enabled, at `WARNING`, followed by the install
instructions:

```text
[rag-guardrails] optional classifier stack not installed, missing=torch; enabled classifier guards will fail open, deterministic guards remain available. The optional classifier stack is not installed. Install it into the image from the plugin directory, first requirements-classifiers-torch-cpu.txt and then requirements-classifiers.txt, and restart the core; ...
```

`missing=` names exactly what is absent — `torch`, `transformers` or
`torch+transformers` — and it is a log field, so the names are a contract.

The severity carries the whole message: an absent stack is a normal
configuration when no classifier is enabled, and a degraded service when one is.
The install instructions appear only in the second case, because on an
installation that never wanted a classifier they would be advice to install a
gigabyte of dependencies nobody asked for.

The state is reported here rather than discovered per message because it cannot
change without restarting the process. It is therefore **not** the same fact as
`guards active`, which changes whenever somebody saves the admin panel.

That difference is also why the probe behind it is computed once and reused. It
*is* consulted per message — the guard summary needs it to report a classifier
honestly — and an uncached lookup was measured at 19.9 ms, against roughly
0.1 ms for every deterministic check together. Caching needs no invalidation
story, because installing the stack means rebuilding the image and restarting,
and a restart clears the cache along with the process.

One more line can appear later, at most once per process, when a model is first
loaded successfully:

```text
[rag-guardrails] optional classifier stack imported, transformers=4.50.3, torch=2.7.1
```

That one reports what the interpreter actually imported, where the activation
line reports what the distribution metadata says is installed. They normally
agree; when they do not, this is the truth the classifier ran against.

## Active guards

One line is written when the plugin starts guarding, and again whenever the
configuration changes — not on every message.

Example:

```text
[rag-guardrails] guards active: limits(max 1000 chars), privacy(input=email+codice_fiscale+iban+phone, input_region=IT, output=email, output_region=IT), security(patterns+classifier meta-llama/Llama-Prompt-Guard-2-86M@0.85), tone(disabled)
```

When a whole family is switched off and that family is expected to be active by
default, the same line is raised as a `WARNING` and names what is left
uncovered:

```text
[rag-guardrails] guards active: limits(max 500 chars), privacy(disabled), security(patterns+classifier …), tone(disabled); no guard covers: privacy
```

Coverage is judged per **stage and category together**, not per category, because
the axes are orthogonal: `privacy` has a verdict on `input` and another on
`output`, and switching off one stage leaves the other working. So one stage
alone can be reported as uncovered:

```text
[rag-guardrails] guards active: limits(max 1000 chars), privacy(input=email+codice_fiscale+iban+phone, input_region=IT, output=disabled), security(patterns), tone(disabled); no guard covers: privacy(output)
```

Two details of that line are deliberate. A stage with nothing enabled says
`output=disabled` rather than being omitted, because the absence of a fragment
is only a signal to a reader who already knows it should be there. And when
*both* privacy stages are off the item collapses back to `privacy`, so the common
case keeps the wording operators already grep for instead of splitting into two
near-identical items.

`tone(disabled)` is intentionally present in the summary but absent from the
warning in the default configuration. The tone guard ships disabled by design,
so that state is reported without making every fresh installation look broken.

### A classifier that is configured but cannot run

The summary reports what can actually run, not what was ticked in the panel. An
enabled classifier appears in one of three forms:

```text
security(patterns+classifier meta-llama/Llama-Prompt-Guard-2-86M@0.85)
security(patterns+classifier(stack not installed: missing=torch+transformers))
security(patterns+classifier(unavailable))
```

The second says the optional classifier stack is absent from the image; the
third says the stack is there and this particular model already failed to load,
so it is in the negative cache and will not be retried until the plugin reloads.
They are different problems and the remedy differs: installing the stack again
fixes only the first.

Coverage follows from that rather than from the toggle:

- `security` stays covered when the deterministic patterns are on, because a
  classifier that cannot start degrades that guard without removing it;
- `security` is reported uncovered when the patterns are off too;
- `tone` is always reported uncovered, because it has no deterministic half.

`tone` appearing in `no guard covers:` is therefore not a contradiction of the
paragraph above. A tone guard *switched off* is a deliberate omission and stays
out of the warning; a tone guard *switched on* that cannot run is a broken
request, and a broken request is announced whatever the category's default.

## Block lines

One `INFO` line is written per block, naming the guard that stopped the turn,
so a refusal can be told apart from a normal answer and from another plugin's
block.

### Input privacy

```text
[rag-guardrails] input blocked, stage='input', category='privacy', verdict='personal_data', detected=email+phone (mobile), latency_ms=0.14; no retrieval, no generation, nothing stored in memory
```

### Output privacy

```text
[rag-guardrails] output blocked, stage='output', category='privacy', verdict='output_personal_data', detected=email; generated reply replaced before delivery
```

### Offensive input

```text
[rag-guardrails] input blocked, stage='input', category='tone', verdict='offensive_input', detector=classifier, model=IMSyPP/hate_speech_multilingual, label=violent, score=0.999, threshold=0.60, latency_ms=79.2; no retrieval, no generation, nothing stored in memory
```

On the offensive-input line, `score` needs one caution: it is the sum of the
classes that count as offensive for that model, not the score of the single
class named in `label`. Those classes are mutually exclusive, so a message can
be split between them and still be certainly offensive.

## Allowed path

A message that passes writes no block line, but it does write one operational
line at `INFO`. This lets an instance keep the core at its normal log level while
still showing that the plugin handled the turn and which checks covered it:

```text
[rag-guardrails] input allowed, stage='input', checks=length+injection_patterns+personal_data+injection_classifier, latency_ms=0.03
```

The output stage writes the matching line when it delivers an answer unchanged:

```text
[rag-guardrails] output allowed, stage='output', checks=email+codice_fiscale+iban+phone, latency_ms=0.08
```

`checks` names the detectors that actually examined the text, so a stage with
everything switched off reports `checks=none` rather than implying a check that
did not happen. On the output stage that line is written even then, before the
detectors are skipped: without it, three unrelated situations produced no output
line at all — a clean answer, a turn refused on `fast_reply` that never reached
generation, and an output stage with every detector off.

**«Actually examined» is meant literally, and on the input stage it is not the
same as «switched on».** The three deterministic checks are settled by the
configuration: when enabled they run every time and cannot fail. The two
classifiers can be enabled and still not look at a message — a model whose load
failed, or one still loading that this request waited five seconds for and gave
up on — so they appear in `checks` only when they report having examined it. A
turn during a cold start therefore reads:

```text
[rag-guardrails] input allowed, stage='input', checks=length+injection_patterns+personal_data, latency_ms=5000.58
```

with no `injection_classifier`, next to a single `classifier unavailable`
warning covering the whole degraded window. The line used to be built from the
settings instead, so it named the classifier on every one of those turns while
the deduplicated warning appeared once — which made the log say the opposite of
what had happened, on exactly the turns where it mattered.

The two lines are mutually exclusive with their `blocked` counterparts, so
counting either one by grepping cannot double count a turn.

While the local classifiers are being evaluated, pipeline reuse is also logged
at `INFO` for either one.

## Degraded configuration

When the configuration cannot be read, the plugin keeps the turn alive on the
shipped defaults and says so at `WARNING`, deduplicated:

```text
[rag-guardrails] settings unavailable (<reason>), using defaults; every configured value is discarded, including the Help Desk address shown to users. Not repeated until the configuration is read successfully
```

Three conditions produce it, and the first word of the reason tells them apart:
`settings unavailable (…)` when the core's own read raises, `settings are empty`
when `settings.json` is empty or `null`, and `invalid settings (…)` when it does
not validate.

**`WARNING` and not `INFO`, because this is a reduction of protection rather
than a normal event.** The fallback discards every configured value at once: the
thresholds, the toggles — so a guard an administrator switched on is now off —
the allowed-contacts list, the reply texts, and the Help Desk address, which
returns to the shipped placeholder and is shown verbatim to users in all five
refusal replies. It is the one degradation whose effect the *user* reads.

The deduplication clears on the first successful read, unlike the classifier
announcements: this condition is repaired by fixing a file, not by reloading the
plugin, so a second episode is announced again instead of being swallowed.

The reason is redacted before it is written. A `ValidationError` quotes the input
that failed validation, and one of the fields it can quote is the Hugging Face
token.

## Logging boundaries

The refused message itself is never logged by this plugin, on any path. Only
the shape of the violation is recorded:

- which detector matched
- which pattern fired
- which label and score crossed a threshold

One consequence is worth keeping in mind: Cheshire Cat itself logs every
incoming message before any plugin runs, so log retention remains a
data-protection question independent of this plugin.

