# Output Guards

Current output-side guard behavior for `rag-guardrails`.

This document describes only the output guards that exist today. The taxonomy
of `stage`, `category` and `verdict` is defined in
`DOC/GuardTaxonomy.md` and is not repeated here.

## Scope

The first output-guard iteration implements one check only:

- `stage='output'`
- `category='privacy'`
- `verdict='output_personal_data'`

It runs just before the generated answer is delivered to the user.

## Implemented check

### Personal data leakage

Purpose:

- stop generated replies that contain personal data

Detectors used:

- e-mail addresses
- phone numbers, validated with `phonenumberslite`
- codice fiscale, validated with its check character
- IBAN, validated with mod-97

The output guard deliberately reuses the same detector family already used on
input. The difference is not the detection logic, but the point in the flow,
the fallback text shown to the user, and the fact that output detectors are now
configured independently from input detectors.

### Contacts that are not personal data

Two exemptions apply to the e-mail and phone detectors, on **both** stages.

The address configured as `Help Desk e-mail` is always exempt. Anything listed
in `Privacy guards: public service contacts` is exempt as well — one contact per
line, e-mail addresses and phone numbers in the same field.

The exemption exists because without it the guard contradicts the deployment.
The prompt asks the model to point the user at the Help Desk when it cannot
answer; an answer that obeys carries a published contact, and a detector with no
notion of "published" reads that as a leak and discards the whole reply. The
same asymmetry applies on input, where a user writing *"I already called
050 509111"* would be refused.

Three properties of how it is implemented are worth knowing, because each one is
a decision rather than an accident:

- **The exclusion happens at match time, not after a verdict.** An exempt
  contact never enters the set of matched detectors, so there is no verdict to
  forgive. The consequence that matters: a text carrying a public contact *and*
  a personal one still blocks, because the personal one is still in the set.
- **Phone numbers are compared as numbers, not as strings.** Both sides are
  normalised to E.164 before comparison, so an entry written `+39050509111`
  exempts a reply that says `050 509111`, and the other way round. A number
  written without an international prefix is resolved against the region of the
  stage doing the checking, and the two stages have separate region settings —
  which is why the field asks for the international prefix.
- **The list is shared by both stages, deliberately**, unlike the detector
  toggles and the regions. Whether a contact is published is a property of the
  contact, not of the direction it travels in; a per-stage list would only
  permit the incoherent state where a number is public on the way out and
  private on the way in.

An entry that does not parse is dropped rather than raised on, so one wrong line
cannot stop the guard from working. The settings model rejects malformed entries
when the panel is saved, which is where the mistake is visible.

The log follows the same rule: an exempt number is excluded from the detail as
well, so a block caused by a personal number does not report the kind of the
service number that happened to sit in the same sentence.

## What happens when it blocks

When the check fires:

- the generated answer is not delivered as-is
- the plugin replaces it with a static fallback reply
- the line is logged as an output-side privacy block

The replacement happens before the answer is written to the AI side of the
conversation history as the final outgoing message.

## Available settings

The current output-side settings are:

- `Output privacy guard: block e-mail`
- `Output privacy guard: block fiscal code`
- `Output privacy guard: block IBAN`
- `Output privacy guard: block phone numbers`
- `Output privacy guard: phone numbers region`
- `Output privacy guard: personal data detected reply`

The output-side guard is active whenever at least one of those output detectors
is enabled.

One setting is shared with the input stage rather than duplicated, for the
reason given above:

- `Privacy guards: allowed contacts`

The corresponding input-side privacy settings are independent:

- `Input privacy guard: block e-mail`
- `Input privacy guard: block fiscal code`
- `Input privacy guard: block IBAN`
- `Input privacy guard: block phone numbers`
- `Input privacy guard: phone numbers region`
- `Privacy guard: personal data detected reply`

## Logging

Example block line:

```text
[rag-guardrails] output blocked, stage='output', category='privacy', verdict='output_personal_data', detected=email; generated reply replaced before delivery
```

As on input:

- the refused/generated text itself is not logged
- the log records only the shape of the violation
- `stage`, `category` and `verdict` stay separate

## Known limits of v1

- only personal-data leakage is implemented on output
- no groundedness or citation-consistency check is active
- no output language check is active
- no register check on the answer is active
- the fallback is a full replacement, not a local redaction

## Verifications deliberately left outside v1

The three checks absent from this iteration are **decisions, not omissions**, and
each one is tracked somewhere so it does not have to be rediscovered. Do not
re-add them to a plan as missing work.

| Check | Intended taxonomy | Where it is tracked |
| --- | --- | --- |
| Answer language matches the question | `output` / `quality` / `output_language_mismatch` | An open issue in `DEV/AGENTS/ISSUES_TODO.md`, plus the manual checklist in `DOC/TestingCode.md`. Deliberately a **final verification**, not a guard |
| Groundedness and citation consistency | `output` / `quality` / `output_groundedness` | An open issue in `DEV/AGENTS/ISSUES_TODO.md`; the architecture is still intentionally left open until a citation format exists |
| Register of the answer | `output` / `tone` / `output_tone` | An open issue in `DEV/AGENTS/ISSUES_TODO.md`, together with the single-rewrite strategy that would remedy it. Its category is `tone`, the same as the input offensive check — see `DOC/ToneGuards.md` |

Why the language check is a verification rather than a guard is worth knowing,
because it looks like an easy win: making the *model* answer in the language of
the question is a prompt instruction, already handled outside this plugin, and a
detector could only contradict it. Measured on the installed instance,
`langdetect` returns Afrikaans for `password` and German for `VPN`, both above
0.999 confidence. On a full answer the text is long enough to classify reliably,
which is why the door is left open — but the first step is confirming on the live
instance that the prompt instruction is honoured, not writing a check.

The groundedness one is the opposite case: it is the **highest-value control still
missing** from the pipeline, because it is the only one that would look at what
the model actually produced against the evidence it was given. It is deferred for
a concrete reason — it needs a citation format to compare against, and that
format is not defined yet.

