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
seen. It caches the **loaded `transformers` pipeline object** for a model, so
the same model does not have to be loaded again on every message.

## The two caches in the plugin

`classifier_runtime.py` keeps two module-level dictionaries.

### Positive cache

```python
_CLASSIFIER_PIPELINES: dict[str, Any] = {}
```

This is the cache of models that loaded successfully.

The key is the model name, for example:

- `meta-llama/Llama-Prompt-Guard-2-86M`
- `IMSyPP/hate_speech_multilingual`

The value is the ready-to-use `transformers.pipeline(...)` object already held
in memory.

### Negative cache

```python
_FAILED_CLASSIFIER_MODELS: dict[str, str] = {}
```

This is the cache of models whose load already failed.

The key is again the model name. The value is the reason for the failure, kept
as a string after secret redaction.

This cache exists for **cost**, not for tidiness: if a model already failed
because it is gated, unavailable, or otherwise broken, retrying it on every
message would add pointless work and noisy logs to the `fast_reply` path.

## How loading works

The central function is `get_pipeline(model_name, token=None, **pipeline_kwargs)`
in `classifier_runtime.py`.

Its behavior is:

1. If the model is already present in `_CLASSIFIER_PIPELINES`, return it
   immediately.
2. If the model is present in `_FAILED_CLASSIFIER_MODELS`, do not retry the
   load and raise `ClassifierUnavailable`.
3. Otherwise, import `transformers` and try to build the pipeline with
   `transformers.pipeline(...)`.
4. If loading succeeds, store the pipeline in `_CLASSIFIER_PIPELINES`.
5. If loading fails, store the redacted failure reason in
   `_FAILED_CLASSIFIER_MODELS` and re-raise the original failure.

Step 3 covers **both** failures, and that is deliberate. `transformers` is an
optional dependency installed by the image rather than by the core, so on an
installation without the optional classifier stack the import itself raises
`ModuleNotFoundError` — and that counts as a failed load like any other.

It used to sit outside the block that records a failure, which made the absent
stack the one error the negative cache did not remember: every message repeated
the import, took the model load lock and paid its five-second timeout, for a
package that cannot appear without restarting the process. It is now remembered
once, and the warning that reports it carries the install instructions.

The callers then turn that into the plugin's fail-open behavior: a classifier
that cannot run must not block the message and must not take the turn down.

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
- **keyed only by model name**

That means a model is loaded once per process, whoever asks for it.

This is intentional. The cache is not "owned" by one guard: it belongs to the
runtime layer shared by the prompt-injection and offensive-input classifiers.

## What the cache does not do

The current implementation does **not**:

- cache the classification result for a message text
- remember "this sentence was offensive" or "this sentence was injection"
- release unused models automatically when the admin changes model selection
- distinguish the cache by device or other advanced runtime configuration

Those limits matter because they explain two existing design consequences:

- trying several different models from the admin panel can leave several models
  resident in memory until the plugin reloads
- a model is reused by name alone, so any future runtime option that changes
  the actual pipeline identity must also enter the cache key

## Relationship with the Hugging Face disk cache

The log line

```text
Transformers will use the local Hugging Face cache when available and download missing files if needed
```

refers to a different cache layer: files already stored on disk by the
Hugging Face libraries.

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
works immediately because the caches are **per model**. A failure of one model
does not poison another.

## Reset point

Both caches live only for the lifetime of the plugin process.

They are reset when the plugin reloads, which in practice means when the Cat
process or container restarts, or when the plugin is reloaded in a way that
re-imports the module.

