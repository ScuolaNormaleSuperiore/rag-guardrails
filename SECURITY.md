# Security Policy

A defect in this plugin is a control that stopped working without saying so.
Report it privately, so a fix exists before the bypass does.

## Supported versions

| Version | Supported |
| --- | --- |
| `1.0.x` | yes |
| earlier | no |

## How to report

**Do not open a public issue.** A working guard bypass posted publicly is an
exploit against every installation that cannot upgrade today.

1. GitHub private vulnerability reporting: repository page, *Security* →
   *Report a vulnerability*.
2. Otherwise write to the contact address published at <https://ict.sns.it>,
   subject `rag-guardrails security`.

Include what you can: the guard involved (`message_length`, `personal_data`,
`prompt_injection`, `offensive_input`, `output_personal_data`), the exact input
and the reply you got instead of a block, the plugin and Cheshire Cat AI
versions, whether the local classifiers were enabled, and any relevant log line
— **redacted**, because a Hugging Face token can surface inside a third-party
exception text.

We will acknowledge the report and say whether we could reproduce it.

## In scope

- Input that passes a guard which should have stopped it.
- A generated reply carrying personal data that the output guard delivers anyway.
- Any path that writes a token, or another secret, into a log line or an error
  message.
- Anything that makes a guard refuse legitimate help-desk traffic.

A missed detection counts even when the input looks contrived: the patterns are
a published, finite list, and an attacker reads them too.

## Out of scope

Known limits rather than defects, documented in the README under
[Operational limits](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/README.md#operational-limits):

- Cheshire Cat AI logs every incoming message before any plugin hook runs, so a
  message refused *for containing personal data* is still in the container logs.
  No plugin can prevent that; log retention is a deployment decision.
- The local classifiers fail open: a model that cannot load does not block, and
  the deterministic guards keep running.
- A Hugging Face token is stored in clear text in `settings.json`, which is its
  only supported location. Restrict access to that file.
- The plugin verifies no other property of an answer: not evidence sufficiency,
  not groundedness, not source consistency, not the language of the reply.
- Defects in Cheshire Cat AI, `transformers`, `torch`, `phonenumberslite` or in a
  downloaded model. This plugin distributes no model weights — every model is
  downloaded at runtime by whoever configures it — so report those to their own
  projects. How this plugin loads, caches or trusts a model does belong here.
