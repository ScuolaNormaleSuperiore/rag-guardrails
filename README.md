# RAG Guardrails

`RAG Guardrails` is a Cheshire Cat AI plugin for website help-desk chatbots.
It checks incoming messages before retrieval, generation and memory storage,
and checks generated answers before delivery.

The guards support Italian and English and cover structured personal data,
message length, prompt injection and optional offensive-language detection.

Read [Requirements](#requirements), [Before going live](#before-going-live) and
[Operational limits](#operational-limits) before installing.

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

1. Download the release ZIP and extract its `rag-guardrails` folder into the
   Cheshire Cat AI plugins directory. For a source checkout, copy that folder
   directly instead.
2. Start or restart Cheshire Cat AI.
3. Open the admin panel and enable `RAG Guardrails` from the plugins list.

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

- **Coverage:** it does not verify evidence sufficiency, groundedness, source
  consistency or the language of a generated answer.
- **Classifiers:** they fail open if a model cannot run. The first use may be
  slow; concurrent requests wait up to five seconds and then fail open. Loaded
  or failed models remain cached until the plugin reloads.
- **Data:** the Hugging Face token is stored in plain text in `settings.json`;
  protect that file and exclude it from support bundles. Cheshire Cat AI logs
  incoming messages before plugin hooks run, so restrict log access and set a
  retention policy.

## Reporting a security problem

Do not open a public issue for a guard bypass. Follow the private reporting
process in
[SECURITY.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/SECURITY.md).

## Documentation

- [Guard taxonomy](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/GuardTaxonomy.md)
- [Prompt injection and classifier operations](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/SecurityGuards.md)
- [Output privacy](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/OutputGuards.md)
- [Testing and release review](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/TestingCode.md)

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

### Built with Llama (when enabled)

`meta-llama/Llama-Prompt-Guard-2-86M` is an optional model for the
prompt-injection classifier. The plugin works without enabling any classifier.

The release ZIP contains no Llama weights or other Llama Materials. If a
deployment enables this model, its operator downloads and uses Llama Materials
under Meta's terms; the following notice applies to that deployment:

> **Llama is licensed under the Llama Community License, Copyright © Meta Platforms, Inc. All Rights Reserved.**

The Meta models are gated. Before enabling one, accept its terms on Hugging
Face, obtain access, generate a read token there, and enter it in
`Security guard: Hugging Face token` in the plugin settings. Public models do
not need a token.

Model licences, the full conditional attribution rationale, and their verification dates are listed in
[DOC/Licenses.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/Licenses.md).

Check them again before each release because publishers can change their terms.
