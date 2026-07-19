"""Generate checksums, SPDX SBOM, and SLSA provenance for Python desktop builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

if __package__:
    from scripts.python_preview_packaging import (
        MANIFEST_FILE,
        PRODUCT_NAME,
        TARGET_RUNTIME,
        PreviewPackagingError,
        read_project_version,
        verify_payload_manifest,
    )
else:
    from python_preview_packaging import (
        MANIFEST_FILE,
        PRODUCT_NAME,
        TARGET_RUNTIME,
        PreviewPackagingError,
        read_project_version,
        verify_payload_manifest,
    )

_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[A-Za-z0-9_.+!-]+)"
    r"(?:;\s*(?P<marker>.+))?$"
)
_PROJECT_NAME = re.compile(r"(?m)^\s*name\s*=\s*[\"'](?P<name>[^\"']+)[\"']\s*$")
_SHA256_NAME: Final = "SHA256SUMS.txt"
_SBOM_SUFFIX: Final = ".spdx.json"
_PROVENANCE_SUFFIX: Final = ".provenance.json"
_RELEASE_MANIFEST: Final = "desktop-release.json"
_MAX_QUALITY_REPORT_BYTES: Final = 32 * 1024 * 1024


class ReleaseMaterialsError(RuntimeError):
    """A release input is unsafe, inconsistent, or incomplete."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ReleaseMaterialsError(f"Unable to write {path}: {exc}") from exc


def _generated_at() -> str:
    source_epoch = os.getenv("SOURCE_DATE_EPOCH", "").strip()
    if source_epoch:
        try:
            instant = datetime.fromtimestamp(int(source_epoch), tz=timezone.utc)
        except (OverflowError, ValueError) as exc:
            raise ReleaseMaterialsError(
                "SOURCE_DATE_EPOCH must be a valid Unix timestamp"
            ) from exc
    else:
        instant = datetime.now(tz=timezone.utc)
    return instant.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _project_name(root: Path) -> str:
    try:
        content = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseMaterialsError(f"Unable to read pyproject.toml: {exc}") from exc
    match = _PROJECT_NAME.search(content)
    if match is None:
        raise ReleaseMaterialsError("pyproject.toml does not define project.name")
    return match.group("name")


def _locked_dependencies(
    root: Path,
    filename: str,
    *,
    allowed_include: str | None = None,
) -> list[dict[str, str]]:
    lock_path = root / filename
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseMaterialsError(f"Unable to read {lock_path}: {exc}") from exc
    dependencies: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line_number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if allowed_include is not None and line == f"-r {allowed_include}":
            continue
        match = _PIN.fullmatch(line)
        if match is None:
            raise ReleaseMaterialsError(
                f"{filename}:{line_number} is not an exact name==version pin"
            )
        name = match.group("name")
        marker = (match.group("marker") or "").strip()
        identity = (name.casefold().replace("_", "-"), marker)
        if identity in seen:
            raise ReleaseMaterialsError(
                f"{filename} contains a duplicate pin for {name!r}"
            )
        seen.add(identity)
        dependencies.append(
            {
                "name": name,
                "version": match.group("version"),
                "marker": marker,
            }
        )
    if not dependencies:
        raise ReleaseMaterialsError(f"{filename} contains no dependencies")
    return dependencies


def _git_revision(root: Path, expected: str | None) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ReleaseMaterialsError("Git revision is unavailable")
    revision = completed.stdout.strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ReleaseMaterialsError("Git returned an invalid revision")
    if expected is not None and revision != expected.strip().casefold():
        raise ReleaseMaterialsError(
            f"Build revision {revision} does not match expected revision {expected}"
        )
    return revision


def _file_record(path: Path) -> dict[str, Any]:
    return {
        "file": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _copy_quality_report(source: Path, output_root: Path) -> Path:
    resolved = source.resolve(strict=True)
    if not resolved.is_file() or resolved.stat().st_size > _MAX_QUALITY_REPORT_BYTES:
        raise ReleaseMaterialsError("Search-quality report is missing or too large")
    target = output_root / "search-quality-comparison.json"
    try:
        shutil.copyfile(resolved, target)
    except OSError as exc:
        raise ReleaseMaterialsError(
            f"Unable to copy search-quality report: {exc}"
        ) from exc
    return target


def generate_release_materials(
    *,
    repository_root: Path,
    output_root: Path,
    expected_revision: str | None = None,
    signing_status: str = "unsigned",
    search_quality_report: Path | None = None,
) -> dict[str, Any]:
    """Verify one build and create deterministic, offline release sidecars."""

    root = repository_root.resolve()
    output = output_root.resolve()
    if signing_status not in {"unsigned", "authenticode"}:
        raise ReleaseMaterialsError("signing_status must be unsigned or authenticode")
    version = read_project_version(root)
    payload_directory = output / "Zvec-Desktop"
    try:
        payload = verify_payload_manifest(payload_directory)
    except PreviewPackagingError as exc:
        raise ReleaseMaterialsError(str(exc)) from exc
    if payload.get("product") != PRODUCT_NAME or payload.get("version") != version:
        raise ReleaseMaterialsError(
            "Payload product/version does not match the project"
        )
    if payload.get("signing_status") != signing_status:
        raise ReleaseMaterialsError(
            "Payload signing_status does not match the requested release status"
        )
    revision = _git_revision(root, expected_revision)
    generated_at = _generated_at()
    runtime_dependencies = _locked_dependencies(root, "requirements-lock.txt")
    build_dependencies = _locked_dependencies(
        root,
        "requirements-packaging.txt",
        allowed_include="requirements-lock.txt",
    )
    build_dependencies.extend(
        (
            {
                "name": "CPython",
                "version": "3.12",
                "marker": "win-x64 build interpreter",
                "purl": "pkg:generic/cpython@3.12",
            },
            {
                "name": "NSIS",
                "version": "3.12",
                "marker": "Windows installer compiler",
                "purl": "pkg:generic/nsis@3.12",
            },
        )
    )

    portable = output / f"Zvec-Desktop-{version}-{TARGET_RUNTIME}-portable.zip"
    installer_name = (
        f"Zvec-Desktop-{version}-{TARGET_RUNTIME}-setup.exe"
        if signing_status == "authenticode"
        else f"Zvec-Desktop-{version}-{TARGET_RUNTIME}-unsigned-setup.exe"
    )
    installer = output / installer_name
    required_artifacts = [portable, installer]
    for artifact in required_artifacts:
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            raise ReleaseMaterialsError(
                f"Required release artifact is missing: {artifact}"
            )

    quality_copy = (
        _copy_quality_report(search_quality_report, output)
        if search_quality_report is not None
        else None
    )
    artifact_records = [_file_record(path) for path in required_artifacts]
    if quality_copy is not None:
        artifact_records.append(_file_record(quality_copy))

    document_id = hashlib.sha256(
        (
            f"{version}\0{revision}\0{_sha256(payload_directory / MANIFEST_FILE)}"
        ).encode()
    ).hexdigest()
    root_spdx = "SPDXRef-Zvec-Desktop"
    packages: list[dict[str, Any]] = [
        {
            "SPDXID": root_spdx,
            "name": _project_name(root),
            "versionInfo": version,
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": "APPLICATION",
        }
    ]
    relationships: list[dict[str, str]] = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": root_spdx,
        }
    ]
    package_index = 0
    for dependency in runtime_dependencies:
        package_index += 1
        package_id = f"SPDXRef-Package-{package_index}"
        runtime_package: dict[str, Any] = {
            "SPDXID": package_id,
            "name": dependency["name"],
            "versionInfo": dependency["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": (
                        f"pkg:pypi/{dependency['name']}@{dependency['version']}"
                    ),
                }
            ],
        }
        if dependency["marker"]:
            runtime_package["comment"] = f"PEP 508 marker: {dependency['marker']}"
        packages.append(runtime_package)
        relationships.append(
            {
                "spdxElementId": root_spdx,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": package_id,
            }
        )

    for dependency in build_dependencies:
        package_index += 1
        package_id = f"SPDXRef-Package-{package_index}"
        build_package: dict[str, Any] = {
            "SPDXID": package_id,
            "name": dependency["name"],
            "versionInfo": dependency["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": "NOASSERTION",
            "copyrightText": "NOASSERTION",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": dependency.get(
                        "purl",
                        f"pkg:pypi/{dependency['name']}@{dependency['version']}",
                    ),
                }
            ],
        }
        if dependency["marker"]:
            build_package["comment"] = dependency["marker"]
        packages.append(build_package)
        relationships.append(
            {
                "spdxElementId": package_id,
                "relationshipType": "BUILD_DEPENDENCY_OF",
                "relatedSpdxElement": root_spdx,
            }
        )

    sbom_path = output / f"Zvec-Desktop-{version}{_SBOM_SUFFIX}"
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"Zvec Desktop {version} {TARGET_RUNTIME}",
        "documentNamespace": f"https://zvec.local/spdx/{version}/{document_id}",
        "creationInfo": {
            "created": generated_at,
            "creators": ["Tool: zvec-python-release-materials-1"],
        },
        "packages": packages,
        "relationships": relationships,
        "annotations": [
            {
                "annotationDate": generated_at,
                "annotationType": "OTHER",
                "annotator": "Tool: zvec-python-release-materials-1",
                "comment": (
                    "PowerShell, WPF, .NET, Docker, and excluded model SDKs are "
                    "rejected by the verified payload contract."
                ),
            }
        ],
    }
    _write_json(sbom_path, sbom)

    subjects = [
        {"name": record["file"], "digest": {"sha256": record["sha256"]}}
        for record in artifact_records + [_file_record(sbom_path)]
    ]
    provenance_path = output / f"Zvec-Desktop-{version}{_PROVENANCE_SUFFIX}"
    provenance = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subjects,
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://zvec.local/build/python-desktop-pyinstaller/v1",
                "externalParameters": {
                    "version": version,
                    "target_runtime": TARGET_RUNTIME,
                    "python_runtime": payload.get("python_runtime"),
                    "pyinstaller_version": payload.get("pyinstaller_version"),
                    "signing_status": signing_status,
                    "search_quality_certified": quality_copy is not None,
                },
                "internalParameters": {
                    "payload_manifest": MANIFEST_FILE,
                    "payload_file_count": payload.get("file_count"),
                    "payload_total_size": payload.get("total_size"),
                },
                "resolvedDependencies": [
                    {
                        "uri": "git+https://github.com/tiyue/zvec-image-search",
                        "digest": {"gitCommit": revision},
                    },
                    {
                        "uri": "requirements-lock.txt",
                        "digest": {"sha256": _sha256(root / "requirements-lock.txt")},
                    },
                    {
                        "uri": "requirements-packaging.txt",
                        "digest": {
                            "sha256": _sha256(root / "requirements-packaging.txt")
                        },
                    },
                ],
            },
            "runDetails": {
                "builder": {"id": "urn:zvec:builder:python-desktop-release:v1"},
                "metadata": {
                    "invocationId": os.getenv("GITHUB_RUN_ID", "local"),
                    "startedOn": generated_at,
                    "finishedOn": generated_at,
                },
            },
        },
    }
    _write_json(provenance_path, provenance)

    release_manifest_path = output / _RELEASE_MANIFEST
    release_manifest = {
        "schema_version": 2,
        "product": PRODUCT_NAME,
        "version": version,
        "target_runtime": TARGET_RUNTIME,
        "git_revision": revision,
        "signing_status": signing_status,
        "search_quality_certified": quality_copy is not None,
        "runtime": {
            "implementation": "CPython",
            "mode": "PyInstaller onedir",
            "docker_required": False,
            "powershell_required": False,
            "dotnet_required": False,
        },
        "payload": {
            "directory": payload_directory.name,
            "manifest": MANIFEST_FILE,
            "file_count": payload.get("file_count"),
            "total_size": payload.get("total_size"),
        },
        "artifacts": artifact_records,
        "supply_chain": {
            "sbom": _file_record(sbom_path),
            "provenance": _file_record(provenance_path),
            "python_lock": _file_record(root / "requirements-lock.txt"),
            "packaging_lock": _file_record(root / "requirements-packaging.txt"),
        },
    }
    _write_json(release_manifest_path, release_manifest)

    checksum_candidates = sorted(
        (
            path
            for path in output.iterdir()
            if path.is_file() and path.name != _SHA256_NAME
        ),
        key=lambda path: path.name.casefold(),
    )
    checksum_path = output / _SHA256_NAME
    try:
        checksum_path.write_text(
            "".join(f"{_sha256(path)}  {path.name}\n" for path in checksum_candidates),
            encoding="utf-8",
        )
    except OSError as exc:
        raise ReleaseMaterialsError(f"Unable to write checksums: {exc}") from exc

    return {
        "status": "ok",
        "release_manifest": str(release_manifest_path),
        "sbom": str(sbom_path),
        "provenance": str(provenance_path),
        "checksums": str(checksum_path),
        "artifacts": artifact_records,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-revision")
    parser.add_argument(
        "--signing-status",
        choices=("unsigned", "authenticode"),
        default="unsigned",
    )
    parser.add_argument("--search-quality-report", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_release_materials(
            repository_root=args.repository_root,
            output_root=args.output_root,
            expected_revision=args.expected_revision,
            signing_status=args.signing_status,
            search_quality_report=args.search_quality_report,
        )
    except (OSError, ReleaseMaterialsError) as exc:
        print(f"Python release material generation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
