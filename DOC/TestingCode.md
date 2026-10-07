# Testing

Guardrails fail silently: when a control stops working the chatbot does not raise an error, it just keeps answering unguarded. Tests are how that stays visible.

## Code layout

| File | Contents | Needs the core? |
| --- | --- | --- |
| `checks.py` | All decision logic: thresholds, verdicts, rules. Imports nothing from `cat` | No |
| `settings.py` | The settings model the admin form is built from, and the shipped defaults | Yes |
| `rag_guardrails.py` | The hooks only: read from the Cat, delegate to `checks`, write back | Yes |
| `.tests/unit/` | Pure logic and shipped metadata. Plain `pytest`, no Cheshire Cat at all | No |
| `.tests/integration/` | Hook wiring and configuration, against a fake `cat` object | Yes |

The test folders are the classification: what goes in `.tests/unit/` must import nothing from `cat`, and a file that breaks that rule fails loudly instead of being silently skipped. Everything under `.tests/unit/` therefore runs anywhere, which is what makes the fast local loop possible.

**The whole suite now runs in both environments with no skips**, and that took
removing something rather than adding it. `tests/unit/test_git_hooks.py` executed
the real `check-staged-secrets.sh` in disposable repositories, so it needed Bash
and Git; the Cheshire Cat container has Bash but no Git, which made eleven tests
fail there for a reason that had nothing to do with this repository. Installing
Git in the image was tried and rejected: the plugin folders live inside the
build context, so every plugin edit invalidates `COPY ./cat` and the rebuild
reinstalls the dependency stack of every plugin, `torch` included — fifteen
minutes for a package of a few megabytes.

**What that removal cost is recorded as an accepted decision in
`DEV/AGENTS/ISSUES_RESOLVED.md`**: nothing verifies the staged-secret scanner's
regular expressions any more. This remains a real loss rather than a cleanup;
the scanner is retained as a preventive control, but its patterns have no
dedicated regression coverage.

`.tests/integration/` needs the core only because the module under test imports `cat.log` and `cat.mad_hatter.decorators` at import time, not because a Cat must be running. Those tests never contact a live instance: the container is used as an interpreter, not as a server. Automated tests against a running instance do not exist yet; see `What is not automated` below.

**Why the folder is hidden.** The Cat imports every `.py` it finds in the plugin folder, recursively — `glob("**/*.py", recursive=True)` in `cat/mad_hatter/plugin.py`, with no exclusions — except what sits in a hidden folder, which `glob` does not enter. The tests used to live in a visible `tests/`, so the Cat imported all of them on every activation, pytest included. Six of them put the plugin folder first on `sys.path`, which inside the Cat process made a bare `import settings` written by *another* plugin resolve to this plugin's `settings.py`: the mechanism that once broke a neighbouring plugin on every activation. Since 2026-10-07 they live in `.tests/`, together with the test runner, and the packaging script lives in `.tools/`. The Cat imports exactly six files: the runtime modules.

`.tests/unit/test_repository_layout.py` keeps it so. It starts from the files that register something with the Cat — hooks, plugin overrides, tools, forms, endpoints — follows their imports, and fails on any `.py` the Cat would import that no plugin code needs, with no list of exceptions. It also checks that `run-tests.py` and `package-plugin.py` stay in their hidden folders, that `pytest.ini` points at `.tests`, that `pytest .` still collects the hidden folder, and that the pre-commit hook fails rather than passes when the folder is missing. It replaces `test_importability.py`, which only kept the imports from raising and left the cause in place.

Two things to know before adding files. A new test goes under `.tests/` and a new development script under `.tools/`, never in a visible folder. And pytest skips folders that start with a dot by default: `pytest.ini` therefore overrides `norecursedirs` without the `.*` pattern, and without that override `pytest .` collects nothing. Some tools, ripgrep among them, also skip hidden folders unless asked. `pytest.ini` is deliberately not a Python file, or it would be imported too.

The `@hook` decorator turns functions into non-callable `CatHook` objects. Tests reach the real function through `.function`.

## Environment setup

Two options, depending on which tests you want to run.

Local interpreter, `.tests/unit` only:

```bash
python -m pip install pytest phonenumberslite
```

`phonenumberslite` is there because `checks.py` imports it at module level: the personal-data guard validates phone numbers against a numbering plan rather than matching a shape. Without it `.tests/unit` fails at import, loudly, which is the wanted outcome: a guard whose behaviour depends on what happens to be installed is worse than one that refuses to start.

It is not the plugin's only runtime dependency — `requirements.txt` also declares `transformers` and `torch` for the optional local classifiers, and the core installs all three on activation. **They are deliberately absent from the command above**, and that is not an oversight: both `prompt_injection_classifier.py` and `offensive_input_classifier.py` import `transformers` lazily, inside `_get_pipeline()`, so nothing under `.tests/unit` touches it — the classifier tests exercise the decision logic around a stubbed pipeline. Adding `torch` to a local install would cost gigabytes and buy nothing. If a future test needs the real pipeline it belongs in `.tests/integration/`, where the container already has it.

Container, whole suite:

```bash
docker compose up -d
```

Add `--build` only after changing the image or the core dependencies: it is not needed to run the plugin or its tests, and it is considerably slower.

## Running the tests

The single source of truth is `.tests/run-tests.py`. It returns pytest's own exit code, and works from any directory.

Direct Python entrypoint:

```bash
python .tests/run-tests.py                 # unit + integration, in the Cheshire Cat container
python .tests/run-tests.py --unit          # (-u) pure logic, local interpreter, no Docker
python .tests/run-tests.py --integration   # (-i) hook adapters, in the container
python .tests/run-tests.py --detailed      # (-d) list every test name; combines with the others
```

`--unit` and `--integration` are mutually exclusive; `--detailed` combines with either, or with neither.

Because the exit code is pytest's own, the script can be reused from a git hook or from CI. If a prerequisite is missing, no interpreter with `pytest`, container not running, `compose.yml` not where expected, it says which command fixes it instead of failing obscurely.

**CI runs the unit tests only.** `.github/workflows/tests.yml` calls `python .tests/run-tests.py --unit` on every push to `main` and on every pull request, against Python 3.10, 3.11 and 3.12. It builds no container on purpose: `.tests/integration` stays a runner job before pushing, and a workflow that built the image would pay for the whole dependency stack, `torch` included, on every commit.

The `pre-commit` hook runs `.tests/unit` too, and nothing else: a commit must not depend on Docker being up, or the hook would either block legitimate commits or skip in silence. `.tests/integration` is for the runners, before pushing.

**Nothing tests the staged-secret hook any more.** Its regression gate against a
malformed pattern silently disabling part of the scan was removed on 2026-09-08
together with its Git dependency. This is an accepted coverage gap recorded in
`DEV/AGENTS/ISSUES_RESOLVED.md`.

Two limits of that gate are worth knowing. It runs `pytest` against the files on disk, not against the staged snapshot, so with unstaged changes in the working tree what passes is not exactly what is being committed. And if no interpreter with `pytest` is available it warns and lets the commit through, on the grounds that blocking for a missing development tool teaches `--no-verify`, which would also disable the secret scan.

The Python runner handles both Compose v2 and the standalone `docker-compose` binary.

Calling `pytest` directly works too. Locally:

```bash
python -m pytest .tests/unit
```

In the container:

```bash
docker compose exec -w /app/cat/plugins/rag-guardrails cheshire-cat-core python -m pytest
```

From Git Bash on Windows that same direct `docker compose exec -w ...` command fails with `Cwd must be an absolute path`, because the shell rewrites the `-w` path. `.tests/run-tests.py` handles that case automatically.

No `PYTHONPATH` is needed: `pytest.ini` declares `pythonpath = . /app`, where `.` makes the plugin modules importable and `/app` makes the core importable inside the container. A path that does not exist is ignored, so the same file works on a developer machine. Without that second entry `.tests/integration/` is skipped rather than failed, which reads as a success.

## What is not automated

Verification against a real instance is currently manual: activate the plugin, send messages through `POST /message`, and read `docker compose logs -f cheshire-cat-core` to confirm which code path ran. A correct-looking answer does not prove it came from this plugin; the log lines do.

This tier matters because it catches what the other two cannot. The interaction with the `Rate Limiter` plugin is the case in point: its checks used to intercept messages before this plugin ever saw them, and nothing in the code of either plugin showed it. The hook priority now settles who answers, and an integration test guards the priority — it lives in `.tests/integration/test_hooks.py`, because reading a hook's priority means importing the module that registers it, and that imports `cat` — but the ordering itself is only ever confirmed on a running instance.

The same tier is where another plugin's side effects show up. Above its own `max_prompt_length`, Rate Limiter still records an infraction and suspends the user for 5, 15 or 60 minutes, silently blocking their next legitimate messages, even though the reply delivered is this plugin's. No test can see that either.

### Which log line proves which path handled the turn

A correct-looking answer does not say where it came from. This table is what makes
a live session conclusive, and it is the reference for every manual check below.

| What handled the turn | What the log shows | Level |
| --- | --- | --- |
| The plugin was activated | `plugin activated, guardrails registered: fast_reply(priority=-1) …, before_cat_sends_message …` | `INFO` |
| An input guard refused | `input blocked, stage='input', category=…, verdict=…` followed by `no retrieval, no generation, nothing stored in memory` | `INFO` |
| The output guard replaced the answer | `output blocked, stage='output', category='privacy', verdict='output_personal_data'` followed by `generated reply replaced before delivery` | `INFO` |
| Everything passed, normal answer | `input allowed` **and** `output allowed`, each naming the checks that covered its stage | `INFO` |
| The answer was delivered with the output stage switched off | `output allowed, stage='output', checks=none` | `INFO` |
| A classifier could not run, message let through | `classifier unavailable (…), continuing without blocking` — **once**, not per message | `WARNING` |
| A loaded classifier failed on one message, which was let through | `classifier failed on one message (…); … the classifier stays active` — once per model and error type | `WARNING` |
| Another plugin refused it | `input allowed …, reply=another_plugin`: the deterministic checks passed, the classifiers were skipped, and the reply it received went out untouched | `INFO` |
| The configuration changed | `guards active: …`, once per change, `WARNING` instead of `INFO` when a stage that ships enabled has been switched off | `INFO`/`WARNING` |

Two readings of this table are worth stating, because they are what makes it
useful rather than decorative.

**Every turn has an operational trace at `INFO`, at each stage it reached.**
`input allowed` proves the plugin handled a passing message and `input blocked`
that a guard stopped it; `output allowed` and `output blocked` say the same for
the generated answer. A turn refused on `fast_reply` therefore has an `input`
line and no `output` line at all, which is how an early refusal is told apart
from an answer that passed the output stage. The separate `guards active`
announcement records the configuration on the first turn and whenever it changes,
and `plugin activated` proves the hooks were registered in the first place.

**A model-produced fallback leaves no dedicated output trace here.** When the answer is the
insufficiency message the prompt asks for — «la risposta non è reperibile nei
contenuti disponibili» — no plugin line is written, because no plugin was
involved in choosing that output: the model obeyed an instruction. Its preceding
`input allowed` line is indistinguishable from the one before a normal answer.
The logs therefore do not measure how often the recall comes back empty.

### Manual check: the tone guard

The service owner confirmed this check through the admin panel on 2026-09-22;
the result is recorded in `DOC/ReleaseReview.md`. Its decision rule is also
covered by unit tests built on the scores the real model produced. Keep the
procedure below as the reproducible check after a deployment change.

The guard ships **switched off**, so every automated test enables it explicitly;
the procedure remains the only check of the administrator-facing path after a
deployment change.

Suggested procedure:

1. Enable `Tone guard: block offensive incoming messages`
   in the panel and save.
2. Confirm the `guards active` line moves from `tone(disabled)` to
   `tone(classifier IMSyPP/hate_speech_multilingual@0.60)`. That line proves the
   setting reached the guard.
3. Send an insult. Expect the static reply, and one `INFO` line with
   `category='tone'`, `verdict='offensive_input'`, a `label`, a `score` above the
   threshold, and **no trace of the message text**.
4. Send `Questa maledetta VPN non funziona mai`. Expect a normal answer: an
   exasperated user must not be refused. This is the false-positive case the
   threshold was chosen for.
5. Send a legitimate help-desk question and confirm the only `INFO` lines are
   `input allowed` with `offensive_input` among its checks, `output allowed`, and
   the classifier cache-hit line.

Without optional classifier preload, the first message after enabling also pays
the model load, so expect it to be slow — see `DOC/ToneGuards.md`.

### Manual check for the current output privacy guard

One concrete live check is now worth keeping in the checklist, because it
exercises the new `before_cat_sends_message` path rather than the input-side
`fast_reply` path.

Suggested procedure:

1. Ensure the relevant output detector is enabled in the admin panel, for example `Output privacy guard: block e-mail`.
2. If you want to test the output path in isolation, disable the corresponding input detector first, for example `Input privacy guard: block e-mail`, so the turn is not stopped on `fast_reply`.
3. Ask a benign help-desk question that is likely to make the model echo personal data in the answer, for example by explicitly requesting a reply that repeats an e-mail address or a phone number.
4. Confirm that the user does **not** receive the generated answer containing the data, but the configured static output-side fallback instead.
5. Confirm in `docker compose logs -f cheshire-cat-core` that the block line is the output-side one:

```text
[rag-guardrails] output blocked, stage='output', category='privacy', verdict='output_personal_data', ...
```

6. Repeat once with the relevant output detector disabled and confirm that the reply is no longer replaced by this plugin.

This check matters because only a running instance proves that the hook is
actually intercepting the final outgoing message object at the right point in
the Cheshire Cat flow.
