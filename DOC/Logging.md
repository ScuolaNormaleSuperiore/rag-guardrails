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

The two lines are mutually exclusive with their `blocked` counterparts, so
counting either one by grepping cannot double count a turn.

While the local classifiers are being evaluated, pipeline reuse is also logged
at `INFO` for either one.

## Logging boundaries

The refused message itself is never logged by this plugin, on any path. Only
the shape of the violation is recorded:

- which detector matched
- which pattern fired
- which label and score crossed a threshold

One consequence is worth keeping in mind: Cheshire Cat itself logs every
incoming message before any plugin runs, so log retention remains a
data-protection question independent of this plugin.

