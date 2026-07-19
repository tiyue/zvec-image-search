"""Download and verify the pinned portable NSIS toolchain without Chocolatey.

The desktop build remains orchestrated by Python.  NSIS itself is a native
compiler, but obtaining it must not require PowerShell, .NET, an installer, or
machine-wide state.  The archive size and both published MD5 plus pinned
SHA-256 are checked before any member is extracted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

NSIS_VERSION = "3.12"
NSIS_ARCHIVE_NAME = f"nsis-{NSIS_VERSION}.zip"
NSIS_ARCHIVE_URL = (
    f"https://downloads.sourceforge.net/nsis/{NSIS_ARCHIVE_NAME}?download"
)
NSIS_ARCHIVE_SIZE = 2_362_938
# SourceForge publishes this MD5 in the project RSS feed.  SHA-256 is pinned
# here as the stronger immutable project-side build input.
NSIS_ARCHIVE_MD5 = "757c22153dd8b90f5e297310d9966997"
NSIS_ARCHIVE_SHA256 = "56581f90db321581c5381193d796fffcf2d24b2f8fed2160a6c6a3baa67f2c4f"
PROVISION_MARKER = ".zvec-nsis-provision.json"


class NsisProvisionError(RuntimeError):
    """The pinned archive could not be downloaded, verified, or extracted."""


@dataclass(frozen=True, slots=True)
class ArchiveIdentity:
    size: int
    md5: str
    sha256: str


@dataclass(frozen=True, slots=True)
class NsisProvisionResult:
    output_directory: Path
    makensis_path: Path
    archive_identity: ArchiveIdentity
    reused: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "ok",
            "version": NSIS_VERSION,
            "output_directory": str(self.output_directory),
            "makensis_path": str(self.makensis_path),
            "archive": {
                "size": self.archive_identity.size,
                "md5": self.archive_identity.md5,
                "sha256": self.archive_identity.sha256,
            },
            "reused": self.reused,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Provision the pinned portable NSIS compiler without PowerShell, "
            "Chocolatey, .NET, or a system-wide installation."
        )
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=Path("build") / "tools" / f"nsis-{NSIS_VERSION}",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        help="Use an existing pinned archive instead of downloading it.",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        help="Optionally append makensis_path for a GitHub Actions step.",
    )
    return parser


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_archive(path: Path) -> ArchiveIdentity:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise NsisProvisionError(f"NSIS archive is not a file: {resolved}")
    return ArchiveIdentity(
        size=resolved.stat().st_size,
        md5=_hash_file(resolved, "md5"),  # noqa: S324 - vendor-published checksum
        sha256=_hash_file(resolved, "sha256"),
    )


def verify_archive(
    path: Path,
    *,
    expected_size: int = NSIS_ARCHIVE_SIZE,
    expected_md5: str = NSIS_ARCHIVE_MD5,
    expected_sha256: str = NSIS_ARCHIVE_SHA256,
) -> ArchiveIdentity:
    """Fail closed unless an archive exactly matches the pinned identity."""

    identity = inspect_archive(path)
    expected = ArchiveIdentity(
        size=expected_size,
        md5=expected_md5.casefold(),
        sha256=expected_sha256.casefold(),
    )
    if identity != expected:
        raise NsisProvisionError(
            f"NSIS archive identity mismatch: actual={identity}, expected={expected}"
        )
    return identity


def _safe_members(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    members = tuple(archive.infolist())
    if not members:
        raise NsisProvisionError("NSIS archive is empty")
    for member in members:
        normalized = member.filename.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or ":" in path.parts[0]
        ):
            raise NsisProvisionError(
                f"NSIS archive contains an unsafe path: {member.filename}"
            )
        unix_mode = member.external_attr >> 16
        if stat.S_ISLNK(unix_mode):
            raise NsisProvisionError(
                f"NSIS archive contains a symbolic link: {member.filename}"
            )
    return members


def extract_archive(archive_path: Path, destination: Path) -> Path:
    """Safely extract and return the resolved portable makensis executable."""

    resolved_archive = archive_path.expanduser().resolve(strict=True)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(resolved_archive, "r") as archive:
            members = _safe_members(archive)
            damaged = archive.testzip()
            if damaged is not None:
                raise NsisProvisionError(
                    f"NSIS archive CRC verification failed: {damaged}"
                )
            archive.extractall(destination, members=members)
    except (OSError, zipfile.BadZipFile) as exc:
        raise NsisProvisionError(f"Unable to extract NSIS archive: {exc}") from exc

    candidates = tuple(
        candidate.resolve()
        for candidate in destination.rglob("makensis.exe")
        if candidate.is_file() and candidate.stat().st_size > 0
    )
    canonical = tuple(
        candidate
        for candidate in candidates
        if candidate.relative_to(destination.resolve()).parts
        == (f"nsis-{NSIS_VERSION}", "makensis.exe")
    )
    if len(canonical) == 1:
        # The official portable archive also contains Bin/makensis.exe.  The
        # documented root executable is the stable build entry and must win
        # deterministically instead of treating the vendor archive as
        # ambiguous.
        return canonical[0]
    if len(candidates) == 1:
        # Retain support for small verified fixture/vendor archives that ship
        # a single compiler under a different top-level directory.
        return candidates[0]
    if not candidates:
        raise NsisProvisionError(
            "Pinned NSIS archive does not contain a non-empty makensis.exe"
        )
    raise NsisProvisionError(
        "Pinned NSIS archive contains multiple compilers without the expected "
        f"nsis-{NSIS_VERSION}/makensis.exe entry"
    )


def _download_archive(destination: Path, *, attempts: int = 5) -> None:
    error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.unlink(missing_ok=True)
        request = urllib.request.Request(
            NSIS_ARCHIVE_URL,
            headers={
                "User-Agent": "Zvec-Python-Desktop-Build/0.4",
                "Accept": "application/octet-stream",
            },
        )
        try:
            with (
                urllib.request.urlopen(request, timeout=90) as response,  # noqa: S310
                temporary.open("wb") as stream,
            ):
                shutil.copyfileobj(response, stream, length=1024 * 1024)
            os.replace(temporary, destination)
            return
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            error = exc
            temporary.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(min(8, attempt * 2))
    raise NsisProvisionError(f"Unable to download pinned NSIS {NSIS_VERSION}: {error}")


def _marker_matches(output: Path) -> NsisProvisionResult | None:
    marker = output / PROVISION_MARKER
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    relative = payload.get("makensis_relative_path")
    if (
        payload.get("version") != NSIS_VERSION
        or payload.get("archive_sha256") != NSIS_ARCHIVE_SHA256
        or not isinstance(relative, str)
    ):
        return None
    makensis = (output / relative).resolve()
    try:
        makensis.relative_to(output.resolve())
    except ValueError:
        return None
    if not makensis.is_file() or makensis.stat().st_size <= 0:
        return None
    return NsisProvisionResult(
        output_directory=output.resolve(),
        makensis_path=makensis,
        archive_identity=ArchiveIdentity(
            NSIS_ARCHIVE_SIZE,
            NSIS_ARCHIVE_MD5,
            NSIS_ARCHIVE_SHA256,
        ),
        reused=True,
    )


def provision_nsis(
    output_directory: Path,
    *,
    archive_path: Path | None = None,
) -> NsisProvisionResult:
    output = output_directory.expanduser().absolute()
    existing = _marker_matches(output)
    if existing is not None:
        return existing
    if output.exists():
        raise NsisProvisionError(
            f"Refusing to replace an unverified output directory: {output}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="zvec-nsis-download-",
        dir=output.parent,
    ) as download_directory:
        selected_archive = (
            archive_path.expanduser().resolve(strict=True)
            if archive_path is not None
            else Path(download_directory) / NSIS_ARCHIVE_NAME
        )
        if archive_path is None:
            _download_archive(selected_archive)
        identity = verify_archive(selected_archive)

        staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
        try:
            makensis = extract_archive(selected_archive, staging / "payload")
            relative = makensis.relative_to(staging).as_posix()
            (staging / PROVISION_MARKER).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": NSIS_VERSION,
                        "archive_sha256": identity.sha256,
                        "makensis_relative_path": relative,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(staging, output)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    result = _marker_matches(output)
    if result is None:
        raise NsisProvisionError("Provisioned NSIS marker could not be verified")
    return NsisProvisionResult(
        output_directory=result.output_directory,
        makensis_path=result.makensis_path,
        archive_identity=identity,
        reused=False,
    )


def _write_github_output(path: Path, result: NsisProvisionResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"makensis_path={result.makensis_path}\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = provision_nsis(
            args.output_directory,
            archive_path=args.archive,
        )
        if args.github_output is not None:
            _write_github_output(args.github_output, result)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except (NsisProvisionError, OSError, ValueError) as exc:
        print(f"NSIS provision failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
