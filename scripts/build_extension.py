#!/usr/bin/env python3
"""Package extension/ into an installable .xpi.

An .xpi is just a zip with the manifest at the root. This validates the
manifest, checks the files it references actually exist, and writes a
deterministic archive to dist/.

    python3 scripts/build_extension.py
    python3 scripts/build_extension.py --version 0.2.0   # also rewrites manifest

The result is unsigned. Firefox for Android will not install an unsigned
extension -- see the signing steps this prints when it finishes.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "extension"
DIST = ROOT / "dist"

EXCLUDE_NAMES = {".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".xpi", ".zip", ".map"}


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_manifest() -> dict:
    path = SOURCE / "manifest.json"
    if not path.is_file():
        fail(f"no manifest at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"manifest is not valid JSON: {exc}")
    return {}


def referenced_files(manifest: dict) -> set[str]:
    """Every file the manifest points at, so a typo fails the build not the install."""
    files: set[str] = set()
    action = manifest.get("action") or manifest.get("browser_action") or {}
    if action.get("default_popup"):
        files.add(action["default_popup"])
    for script in (manifest.get("background") or {}).get("scripts", []):
        files.add(script)
    for entry in manifest.get("content_scripts") or []:
        files.update(entry.get("js", []))
        files.update(entry.get("css", []))
    for size_map in (manifest.get("icons") or {}).values():
        files.add(size_map)
    return files


def collect_files() -> list[Path]:
    found = []
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith(".") or path.name in EXCLUDE_NAMES:
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        found.append(path)
    return found


def check_html_scripts(files: list[Path]) -> list[str]:
    """Inline <script> is blocked by extension CSP; catch it before Firefox does."""
    problems = []
    for path in files:
        if path.suffix != ".html":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for chunk in text.split("<script")[1:]:
            head = chunk.split(">")[0]
            if "src=" not in head:
                problems.append(f"{path.name}: inline <script> will be blocked by CSP")
    return problems


def set_version(new_version: str) -> None:
    path = SOURCE / "manifest.json"
    raw = path.read_text(encoding="utf-8")
    manifest = json.loads(raw)
    old = manifest.get("version")
    manifest["version"] = new_version
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"version {old} -> {new_version}")


def build(manifest: dict) -> Path:
    files = collect_files()
    if not files:
        fail("extension/ has no files to package")

    names = {str(p.relative_to(SOURCE)) for p in files}
    missing = sorted(referenced_files(manifest) - names)
    if missing:
        fail("manifest references files that are not present: " + ", ".join(missing))

    for problem in check_html_scripts(files):
        fail(problem)

    if "browser_specific_settings" not in manifest:
        fail("manifest needs browser_specific_settings.gecko.id for signing")

    DIST.mkdir(exist_ok=True)
    name = manifest.get("name", "extension").lower().replace(" ", "-")
    target = DIST / f"{name}-{manifest['version']}.xpi"

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(str(path.relative_to(SOURCE)))
            # Fixed timestamp and mode: same input, same bytes out.
            info.date_time = (2024, 1, 1, 0, 0, 0)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())

    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="package extension/ into dist/*.xpi")
    parser.add_argument("--version", help="set this version in the manifest first")
    args = parser.parse_args()

    if args.version:
        set_version(args.version)

    manifest = load_manifest()
    target = build(manifest)
    size_kb = target.stat().st_size / 1024

    print(f"\nbuilt {target.relative_to(ROOT)}  ({size_kb:.1f} KB)")
    with zipfile.ZipFile(target) as archive:
        for name in archive.namelist():
            print(f"    {name}")

    android = (manifest.get("browser_specific_settings") or {}).get("gecko_android")
    print(
        "\nDesktop: about:debugging → Load Temporary Add-on → extension/manifest.json"
        "\n         (no need for this .xpi)"
        "\n"
        "\nAndroid: the .xpi must be signed by Mozilla first — unsigned installs"
        "\n         are refused. Either:"
        "\n"
        "\n  a) web-ext sign --channel=unlisted \\"
        "\n         --api-key=$AMO_JWT_ISSUER --api-secret=$AMO_JWT_SECRET \\"
        f"\n         --source-dir={SOURCE.relative_to(ROOT)}"
        "\n     (keys from addons.mozilla.org/developers/addon/api/key/)"
        "\n"
        "\n  b) upload this .xpi at addons.mozilla.org/developers/addon/submit/"
        "\n     choosing 'On your own' (unlisted), then download the signed copy."
    )
    if not android:
        print(
            "\nwarning: manifest has no browser_specific_settings.gecko_android —"
            "\n         AMO will treat this as desktop-only and Firefox Android"
            "\n         will refuse to install it."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
