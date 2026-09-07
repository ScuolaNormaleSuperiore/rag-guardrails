# Guard Taxonomy

This document defines the taxonomy used by `rag-guardrails` to describe
guard activity in a way that stays stable across implementation changes.

The taxonomy has three independent axes:

- `stage`
- `category`
- `verdict`

They must stay separate. A name on one axis must not encode another axis.

## Purpose

The plugin needs guard names that answer different questions cleanly:

- **Where** in the flow did a control act?
- **Why** did it act?
- **Which specific control** acted?

Trying to answer all three with one label leads to noisy names such as
`security-input` or `privacy-output-personal-data`, which become hard to log,
count, and evolve.

The taxonomy below keeps those concerns separate.

## Axis 1: `stage`

`stage` says **where in the pipeline** a guard acts.

Current values:

- `input`
- `output`

Possible future values:

- `retrieval`
- `session`

Examples:

- an input length check belongs to `input`
- a leakage check on the generated answer belongs to `output`

## Axis 2: `category`

`category` says **what kind of risk or problem** the guard addresses.

Current categories:

- `limits`
- `privacy`
- `security`
- `tone`
- `quality`

Meaning of each category:

- `limits`
  - quantitative or structural limits
  - example: maximum message length
- `privacy`
  - personal data, sensitive data, or leakage of such data
  - examples: user message contains an e-mail address; generated answer exposes
    a phone number
- `security`
  - attempts to bypass rules, manipulate the assistant, or obtain hidden
    instructions
  - examples: prompt injection, jailbreak-style input
- `tone`
  - register: how the user expresses themselves, and how the assistant does
  - examples: offensive or violent incoming message; answer written in a
    non-default tone
- `quality`
  - answer correctness or fitness problems that are not primarily privacy,
    security or register issues
  - examples: groundedness, relevance, language mismatch

`tone` and `quality` are the pair most easily confused, so the boundary is
stated rather than left to judgement: `tone` is about *how* something is said,
`quality` about whether the answer is *right and useful*. An answer that is
grounded and correct but rude is a `tone` problem; an answer that is polite and
ungrounded is a `quality` problem.

Category names must describe the **type of issue**, not where it happened.

For that reason, names such as `security-input` or `quality-output` should not
be used as categories.

## Axis 3: `verdict`

`verdict` says **which specific control** fired.

A verdict is the most specific label in the taxonomy.

Examples of current and planned verdicts:

- `message_length`
- `personal_data`
- `prompt_injection`
- `offensive_input`
- `output_personal_data`
- `output_tone`
- `output_language_mismatch`
- `output_groundedness`

Verdict names may contain a prefix such as `output_` when that helps keep them
clear and unique, but this does not replace the `stage` axis in logs or
telemetry.

## Current mapping

| Stage | Category | Verdict |
| --- | --- | --- |
| `input` | `limits` | `message_length` |
| `input` | `privacy` | `personal_data` |
| `input` | `security` | `prompt_injection` |
| `input` | `tone` | `offensive_input` |
| `output` | `privacy` | `output_personal_data` |

Planned output examples:

| Stage | Category | Verdict |
| --- | --- | --- |
| `output` | `tone` | `output_tone` |
| `output` | `quality` | `output_language_mismatch` |
| `output` | `quality` | `output_groundedness` |

`privacy` and `tone` each carry a verdict on both stages, which is the clearest
demonstration that the axes are orthogonal and not nested: the category says what
kind of problem it is, the stage says where it was caught, and neither is derived
from the other.

## Naming rules

- `stage` answers **where**
- `category` answers **why**
- `verdict` answers **which control**

Do not merge those answers into one field.

Good examples:

- `stage='input', category='security', verdict='prompt_injection'`
- `stage='output', category='quality', verdict='output_groundedness'`
- `stage='output', category='privacy', verdict='output_personal_data'`

Bad examples:

- `category='security-input'`
- `category='privacy-output'`
- `verdict='output-quality-groundedness'`

## Why this separation matters

Keeping the three axes separate makes the logs and future telemetry answer
different operational questions without brittle parsing:

- How many `privacy` events happened in total?
- How many happened on `input` versus `output`?
- Which specific `verdict` fires most often?
- Which `quality` checks are too noisy?

If stage and category are merged, those questions become harder to answer and
harder to keep stable over time.

## Guidance for future guards

When adding a new guard:

1. Decide the `stage`
2. Reuse an existing `category` if the risk type already exists
3. Add a new `verdict` only for the specific control
4. Keep the mapping explicit in code and tests

Examples:

- the current output PII leak guard:
  - `stage='output'`
  - `category='privacy'`
  - `verdict='output_personal_data'`
- a future output tone check:
  - `stage='output'`
  - `category='tone'`
  - `verdict='output_tone'`
  - the same category as the input offensive check, because the kind of problem
    is the same one seen from the other end of the turn
- a future retrieval normalization check:
  - `stage='retrieval'`
  - category depends on what it protects
  - verdict should name the specific retrieval control

