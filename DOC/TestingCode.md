# Testing

Guardrails fail silently: when a control stops working the chatbot does not raise an error, it just keeps answering unguarded. Tests are how that stays visible.

## Code layout

| File | Contents | Needs the core? |
| --- | --- | --- |
| `checks.py` | All decision logic: thresholds, verdicts, rules. Imports nothing from `cat` | No |
| `settings.py` | The settings model the admin form is built from, and the shipped defaults | Yes |
| `rag_guardrails.py` | The hooks only: read from the Cat, delegate to `checks`, write back | Yes |
| `tests/unit/` | Pure logic and shipped metadata. Plain `pytest`, no Cheshire Cat at all | No |
| `tests/integration/` | Hook wiring and configuration, against a fake `cat` object | Yes |

The test folders are the classification: what goes in `tests/unit/` must import nothing from `cat`, and a file that breaks that rule fails loudly instead of being silently skipped. Everything under `tests/unit/` therefore runs anywhere, which is what makes the fast local loop possible.

**The whole suite now runs in both environments with no skips**, and that took
removing something rather than adding it. `tests/unit/test_git_hooks.py` executed
the real `check-staged-secrets.sh` in disposable repositories, so it needed Bash
and Git; the Cheshire Cat container has Bash but no Git, which made eleven tests
fail there for a reason that had nothing to do with this repository. Installing
Git in the image was tried and rejected: the plugin folders live inside the
build context, so every plugin edit invalidates `COPY ./cat` and the rebuild
reinstalls the dependency stack of every plugin, `torch` included — fifteen
minutes for a package of a few megabytes.

**What that removal cost is written down in `DEV/AGENTS/ISSUES_TODO.md`**, because
it is a real loss and not a cleanup: nothing verifies the secret scanner's
regular expressions any more, and this repository already had two patterns that
silently matched nothing for months. Those tests are how that was found.

`tests/integration/` needs the core only because the module under test imports `cat.log` and `cat.mad_hatter.decorators` at import time, not because a Cat must be running. Those tests never contact a live instance: the container is used as an interpreter, not as a server. Automated tests against a running instance do not exist yet; see `What is not automated` below.

One thing to know before adding files here: the Cat imports every `.py` it finds in the plugin folder, recursively, including `tests/`, and it does so under a package name where a bare `import checks` does not resolve. Left alone, that makes the core log `Unable to load plugin rag-guardrails` on every activation. Both test modules therefore put the plugin folder on `sys.path` before importing, which keeps a genuine breakage failing rather than skipping. It is also why `pytest.ini` and the two runner scripts are deliberately not Python files.

The `@hook` decorator turns functions into non-callable `CatHook` objects. Tests reach the real function through `.function`.

## Environment setup

Two options, depending on which tests you want to run.

Local interpreter, `tests/unit` only:

```bash
python -m pip install pytest phonenumberslite
```

`phonenumberslite` is there because `checks.py` imports it at module level: the personal-data guard validates phone numbers against a numbering plan rather than matching a shape. Without it `tests/unit` fails at import, loudly, which is the wanted outcome: a guard whose behaviour depends on what happens to be installed is worse than one that refuses to start.

It is not the plugin's only runtime dependency — `requirements.txt` also declares `transformers` and `torch` for the optional local classifiers, and the core installs all three on activation. **They are deliberately absent from the command above**, and that is not an oversight: both `prompt_injection_classifier.py` and `offensive_input_classifier.py` import `transformers` lazily, inside `_get_pipeline()`, so nothing under `tests/unit` touches it — the classifier tests exercise the decision logic around a stubbed pipeline. Adding `torch` to a local install would cost gigabytes and buy nothing. If a future test needs the real pipeline it belongs in `tests/integration/`, where the container already has it.

Container, whole suite:

```bash
docker compose up -d
```

Add `--build` only after changing the image or the core dependencies: it is not needed to run the plugin or its tests, and it is considerably slower.

## Running the tests

The single source of truth is `run-tests.py`. It returns pytest's own exit code.

Direct Python entrypoint:

```bash
python run-tests.py --unit
python run-tests.py
python run-tests.py --detailed
```

Because the exit code is pytest's own, the script can be reused from a git hook or from CI. If a prerequisite is missing, no interpreter with `pytest`, container not running, `compose.yml` not where expected, it says which command fixes it instead of failing obscurely.

The `pre-commit` hook runs `tests/unit` too, and nothing else: a commit must not depend on Docker being up, or the hook would either block legitimate commits or skip in silence. `tests/integration` is for the runners, before pushing.

**Nothing tests the staged-secret hook any more.** Its regression gate against a
malformed pattern silently disabling part of the scan was removed on 2026-09-08
together with its Git dependency; the open issue in
`DEV/AGENTS/ISSUES_TODO.md` carries what that costs and how to get the coverage
back without Git.

Two limits of that gate are worth knowing. It runs `pytest` against the files on disk, not against the staged snapshot, so with unstaged changes in the working tree what passes is not exactly what is being committed. And if no interpreter with `pytest` is available it warns and lets the commit through, on the grounds that blocking for a missing development tool teaches `--no-verify`, which would also disable the secret scan.

The Python runner handles both Compose v2 and the standalone `docker-compose` binary.

Calling `pytest` directly works too. Locally:

```bash
python -m pytest tests/unit
```

In the container:

```bash
docker compose exec -w /app/cat/plugins/rag-guardrails cheshire-cat-core python -m pytest
```

From Git Bash on Windows that same direct `docker compose exec -w ...` command fails with `Cwd must be an absolute path`, because the shell rewrites the `-w` path. `run-tests.py` handles that case automatically.

No `PYTHONPATH` is needed: `pytest.ini` declares `pythonpath = . /app`, where `.` makes the plugin modules importable and `/app` makes the core importable inside the container. A path that does not exist is ignored, so the same file works on a developer machine. Without that second entry `tests/integration/` is skipped rather than failed, which reads as a success.

## What is not automated

Verification against a real instance is currently manual: activate the plugin, send messages through `POST /message`, and read `docker compose logs -f cheshire-cat-core` to confirm which code path ran. A correct-looking answer does not prove it came from this plugin; the log lines do.

This tier matters because it catches what the other two cannot. The interaction with the `Rate Limiter` plugin is the case in point: its checks used to intercept messages before this plugin ever saw them, and nothing in the code of either plugin showed it. The hook priority now settles who answers, and a unit test guards the priority, but the ordering itself is only ever confirmed on a running instance.

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
| Another plugin refused it | **nothing** from this plugin: its checks passed and it returned the reply it received untouched | — |
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
`input allowed` line is indistinguishable from the one before a normal answer,
which is precisely why *how often the recall comes back empty* is an open issue
and not something the log already answers.

### Manual check still outstanding: the tone guard

The offensive-input guard has **never been exercised through the admin panel**.
Its decision rule is covered by unit tests built on the scores the real model
produced, and the classifier was run against that model through the plugin's own
code path — but nobody has switched the toggle on in the panel and sent a message
through the running instance.

That is the gap only this tier can close, and it is wider than usual here because
the guard ships **switched off**: every automated test that exercises it has to
enable it itself, so the path an administrator actually takes is the one path
never taken.

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

The first message after enabling also pays the model load, so expect it to be
slow — see `DOC/ToneGuards.md`.

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
