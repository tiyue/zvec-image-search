"""Validate a GitHub release request without PowerShell or Docker."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from scripts.verify_search_quality_gate import verify_release_gate
    from scripts.webview_preview_packaging import public_version, read_project_version
else:
    from verify_search_quality_gate import verify_release_gate
    from webview_preview_packaging import public_version, read_project_version

_SEMVER = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?P<prerelease>-(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ReleaseRequestError(ValueError):
    """A requested release violates an explicit publication policy."""


def _boolean(value: str, label: str) -> bool:
    normalized = value.strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ReleaseRequestError(f"{label} must be true or false")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _license_present(root: Path) -> bool:
    return any(
        path.is_file() and re.match(r"(?i)^licen[cs]e(?:\.|$)", path.name)
        for path in root.iterdir()
    )


def _tag_commit(root: Path, tag_ref: str) -> str:
    completed = subprocess.run(
        ["git", "rev-list", "-n", "1", f"{tag_ref}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise ReleaseRequestError(f"Unable to resolve release tag {tag_ref}")
    return completed.stdout.strip().casefold()


def validate_release_request(
    *,
    repository_root: Path,
    version: str,
    requested_prerelease: bool,
    search_quality_gate_path: str,
    allow_uncertified_search_quality: bool,
    github_ref: str,
    github_sha: str,
) -> dict[str, str]:
    root = repository_root.resolve()
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ReleaseRequestError("version must be SemVer without build metadata")
    suffix = (match.group("prerelease") or "").removeprefix("-")
    for identifier in suffix.split(".") if suffix else ():
        if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
            raise ReleaseRequestError(
                "Numeric prerelease identifiers cannot contain leading zeroes"
            )
    version_prerelease = bool(suffix)
    if version_prerelease != requested_prerelease:
        raise ReleaseRequestError(
            "prerelease must exactly match the SemVer prerelease suffix"
        )
    if version_prerelease:
        raise ReleaseRequestError("Stable releases require a SemVer without a suffix")
    if read_project_version(root) != version:
        raise ReleaseRequestError("version must exactly match pyproject.toml")

    expected_ref = f"refs/tags/v{version}"
    exact_tag_ref = github_ref == expected_ref
    if exact_tag_ref and _tag_commit(root, expected_ref) != github_sha.casefold():
        raise ReleaseRequestError(
            "The release tag does not resolve to the workflow commit"
        )
    quality_text = search_quality_gate_path.strip()
    if quality_text and allow_uncertified_search_quality:
        raise ReleaseRequestError(
            "Use either a search-quality gate or the explicit uncertified option, "
            "not both"
        )
    if not quality_text and not allow_uncertified_search_quality:
        raise ReleaseRequestError(
            "A formal search-quality gate is required unless the uncertified "
            "option is explicit"
        )
    normalized_quality_path = ""
    quality_certified = False
    if quality_text:
        supplied = Path(quality_text)
        resolved = (
            (root / supplied).resolve()
            if not supplied.is_absolute()
            else supplied.resolve()
        )
        if not _is_within(resolved, root) or not resolved.is_file():
            raise ReleaseRequestError(
                "search_quality_gate_path must name a file inside the repository"
            )
        verify_release_gate(resolved, root)
        normalized_quality_path = resolved.relative_to(root).as_posix()
        quality_certified = True

    return {
        "exact_tag_ref": str(exact_tag_ref).lower(),
        "license_present": str(_license_present(root)).lower(),
        "search_quality_certified": str(quality_certified).lower(),
        "search_quality_gate_path": normalized_quality_path,
        "public_version": public_version(version),
        "version_prerelease": str(version_prerelease).lower(),
    }


def _append_outputs(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ReleaseRequestError(f"Unsafe multiline GitHub output: {key}")
            stream.write(f"{key}={value}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--version", required=True)
    parser.add_argument("--prerelease", required=True)
    parser.add_argument("--search-quality-gate-path", default="")
    parser.add_argument("--allow-uncertified-search-quality", required=True)
    parser.add_argument("--github-ref", required=True)
    parser.add_argument("--github-sha", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        values = validate_release_request(
            repository_root=args.repository_root,
            version=args.version,
            requested_prerelease=_boolean(args.prerelease, "prerelease"),
            search_quality_gate_path=args.search_quality_gate_path,
            allow_uncertified_search_quality=_boolean(
                args.allow_uncertified_search_quality,
                "allow_uncertified_search_quality",
            ),
            github_ref=args.github_ref,
            github_sha=args.github_sha,
        )
        _append_outputs(args.github_output, values)
        if args.summary is not None:
            summary = [
                "### YaoLens release request",
                "",
                f"- Machine version: `{args.version}`",
                f"- Public version: `{values['public_version']}`",
                f"- Exact version tag: `{values['exact_tag_ref']}`",
                f"- Repository license present: `{values['license_present']}`",
                f"- Search quality certified: `{values['search_quality_certified']}`",
                "- WebView runtime: `CPython / PyInstaller / pywebview / win-x64`",
                "- PowerShell, .NET, WPF, Docker required: `false`",
            ]
            args.summary.write_text("\n".join(summary) + "\n", encoding="utf-8")
    except (OSError, ReleaseRequestError, ValueError) as exc:
        print(f"Release request validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
