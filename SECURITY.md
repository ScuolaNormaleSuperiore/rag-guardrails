# Security Policy

This plugin exists to block things. A defect in it is therefore not an
inconvenience: it is the absence of a control someone is relying on, and it is
silent by nature. Please report it privately, so a fix exists before the bypass
does.

## Supported versions

| Version | Supported |
| --- | --- |
| `1.0.x` | yes |
| `0.0.x` | no — internal pre-release versions, never published |

## How to report

**Do not open a public issue for a security defect.** A working guard bypass
posted publicly is an exploit against every installation of this plugin,
including the ones that cannot upgrade today.

1. Preferred: GitHub private vulnerability reporting — on the repository page,
   *Security* → *Report a vulnerability*. It creates a private advisory visible
   only to the maintainers.
2. If you cannot use GitHub, write to the contact address published at
   <https://ict.sns.it> and put `rag-guardrails security` in the subject.

Please include, as far as you can:

- the guard involved (`message_length`, `personal_data`, `prompt_injection`,
  `offensive_input`, `output_personal_data`);
- the exact input, and the reply you got instead of the expected block;
- the plugin version from `plugin.json`, the Cheshire Cat AI version, and
  whether the local classifiers were enabled;
- any log line from the plugin that looks relevant. Redact secrets before
  sending: a Hugging Face token can appear in a third-party exception text.

We will acknowledge your report and tell you whether we can reproduce it. If we
cannot, we will say what we tried, rather than close it in silence.

## In scope

- Any input that passes a guard which should have stopped it — in particular a
  prompt injection the bilingual patterns miss, or personal data the privacy
  guards do not recognise.
- Any generated reply containing personal data that the output guard delivers
  anyway.
- Any path that writes a Hugging Face token, or any other secret, into a log
  line or into an error message.
- Any way to make a guard fail *closed on the wrong input* — a denial of
  service against legitimate help-desk traffic.

A missed detection is a real finding even when the input looks contrived: the
patterns are a published, finite list, and an attacker reads them too.

## Out of scope

These are known and documented limits, not defects. They are listed in the
README, under
[Operational Limits](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/README.md#operational-limits):

- **The core logs the incoming message before any plugin hook runs.** A message
  refused *because it contained personal data* is still in the container logs,
  and no plugin can prevent that. Log retention is a deployment decision.
- **The local classifiers fail open.** If a model cannot be loaded, the guard
  does not block; the deterministic checks still run.
- **A Hugging Face token entered in the admin panel is stored in plain text in
  `settings.json`.** Pass it through the `HF_TOKEN` environment variable
  instead. That the weaker path exists at all is tracked as a known issue.
- **The plugin verifies no property of the answer's content** beyond personal
  data: not evidence sufficiency, not groundedness, not source consistency, not
  the language of the reply.
- Vulnerabilities in Cheshire Cat AI itself, or in `transformers`, `torch`,
  `phonenumberslite` or a downloaded model. Report those to their own projects.

## Model weights

This plugin distributes no model weights. Every classifier model is downloaded
at runtime from Hugging Face by whoever installs and configures the plugin,
under the licence they accept directly with its publisher. A concern about a
model's own behaviour or licence belongs with that publisher; a concern about
how this plugin loads, caches or trusts it belongs here.
