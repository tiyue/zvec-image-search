"""Quick batch tag CLI for Zvec.

Two modes:
  1. Gateway mode (recommended, no token needed):
     Copy the URL from the Zvec WebView address bar and pass it via --gateway.
     Example: python tools/quick_tag.py --gateway "http://127.0.0.1:5635/AbC.../" \
         --folder "D:/images/cosplay/JK" --add "JK" "制服"

  2. Direct backend mode:
     python tools/quick_tag.py --backend-url http://127.0.0.1:8765 --token <TOKEN> \
         --folder "D:/images/cosplay/JK" --add "JK" "制服"

Operations:
    --add TAG [TAG ...]       Add tags to every image in the folder.
    --remove TAG [TAG ...]    Remove tags from every image in the folder.
    --replace TAG [TAG ...]   Replace all manual tags with these tags.
    --include-subfolders      Also apply to images in subfolders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

_TERMINAL_STATUSES = frozenset(
    {"succeeded", "partial", "needs_attention", "failed", "cancelled"}
)
_POLL_INTERVAL_SECONDS = 1.0
_MAX_POLL_SECONDS = 300.0
_FOLDER_LIST_PAGE_SIZE = 500


class QuickTagError(Exception):
    """User-facing error with a clear message."""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class _ApiClient:
    """HTTP client that works with both the direct backend and the WebView gateway."""

    def __init__(self, base_url: str, token: str | None = None) -> None:
        # Normalize: strip trailing slash, ensure gateway paths end with /
        self.base_url = base_url.rstrip("/")
        self.token = token
        # Detect gateway mode: gateway URLs have a token path segment
        # e.g. http://127.0.0.1:5635/AbCdEf.../
        self._is_gateway = token is None

    def _url(self, path: str) -> str:
        if self._is_gateway:
            # Gateway: base_url already includes the token path
            return f"{self.base_url}/api/{path}"
        # Direct backend: /v1/ prefix
        return f"{self.base_url}/v1/{path}"

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a request and return the parsed JSON body."""
        url = self._url(path)
        data: bytes | None = None
        headers: dict[str, str] = {"User-Agent": "zvec-quick-tag/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(data))
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, OSError):
                error_body = {}
            error = error_body.get("error", {})
            code = error.get("code", f"HTTP {exc.code}")
            message = error.get("message", str(exc))
            raise QuickTagError(f"Backend error [{code}]: {message}") from exc
        except urllib.error.URLError as exc:
            raise QuickTagError(
                "Cannot reach the Zvec backend. Is the desktop app running?\n"
                f"  {exc.reason}"
            ) from exc
        except OSError as exc:
            raise QuickTagError(f"Connection error: {exc}") from exc

    def submit_job(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        """Submit a job and return the unwrapped job snapshot."""
        response = self.request(
            "jobs",
            method="POST",
            body={"command": command, "params": params},
        )
        return response.get("job", response)

    def poll_job(self, job_id: str) -> dict[str, Any]:
        """Poll a job until it reaches a terminal status."""
        deadline = time.monotonic() + _MAX_POLL_SECONDS
        last_progress = ""
        while True:
            response = self.request(f"jobs/{job_id}")
            job = response.get("job", response)
            status = job.get("status", "unknown")
            progress = (job.get("progress") or {}).get("message", "")
            if progress and progress != last_progress:
                print(f"  {progress}", file=sys.stderr)
                last_progress = progress
            if status in _TERMINAL_STATUSES:
                return job
            if time.monotonic() >= deadline:
                raise QuickTagError(
                    f"Job {job_id} timed out after {_MAX_POLL_SECONDS:.0f}s "
                    f"(last status: {status})"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Library & folder resolution
# ---------------------------------------------------------------------------


def _resolve_folder_key(
    client: _ApiClient,
    folder: Path,
    library_id: str | None,
) -> tuple[str, str]:
    """Find the folder_key and library_id for a local folder path.

    Returns (library_id, folder_key).
    """
    # 1. Get libraries
    job = client.submit_job("libraries", {})
    job = client.poll_job(job["id"])
    if job["status"] not in {"succeeded", "partial", "needs_attention"}:
        error = job.get("error", {})
        raise QuickTagError(
            f"Failed to list libraries: {error.get('message', job['status'])}"
        )
    catalog = job.get("result", {})
    libraries: list[dict[str, Any]] = catalog.get("libraries", [])
    if not libraries:
        raise QuickTagError("No libraries configured. Index some images first.")

    # If library_id is specified, use only that one
    if library_id:
        matches = [lib for lib in libraries if lib.get("id") == library_id]
        if not matches:
            available = ", ".join(lib.get("id", "?") for lib in libraries)
            raise QuickTagError(
                f"Library {library_id} not found. Available: {available}"
            )
        libraries = matches

    # 2. Find which library contains the folder
    resolved_folder = folder.expanduser().resolve()
    matched_library: dict[str, Any] | None = None
    matched_root: str | None = None

    for lib in libraries:
        image_root_raw = lib.get("image_root")
        if not image_root_raw:
            continue
        image_root = Path(image_root_raw).expanduser().resolve()
        try:
            resolved_folder.relative_to(image_root)
        except ValueError:
            continue
        matched_library = lib
        matched_root = str(image_root)
        break

    if matched_library is None:
        lib_paths = "\n".join(
            f"  - {lib.get('image_root', '(none)')}" for lib in libraries
        )
        raise QuickTagError(
            f"Folder is not under any library image root:\n"
            f"  Folder: {resolved_folder}\n"
            f"  Library roots:\n{lib_paths}"
        )

    # 3. Compute the relative folder path
    relative_folder = PurePosixPath(
        str(resolved_folder.relative_to(Path(matched_root))).replace("\\", "/")
    )

    # 4. List all folders under this library to find the matching folder_key
    lib_id = matched_library["id"]
    offset = 0
    while True:
        folder_job = client.submit_job(
            "folder_list",
            {
                "library_id": lib_id,
                "offset": offset,
                "limit": _FOLDER_LIST_PAGE_SIZE,
            },
        )
        folder_job = client.poll_job(folder_job["id"])
        if folder_job["status"] not in {"succeeded", "partial", "needs_attention"}:
            error = folder_job.get("error", {})
            raise QuickTagError(
                f"Failed to list folders: {error.get('message', folder_job['status'])}"
            )
        data = folder_job.get("result", {})
        folders: list[dict[str, Any]] = data.get("folders", [])

        # Search for matching relative_folder
        target = str(relative_folder)
        for folder_info in folders:
            if folder_info.get("relative_folder") == target:
                return lib_id, folder_info["folder_key"]

        if not data.get("has_more", False):
            break
        offset += _FOLDER_LIST_PAGE_SIZE

    raise QuickTagError(
        f"Folder not found in library {lib_id}.\n"
        f"  Relative path: {target}\n"
        f"  Make sure images in this folder have been indexed first."
    )


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Quickly add, remove, or replace tags on images in a folder.",
    )
    parser.add_argument(
        "--folder",
        required=True,
        type=Path,
        help="Local folder path containing images to tag.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--add",
        nargs="+",
        metavar="TAG",
        help="Tags to add to every image in the folder.",
    )
    group.add_argument(
        "--remove",
        nargs="+",
        metavar="TAG",
        help="Tags to remove from every image in the folder.",
    )
    group.add_argument(
        "--replace",
        nargs="+",
        metavar="TAG",
        help="Replace all existing manual tags with these tags.",
    )
    parser.add_argument(
        "--include-subfolders",
        action="store_true",
        help="Also apply tags to images in subfolders.",
    )
    parser.add_argument(
        "--gateway",
        help=(
            "WebView gateway URL (from the address bar, e.g. "
            "http://127.0.0.1:5635/AbCdEf.../). No token needed."
        ),
    )
    parser.add_argument(
        "--backend-url",
        default="http://127.0.0.1:8765",
        help="Direct backend HTTP address (default: http://127.0.0.1:8765).",
    )
    parser.add_argument(
        "--token",
        help="Bearer token for direct backend mode (default: ZVEC_BACKEND_TOKEN env).",
    )
    parser.add_argument(
        "--library-id",
        help="Library ID to use (auto-detected if omitted).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Build API client: gateway mode or direct backend mode
    if args.gateway:
        client = _ApiClient(args.gateway)
        print(f"Using gateway: {args.gateway}", file=sys.stderr)
    else:
        token = args.token or os.getenv("ZVEC_BACKEND_TOKEN", "").strip()
        if not token:
            print(
                "Error: No backend token provided.\n"
                "  Use --gateway <URL> (from WebView address bar), or\n"
                "  set ZVEC_BACKEND_TOKEN / use --token for direct backend mode.",
                file=sys.stderr,
            )
            return 1
        client = _ApiClient(args.backend_url, token=token)

    # Determine operation and tags
    if args.add:
        operation = "add"
        tags = args.add
    elif args.remove:
        operation = "remove"
        tags = args.remove
    else:
        operation = "replace_manual"
        tags = args.replace or []

    folder = args.folder
    if not folder.is_dir():
        print(f"Error: Folder does not exist: {folder}", file=sys.stderr)
        return 1

    try:
        # Resolve folder to folder_key
        print(f"Resolving folder: {folder}", file=sys.stderr)
        library_id, folder_key = _resolve_folder_key(
            client, folder, args.library_id
        )
        print(f"  Library: {library_id}", file=sys.stderr)

        # Submit manual_tag_batch
        verb = {"add": "Adding", "remove": "Removing", "replace_manual": "Replacing"}
        print(
            f"\n{verb[operation]} tags: {', '.join(tags)}",
            file=sys.stderr,
        )

        job = client.submit_job(
            "manual_tag_batch",
            {
                "library_id": library_id,
                "selection": {
                    "mode": "folder",
                    "folder_key": folder_key,
                    "include_subfolders": args.include_subfolders,
                },
                "operation": operation,
                "tags": tags,
            },
        )
        job = client.poll_job(job["id"])

        # Print result
        result = job.get("result") or {}
        status = job.get("status", "unknown")

        if status == "failed":
            error = job.get("error", {})
            print(
                f"\nTag operation failed: {error.get('message', 'Unknown error')}",
                file=sys.stderr,
            )
            return 1

        updated = result.get("updated", 0)
        unchanged = result.get("unchanged", 0)
        failed = result.get("failed", 0)
        total = result.get("selected", result.get("processed", 0))

        print(f"\nDone. {operation} completed.", file=sys.stderr)
        print(f"  Selected: {total}")
        print(f"  Updated:  {updated}")
        print(f"  Unchanged:{unchanged}")
        if failed:
            print(f"  Failed:   {failed}")

        needs_attention = result.get("needs_attention", False)
        if needs_attention:
            print(
                "\n  Warning: Some tags may be inconsistent across libraries.",
                file=sys.stderr,
            )

        if failed:
            return 2
        if updated == 0 and unchanged == 0:
            return 3
        return 0

    except QuickTagError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
