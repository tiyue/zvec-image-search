"""Verify WebView x64 payload, portable ZIP, and NSIS installer without installing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

if __package__:
    from scripts.webview_preview_packaging import (
        MANIFEST_FILE,
        WebviewPreviewPackagingError,
        verify_payload_manifest,
    )
else:
    from webview_preview_packaging import (
        MANIFEST_FILE,
        WebviewPreviewPackagingError,
        verify_payload_manifest,
    )

_ALLOWED_NSIS_SUPPORT_FILES = frozenset(
    {
        "$PLUGINSDIR/modern-wizard.bmp",
        "$PLUGINSDIR/nsDialogs.dll",
        "$PLUGINSDIR/nsExec.dll",
        "$PLUGINSDIR/System.dll",
    }
)


class WebviewReleaseVerificationError(RuntimeError):
    """A release artifact differs from its verified frozen payload."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stream_sha256(stream: Any) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _manifest_entries(payload_directory: Path) -> dict[str, tuple[int, str]]:
    manifest = verify_payload_manifest(payload_directory)
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise WebviewReleaseVerificationError("Payload manifest files are invalid")
    result: dict[str, tuple[int, str]] = {}
    for raw in entries:
        if not isinstance(raw, Mapping):
            raise WebviewReleaseVerificationError("Payload manifest entry is invalid")
        path = raw.get("path")
        size = raw.get("size")
        digest = raw.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or not isinstance(digest, str)
        ):
            raise WebviewReleaseVerificationError("Payload manifest entry is invalid")
        result[path] = (size, digest)
    return result


def _safe_archive_name(name: str) -> str:
    if "\\" in name:
        raise WebviewReleaseVerificationError(
            f"Portable archive uses a non-portable path: {name}"
        )
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise WebviewReleaseVerificationError(
            f"Portable archive contains an unsafe path: {name}"
        )
    return path.as_posix()


def verify_portable_archive(
    payload_directory: Path,
    portable_archive: Path,
) -> dict[str, object]:
    """Verify every ZIP member against the payload manifest and payload files."""

    payload = payload_directory.resolve(strict=True)
    archive_path = portable_archive.resolve(strict=True)
    entries = _manifest_entries(payload)
    manifest_path = payload / MANIFEST_FILE
    expected = set(entries) | {MANIFEST_FILE}
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                name = _safe_archive_name(info.filename)
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise WebviewReleaseVerificationError(
                        f"Portable archive contains a symbolic link: {name}"
                    )
                if name in infos:
                    raise WebviewReleaseVerificationError(
                        f"Portable archive contains a duplicate path: {name}"
                    )
                infos[name] = info
            actual = set(infos)
            if actual != expected:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                raise WebviewReleaseVerificationError(
                    "Portable archive inventory mismatch; "
                    f"missing={missing[:5]}, extra={extra[:5]}"
                )
            damaged = archive.testzip()
            if damaged is not None:
                raise WebviewReleaseVerificationError(
                    f"Portable archive CRC verification failed: {damaged}"
                )
            for relative, (size, digest) in entries.items():
                info = infos[relative]
                if info.file_size != size:
                    raise WebviewReleaseVerificationError(
                        f"Portable archive size mismatch: {relative}"
                    )
                with archive.open(info) as stream:
                    if _stream_sha256(stream) != digest:
                        raise WebviewReleaseVerificationError(
                            f"Portable archive SHA-256 mismatch: {relative}"
                        )
            manifest_info = infos[MANIFEST_FILE]
            if manifest_info.file_size != manifest_path.stat().st_size:
                raise WebviewReleaseVerificationError(
                    "Portable archive payload manifest size mismatch"
                )
            with archive.open(manifest_info) as stream:
                if _stream_sha256(stream) != _sha256(manifest_path):
                    raise WebviewReleaseVerificationError(
                        "Portable archive payload manifest SHA-256 mismatch"
                    )
    except (OSError, zipfile.BadZipFile) as exc:
        raise WebviewReleaseVerificationError(
            f"Unable to verify portable archive: {exc}"
        ) from exc
    return {
        "path": str(archive_path),
        "size": archive_path.stat().st_size,
        "sha256": _sha256(archive_path),
        "verified_files": len(expected),
    }


def verify_extracted_installer(
    payload_directory: Path,
    extraction_root: Path,
) -> dict[str, object]:
    """Compare every manifest-owned installer member with the source payload."""

    payload = payload_directory.resolve(strict=True)
    extracted = extraction_root.resolve(strict=True)
    if not (extracted / "Zvec.WebviewPreview.exe").is_file():
        nested = extracted / "Zvec-Webview-Preview"
        if not (nested / "Zvec.WebviewPreview.exe").is_file():
            raise WebviewReleaseVerificationError(
                "Extracted installer has no documented WebView payload root"
            )
        extracted = nested
    entries = _manifest_entries(payload)
    for relative, (size, digest) in entries.items():
        candidate = (extracted / Path(relative)).resolve()
        try:
            candidate.relative_to(extracted)
        except ValueError as exc:
            raise WebviewReleaseVerificationError(
                f"Extracted installer path escapes its root: {relative}"
            ) from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise WebviewReleaseVerificationError(
                f"Extracted installer file is missing or unsafe: {relative}"
            )
        if candidate.stat().st_size != size or _sha256(candidate) != digest:
            raise WebviewReleaseVerificationError(
                f"Extracted installer payload mismatch: {relative}"
            )
    source_manifest = payload / MANIFEST_FILE
    extracted_manifest = extracted / MANIFEST_FILE
    if not extracted_manifest.is_file() or _sha256(extracted_manifest) != _sha256(
        source_manifest
    ):
        raise WebviewReleaseVerificationError(
            "Extracted installer payload manifest does not match"
        )
    expected = set(entries) | {MANIFEST_FILE}
    actual = {
        path.relative_to(extracted).as_posix()
        for path in extracted.rglob("*")
        if path.is_file()
    }
    unexpected = sorted(actual - expected - _ALLOWED_NSIS_SUPPORT_FILES)
    if unexpected:
        raise WebviewReleaseVerificationError(
            f"Extracted installer contains unexpected files: {unexpected[:5]}"
        )
    return {
        "payload_root": str(extracted),
        "verified_files": len(entries) + 1,
        "support_files": sorted(actual & _ALLOWED_NSIS_SUPPORT_FILES),
    }


def verify_installer_archive(
    payload_directory: Path,
    installer: Path,
    seven_zip: Path,
) -> dict[str, object]:
    """Use 7-Zip to inspect NSIS contents without executing the installer."""

    installer_path = installer.resolve(strict=True)
    extractor = seven_zip.resolve(strict=True)
    with TemporaryDirectory(prefix="zvec-webview-installer-verify-") as temporary:
        extraction_root = Path(temporary)
        completed = subprocess.run(
            [
                str(extractor),
                "x",
                "-y",
                f"-o{extraction_root}",
                str(installer_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-1000:]
            raise WebviewReleaseVerificationError(
                f"7-Zip could not extract the NSIS installer: {detail}"
            )
        comparison = verify_extracted_installer(
            payload_directory,
            extraction_root,
        )
    return {
        "path": str(installer_path),
        "size": installer_path.stat().st_size,
        "sha256": _sha256(installer_path),
        "verified_files": comparison["verified_files"],
        "installed": False,
    }


def verify_release(
    *,
    payload_directory: Path,
    portable_archive: Path,
    installer: Path,
    seven_zip: Path,
) -> dict[str, object]:
    payload = payload_directory.resolve(strict=True)
    manifest = verify_payload_manifest(payload)
    return {
        "status": "ok",
        "target_runtime": manifest["target_runtime"],
        "version": manifest["version"],
        "payload": {
            "path": str(payload),
            "file_count": manifest["file_count"],
            "total_size": manifest["total_size"],
        },
        "portable": verify_portable_archive(payload, portable_archive),
        "installer": verify_installer_archive(payload, installer, seven_zip),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", type=Path, required=True)
    parser.add_argument("--portable", type=Path, required=True)
    parser.add_argument("--installer", type=Path, required=True)
    parser.add_argument("--seven-zip", type=Path, required=True)
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path for an atomic machine-readable verification report.",
    )
    parser.add_argument(
        "--output-checksums",
        type=Path,
        help=(
            "Optional SHA256SUMS.txt covering the verified portable archive, "
            "installer, and JSON report. Requires --output-json."
        ),
    )
    return parser


def _write_report(path: Path, report: dict[str, object]) -> None:
    output = path.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def write_release_checksums(path: Path, artifacts: Sequence[Path]) -> None:
    """Atomically write basename-only SHA-256 entries for verified artifacts."""

    output = path.expanduser().absolute()
    resolved_artifacts: list[Path] = []
    names: set[str] = set()
    for artifact in artifacts:
        resolved = artifact.expanduser().absolute().resolve(strict=True)
        if not resolved.is_file() or resolved.is_symlink():
            raise WebviewReleaseVerificationError(
                f"Release artifact is missing or unsafe: {resolved}"
            )
        if resolved.name in names:
            raise WebviewReleaseVerificationError(
                f"Duplicate release artifact basename: {resolved.name}"
            )
        if any(character in resolved.name for character in "\r\n/\\"):
            raise WebviewReleaseVerificationError(
                f"Unsafe release artifact basename: {resolved.name!r}"
            )
        names.add(resolved.name)
        resolved_artifacts.append(resolved)
    if output.name in names:
        raise WebviewReleaseVerificationError(
            "Checksum output cannot overwrite a verified release artifact"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{_sha256(artifact)}  {artifact.name}\n"
        for artifact in sorted(
            resolved_artifacts,
            key=lambda item: item.name.casefold(),
        )
    ]
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        temporary.write_text("".join(lines), encoding="utf-8", newline="\n")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = verify_release(
            payload_directory=args.payload,
            portable_archive=args.portable,
            installer=args.installer,
            seven_zip=args.seven_zip,
        )
        if args.output_json is not None:
            _write_report(args.output_json, report)
        if args.output_checksums is not None:
            if args.output_json is None:
                raise WebviewReleaseVerificationError(
                    "--output-checksums requires --output-json"
                )
            write_release_checksums(
                args.output_checksums,
                (args.portable, args.installer, args.output_json),
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (
        OSError,
        subprocess.SubprocessError,
        WebviewPreviewPackagingError,
        WebviewReleaseVerificationError,
    ) as exc:
        print(f"WebView release verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
