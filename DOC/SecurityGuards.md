# Security Guards

This document explains the security-oriented guards currently implemented in
`rag-guardrails`, with a detailed focus on the prompt injection guard
introduced in v1.

## Prompt Injection Guard v1

### Purpose

The prompt injection guard blocks user messages that try to alter the chatbot's
instructions, bypass its rules, or reveal hidden prompt material before the
request reaches retrieval or generation.

The guard runs on the Cheshire Cat `fast_reply` hook, so a blocked message:

- does not reach retrieval
- does not spend generation tokens
- is not written to episodic memory

This is the same early-stop path used by the existing length guard.

### Two-stage detection

The guard uses two independent detectors.

1. Custom detector

- implemented in `checks.py`
- conservative built-in IT/EN pattern set
- designed to catch explicit attempts such as:
  - ignoring previous instructions
  - bypassing rules or guardrails
  - revealing the system prompt or internal instructions

2. Local classifier

- implemented through `transformers.pipeline("text-classification", ...)`
- runs only if the custom detector does not block first
- uses the configured model and threshold

The combined logic is `OR`: one positive detector is enough to block.

### Execution flow

1. The plugin extracts the incoming text from working memory.
2. The normal deterministic input checks run.
3. If the prompt injection custom detector matches, the plugin returns the
   configured static reply immediately.
4. Otherwise, if the classifier is enabled, the plugin runs the configured
   local model.
5. If the classifier returns the configured injection label with score greater
   than or equal to the configured threshold, the plugin returns the same
   static reply immediately.
6. If neither detector trips, the message continues normally.

### Settings

The guard adds these admin settings:

- `Security guard: block prompt injection patterns`
- `Security guard: block prompt injection with local classifier`
- `Security guard: prompt injection classifier model`
- `Security guard: prompt injection classifier threshold`
- `Security guard: Hugging Face token`
- `Security guard: prompt injection reply`

The threshold is the minimum confidence required for the classifier to block a
message. Example:

- classifier output: `label=MALICIOUS`, `score=0.91`
- threshold: `0.85`
- result: block

If the score were `0.62`, the same label would not block with that threshold.

### Supported models in v1

- `meta-llama/Llama-Prompt-Guard-2-86M`
- `meta-llama/Llama-Prompt-Guard-2-22M`
- `deepset/deberta-v3-base-injection`

The default is `meta-llama/Llama-Prompt-Guard-2-86M`, chosen because this
project needs both Italian and English support.

### Access profile of the supported models

The three do not share one. Verified against the Hugging Face model cards on
2026-08-06:

| Model | Access | Token needed |
| --- | --- | --- |
| `meta-llama/Llama-Prompt-Guard-2-86M` | gated, approval granted manually by Meta | yes |
| `meta-llama/Llama-Prompt-Guard-2-22M` | gated, approval granted manually by Meta | yes |
| `deepset/deberta-v3-base-injection` | public | no |

Their licences are deliberately **not** repeated here. `DOC/Licenses.md` is the
single source for them, and it is the document re-verified before a release — a
second copy in this file would be the one nobody remembers to update.

The **shipped default model is gated**, but the classifier itself now ships
disabled. A fresh installation therefore starts on the built-in patterns alone,
with no warning and no Hugging Face dependency. If the classifier is enabled
without arranged access, it fails open, the guard falls back to its built-in
patterns alone, and the condition is reported in the log.

The full licence picture for every model this plugin can run, including the
offensive-input ones and what each licence implies, is in `DOC/Licenses.md`, which
carries the per-model table and its verification date. `README.md`, section
*License and model terms*, holds the short version and the legal attribution
required by Meta. Neither lives here.

### Enabling a gated model, the two steps

The failure is administrative, not technical, so restarting alone never fixes it:

1. **Accept the model terms** at `https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M`
   and wait for approval. For the Meta models it is granted manually and it is not
   immediate.
2. **Provide a read token**, then **restart the container**. The restart is not
   optional: a failed load is remembered and never retried until the plugin
   reloads, so a token added afterwards has no effect on a running instance.

The plugin says both of these in the log itself when it recognises an
authorisation failure, so whoever reads the warning does not have to find this
document:

```
[rag-guardrails] failed to load classifier model meta-llama/Llama-Prompt-Guard-2-86M: 401 Client Error … ; it will not be retried until the plugin reloads. This model needs authorised access, so the fix is not technical: 1) accept the model terms at https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M and wait for approval, which for the Meta models is granted manually and is not immediate; 2) fill in the Hugging Face token field in the plugin settings, then restart the container …
```

That guidance is appended only when the error text looks like an authorisation
problem — `401`, `403`, `gated`, `awaiting a review` and similar. A failure with
any other cause gets no instructions, deliberately: guessing the cause would send
the reader after the wrong problem.

### Hugging Face token handling

The plugin reads a token for gated models only from `Security guard: Hugging Face
token` in the plugin admin settings. It stores that value in plain text in
`settings.json` under the plugin directory. The file is ignored by Git, but it
must still be excluded from backups, container copies and support snapshots.

The token is used only to load gated classifier models. Public models do not
need it.

Operationally:

- no token is required for `deepset/deberta-v3-base-injection`
- a valid token plus approved model access are required for the two Meta models
- if a gated model is selected without valid access, the classifier goes
  `fail-open`, logs a warning, and the message continues normally

### Error policy

The classifier is `fail-open`.

If model loading, dependency import, or inference fails:

- the message is not blocked by the classifier
- the flow continues normally
- the plugin logs a warning

This keeps the chatbot available even when the classifier runtime is not.

#### A turn can also give up waiting for a load

There is a fourth way in, and it is the only one that leaves nothing broken
behind. Only one request loads a given model; the others wait for it, and the wait
is bounded at `CLASSIFIER_LOAD_WAIT_SECONDS`, currently `5.0`. A request that
reaches that bound raises `ClassifierUnavailable` and fails open exactly like the
others.

Nothing is wrong in that case: the load may well succeed a moment later, and the
next message finds the pipeline cached. What it costs is the wait itself, paid
inside `fast_reply` by every turn arriving during a cold start. Removing it means
loading the configured models outside the turn, which is tracked as open work in
`DEV/AGENTS/ISSUES_TODO.md`.

The full mechanism is in `DOC/ClassifierCache.md`, section *Only one request loads
a model*.

#### A failed model is not retried

When a model fails to load, the failure is remembered for the lifetime of the
plugin and the load is never attempted again. This is about cost, not tidiness.

Without it, every message retries the load, and `transformers` re-resolves the
repository on the Hub each time — so the shipped default, a gated Meta model with
no token, would cost a **network round trip inside `fast_reply`**, the hook that
runs before retrieval, before generation, before anything. The turn's latency
would depend on Hugging Face's response time, and on an unreachable Hub, on the
timeout. Measured on three clean messages in that configuration, the failure path
went from nine log lines to three, all of them on the first message.

Retrying could not help anyway: the token comes from the settings, and changing
it requires a plugin reload. The reload also clears failed and successful
pipelines.

Two consequences worth knowing:

- **Adding a token or approving model access requires a restart** of the Cheshire
  Cat container to take effect. Selecting a *different* model from the admin panel
  does not: the cache is per model, so switching to the public
  `deepset/deberta-v3-base-injection` works immediately.
- **The failure is reported once, not once per message.** The state cannot change
  until the plugin reloads, so repeating it every turn would bury the log exactly
  when a configuration problem needs diagnosing. The single warning names what
  still covers the turn — the built-in patterns — or states that the `security`
  category covers nothing at all when those are disabled too. That matters because
  the `guards active` announcement is built from the settings, so it claims a
  classifier that turns out not to run.

While a model is unavailable, the `INFO` line of an allowed message stops listing
`injection_classifier` among the checks that covered the turn. A check that cannot
run must not appear as coverage.

### Dependency model

The classifier introduces explicit runtime dependencies declared in
`requirements.txt`.

This is intentional. The plugin must not depend on libraries that happen to be
installed only because another plugin shares the same Cheshire Cat instance.

### Logging and measurement

The guard logs the minimum information needed to evaluate effectiveness and
latency without logging the original message text.

Whether the guard is active at all is announced separately, once when the plugin
starts guarding and again on every configuration change, never per message. The
`security(...)` part of that line reports which mechanisms are on, with the
model and threshold in use; with both mechanisms off it becomes a `WARNING`
naming `security` as uncovered. This matters because a disabled guard is
otherwise indistinguishable, in the log, from a guard that finds nothing.

A message that passes writes no verdict line. At `INFO` it writes one line
listing the checks that covered the turn — `injection_patterns`,
`injection_classifier` — and the latency. This makes the guard path visible while
the core stays at its default `INFO` level. Pipeline reuse is logged at the same
level while the classifier feature is being evaluated.

When a block happens, the logs identify at least:

- the guard category, `security`, and the verdict, `prompt_injection`
- whether the detector was `custom` or `classifier`
- which of the built-in patterns matched, by name, if the custom detector blocked
- the selected model, if the classifier blocked
- the classifier score and threshold, if applicable
- the classifier latency in milliseconds, when measured

The pattern name is what makes a false positive diagnosable: since the refused
text is deliberately never logged, without it the line holds nothing to reason
from. The names are therefore part of the log contract — renaming one is a
breaking change for whoever greps these lines.

During model loading, the runtime also logs, at `INFO`:

- when the plugin starts loading a classifier model into memory
- when model loading succeeds
- when model loading fails, as a warning
- when a classifier pipeline is found already cached in memory

On the first successful use of each configured model, the plugin also checks
that the model declares the raw label mapped to `MALICIOUS`: `LABEL_1` for both
Meta models and `INJECTION` for DeBERTa. The classifier translates the raw
label before applying the threshold, so logs and hook results use the shared
semantic names `BENIGN` and `MALICIOUS`. If the raw blocking label is absent,
the plugin logs a `WARNING` saying the classifier is enabled but cannot block
anything until its label mapping is updated.

A pipeline found already cached in memory is currently logged at `INFO` too.
That is acceptable for v1 because it makes classifier reuse visible while the
feature is being evaluated, but if it proves too noisy under real traffic it
should be demoted to `DEBUG` in a later cleanup.

Those messages are intentionally phrased in terms of the plugin's own state.
With a plain `transformers.pipeline(...)` call, the plugin can reliably know
whether the model was already cached in memory, but it cannot always prove
whether the underlying files were freshly downloaded from Hugging Face or read
from the local Hugging Face disk cache.

### Limits of v1

This first version is intentionally narrow.

- The custom detector is conservative and catches explicit attempts only.
- The classifier is local and model-based, but its real precision on this 
  corpus must be measured rather than assumed.
- The guard covers direct prompt injection on the user message, not indirect
  prompt injection through retrieved documents.
- Different thresholds per model, GPU selection, and structured telemetry are
  outside the scope of v1.
