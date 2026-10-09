"""A warmed pipeline must classify: the one thing every other test stubs away.

Every other test replaces the `transformers` pipeline with a stand-in, so none of
them can see what a real one does with the arguments the plugin hands it. That
is how the offline warm-up shipped broken: `local_files_only=True` was passed to
`transformers.pipeline()` as a keyword, which kept it as a preprocessing
parameter and gave it to the tokenizer on every call. The model loaded, the
warm-up reported success, and then both classifiers raised `TypeError` on every
message. It was found in the production log on 2026-10-09.

These tests build a tiny random classifier on the fly — no download, a few
kilobytes — and run it through the plugin's own loading code and the same calls
the two classifiers make. They live here because they need `torch` and
`transformers`, which the container has and a bare local interpreter does not.
"""

import shutil
import socket
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("torch")
pytest.importorskip("transformers")

from tokenizers import Tokenizer, models, pre_tokenizers  # noqa: E402
from transformers import (  # noqa: E402
    DistilBertConfig,
    DistilBertForSequenceClassification,
    PreTrainedTokenizerFast,
)

import classifier_runtime as runtime  # noqa: E402


TEXT = "ciao come posso la password"
REPOSITORY = "tiny-org/tiny-classifier"

# The two calls the classifiers make, with the bound they pass.
PROMPT_INJECTION_CALL = {"truncation": True, "max_length": runtime.CLASSIFIER_MAX_INPUT_TOKENS}
TONE_CALL = {"top_k": None, **PROMPT_INJECTION_CALL}


@pytest.fixture(scope="module")
def tiny_model(tmp_path_factory):
    """A saved random DistilBERT classifier with a five-word vocabulary."""
    directory = tmp_path_factory.mktemp("tiny-model")
    vocabulary = {"[PAD]": 0, "[UNK]": 1, "ciao": 2, "come": 3, "posso": 4, "la": 5, "password": 6}
    core = Tokenizer(models.WordLevel(vocabulary, unk_token="[UNK]"))
    core.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=core,
        unk_token="[UNK]",
        pad_token="[PAD]",
        # DistilBERT takes no `token_type_ids`, and a tokenizer that returns them
        # makes its `forward()` raise: that would fail the normal load too.
        model_input_names=["input_ids", "attention_mask"],
    )
    config = DistilBertConfig(
        vocab_size=len(vocabulary),
        dim=8,
        n_layers=1,
        n_heads=1,
        hidden_dim=16,
        max_position_embeddings=64,
        num_labels=2,
        id2label={0: "LABEL_0", 1: "LABEL_1"},
        label2id={"LABEL_0": 0, "LABEL_1": 1},
    )
    DistilBertForSequenceClassification(config).save_pretrained(directory)
    tokenizer.save_pretrained(directory)
    return directory


@pytest.fixture(autouse=True)
def clean_caches():
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()
    yield
    runtime._CLASSIFIER_PIPELINES.clear()
    runtime._FAILED_CLASSIFIER_MODELS.clear()


def scores_of(result):
    entries = result[0] if isinstance(result[0], list) else result
    return {entry["label"]: entry["score"] for entry in entries}


class TestWarmedPipeline:
    @pytest.mark.parametrize(
        "call", [PROMPT_INJECTION_CALL, TONE_CALL], ids=["prompt-injection", "tone"]
    )
    def test_a_warmed_pipeline_classifies_with_the_calls_the_classifiers_make(
        self, tiny_model, call
    ):
        assert runtime.warm_pipeline(str(tiny_model)) is True

        # The next message gets the pipeline the warm-up cached.
        pipeline = runtime.get_pipeline(str(tiny_model))
        result = pipeline(TEXT, **call)

        scores = scores_of(result)
        assert set(scores) <= {"LABEL_0", "LABEL_1"}
        assert all(0.0 <= value <= 1.0 for value in scores.values())

    def test_a_warmed_pipeline_agrees_with_a_normally_loaded_one(self, tiny_model):
        runtime.get_pipeline(str(tiny_model))
        normal = scores_of(runtime.get_pipeline(str(tiny_model))(TEXT, **TONE_CALL))

        runtime._CLASSIFIER_PIPELINES.clear()
        runtime.warm_pipeline(str(tiny_model))
        warmed = scores_of(runtime.get_pipeline(str(tiny_model))(TEXT, **TONE_CALL))

        assert warmed.keys() == normal.keys()
        for label, value in normal.items():
            assert warmed[label] == pytest.approx(value, abs=1e-6)

    def test_the_warmed_pipeline_survives_many_messages(self, tiny_model):
        # The failure was per message and permanent: one call proves loading, a
        # run of them proves the cached object is reusable.
        runtime.warm_pipeline(str(tiny_model))
        pipeline = runtime.get_pipeline(str(tiny_model))

        for _ in range(5):
            assert pipeline(TEXT, **PROMPT_INJECTION_CALL)

    def test_the_pipeline_does_not_carry_the_offline_option_to_the_tokenizer(
        self, tiny_model
    ):
        # The mechanism itself, read off the object: whatever the pipeline will
        # pass to the tokenizer at every call must not include loading options.
        runtime.warm_pipeline(str(tiny_model))
        pipeline = runtime.get_pipeline(str(tiny_model))

        assert "local_files_only" not in pipeline._preprocess_params


class TestWarmUpNeedsNoNetwork:
    """The promise in the setting's description: it never downloads.

    Resolved by name, from a hand-built cache, with every connection attempt
    recorded. Where the environment already forces offline mode, as the image
    does, the load cannot touch the network whatever the code does; elsewhere
    this is what proves it.
    """

    @staticmethod
    def use_cache(monkeypatch, cache):
        """Point both libraries at `cache`.

        `transformers` reads its own module-level `TRANSFORMERS_CACHE` and
        `huggingface_hub` reads `constants.HF_HUB_CACHE`; each was fixed at import
        time from the environment, so patching one leaves the other looking in
        the real cache.
        """
        import huggingface_hub
        from transformers.utils import hub

        monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_CACHE", str(cache))
        monkeypatch.setattr(hub, "TRANSFORMERS_CACHE", str(cache))

    @pytest.fixture
    def cached_repository(self, tiny_model, tmp_path, monkeypatch):
        cache = tmp_path / "hub"
        revision = "0123456789abcdef0123456789abcdef01234567"
        repository = cache / "models--tiny-org--tiny-classifier"
        snapshot = repository / "snapshots" / revision
        shutil.copytree(tiny_model, snapshot)
        (repository / "refs").mkdir(parents=True)
        (repository / "refs" / "main").write_text(revision, encoding="utf-8")
        self.use_cache(monkeypatch, cache)
        return REPOSITORY

    @pytest.fixture
    def connection_attempts(self, monkeypatch):
        attempts = []

        def refuse(*args, **kwargs):
            attempts.append(args[:2] if args else kwargs)
            raise OSError("the network is not available in this test")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)
        return attempts

    def test_a_model_resolved_by_name_is_loaded_without_a_connection_attempt(
        self, cached_repository, connection_attempts
    ):
        assert runtime.warm_pipeline(cached_repository) is True

        pipeline = runtime.get_pipeline(cached_repository)
        assert pipeline(TEXT, **PROMPT_INJECTION_CALL)
        assert connection_attempts == []

    def test_a_model_that_is_not_cached_is_skipped_not_downloaded(
        self, connection_attempts, tmp_path, monkeypatch
    ):
        self.use_cache(monkeypatch, tmp_path)

        assert runtime.warm_pipeline("tiny-org/not-in-the-cache") is False
        assert connection_attempts == []
        # A failed warm-up must leave the model loadable normally later.
        assert runtime.classifier_load_error("tiny-org/not-in-the-cache") is None
