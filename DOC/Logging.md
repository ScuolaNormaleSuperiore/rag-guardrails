# Logging

How `RAG Guardrails` reports its activity in the logs.

This document is the detailed reference for guard-related log lines. The
taxonomy of `stage`, `category` and `verdict` is defined in
`DOC/GuardTaxonomy.md` and is not repeated here in full.

## Purpose

A guardrail that stops working raises no error: the chatbot simply keeps
answering unguarded. The log is what makes that visible, so it answers two
different questions:

- which guards are active
- why a request or reply was blocked

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

A message that passes writes no block line. The guards stay silent when they
find nothing.

Set `CCAT_LOG_LEVEL=DEBUG` to get one line per allowed input message when
diagnosing a specific request:

```text
[rag-guardrails] input allowed, stage='input', checks=length+injection_patterns+personal_data+injection_classifier, latency_ms=0.03
```

One exception currently remains at `INFO`: while the local classifiers are
being evaluated, pipeline reuse is still logged at that level for either one.

## Logging boundaries

The refused message itself is never logged by this plugin, on any path. Only
the shape of the violation is recorded:

- which detector matched
- which pattern fired
- which label and score crossed a threshold

One consequence is worth keeping in mind: Cheshire Cat itself logs every
incoming message before any plugin runs, so log retention remains a
data-protection question independent of this plugin.

