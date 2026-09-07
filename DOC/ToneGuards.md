# Tone Guards

Current register-oriented guard behaviour for `rag-guardrails`, with a
detailed focus on the offensive-input check.

The taxonomy of `stage`, `category` and `verdict` is defined in
[GuardTaxonomy.md](GuardTaxonomy.md), which is the single source for it. This
document describes only what exists today.

## What `tone` covers, and what it does not

`tone` is the guard family for **register**: how the user expresses themselves,
and how the assistant does. It is deliberately separate from two neighbours that
are easy to confuse it with:

- not `security`, which is about *manipulating* the assistant — an insult is not a
  prompt injection;
- not `quality`, which is about whether the answer is *right and useful* — an
  answer that is grounded and correct but rude is a `tone` problem, one that is
  polite and ungrounded is a `quality` problem.

| Stage | Category | Verdict | State |
| --- | --- | --- | --- |
| `input` | `tone` | `offensive_input` | implemented, shipped **switched off** |
| `output` | `tone` | `output_tone` | not built: an open issue in `DEV/AGENTS/ISSUES_TODO.md` |

## Offensive Input Guard

### Purpose

Refuse incoming messages carrying offensive or violent content, before they reach
retrieval or generation. It runs on `fast_reply`, so a refused message:

- does not reach retrieval
- does not spend generation tokens
- is not written to episodic memory

This is the same early-stop path used by the length, privacy and prompt-injection
guards.

### It is the only check that ships switched off

`detect_offensive_input_classifier` defaults to `False`. That is a decision, not an
oversight, and it has two reasons:

- it loads a **second model** into memory and adds one inference to every message
  that reaches it — measured at **79 ms** on CPU on the development instance;
- its precision on real help-desk traffic has not been measured yet. The shipped
  threshold comes from a seven-message probe, which is a starting point and not a
  calibration.

Enable it from the admin panel after reading the log line it writes on the first
message. Until then the `tone` category is uncovered, and the `guards active` line
says so as `tone(disabled)`.

### Where it runs in the sequence

Last of the input checks:

1. `length` — deterministic
2. `injection_patterns` — deterministic
3. `personal_data` — deterministic
4. `injection_classifier` — model
5. `offensive_input` — model

The first verdict found wins and stops the sequence. One consequence is worth
knowing because it is deliberate: a message that is **both** offensive **and** a
prompt-injection attempt is reported as `prompt_injection`, and the user gets that
reply. An attack on the assistant is the more pertinent correction to give back.

A message stopped by any deterministic check never reaches this one, so a refusal
for length or personal data costs no model inference.

### The threshold is compared against a sum, not a single label

This is the one thing to understand before configuring it, and it differs from the
prompt-injection guard.

The supported models expose several classes that all mean «refuse». Their classes
are mutually exclusive — verified as `single_label_classification`, so the scores
are a softmax and add up to one — which makes the **sum of the blocking classes**
the probability that the message belongs to the set being refused. That sum is
what the threshold meets.

Comparing only the highest label instead would let through the case where the
model is certain about the set and undecided inside it:

```
offensive 0.45   violent 0.40   appropriate 0.10   inappropriate 0.05
```

Neither blocking class reaches 0.60 alone, while together they are an 85% verdict
that no single label reports.

**The same number therefore means something stricter here than in the
prompt-injection guard**, whose threshold meets the score of one label. The two
settings are not comparable and the shipped defaults differ for that reason: 0.60
here, 0.85 there.

### Measured behaviour

On the installed instance with the default model, blocking sum per message:

| Message | Sum | At 0.60 | At 0.85 |
| --- | --- | --- | --- |
| Legitimate help-desk question, IT | 0.006 | passes | passes |
| Legitimate help-desk question, EN | 0.017 | passes | passes |
| Exasperated user swearing at a broken service | 0.423 | passes | passes |
| Explicit hate speech, IT | **0.782** | **refused** | passes |
| Insult, IT | 0.984 | refused | refused |
| Insult, EN | 0.994 | refused | refused |
| Threat of violence, IT | 0.999 | refused | refused |

The measured gap is between 0.42 and 0.78, and the default sits in it with margin
on both sides. **This is why the threshold was not inherited from the
prompt-injection guard**: at 0.85 the hate-speech message is delivered unblocked.

Seven messages are not a corpus. Widen the probe to real traffic before enabling
the check in production.

### `inappropriate` does not block, and that is a decision

The blocking set is `offensive` and `violent`. The four-class models also expose
`inappropriate` — vulgar or rude, with no target — and it is deliberately left out.

A help desk receives exasperated users, and «questa maledetta VPN non
funziona mai» falls on `inappropriate`: it is a support request written badly.
Refusing it is the kind of error that gets a guard switched off by the
administrator, and then the protection is not smaller, it is absent.

Two things make the exclusion cheaper than it looks. With the sum semantics,
including `inappropriate` would be considerably more aggressive than it would be
with a single-label rule — a message at `inappropriate 0.50 + offensive 0.30 +
violent 0.10` would reach 0.90 where two classes leave it at 0.40. And the
assistant's own register is covered elsewhere: `output_tone`, in the same
category, which is an open issue and not built. The input guard does not have to teach the user manners, it
has to stop insults and threats.

### Supported models and their labels

| Model | Blocking classes | Licence |
| --- | --- | --- |
| `IMSyPP/hate_speech_multilingual` — **default** | `offensive`, `violent` | MIT |
| `patriciacarla/HS-multilingual-DNR` | `offensive`, `violent` | Apache-2.0 |
| `textdetox/bert-multilingual-toxicity-classifier` | `toxic` | OpenRAIL++ |

All three are public: no Hugging Face token is needed. Licences were verified
against the model cards on 2026-08-06 and the full picture, including what
OpenRAIL++ implies, is in `README.md`, section *License and Legal Notes*.

**The labels these models return are technical, not semantic.** All three expose
`LABEL_0`, `LABEL_1`, … through `id2label`; the readable class names of the model
cards exist nowhere at runtime. The plugin therefore carries two tables:
`OFFENSIVE_INPUT_CLASSIFIER_CLASSES` translates what the pipeline returns into the
readable class, and `OFFENSIVE_INPUT_CLASSIFIER_LABELS` names which readable
classes block.

That is not a duplication. It keeps the knowledge of the model card written down
instead of buried in indices, it keeps the blocking set reviewable without
counting positions, and it lets the log say `label=violent` rather than
`label=LABEL_3`. For the default model the mapping is
`LABEL_0` appropriate, `LABEL_1` inappropriate, `LABEL_2` offensive,
`LABEL_3` violent — an ordering confirmed by inference, not only by the model
card: a legitimate help-desk question scores 0.99 on `LABEL_0`, an insult 0.98 on
`LABEL_2`, a threat 1.00 on `LABEL_3`.

Configuration is by model choice, never by writing label strings in the admin
panel: the labels are not uniform across models, so a single free-text field would
be wrong for at least two of the three.

### A label mismatch is reported, loudly

If a model returns labels the translation table does not map, no blocking class is
ever reached, every message passes, and **nothing else would say so** — a message
that passes writes no verdict line, so a guard that is switched on and inert looks
exactly like a guard that is finding nothing.

On the first load of each model the plugin therefore compares the labels the model
declares against its own table, and logs a `WARNING` when no blocking class is
reachable, naming the labels it received and what it expected. Reported once per
model, and never raised: a mapping problem must not take down the hook that runs
before everything else.

While a model is in that state it is also dropped from the `checks=` list of the
`INFO` line for an allowed message. A check that cannot block must not appear as
coverage.

### Error policy

`fail-open`, like the prompt-injection classifier. If loading, the dependency
import, or inference fails:

- the message is not blocked by this check
- the flow continues normally
- the plugin logs a warning, once per failure, not once per message

The warning differs from the prompt-injection one in what it can promise. That
guard falls back on its built-in patterns; **this one has no deterministic half**,
so when its model does not load the `tone` category covers nothing, and the line
says exactly that:

```
[rag-guardrails] offensive-input classifier unavailable (IMSyPP/hate_speech_multilingual: …), continuing without blocking; no guard covers: tone — this check has no deterministic fallback. Not repeated until the plugin reloads
```

A failed load is remembered and never retried until the plugin reloads, which is
what keeps a broken configuration from costing a network round trip inside
`fast_reply` on every message. Selecting a *different* model from the admin panel
works immediately, because the cache is per model.

### What the log records

One `INFO` line per refusal, never the refused text:

```
[rag-guardrails] input blocked, stage='input', category='tone', verdict='offensive_input', detector=classifier, model=IMSyPP/hate_speech_multilingual, label=violent, score=0.999, threshold=0.60, latency_ms=79.2; no retrieval, no generation, nothing stored in memory
```

`label` is the strongest blocking class and `score` is the **sum** of all of them.
Both are needed: without the label a refusal at 0.9 would not say which behaviour
was recognised, and the label alone would not explain the number.

The refused message itself never reaches the log, on any path, and a test asserts
it.

### Limits of this version

- The threshold is a measured starting point over seven messages, not a
  calibration on real traffic.
- Only direct offensive content in the user message is covered, not offensive
  material arriving through retrieved documents.
- The register of the *assistant's* answer is not checked here: that is
  `output_tone`, which is an open issue in the same category and not built.
- No per-class thresholds and no GPU selection.

