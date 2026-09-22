# RAG Guardrails

`RAG Guardrails` is a Cheshire Cat AI plugin for website help-desk chatbots.
It checks incoming messages before retrieval, generation and memory storage,
and checks generated answers before delivery.

The guards support Italian and English. They cover structured personal data,
message length, prompt injection and optional offensive-language detection.
The plugin is deployed in production.

Prompt instructions, retrieval tuning and evidence policy remain deployment
configuration.

Read [Requirements](#requirements) and [Operational limits](#operational-limits) before installing.

## Guards

Each result has a `stage`, `category` and `verdict`. Their definitions are in
[DOC/GuardTaxonomy.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/GuardTaxonomy.md).

| Guard (`verdict`) | Stage | Default | Purpose |
| --- | --- | --- | --- |
| `message_length` | input | on | Blocks messages longer than the configured limit. |
| `personal_data` | input | on | Detects e-mail addresses, valid phone numbers, IBANs and ordinary checksum-valid Italian fiscal codes. |
| `prompt_injection` | input | patterns on, classifier off | Detects explicit attempts to alter instructions or expose internal information. |
| `offensive_input` | input | off | Uses a local multilingual classifier to detect offensive or violent input. |
| `output_personal_data` | output | on | Replaces an answer containing structured personal data before delivery. |

An input guard returns a configurable default reply with the Help Desk address
and stops retrieval, language-model generation and episodic-memory storage. The
output privacy guard replaces a generated reply containing detected personal
data before delivery.

The classifier options ship disabled. They require model downloads, add memory
and inference costs, and fail open if their model cannot be loaded.

## Requirements

| Component | Requirement |
| --- | --- |
| Cheshire Cat AI | `1.9.2`, on the `1.x` line |
| Host | A website chatbot integration that sends messages to Cheshire Cat AI |


Cheshire Cat AI installs missing packages from the plugin's `requirements.txt`
when the plugin is activated.

| Package | Used for | Imported when |
| --- | --- | --- |
| `phonenumberslite` | Phone-number validation without unused geocoding, carrier or timezone data | Plugin load |
| `transformers` | Local classifiers | A classifier guard is enabled |
| `torch` | Classifier inference | A classifier guard is enabled |

**Deployment recommendation.** Preinstall `torch` and `transformers` in the
Cheshire Cat AI Dockerfile. Without a GPU, use the CPU-only PyTorch build to
avoid unnecessary GPU libraries and reduce image size.

Packages already present in the same Python environment are not installed again
during plugin activation. Model weights are not included and are downloaded
only when an enabled classifier first loads its configured model.

## Installation

1. Copy the plugin folder into the Cheshire Cat AI plugins directory.
2. Start or restart Cheshire Cat AI.
3. Open the Cheshire Cat AI admin panel.
4. Enable `RAG Guardrails` from the plugins list.

## Before going live

Open `Plugins -> RAG Guardrails -> Settings`, then:

1. Replace the `helpdesk@example.org` placeholder.
2. Add only genuinely public contacts to `Privacy guards: allowed contacts`.
   These contacts are exempt from both input and output privacy checks.
3. Decide whether the two classifier guards justify their download, memory and
   inference costs.
4. For gated models, enter a read token in `Security guard: Hugging Face token`.
   This field is stored in plain text in `settings.json`.

The Help Desk address is always exempt and does not need to be added to the
allowed contacts list.

## Operational limits

- The plugin does not verify evidence sufficiency, groundedness, source
  consistency or the language of a generated answer.
- Classifiers fail open: if a model cannot run, that classifier does not block
  the message. Other enabled guards remain active.
- The first request that uses a classifier loads its model and may be slow.
- While one request loads a model, concurrent requests for the same model wait
  up to five seconds, then fail open.
- A failed model load is not retried until the plugin reloads.
- A loaded classifier pipeline stays in memory until the plugin reloads.
- Changing the configured model does not immediately release the previous model
  from memory.
- A token saved through the admin panel is stored in plain text. Exclude
  `settings.json` from backups and support bundles.
- Cheshire Cat AI 1.9.2 logs incoming messages before plugin hooks run. Restrict
  access to core logs and configure their retention accordingly.

## Guard order

Input checks run in this order:

1. `message_length`
2. `prompt_injection` patterns
3. `personal_data`
4. `prompt_injection` classifier
5. `offensive_input`

The first matching guard decides the reply. Deterministic checks run before the
classifiers, and prompt injection takes precedence over offensive input.

## Reporting a security problem

Do not open a public issue for a guard bypass. Follow the private reporting
process in
[SECURITY.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/SECURITY.md).

## Documentation

- Guard behaviour:
  [taxonomy](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/GuardTaxonomy.md),
  [prompt injection](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/SecurityGuards.md),
  [tone](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/ToneGuards.md),
  [output privacy](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/OutputGuards.md).
- Classifiers:
  [labels](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/ClassifierLabels.md),
  [cache](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/ClassifierCache.md),
  [licences](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Licenses.md).
- Operations:
  [logging](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Logging.md),
  [testing](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/TestingCode.md),
  [release review](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/ReleaseReview.md),
  [Rate Limiter compatibility](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/RateLimiter.md).

## Development

Pure decision logic lives in `checks.py` and imports nothing from `cat`.
`rag_guardrails.py` contains the hook adapters, `classifier_runtime.py` manages
classifier loading and caching, and `settings.py` defines the admin settings.

```bash
python run-tests.py --unit          # pure logic, local interpreter
python run-tests.py --integration   # hook adapters, Cheshire Cat container
python run-tests.py                 # both
python run-tests.py --detailed      # both, listing every test name
python package-plugin.py     # build the release zip
```

When a new file must ship with the plugin, add it to `package-plugin.py`.

## License and model terms

The plugin code is released under the **GNU General Public License v3.0 only**.
See [LICENSE](LICENSE).

The plugin distributes **no model weights**. Hugging Face is contacted only
when an optional classifier is enabled; its models are downloaded at runtime
under terms accepted directly by the person configuring the plugin. Never add
model weights to the release package.

### Optional Llama Prompt Guard model

`meta-llama/Llama-Prompt-Guard-2-86M` is an optional model for the
prompt-injection classifier. The plugin works without enabling any classifier.

When this model is selected, this notice applies:

> **Llama is licensed under the Llama Community License, Copyright © Meta Platforms, Inc. All Rights Reserved.**

The Meta models are gated. Before enabling one, accept its terms on Hugging
Face, obtain access, generate a read token there, and enter it in
`Security guard: Hugging Face token` in the plugin settings. Public models do
not need a token.

Model licences and their verification dates are listed in
[DOC/Licenses.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Licenses.md).

Check them again before each release because publishers can change their terms.
