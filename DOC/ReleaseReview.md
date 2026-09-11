# Release Review Dossier

The facts a release review needs, gathered so the meeting can start from a
document instead of from the source. It is written for the people who have to
decide — the service owner, the security contact, the DPO — and not for whoever
maintains the plugin.

Nothing here is a decision. Each open question says who has to answer it and
what the answer changes.

Verified against Cheshire Cat AI `1.9.2` and plugin `1.0.1` on 2026-09-11. The
line references are to the installed core, which is the authority where it and
the public documentation disagree.

---

## 1. What this plugin is, in one paragraph

`RAG Guardrails` sits around the normal question-and-answer flow of a help-desk
chatbot and stops some messages before they go further. It applies four checks
to what a user writes, before the chatbot searches its documents and before the
language model is called, and one check to the answer before it is delivered.
Every check it applies is deterministic or runs a small model **on the same
machine**. The plugin never sends anything to a language model, on any path.

## 2. What data it touches

| Data | Where it comes from | What the plugin does with it | Where it ends up |
| --- | --- | --- | --- |
| The user's message text | The website chat widget, through Cheshire Cat AI | Reads it, measures its length, matches patterns against it, optionally passes it to a local classifier model | Nowhere the plugin controls. It is not copied, not stored and not forwarded |
| The generated answer | Cheshire Cat AI, after the language model produced it | Reads it, matches the same personal-data patterns against it | Replaced by a fixed text when it carries personal data; otherwise passed on untouched |
| The verdict of a refusal | Produced by the plugin | Written to the session's working memory, which lives in memory for the session only | Discarded when the session ends |
| The plugin's configuration | The admin panel | Read on every message | `settings.json`, in the plugin folder |

**The plugin keeps no database, no file of its own and no history.** It writes
exactly two kinds of output: the replacement replies a user sees, and the log
lines described below.

## 3. What reaches the logs

This is the section the DPO needs, and it has two halves that must not be
confused.

### What this plugin writes

Never the message, never the answer, never the personal data it found. Only the
*shape* of what happened: which stage acted, which category of risk, which
control fired, how long it took, and which detector matched by name — `detected=email+phone (mobile)`
says an e-mail address and a mobile number were present, and nothing more.

A test asserts that no log line can carry the Hugging Face token, after a real
leak was found and fixed on 2026-08-06. Third-party exception texts are stripped
of anything credential-shaped before they are written.

### What the core writes, before this plugin exists

**Cheshire Cat AI logs every incoming message, its full text included, at `INFO`
level, before any plugin runs** — `cat/looking_glass/stray_cat.py:460`, and
`INFO` is the shipped default log level. The message is parsed and logged at line
460; the first plugin hook runs at line 469.

The consequence is precise and cannot be engineered away from inside a plugin: a
message refused *for containing personal data* is still written to the container
log in full. The user is told the message was not stored in the chatbot's
memory, which is true — it never reaches the vector database — but it is not the
same as saying it left no trace.

> **Open question 1, for the DPO.** How long are container logs retained, who can
> read them, and do they need to be excluded from ordinary support bundles and
> backups? A shorter retention or a restricted access path is the only available
> mitigation. Changing the core's log level would suppress the plugin's own
> operational lines too, which are what make a guard's silence distinguishable
> from a guard that is not running.

## 4. What leaves the machine

| When | To where | Carrying what |
| --- | --- | --- |
| Nothing, with the shipped configuration | — | — |
| First use of a classifier guard, if an administrator enables one | `huggingface.co` | A request for the model files, and the access token if one is configured |

Both classifier guards ship **switched off**. With the shipped settings the
plugin makes no network call at all, and no message text ever leaves the
machine: model inference, when enabled, runs locally on the downloaded files.

> **Open question 2, for the service owner.** Are the two classifier guards to be
> enabled in production? If yes, the download happens once per model per
> container and the machine needs outbound access to `huggingface.co`; the tone
> guard's model is about 1.1 GB.

## 5. Where secrets live

One credential is in play: a Hugging Face read token, needed only by the two
gated Meta models for prompt-injection detection. The other four supported
models need none.

| Path | Storage | Status |
| --- | --- | --- |
| `HF_TOKEN` environment variable | Container environment | **Recommended.** Takes precedence over everything else |
| `HUGGING_FACE_HUB_TOKEN` environment variable | Container environment | Legacy name, also honoured |
| Admin panel field | `settings.json`, **in clear text**, in the plugin folder | Accepted with its risk on 2026-09-11. The admin panel of 1.9.2 cannot mask a field — verified against the shipped admin bundle, which renders only text and number inputs |

> **Open question 3, for the security contact.** Is the clear-text field
> acceptable given that `settings.json` must then be excluded from backups,
> container copies and support snapshots, or should the deployment forbid it and
> use the environment variable only?

## 6. What is already settled

Recorded so the review does not spend time re-deriving it.

- **Licensing.** Every one of the six models the plugin can run has its licence
  documented and verified with the date of verification, in `README.md` under
  *License and Legal Notes* and in `DOC/Licenses.md`. Two of them are not free
  software, and both are gated.
- **No model weights are distributed.** The release package contains code and
  documentation only. This is a licensing boundary, not a size decision: the code
  is GPL-3.0-only, while some model licences impose use restrictions that GPLv3
  section 10 forbids adding to conveyed material. Whoever installs the plugin
  accepts each model's terms directly with its publisher.
- **No generative call.** The plugin does not call a language model on any path,
  including its fallback replies. This is a standing rule of the project, not a
  current coincidence.
- **The token cannot reach a log.** Asserted by a test, after a real leak.
- **A refused message does not reach the vector database.** The input checks run
  on the earliest hook available, before retrieval and before the message is
  stored — confirmed against the installed core, where storage happens at
  `stray_cat.py:527` and the hook runs at `:469`.
- **A failing guard cannot take the chatbot down.** Eleven failure paths were
  provoked during the code review of 2026-09-11 — unreadable settings, a model
  that will not load, a load that times out, malformed classifier output,
  anomalous message shapes — and the turn survived every one. The core also wraps
  each hook in its own error handling.

## 7. What is not covered, by decision

Stated here because a review should know the perimeter, not discover it later.
Each of these is a recorded decision with its reasoning in the backlog, not an
oversight.

- Names and postal addresses written in prose are **not** detected. The privacy
  guards cover structured data: e-mail addresses, phone numbers, codice fiscale,
  IBAN. A model able to find names is tracked, and its first filter is a licence
  question rather than a technical one.
- Whether an answer is **factually grounded** in the retrieved documents is not
  checked. Neither is the language it comes back in. Both are currently prompt
  instructions, configured outside this plugin.
- Retrieval tuning, the prompt itself and the episodic-memory policy are
  delegated to the `Cat Advanced Tools` plugin. They shape what the model is
  given; this plugin guards the edges.
- The prompt-injection patterns are an explicit first barrier in Italian and
  English. They are not a general proof of immunity to jailbreaks.

## 8. Live verification still outstanding

None of the following can be established from the code, and none has been run on
a live instance. They are the practical half of this gate.

| # | What to confirm | How |
| --- | --- | --- |
| 1 | The guards behave as documented against the retrieval values and the prompt actually configured in `Cat Advanced Tools` | Send the checklist messages in `DOC/TestingCode.md` and read `docker compose logs -f cheshire-cat-core` |
| 2 | The tone guard works through the admin panel — it has **never** been switched on that way, and it ships off, so the path an administrator actually takes is the one path never taken | The five-step procedure in `DOC/TestingCode.md`, section *Manual check still outstanding: the tone guard* |
| 3 | The hook order against `Rate Limiter` and `Hate Defender`, which share the same hook | Only visible on a running instance; the log lines say who answered |
| 4 | What the chatbot answers when the document search finds nothing | Tracked as its own backlog issue: it produces a number, not a tick |
| 5 | Whether answers come back in the language of the question | Tracked as its own backlog issue, same reason |
| 6 | The privacy guard against a spaced address, fixed on 2026-09-11 | Ask for an answer that repeats an address, confirm the replacement reply and the `output blocked` line |

Items 4 and 5 are deliberately not folded into this checklist: inside a list of
ticks they become lines someone marks as done without measuring anything.

## 9. What a `GO` would mean

For clarity, since the plugin is already running in production and this gate is
about publishing it:

- the three open questions above have recorded answers;
- the six live verifications have been run, with their outcome written down;
- the backlog carries no open `Critical` or `High` defect.

A sign-off here is a decision by the people who own the service. It is not a
security certification of the plugin, and this document does not make one.
