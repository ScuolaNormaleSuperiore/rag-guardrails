# Classifier Labels

How classifier labels are handled in `rag-guardrails`.

This document explains, in detail, how the plugin interprets the labels returned
by its local classifiers, how it decides whether a message must be blocked, and
what happens when a model declares unexpected labels or no readable labels at
all.

The plugin currently has two model-based guards:

- the prompt-injection classifier
- the offensive-input classifier

They do **not** handle labels in the same way, because their decision rules are
different.

## Two layers to keep separate

Whenever a classifier runs, there are two separate questions:

1. **What labels can this model return?**
2. **Which of those labels count as a block for this guard?**

The first question is about the model's declared output space.
The second is about the plugin's policy.

For the prompt-injection guard, the policy is "one expected blocking label per
model". For the offensive-input guard, the policy is "a set of blocking classes
per model, aggregated by score".

## Prompt-Injection Classifier

Implementation: `prompt_injection_classifier.py`

### Static label mapping

The plugin defines one expected blocking label per supported model:

| Model | Expected blocking label |
| --- | --- |
| `meta-llama/Llama-Prompt-Guard-2-86M` | `MALICIOUS` |
| `meta-llama/Llama-Prompt-Guard-2-22M` | `MALICIOUS` |
| `deepset/deberta-v3-base-injection` | `INJECTION` |

This mapping is the table `PROMPT_INJECTION_CLASSIFIER_LABELS`.

### Decision rule

When the classifier runs:

1. the plugin gets the model result
2. it takes the **top** label only
3. it normalizes that label with `strip().upper()`
4. it compares it to the expected blocking label of that model
5. it blocks only if:
   - the normalized label equals the expected blocking label
   - the score is greater than or equal to the configured threshold

So this guard is a **single-label** decision rule.

Example:

- model: `meta-llama/Llama-Prompt-Guard-2-86M`
- result: `label="MALICIOUS", score=0.91`
- threshold: `0.85`
- outcome: block

If the same model returns:

- `label="BENIGN", score=0.99`

the message is **not** blocked, because the label does not match the expected
blocking label, however high its score is.

### Runtime verification of the declared labels

At the first successful use of each configured model, the plugin also checks
that the model actually declares the expected blocking label in its `id2label`.

That check is:

- read the labels through `model_labels(pipeline)`
- normalize them to upper case
- confirm that the expected blocking label is present

If the expected label is missing, the plugin writes a `WARNING`.

That warning means:

- the classifier is enabled
- the model loaded successfully
- but the plugin's mapping for that model is wrong or stale
- therefore the check cannot block anything reliably

The warning is emitted once per model, not once per message.

### If the labels are correct but the model classifies badly

This is **not** a label-mapping problem.

Example:

- the model declares `BENIGN` and `MALICIOUS`
- the plugin confirms that `MALICIOUS` exists
- but the model still classifies an ordinary insult as `MALICIOUS`

That is a problem of:

- model behavior
- threshold choice
- domain mismatch

The plugin does not and should not treat that as a label mismatch.

## Offensive-Input Classifier

Implementation: `offensive_input_classifier.py`

This guard is more complex because the supported models do not expose one
uniform blocking label.

### Static translation table: raw label -> semantic class

Many of these models return labels such as `LABEL_0`, `LABEL_1`, and so on.
Those are not useful enough to reason about directly, so the plugin translates
them into readable semantic class names.

Example for `IMSyPP/hate_speech_multilingual`:

| Raw label | Semantic class |
| --- | --- |
| `LABEL_0` | `appropriate` |
| `LABEL_1` | `inappropriate` |
| `LABEL_2` | `offensive` |
| `LABEL_3` | `violent` |

Example for `textdetox/bert-multilingual-toxicity-classifier`:

| Raw label | Semantic class |
| --- | --- |
| `LABEL_0` | `neutral` |
| `LABEL_1` | `toxic` |

This mapping is the table `OFFENSIVE_INPUT_CLASSIFIER_CLASSES`.

### Static blocking set: which semantic classes block

The plugin then defines, per model, which of those semantic classes count as a
block.

| Model | Blocking classes |
| --- | --- |
| `IMSyPP/hate_speech_multilingual` | `OFFENSIVE`, `VIOLENT` |
| `patriciacarla/HS-multilingual-DNR` | `OFFENSIVE`, `VIOLENT` |
| `textdetox/bert-multilingual-toxicity-classifier` | `TOXIC` |

This mapping is the table `OFFENSIVE_INPUT_CLASSIFIER_LABELS`.

One important decision is explicit here:

- `INAPPROPRIATE` is **not** a blocking class

That is intentional, because a generic help desk receives frustrated and rude users
whose messages should not automatically be refused.

### Decision rule

This guard does **not** use the top label only.

When the classifier runs:

1. the plugin gets **all** scores
2. it translates each raw label into a semantic class
3. it keeps only the classes that belong to the blocking set for that model
4. it **sums** the scores of those blocking classes
5. it blocks if the sum is greater than or equal to the configured threshold

The returned values are:

- `score`: the sum of the blocking classes
- `label`: the strongest blocking class among them

So this guard is a **multi-label-by-aggregation** decision rule, even when the
underlying model is a single-label classifier.

Example:

- `OFFENSIVE = 0.45`
- `VIOLENT = 0.40`
- total blocking score = `0.85`

If the threshold is `0.60`, the message is blocked, even though neither class
alone reaches `0.60`.

## Runtime verification of offensive-input labels

The offensive-input guard also verifies labels at runtime, but the check is
different from the prompt-injection one.

It asks:

- do the labels declared by the model map, through the plugin's translation
  table, to **at least one reachable blocking class**?

If the answer is no, the plugin writes a `WARNING`.

That warning means:

- the classifier is enabled
- the model loaded successfully
- but none of the labels the model declares can ever reach a blocking class
- therefore the guard is active in configuration but inert in practice

Again, the warning is emitted once per model, not once per message.

## What happens if `id2label` cannot be read

Both guards rely on `model_labels(pipeline)` from `classifier_runtime.py` to
read the model's declared labels.

If the configuration cannot be read, `model_labels(pipeline)` returns an empty
tuple.

This has a narrow effect:

- the **verification** step cannot confirm or reject the mapping
- the classifier still runs on the model output it receives

So:

- no label-mismatch warning is emitted
- no load failure happens just because `id2label` was unreadable
- the decision still follows the normal rule of the guard
- **the model is not recorded as verified**, so the check is attempted again on
  the next message rather than skipped for the life of the plugin

This is deliberate: inability to verify the labels is weaker than inability to
run the model at all.

That last point is the difference between «not verified yet» and «verified»,
and getting it wrong is silent. The tone guard used to record the model before
reading its labels, so an unreadable configuration filed it as verified without
anything having been checked — and the warning that exists precisely to catch a
guard which is switched on and cannot block would then never be emitted for that
model. Both guards now record only after a successful read.

## What happens if the model cannot load

This is not a label-handling case.

If the model cannot load:

- `classifier_runtime.get_pipeline()` fails
- the reason goes into the negative cache
- the guard goes fail-open
- the message is not blocked by that classifier

In that situation, label handling never starts, because there is no loaded model
to inspect.

## Exact behavior by situation

### Prompt-injection classifier

| Situation | What happens |
| --- | --- |
| Expected label exists, top label matches, score above threshold | block |
| Expected label exists, top label matches, score below threshold | allow |
| Expected label exists, top label does not match | allow |
| Expected label missing from declared labels | warning, then normal classification still runs |
| Declared labels unreadable | no warning, normal classification still runs |
| Model load fails | fail-open, no classification |

### Offensive-input classifier

| Situation | What happens |
| --- | --- |
| Blocking classes reachable, summed blocking score above threshold | block |
| Blocking classes reachable, summed blocking score below threshold | allow |
| No declared label maps to a blocking class | warning, then normal classification still runs but cannot block meaningfully |
| Declared labels unreadable | no warning, normal classification still runs |
| Model load fails | fail-open, no classification |

## Summary Table

| Guard | What the model returns | Plugin mapping | Decision rule | Runtime warning condition |
| --- | --- | --- | --- | --- |
| Prompt injection | one top label with score | one expected blocking label per model | block if top label equals expected label and score >= threshold | expected label not present in declared labels |
| Offensive input | all labels with scores | raw label -> semantic class, plus blocking set per model | block if the sum of blocking-class scores >= threshold | no declared label maps to any blocking class |

## Why the two guards differ

The difference is architectural, not accidental.

Prompt injection is treated as:

- one model-specific blocking label
- one score to compare against one threshold

Offensive input is treated as:

- several semantic behaviors that all mean "refuse"
- a sum over the classes that belong to that set

Using the same label-handling strategy for both would be misleading. The plugin
therefore keeps two separate policies, and verifies each one against the labels
the loaded model actually declares.

