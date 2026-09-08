# Changelog

Notable changes to `RAG Guardrails`. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and version numbers
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
