# RAG Guardrails


`RAG Guardrails` is a Cheshire Cat AI plugin for website-based help-desk chatbots.

It adds deterministic and configurable guardrails around the normal RAG flow so that risky or invalid requests can be stopped early, before they reach retrieval or generation.

The plugin ships **no model weights**. Its prompt-injection guard can be configured to run `meta-llama/Llama-Prompt-Guard-2-86M`, which is downloaded at runtime from Hugging Face by whoever installs the plugin, under Meta's own terms. The attribution that licence requires is in [License and Legal Notes](#license-and-legal-notes); the licence of every model this plugin can run is in [DOC/Licenses.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Licenses.md).

## Production Status

The plugin is deployed in production. The controls currently enforced are listed
in the summary table below.

Prompt instructions, retrieval tuning and evidence policy remain deployment
configuration rather than controls implemented by this plugin. See
[Operational Limits](#operational-limits) before installing it.

The naming of guards is documented in [DOC/GuardTaxonomy.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/GuardTaxonomy.md). The plugin keeps three axes separate:

- `stage`: where the control acts
- `category`: what kind of risk it addresses
- `verdict`: which specific control fired

## Guard Summary

Quick reference of the guards currently implemented in the plugin. This is the
fastest way to see what the plugin does today.

| Stage | Category | Verdict | Hook | Type | What it does |
| --- | --- | --- | --- | --- | --- |
| `input` | `limits` | `message_length` | `fast_reply` | Python | Stops over-long user messages before retrieval and generation. |
| `input` | `privacy` | `personal_data` | `fast_reply` | Regex + checksum + library | Stops user messages containing personal data such as e-mail addresses, phone numbers, codice fiscale or IBAN. |
| `input` | `security` | `prompt_injection` | `fast_reply` | Regex + local classifier | Stops explicit prompt-injection attempts with built-in bilingual patterns and, optionally, with a local classifier. |
| `input` | `tone` | `offensive_input` | `fast_reply` | Local classifier | Stops offensive or violent incoming messages with a local multilingual classifier when this optional guard is enabled. |
| `output` | `privacy` | `output_personal_data` | `before_cat_sends_message` | Regex + checksum + library | Replaces a generated reply before delivery if it contains personal data. |

## Architecture

The plugin is split into a small number of focused parts:

- `checks.py`: pure decision logic, with no imports from `cat`
- `rag_guardrails.py`: Cheshire Cat hooks, settings loading and log wiring
- `classifier_runtime.py`: shared runtime support for local classifiers, including pipeline cache and negative cache on failed loads
- `prompt_injection_classifier.py`: model-specific wrapper for the prompt-injection classifier
- `offensive_input_classifier.py`: model-specific wrapper for the offensive-input classifier
- `settings.py`: admin settings model and shipped defaults

This keeps the rule logic testable on its own, while the hook layer stays thin
and focused on the Cheshire Cat integration.

Project-specific architecture notes and development guidance live under
`DEV/AGENTS/`.

## Requirements

- Cheshire Cat AI `1.9.2` on the `1.x` line
- A website chatbot integration that sends user messages to Cheshire Cat AI

The plugin is self-contained: it requires no companion plugin, and every
third-party dependency it needs is declared in its own `requirements.txt`,
which Cheshire Cat AI installs on activation. It currently declares
`phonenumberslite` for phone-number validation and `transformers` plus `torch`
for the optional local classifiers.

Three things about that file are deliberate, and the first one is a trap worth
knowing before editing it:

- **It carries no comments and no blank lines.** Cheshire Cat AI does not hand
  the file to pip: it calls `packaging.Requirement()` on every line, inside a
  `try` that abandons the whole loop on the first failure. A comment is valid
  for pip and fatal here — it makes the core install *no* dependency at all,
  logging one error while activation continues, so the plugin then works on a
  machine that already has the packages and fails at import on a clean one.
- **`phonenumberslite`, not `phonenumbers`.** The full package carries
  geocoding, carrier and timezone data for 20.9 MB installed, none of which is
  used here; the lite build is 2.2 MB and provides the parsing and validation
  the personal-data guard needs.
- **No `==` pins.** The core compares only the package name against what is
  already installed and ignores the version, so an exact pin is skipped in
  silence when another plugin installed a different one — and breaks that
  plugin if this one is activated first.

Sharing an installation with other plugins is supported: when one of its own checks does not trigger, a reply another plugin has already produced is passed through untouched.

## Installation

1. Copy the plugin folder into the Cheshire Cat plugins directory.
2. Start or restart Cheshire Cat AI.
3. Open the Cheshire Cat admin panel.
4. Enable `RAG Guardrails` from the plugins list.

## Configuration

After activation, open:

`Plugins -> RAG Guardrails -> Settings`

Settings are named after the guard family they belong to, so related options
read together: `Limits guard:`, `Input privacy guard:`, `Output privacy guard:`,
`Security guard:`, `Tone guard:`. Every field carries its own description in the
panel, so this document lists only the ones that need a decision rather than a
reading.

**Two guard toggles ship switched off.** `Security guard: block prompt injection
with local classifier`, so a first installation does not depend on a model
download or on access to a gated repository; and `Tone guard: block offensive
incoming messages`, because it loads a second model into memory, adds one
inference to every message that reaches it, and its precision on real help-desk
traffic still has to be measured. All other guard toggles ship enabled.

**The shipped Help Desk address is a placeholder** and is shown to users
verbatim on any installation where nobody opens the panel. Replace it.

**`Privacy guards: allowed contacts` ships empty**, and is the one setting shared
by the input and output privacy guards rather than duplicated per stage.
Contacts listed there — one per line, e-mail addresses and phone numbers
together — are not treated as personal data on either stage, which is what lets
the assistant give out the Help Desk number without the answer being replaced by
the fallback. The Help Desk address is always exempt and does not need to be
listed. Every entry is a deliberate hole in the privacy guards, so list only
genuinely published contacts; the details are in
[DOC/OutputGuards.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/OutputGuards.md).

**If the `Rate Limiter` plugin is also installed**, its content checks overlap
with these and its suspensions are outside this plugin's reach. See
[DOC/RateLimiter.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/RateLimiter.md).

## Operational Limits

- The plugin enforces only the controls in [Guard Summary](#guard-summary). It
  does not verify evidence sufficiency, groundedness, source consistency or the
  language of a generated answer.
- Local classifiers are disabled by default and fail open if a model cannot be
  loaded. Enabling one requires enough memory and may make the first matching
  request wait while the model loads.
- Classifier pipelines stay in memory until the plugin reloads. Concurrent cold
  requests can temporarily load the same model more than once, and changing a
  configured model does not immediately release the previous one.
- A Hugging Face token entered in the admin panel is stored in plain text in
  `settings.json`. Prefer the `HF_TOKEN` environment variable and exclude that
  file from backups and support bundles.
- Input guards run before retrieval and chatbot memory storage, but Cheshire Cat
  AI can log the incoming message before plugin hooks run. Configure core log
  access and retention accordingly.

## Guard Order

The order of the input-side checks is part of the behavior, not an
implementation detail. The current order is:

1. `message_length`
2. `prompt_injection` patterns
3. `personal_data`
4. `prompt_injection` classifier
5. `offensive_input`

This means:

- cheap deterministic checks run before local classifiers
- a message containing personal data is stopped before classifier-based checks
- a message that is both offensive and a prompt-injection attempt is reported as
  `prompt_injection`, because that guard runs first and gives the more
  pertinent correction

### Logging

For a detailed reference of the log lines emitted by the plugin — activation,
active-guard announcements, allowed and blocked stage lines at `INFO`, and
logging boundaries —
see [DOC/Logging.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Logging.md).

## Testing

Run unit tests only:

```bash
python run-tests.py --unit
```

Run the full suite:

```bash
python run-tests.py
```

These are the only two commands needed to run the tests. Everything else about
testing — the test layout, which tests need the Cheshire Cat container, how the
runner behaves, and what is verified manually — is in
[DOC/TestingCode.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/TestingCode.md).

## Related Docs

- [DOC/ClassifierLabels.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/ClassifierLabels.md): how classifier labels are mapped, verified, and used in decisions
- [DOC/GuardTaxonomy.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/GuardTaxonomy.md): taxonomy of `stage`, `category` and `verdict`
- [DOC/ClassifierCache.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/ClassifierCache.md): how the local-classifier cache and negative cache work
- [DOC/SecurityGuards.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/SecurityGuards.md): prompt-injection guard details
- [DOC/ToneGuards.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/ToneGuards.md): offensive-input guard details
- [DOC/OutputGuards.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/OutputGuards.md): output-side privacy guard details
- [DOC/Logging.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Logging.md): detailed log reference
- [DOC/TestingCode.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/TestingCode.md): test layout, runners and manual checks
- [DOC/Licenses.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Licenses.md): the licence of the plugin and of every supported model
- [DOC/RateLimiter.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/RateLimiter.md): sharing an installation with the Rate Limiter plugin

## Packaging

Build the distributable zip with:

```bash
python package-plugin.py
```

When a new file must be shipped with the plugin, update `package-plugin.py` so the release package stays explicit and complete.

## License and Legal Notes

The code in this repository is released under **GNU General Public License v3.0
only**. See `LICENSE`.

The plugin distributes **no model weights**. Every classifier model is downloaded
at runtime, from Hugging Face, by the person who installs and configures the
plugin, and each one carries its own licence which that person accepts directly
with its publisher. That separation is deliberate and load-bearing: the GPL
governs this code and cannot govern weights it never ships, and some of the
supported models carry use restrictions that GPLv3 section 10 forbids adding to
conveyed material. **Never add model weights to the release package.**

### Built with Llama

The prompt-injection guard can be configured to run Meta's Llama Prompt Guard 2.
When it is, the following notice applies:

> **Llama is licensed under the Llama Community License, Copyright © Meta Platforms, Inc. All Rights Reserved.**

Both Meta models are **gated**: access is granted manually by Meta after the
request is accepted, so using them requires accepting Meta's terms on the model
page and authenticating at runtime.

### The rest

The licence of each of the six supported models, with the date it was verified,
which two are not free software, and the runtime dependencies —
[DOC/Licenses.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Licenses.md).

Check that table again before a release: a publisher can change a licence.
