"""Assemble verified desktop, WebView, wheel, and Android preview artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class ReleaseAssemblyError(RuntimeError):
    """Release assets or policy inputs are inconsistent."""


_CHECKSUM_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<name>[^/\\]+)$")


def _boolean(value: str, label: str) -> bool:
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ReleaseAssemblyError(f"{label} must be true or false")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _copy_unique_files(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise ReleaseAssemblyError(f"Artifact directory is missing: {source}")
    for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.name == "SHA256SUMS.txt":
            continue
        destination = target / path.name
        if destination.exists():
            raise ReleaseAssemblyError(
                f"Duplicate GitHub Release asset basename: {path.name}"
            )
        shutil.copyfile(path, destination)


def _copy_checksum(source: Path, target: Path) -> None:
    checksum = source / "SHA256SUMS.txt"
    if not checksum.is_file():
        raise ReleaseAssemblyError(f"Checksum file is missing: {checksum}")
    shutil.copyfile(checksum, target)


def _verify_checksum_directory(source: Path) -> None:
    checksum = source / "SHA256SUMS.txt"
    if not checksum.is_file():
        raise ReleaseAssemblyError(f"Checksum file is missing: {checksum}")
    try:
        lines = checksum.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReleaseAssemblyError(f"Unable to read {checksum}: {exc}") from exc
    recorded: set[str] = set()
    for line_number, line in enumerate(lines, start=1):
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None:
            raise ReleaseAssemblyError(
                f"Invalid checksum line {line_number} in {checksum.name}"
            )
        name = match.group("name")
        if name in recorded:
            raise ReleaseAssemblyError(f"Duplicate checksum entry: {name}")
        recorded.add(name)
        path = source / name
        if not path.is_file() or path.is_symlink():
            raise ReleaseAssemblyError(f"Checksummed artifact is missing: {path}")
        if _sha256(path) != match.group("digest"):
            raise ReleaseAssemblyError(
                f"SHA-256 mismatch for upstream artifact: {name}"
            )
    actual = {
        path.name
        for path in source.iterdir()
        if path.is_file() and path.name != "SHA256SUMS.txt"
    }
    if recorded != actual:
        raise ReleaseAssemblyError(
            "Upstream checksum coverage does not match the artifact directory"
        )


def _validate_android_directory(source: Path, version: str) -> str:
    expected_name = f"Zvec-LAN-Viewer-{version}-android-debug-preview.apk"
    artifacts = {
        path.name
        for path in source.iterdir()
        if path.is_file() and path.name != "SHA256SUMS.txt"
    }
    if artifacts != {expected_name}:
        raise ReleaseAssemblyError(
            "Android artifact directory must contain only the expected debug-preview "
            f"APK: {expected_name}"
        )
    return expected_name


def assemble_release(
    *,
    desktop_directory: Path,
    webview_directory: Path,
    native_directory: Path,
    android_directory: Path,
    output_directory: Path,
    version: str,
    revision: str,
    signing_status: str,
    version_prerelease: bool,
    exact_tag_ref: bool,
    license_present: bool,
    search_quality_certified: bool,
) -> dict[str, str]:
    if signing_status not in {"unsigned", "authenticode"}:
        raise ReleaseAssemblyError("signing_status must be unsigned or authenticode")
    output = output_directory.resolve()
    assets = output / "release-assets"
    if output.exists():
        raise ReleaseAssemblyError(f"Output directory already exists: {output}")
    _verify_checksum_directory(desktop_directory)
    _verify_checksum_directory(webview_directory)
    _verify_checksum_directory(native_directory)
    _verify_checksum_directory(android_directory)
    android_apk_name = _validate_android_directory(android_directory, version)
    assets.mkdir(parents=True)

    reasons: list[str] = []
    if version_prerelease:
        reasons.append("the version has a SemVer prerelease suffix")
    if signing_status != "authenticode":
        reasons.append("desktop artifacts are explicitly unsigned")
    if not license_present:
        reasons.append("the repository has no LICENSE file")
    if not exact_tag_ref:
        reasons.append("the workflow ref is not the exact v<version> tag")
    if not search_quality_certified:
        reasons.append("search quality is explicitly uncertified")
    reasons.append("the Android client is a debug-signed preview")
    effective_prerelease = bool(reasons)
    if signing_status != "authenticode" and not effective_prerelease:
        raise ReleaseAssemblyError("Unsigned artifacts cannot enter the stable channel")

    qualifier = (
        "UNSIGNED PREVIEW"
        if signing_status == "unsigned"
        else "PRERELEASE"
        if effective_prerelease
        else "SIGNED STABLE CANDIDATE"
    )
    title = f"Zvec {version} - {qualifier}"

    _copy_checksum(desktop_directory, assets / "DESKTOP-SHA256SUMS.txt")
    _copy_checksum(webview_directory, assets / "WEBVIEW-SHA256SUMS.txt")
    _copy_checksum(native_directory, assets / "NATIVE-SHA256SUMS.txt")
    _copy_checksum(android_directory, assets / "ANDROID-SHA256SUMS.txt")
    _copy_unique_files(desktop_directory, assets)
    _copy_unique_files(webview_directory, assets)
    _copy_unique_files(native_directory, assets)
    _copy_unique_files(android_directory, assets)

    policy = {
        "schema_version": 2,
        "version": version,
        "git_revision": revision,
        "draft": True,
        "prerelease": effective_prerelease,
        "signing_status": signing_status,
        "exact_tag_ref": exact_tag_ref,
        "license_present": license_present,
        "search_quality_certified": search_quality_certified,
        "stable_channel_eligible": not effective_prerelease,
        "preview_reasons": reasons,
        "desktop_runtime": {
            "implementation": "CPython 3.12 / PyInstaller",
            "target": "win-x64",
            "powershell_required": False,
            "dotnet_required": False,
            "docker_required": False,
        },
        "webview_preview": {
            "implementation": "CPython 3.12 / PyInstaller / pywebview / Vue 3",
            "target": "win-x64",
            "channel": "independent unsigned preview",
            "powershell_required": False,
            "dotnet_required": False,
            "docker_required": False,
        },
        "android_client": {
            "implementation": "Kotlin / Jetpack Compose",
            "target": "Android 8.0 (API 26) or later",
            "channel": "debug preview",
            "signing_status": "android-debug",
            "production_signed": False,
            "artifact": android_apk_name,
        },
    }
    _write_json(assets / "RELEASE-POLICY.json", policy)

    checksum_lines = [
        f"{_sha256(path)}  {path.name}\n"
        for path in sorted(assets.iterdir(), key=lambda item: item.name.casefold())
        if path.is_file()
    ]
    (assets / "RELEASE-ASSETS-SHA256SUMS.txt").write_text(
        "".join(checksum_lines), encoding="utf-8"
    )

    warning = (
        "This is a draft prerelease/preview and must not be represented as a "
        "stable signed release."
        if effective_prerelease
        else "This is a signed stable candidate; the GitHub Release remains draft."
    )
    notes = [
        f"# {title}",
        "",
        warning,
        "",
        f"Git revision: `{revision}`",
        f"Desktop signing status: `{signing_status}`",
        f"Search quality certified: `{str(search_quality_certified).lower()}`",
        "Desktop: pure Python win-x64 payload and installer verification passed",
        "WebView Preview: frozen payload, ZIP, and NSIS inventory verification passed",
        "Native CLI: isolated Python 3.12 wheel install and command smoke passed",
        (
            "Android LAN Viewer: unit tests, lint, and debug APK assembly passed; "
            "the APK is preview-only and is not production signed"
        ),
    ]
    if reasons:
        notes.extend(["", "Preview reasons:", "", *(f"- {item}" for item in reasons)])
    (output / "RELEASE-NOTES.md").write_text("\n".join(notes) + "\n", encoding="utf-8")
    return {
        "effective_prerelease": str(effective_prerelease).lower(),
        "release_title": title,
    }


def _append_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ReleaseAssemblyError(f"Unsafe multiline GitHub output: {key}")
            stream.write(f"{key}={value}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop-directory", type=Path, required=True)
    parser.add_argument("--webview-directory", type=Path, required=True)
    parser.add_argument("--native-directory", type=Path, required=True)
    parser.add_argument("--android-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--signing-status",
        choices=("unsigned", "authenticode"),
        required=True,
    )
    parser.add_argument("--version-prerelease", required=True)
    parser.add_argument("--exact-tag-ref", required=True)
    parser.add_argument("--license-present", required=True)
    parser.add_argument("--search-quality-certified", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outputs = assemble_release(
            desktop_directory=args.desktop_directory.resolve(),
            webview_directory=args.webview_directory.resolve(),
            native_directory=args.native_directory.resolve(),
            android_directory=args.android_directory.resolve(),
            output_directory=args.output_directory,
            version=args.version,
            revision=args.revision,
            signing_status=args.signing_status,
            version_prerelease=_boolean(args.version_prerelease, "version_prerelease"),
            exact_tag_ref=_boolean(args.exact_tag_ref, "exact_tag_ref"),
            license_present=_boolean(args.license_present, "license_present"),
            search_quality_certified=_boolean(
                args.search_quality_certified, "search_quality_certified"
            ),
        )
        _append_outputs(args.github_output, outputs)
    except (OSError, ReleaseAssemblyError) as exc:
        print(f"Release assembly failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
