# Changelog

Notable changes to `RAG Guardrails`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).




## [1.1.0] - 2026-09-16

**This release changes how the plugin is installed.** Read the migration note
below before upgrading an installation where a classifier guard is enabled.

### Changed
- `torch` and `transformers` are no longer installed automatically.
  `requirements.txt` now declares `phonenumberslite` alone, which is the only
  dependency the plugin imports unconditionally.

  The reason is what the core does with that file. It installs it on every
  activation and offers no way to choose an index, so `torch>=2` resolved to a
  CUDA build: roughly 3 GB of NVIDIA wheels on a pod with no GPU, on every
  start. It also replaced `huggingface-hub` and `tokenizers`, which the core
  itself uses for its embedders. Both classifier guards ship **disabled**, so a
  default installation paid all of that for a feature it never used.
- The guard summary now reports what can actually run, not what was configured.
  An enabled classifier on an image without the optional stack is announced as
  `classifier(stack not installed: missing=…)` instead of being reported as
  active coverage, and `tone` is listed as uncovered when its classifier cannot
  start. `security` stays covered by its deterministic patterns.

### Added
- `requirements-classifiers-torch-cpu.txt` and `requirements-classifiers.txt`:
  the optional stack, installed into the image rather than by the core. Two
  files and two pip invocations, because `--index-url` applies to a whole
  invocation, and Torch has to come from the PyTorch CPU index before
  Transformers resolves anything from PyPI. See README.md, section *The optional
  classifier stack*.
- One line at activation saying whether the optional stack is installed:
  `INFO` when no classifier is enabled, `WARNING` with install instructions when
  one is and it cannot run. A complete stack is reported too, with the versions
  actually installed — the core matches requirements by package name and ignores
  the version, so a version bound in the optional file cannot be enforced, and
  this line is what makes a breach of it visible.

### Fixed
- A missing `transformers` no longer retries on every message. The import sat
  outside the block that records a failed load, so a `ModuleNotFoundError`
  bypassed the negative cache: every message repeated the import, took the model
  load lock and paid its five-second wait, for a package that cannot appear
  without restarting the process. It is now remembered once, like any other
  failed load.

### Migration
Nothing to do **if no classifier guard is enabled**, which is the shipped
configuration. The deterministic guards — length, personal data on both stages,
and the prompt-injection patterns — do not use the optional stack and behave
identically without it.

**If a classifier guard is enabled**, the order matters and there is no window
in which the updated plugin runs without the stack:

1. build a new image with the optional stack installed, running the two
   commands in README.md during the build, not at pod start;
2. verify the image with `pip check` and confirm `torch.version.cuda is None`;
3. deploy the image;
4. only then update the plugin to 1.1.0;
5. confirm the `guards active` line names the model and threshold rather than
   `stack not installed`.

If the new image fails, keep the previous one and do not update the plugin.

Note that updating the plugin does **not** shrink an existing image: the core
installs dependencies and never removes them. The saving arrives with the first
image built from scratch.


## [1.0.1] - 2026-09-10
- Bug-fixing: Fixed bugs and security issues
- Updated the documentation


## [1.0.0] - 2026-09-08
First public release. The plugin was already running in production before this
tag: the jump from `0.0.5` marks the move from an internal deployment to a
published plugin, not a change in what the guards do.
### Guards in this release
| Stage | Verdict | Default |
| --- | --- | --- |
| `input` | `message_length` | on |
| `input` | `personal_data` — e-mail, phone, codice fiscale, IBAN | on |
| `input` | `prompt_injection` | patterns on, classifier off |
| `input` | `offensive_input` | off |
| `output` | `output_personal_data` | on |
### Added
- `min_cat_version` in `plugin.json`, declaring the Cheshire Cat AI `1.9.2`
  baseline that until now lived only in the README.
- This changelog, and `SECURITY.md` with a private channel for reporting a
  guard bypass.
- Continuous integration: the unit suite runs on every push and pull
  request, on Python 3.10 through 3.12.
- A test asserting that every file under `tests/` imports on its own. The
  core imports a plugin's files recursively, `tests/` included, so a test
  file that cannot be imported makes it log `Unable to load plugin` for a
  plugin that is running normally.
### Changed
- README restructured for a first-time reader: a *What a User Sees* section
  with the actual replies, default state moved into the guard table, and the
  configuration steps turned into a *Before Going Live* checklist.
- `plugin.json` description now says "deterministic checks and optional local
  classifiers" instead of "deterministic": two of the five guards are
  classifiers, and calling them deterministic was wrong.

## [0.0.5] and earlier

Internal versions, developed and deployed at Scuola Normale Superiore and never
published. `REL-0.0.3` is the only tag from that period, and the git history is
the only record of what changed between them.
