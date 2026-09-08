# Sharing an installation with the Rate Limiter plugin

Only relevant if the `Rate Limiter` plugin is installed alongside this one. If it
is not, nothing here applies.

The two plugins overlap: both can refuse a message for its length, and both hook
`fast_reply`. Which one answers is settled by hook priority — this plugin
registers at `-1` and therefore runs last, so its reply is the one delivered.
What priority does **not** settle is another plugin's side effects, and that is
the whole reason this document exists.

## If Rate Limiter is only needed for request frequency

Disable its content checks: set `max_prompt_length` and `complexity_threshold`
to `0` and leave its blocked-keyword list empty. `RAG Guardrails` then handles
length, privacy and content, and Rate Limiter keeps doing the one thing it is
uniquely good at.

This is the configuration to prefer. It removes the overlap instead of managing
it.

## If Rate Limiter must also enforce a message length

Keep `Limits guard: max message chars` **below** its `max_prompt_length`.

For a message longer than Rate Limiter's limit but shorter than this plugin's,
Rate Limiter is the plugin that answers, with its own text. It also records a
content infraction and applies a progressive suspension of 5, 15 or 60 minutes,
silently blocking the user's next legitimate messages.

**Nothing in this plugin can undo that**, because the side effect happens before
this plugin's reply is delivered. Priority decides who speaks, not who acts.

Keeping this limit lower means messages between the two limits are refused by
`RAG Guardrails`, with an explanation of what to correct and no suspension.

## Why this is not visible in either plugin's code

It is worth stating, because it is the reason the interaction was found late.
Neither plugin's source shows the other: they meet only at runtime, through a
hook both register, and the outcome depends on a priority number in one and a
threshold in the other.

That makes it a case only a running instance can settle, which is why it appears
in the manual verification checklist in
[DOC/TestingCode.md](https://github.com/ScuolaNormaleSuperiore/rag-guardrails/blob/main/DOC/TestingCode.md)
rather than in any automated test. A unit test guards the priority; the ordering
itself is confirmed by sending messages and reading the log.
