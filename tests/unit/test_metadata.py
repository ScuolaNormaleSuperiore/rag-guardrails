"""Sanity checks for plugin metadata and shipped defaults."""

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def read_json(name: str):
    with (REPO_ROOT / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class TestPluginMetadata:
    def test_plugin_json_has_the_minimum_expected_fields(self):
        plugin = read_json("plugin.json")

        required = {
            "name",
            "version",
            "description",
            "author_name",
            "author_url",
            "plugin_url",
            "tags",
            "thumb",
        }

        assert required <= set(plugin)

    def test_plugin_json_matches_the_repository_identity(self):
        plugin = read_json("plugin.json")

        assert plugin["name"] == "RAG Guardrails"
        assert "guardrails" in plugin["description"].lower()
        assert "rag flow" in plugin["description"].lower()
        assert "retrieval or generation" in plugin["description"].lower()


class TestLlamaAttribution:
    """Attribution required by the Llama Community License.

    These assert on documents rather than on behaviour, which is unusual in this
    suite and deliberate: the obligation is to *display* the attribution, so the
    failure mode is silent by nature. Nothing stops working when the notice is
    dropped from the README or from the admin-panel description — the plugin
    simply becomes non-compliant, and only a reader would ever notice.

    The trigger is the model list, not the current default: as long as this plugin
    can be configured to run a `meta-llama/*` model, the attribution is required.
    """

    REQUIRED_NOTICE = (
        "Llama is licensed under the Llama Community License, "
        "Copyright © Meta Platforms, Inc. All Rights Reserved."
    )

    def readme(self) -> str:
        return (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_a_meta_model_is_still_offered(self):
        # If this ever fails the obligation may have gone away with the model, and
        # the tests below should be revisited rather than deleted blindly.
        source = (REPO_ROOT / "prompt_injection_classifier.py").read_text(
            encoding="utf-8"
        )

        assert "meta-llama/" in source

    def test_the_readme_displays_built_with_llama(self):
        assert "Optional Llama Prompt Guard model" in self.readme()

    def test_the_readme_carries_the_required_copyright_notice(self):
        # Character for character, including the © and the final full stop: it is a
        # prescribed notice, not a paraphrase.
        assert self.REQUIRED_NOTICE in self.readme()

    def test_the_readme_states_that_no_weights_are_distributed(self):
        # The fact the whole GPLv3 arrangement rests on: some of these models are
        # distributed under licences that impose use restrictions, and GPLv3
        # section 10 forbids adding restrictions to conveyed material.
        assert "no model weights" in self.readme().lower()

    def test_the_release_package_ships_no_model_weights(self):
        # The same claim, checked against the packaging list rather than the prose.
        import importlib.util

        path = REPO_ROOT / "package-plugin.py"
        spec = importlib.util.spec_from_file_location("package_plugin_legal", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        weights = (".safetensors", ".bin", ".pt", ".pth", ".onnx", ".gguf")

        assert not [name for name in module.INCLUDED_FILES if name.endswith(weights)]


class TestShippedSettings:
    """The defaults the plugin is distributed with.

    Read from the source files, never from `settings.json`: that file holds the
    configuration of one installation, it is excluded from version control, and
    it does not exist at all until the plugin is activated for the first time.
    Asserting on it would make these tests fail on a fresh clone for a reason
    that has nothing to do with the plugin.

    The source is read as text rather than imported, so this module keeps
    needing nothing but the standard library, which is what allows it to live in
    tests/unit.
    """

    def settings_source(self) -> str:
        return (REPO_ROOT / "settings.py").read_text(encoding="utf-8")

    def test_the_default_help_desk_address_looks_like_an_email(self):
        match = re.search(
            r'^DEFAULT_HELP_DESK_EMAIL\s*=\s*"([^"]+)"',
            self.settings_source(),
            re.MULTILINE,
        )

        assert match is not None, "DEFAULT_HELP_DESK_EMAIL is not defined"
        address = match.group(1)
        assert "@" in address.strip("@"), f"not an email address: {address}"

    def test_the_default_reply_carries_the_email_placeholder(self):
        # Without the placeholder, changing the address in the admin panel would
        # leave the old one in the text shown to the user.
        match = re.search(
            r"^DEFAULT_MESSAGE_TOO_LONG\s*=\s*\((.*?)\)$",
            self.settings_source(),
            re.MULTILINE | re.DOTALL,
        )

        assert match is not None, "DEFAULT_MESSAGE_TOO_LONG is not defined"
        assert "{help_desk_email}" in match.group(1)

    def test_the_length_limit_has_a_single_source_of_truth(self):
        # The settings model must take its default from checks.py instead of
        # repeating a number, otherwise the two can drift apart.
        assert "DEFAULT_MAX_MESSAGE_CHARS" in self.settings_source()

        checks_source = (REPO_ROOT / "checks.py").read_text(encoding="utf-8")
        match = re.search(
            r"^DEFAULT_MAX_MESSAGE_CHARS\s*=\s*(\d+)",
            checks_source,
            re.MULTILINE,
        )

        assert match is not None, "DEFAULT_MAX_MESSAGE_CHARS is not defined"
        assert int(match.group(1)) > 0
