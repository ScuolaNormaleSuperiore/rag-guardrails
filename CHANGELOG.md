# Changelog

Notable changes to `RAG Guardrails`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]
### Security
- A message longer than 512 tokens could pass the offensive-input guard
  unclassified. The classifiers were handed up to 1,024 tokens, past the window
  of the tone models, which raised on the longer input; the guard then failed
  open. Reachable only with the tone guard enabled and the length limit raised
  above about 2,000 characters.
### Fixed
- An error raised by a loaded classifier on one message was announced as
  `classifier unavailable … Not repeated until the plugin reloads`, which was
  false: the next message was classified normally. It now has its own warning,
  written once per model and error type. The old line also included the
  exception text, which can quote the message; the new one names only the
  exception class.
### Changed
- Classifier input is bounded at 512 tokens, the window of every supported
  model, lowered further when a model declares a shorter one. On a long
  message the prompt-injection classifier now costs about 1.4 s instead of up
  to 5.6 s. Text past the bound is checked only by the deterministic guards.

## [1.0.4] - 2026-10-07
### Fixed
- Cheshire Cat no longer imports the test suite on every activation. The tests
  moved from `tests/` to the hidden `.tests/`, which the core's recursive
  `glob` does not enter. Six test files put the plugin folder first on
  `sys.path`, so inside the Cat process a bare `import settings` written by
  another plugin could resolve to this plugin's `settings.py`.
- The two development scripts are no longer imported either: the test runner
  moved to `.tests/run-tests.py` and the packaging script to
  `.tools/package-plugin.py`. The Cat now imports 6 files instead of 17, all of
  them runtime modules.
### Changed
- The development commands are now `python .tests/run-tests.py` and
  `python .tools/package-plugin.py`.
- `test_importability.py` is replaced by `test_repository_layout.py`, which
  fails on any file the Cat would import that no plugin code needs.
- The pre-commit hook now blocks the commit when the unit-test folder is
  missing, instead of reporting nothing to run.

## [1.0.3] - 2026-09-22
- AI Code review
- Bug-fixing: Fixed bugs and security issues
- Updated the documentation

## [1.0.2] - 2026-09-21
- Bug-fixing: Fixed bugs and security issues
- Updated the documentation

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
