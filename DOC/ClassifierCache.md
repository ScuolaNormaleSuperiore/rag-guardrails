# Classifier Cache

How the local-classifier cache works in `rag-guardrails`.

This document explains the runtime cache used by the two local classifier
guards:

- the prompt-injection classifier
- the offensive-input classifier

The shared implementation lives in `classifier_runtime.py`.

## What "classifier cache" means here

There are two different caches involved, and they should not be confused:

- the plugin's own **in-memory cache**
- Hugging Face's **disk cache**

This document is mainly about the first one.

The plugin does **not** cache the verdict for a message text it has already
seen. It caches the **loaded `transformers` pipeline object** for a model-and-device
pair, so the same placement does not have to be loaded again on every message.

## The two caches in the plugin

`classifier_runtime.py` keeps two module-level dictionaries.

### Positive cache

```python
_CLASSIFIER_PIPELINES: dict[str, Any] = {}
```

This is the cache of models that loaded successfully.

The key is the model name on CPU, or the model name plus device on CUDA, for
example:

- `meta-llama/Llama-Prompt-Guard-2-86M`
- `meta-llama/Llama-Prompt-Guard-2-86M::device=0`
- `IMSyPP/hate_speech_multilingual`

The value is the ready-to-use `transformers.pipeline(...)` object already held
in memory.

### Negative cache

```python
_FAILED_CLASSIFIER_MODELS: dict[str, str] = {}
```

This is the cache of models whose load already failed.

The key uses the same model-and-device form. The value is the reason for the
failure, kept as a string after secret redaction.

This cache exists for **cost**, not for tidiness: if a model already failed
because it is gated, unavailable, or otherwise broken, retrying it on every
message would add pointless work and noisy logs to the `fast_reply` path.

## How loading works

The central function is `get_pipeline(model_name, token=False, **pipeline_kwargs)`
in `classifier_runtime.py`.

Its behavior is:

1. If the model is already present in `_CLASSIFIER_PIPELINES`, return it
   immediately.
2. If the model is present in `_FAILED_CLASSIFIER_MODELS`, do not retry the
   load and raise `ClassifierUnavailable`.
3. Otherwise, acquire that model-and-device pair's load lock, waiting at most
   `CLASSIFIER_LOAD_WAIT_SECONDS`. See *Only one request loads a model* below.
4. Re-read both caches, because another request may have filled either one while
   this one waited.
5. Try to build the pipeline with `transformers.pipeline(...)`.
6. If loading succeeds, store the pipeline in `_CLASSIFIER_PIPELINES`.
7. If loading fails, store the redacted failure reason in
   `_FAILED_CLASSIFIER_MODELS` and re-raise the original failure.

The callers then turn that into the plugin's fail-open behavior: a classifier
that cannot run must not block the message and must not take the turn down.

### Only one request loads a model

A cold load can involve disk I/O and a download from Hugging Face, and without a
lock every concurrent turn would start its own. `_CLASSIFIER_LOAD_LOCKS` holds one
lock per model-and-device pair — different models and placements stay independent
— and the wait is bounded at `CLASSIFIER_LOAD_WAIT_SECONDS`, currently `5.0`.

That bound is the third way `ClassifierUnavailable` is raised, alongside the
negative cache and a load that failed outright. A request that times out has
examined nothing, so it degrades to the usual fail-open behaviour and, on the
input stage, is left out of the `checks=` list of the log line: it never looked at
the message.

The timeout exists so a stuck third-party load cannot hold every later request
indefinitely. Its cost is that during a slow cold start each concurrent turn waits
the full five seconds before giving up. Deployments that need to avoid that first
turn can enable the optional offline warm-up described below.

## What happens on repeated messages

### Case 1: the model is available

First message reaching the classifier:

- the model is not in the positive cache
- the model is not in the negative cache
- the plugin loads it
- the plugin logs that it is loading the model
- the plugin logs that the model was loaded and cached in memory

Second message reaching the same classifier:

- `get_pipeline()` finds the model in `_CLASSIFIER_PIPELINES`
- the model is reused directly
- the plugin logs a `classifier pipeline cache hit`

So the expensive part, loading the model into memory, is paid only once per
plugin process.

### Case 2: the model is unavailable

First message reaching the classifier:

- the model is not in the positive cache
- the model is not in the negative cache
- the plugin tries to load it
- the load fails, for example with a `401`, `403`, or another Hugging Face
  error
- the failure reason is stored in `_FAILED_CLASSIFIER_MODELS`
- the plugin logs the failure once

Second message reaching the same classifier:

- `get_pipeline()` sees that the model is already in the negative cache
- it does **not** try to load it again
- it raises `ClassifierUnavailable`
- the guard stays fail-open, without repeating the same load attempt

This is the reason the warning says the condition is **not repeated until the
plugin reloads**.

## Cache scope

The caches are:

- **module-level**
- **shared by both classifier guards**
- **keyed by model name and selected device**

That means a model is loaded once per process for each selected device, whoever
asks for it. CPU keeps the bare model-name key; CUDA uses
`model_name::device=N`.

This is intentional. The cache is not "owned" by one guard: it belongs to the
runtime layer shared by the prompt-injection and offensive-input classifiers.

## What the cache does not do

The current implementation does **not**:

- cache the classification result for a message text
- remember "this sentence was offensive" or "this sentence was injection"
- distinguish cache entries by pipeline-construction options other than device

Those limits matter because they explain two existing design consequences:

- changing model selection or device releases the no-longer-active cached
  placement on the next input turn
- a future runtime option that changes the actual pipeline identity must enter
  the cache key too

## Relationship with the Hugging Face disk cache

On the normal request path, the log line

```text
Transformers will use the local Hugging Face cache when available and download missing files if needed
```

refers to a different cache layer: files already stored on disk by the
Hugging Face libraries.

Activation warm-up is different: it logs that it is loading from locally cached
files only and never downloads missing files. A missing local model is reported
as a skipped warm-up and is not recorded as a permanent classifier failure.

That is not the same thing as the plugin cache:

- Hugging Face disk cache: the model files are already present on disk
- plugin in-memory cache: the `pipeline` object is already created and ready
  to run

So these are different situations:

- **No disk cache, no plugin cache**: the slowest case
- **Disk cache yes, plugin cache no**: the files are already on disk, but the
  pipeline still has to be created in memory
- **Plugin cache yes**: the pipeline is already alive in memory and is reused
  immediately

## Example

Suppose the prompt-injection classifier is enabled with
`meta-llama/Llama-Prompt-Guard-2-86M`.

### Example A: access is configured correctly

Message 1:

- `get_pipeline("meta-llama/Llama-Prompt-Guard-2-86M")` finds no cache entry
- the model loads
- the pipeline is stored in `_CLASSIFIER_PIPELINES`
- the classifier runs

Message 2:

- `get_pipeline("meta-llama/Llama-Prompt-Guard-2-86M")` finds the cached pipeline
- the pipeline is reused
- no second load happens

### Example B: access is not configured correctly

Message 1:

- `get_pipeline("meta-llama/Llama-Prompt-Guard-2-86M")` finds no cache entry
- the load fails because the repository is gated and the instance has no valid
  access
- the failure reason is stored in `_FAILED_CLASSIFIER_MODELS`
- the guard continues fail-open

Message 2:

- `get_pipeline("meta-llama/Llama-Prompt-Guard-2-86M")` sees the negative cache
- the load is not retried
- the guard continues fail-open again, but without a second network attempt

If the administrator then switches to `deepset/deberta-v3-base-injection`, that
works immediately because the caches are **per model and device**. A failure of
one model or placement does not poison another.

## Releasing models no longer configured

When the active classifier configuration changes, the plugin removes positive
cache entries for model-and-device pairs no longer selected. This releases their
process memory and clears the CUDA allocator cache when CUDA is available. It
does **not** delete any Hugging Face files on disk and does not uninstall
dependencies.

A turn that already holds a pipeline can finish safely; removing the cache entry
only prevents later turns from reusing that inactive model. If it is selected
again, it is loaded again, normally from the Hugging Face disk cache.

## Optional warm-up at activation

`Preload classifiers on plugin activation` ships disabled. When enabled, the
plugin tries to load only files already available in the local Hugging Face
cache. The setting takes effect on the next plugin activation, not when it is
saved. It never downloads during activation: a missing or incomplete cache is
logged and the chatbot still starts. The first normal classifier use retains its
usual behaviour and may download the model. A process warms each enabled-model
and device configuration at most once, so core-driven plugin rediscovery does
not repeat the local load.

## Reset point

Both caches live only for the lifetime of the plugin process. In addition, the
positive cache releases models that are no longer active as described above.

They are reset when the plugin reloads, which in practice means when the Cat
process or container restarts, or when the plugin is reloaded in a way that
re-imports the module.
