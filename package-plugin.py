"""Build a release zip containing only the distributable plugin files.

The package is created under `dist/` and contains a single top-level folder
named after the plugin slug, ready to be copied into a Cheshire Cat plugins
directory or attached to a release.

By default this excludes development-only material such as `DEV/`, `DOC/`,
tests, local caches, and runner scripts. Public documentation should be shipped
only when it is explicitly meant to be part of the release package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


REPO_ROOT = Path(__file__).resolve().parent
DIST_DIR = REPO_ROOT / "dist"
PLUGIN_METADATA_FILE = REPO_ROOT / "plugin.json"

# Keep the package explicit: only runtime files listed here are shipped.
# Internal documentation under `DOC/` is intentionally excluded by default.
#
# **Never add model weights or a Hugging Face cache directory to this list.** It
# is a licensing boundary, not a size optimisation. This plugin is GPL-3.0-only,
# while two of the classifier models it can run are distributed under the Llama
# Community License and one under OpenRAIL++ — licences that impose use
# restrictions. GPLv3 section 10 forbids adding restrictions to conveyed
# material, so shipping those weights alongside this code would create a real
# incompatibility. Downloading them at runtime, under the terms the installer
# accepts directly with the publisher, is what keeps the two apart. See
# `README.md`, section *License and Legal Notes*.
INCLUDED_FILES = (
    "plugin.json",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "requirements.txt",
    "checks.py",
    "classifier_runtime.py",
    "prompt_injection_classifier.py",
    "offensive_input_classifier.py",
    "settings.py",
    "rag_guardrails.py",
)


def load_plugin_metadata() -> dict:
    with PLUGIN_METADATA_FILE.open(encoding="utf-8") as handle:
        return json.load(handle)


def plugin_slug(metadata: dict) -> str:
    plugin_url = metadata.get("plugin_url", "").rstrip("/")
    slug = plugin_url.rsplit("/", 1)[-1] if plugin_url else ""
    if slug:
        return slug.removesuffix(".git")
    return "rag-guardrails"


def package_name(metadata: dict, slug: str) -> str:
    version = metadata.get("version", "0.0.0")
    return f"{slug}-{version}.zip"


def validate_included_files() -> list[Path]:
    missing = [name for name in INCLUDED_FILES if not (REPO_ROOT / name).is_file()]
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise FileNotFoundError(f"Missing required package files: {missing_text}")

    return [REPO_ROOT / name for name in INCLUDED_FILES]


def build_zip(zip_path: Path, slug: str, files: list[Path]) -> None:
    if zip_path.exists():
        zip_path.unlink()

    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for source_path in files:
            archive_path = Path(slug) / source_path.relative_to(REPO_ROOT)
            archive.write(source_path, archive_path.as_posix())


def main() -> int:
    try:
        metadata = load_plugin_metadata()
        slug = plugin_slug(metadata)
        files = validate_included_files()

        DIST_DIR.mkdir(exist_ok=True)
        zip_path = DIST_DIR / package_name(metadata, slug)
        build_zip(zip_path, slug, files)
    except Exception as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        return 1

    print(f"Created: {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

