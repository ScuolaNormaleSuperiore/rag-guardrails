# Licences

The licence position of `rag-guardrails` and of every model it can run.

`README.md` carries the short version: the plugin is GPL-3.0-only, ships no
model weights, and explains the conditional Llama attribution. This document
holds the reasoning and per-model detail that need checking before a release.

## The plugin

The code in this repository is released under **GNU General Public License v3.0
only**. See `LICENSE`.

## Why the models are not part of it

This plugin distributes **no model weights**. The release package contains twelve
files — the six Python modules, `plugin.json`, `requirements.txt`, and the four
documents a release carries: `README.md`, `CHANGELOG.md`, `SECURITY.md` and
`LICENSE` — and nothing else. Every classifier model is downloaded at
runtime, from Hugging Face, by the person who installs and configures the
plugin, and each one carries its own licence which that person accepts directly
with its publisher.

That separation is what keeps the arrangement clean. The GPL governs this code;
it does not and cannot govern weights it never ships.

**Never add model weights to the release package.** Some of the models below are
distributed under licences that impose use restrictions, and GPLv3 section 10
forbids adding restrictions to conveyed material — bundling them would create a
genuine incompatibility where today there is none. This is a licensing boundary,
not a size optimisation, and the same warning is repeated in
`package-plugin.py`, above the file list.

## Built with Llama (when enabled)

The prompt-injection guard can be configured to run Meta's Llama Prompt Guard 2.
The plugin release contains no Llama Materials: it distributes no Meta weights,
tokenizer, inference code or Meta documentation. A plugin configured with a
Meta model downloads it at runtime under terms accepted by that deployment's
operator.

Section 1.b.i of the Llama 4 Community License requires a prominent `Built with
Llama` display when a party distributes or makes available Llama Materials, or a
product or service containing them. Therefore the requirement applies when a
deployment enables a Meta model, not to an installation that leaves the optional
classifier disabled and not to the plugin ZIP by itself. The README displays the
following notice conditionally for deployments that enable it:

> **Llama is licensed under the Llama Community License, Copyright © Meta Platforms, Inc. All Rights Reserved.**

This interpretation is a documented release position, not legal advice. The
service owner remains responsible for accepting Meta's terms and obtaining any
legal review required for its deployment.

The applicable version, read from the model card on 2026-08-06, is the
**Llama 4 Community License Agreement** (`license_name: llama4`). Both Meta
models are **gated**: access is granted manually by Meta after the request is
accepted, so using them requires accepting Meta's terms on the model page and
authenticating at runtime. See
[DOC/SecurityGuards.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/SecurityGuards.md)
for the operational steps.

## Licence of each supported model

Verified against the Hugging Face model cards on 2026-08-06. **Check them again
before a release**: a publisher can change a licence, and this table is a
snapshot rather than a promise.

| Model | Guard | Licence | Gated |
| --- | --- | --- | --- |
| `meta-llama/Llama-Prompt-Guard-2-86M` | prompt injection, **shipped default** | Llama 4 Community License | yes, manual approval |
| `meta-llama/Llama-Prompt-Guard-2-22M` | prompt injection | Llama 4 Community License | yes, manual approval |
| `deepset/deberta-v3-base-injection` | prompt injection | MIT | no |
| `IMSyPP/hate_speech_multilingual` | offensive input, **shipped default** | MIT | no |
| `patriciacarla/HS-multilingual-DNR` | offensive input | Apache-2.0 | no |
| `textdetox/bert-multilingual-toxicity-classifier` | offensive input | OpenRAIL++ | no |

Two entries deserve attention before you enable them:

- **The two Meta models are not free software.** The Llama Community License is
  not an open-source licence: it carries an acceptable-use policy, a
  monthly-active-users clause and naming requirements. Nothing about that
  conflicts with this plugin's GPLv3 as long as the weights stay out of the
  package, but an installation that enables them has accepted terms the GPL does
  not grant.
- **`textdetox/bert-multilingual-toxicity-classifier` is OpenRAIL++**, which
  permits redistribution but attaches behavioural use restrictions that must be
  passed on downstream. It is the only offensive-input model of the three that
  is not plainly permissive.

The two shipped defaults sit on opposite sides of this: the tone guard defaults
to an MIT model, the prompt-injection guard defaults to a gated Meta one. If a
deployment needs to avoid non-free licences entirely, both guards have a
permissive option — `deepset/deberta-v3-base-injection` (MIT) and the default
`IMSyPP/hate_speech_multilingual` (MIT) — selectable from the admin panel with
no code change.

## Runtime dependencies

Declared in `requirements.txt`, all GPL-compatible: `phonenumberslite`
(Apache-2.0), `transformers` (Apache-2.0), `torch` (BSD-3-Clause).
