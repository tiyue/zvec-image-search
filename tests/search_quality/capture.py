from __future__ import annotations

import argparse
import copy
import importlib
import ipaddress
import json
import math
import ntpath
import os
import posixpath
import re
import sys
import time
import uuid
import warnings
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from PIL import Image

quality_evaluate = importlib.import_module(
    "tests.search_quality.evaluate" if __package__ else "evaluate"
)
image_identity = importlib.import_module(
    "tests.search_quality.image_identity" if __package__ else "image_identity"
)
quality_split = importlib.import_module(
    "tests.search_quality.split" if __package__ else "split"
)

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}
SENSITIVE_TOKEN_ENV_MARKERS = ("API_KEY", "DASHSCOPE")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TOKEN_BYTES = 16 * 1024
MAX_QUERY_IMAGE_BYTES = 100 * 1024 * 1024
MAX_QUERY_IMAGE_PIXELS = 40_000_000
ALLOWED_QUERY_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".ico",
        ".dib",
        ".icns",
        ".sgi",
    }
)
ALLOWED_QUERY_IMAGE_FORMATS = frozenset(
    {"JPEG", "PNG", "WEBP", "BMP", "TIFF", "ICO", "ICNS", "SGI"}
)


class CaptureError(RuntimeError):
    pass


def _capture_split_metadata(dataset: dict[str, Any]) -> dict[str, Any] | None:
    """Carry a frozen query subset identity into its captured run artifact."""
    value = dataset.get("split")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("dataset split metadata must be an object")
    role = value.get("role")
    if role not in quality_split.ROLES:
        raise ValueError("dataset split role must be calibration or validation")
    manifest_fingerprint = value.get("manifest_fingerprint")
    if (
        value.get("kind") != quality_split.SPLIT_KIND
        or value.get("manifest_fingerprint_algorithm")
        != quality_split.SPLIT_FINGERPRINT_ALGORITHM
        or not isinstance(manifest_fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", manifest_fingerprint) is None
    ):
        raise ValueError("dataset split metadata is not bound to a valid manifest")
    return {
        "kind": quality_split.SPLIT_KIND,
        "role": role,
        "manifest_fingerprint": manifest_fingerprint,
        "manifest_fingerprint_algorithm": quality_split.SPLIT_FINGERPRINT_ALGORITHM,
    }


class BackendTransport(Protocol):
    request_count: int

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class BackendClient:
    """Minimal client for the authenticated persistent-backend JSON protocol."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base URL must be an absolute HTTP(S) URL")
        try:
            hostname = parsed.hostname
        except ValueError as exc:
            raise ValueError("base URL contains an invalid host") from exc
        if not hostname:
            raise ValueError("base URL must contain a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base URL must not contain embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base URL must not contain a query string or fragment")
        if parsed.scheme == "http" and not _is_loopback_host(hostname):
            raise ValueError(
                "plain HTTP backend URLs are only allowed for loopback hosts"
            )
        if not token.strip():
            raise ValueError("backend Bearer token must not be empty")
        if timeout <= 0:
            raise ValueError("HTTP timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._timeout = timeout
        self.request_count = 0

    @property
    def base_url(self) -> str:
        return self._base_url

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "zvec-search-quality-capture/1",
        }
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        request = Request(
            f"{self._base_url}/{path.lstrip('/')}",
            data=body,
            headers=headers,
            method=method,
        )
        self.request_count += 1
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                response_body = _read_limited_response(response)
        except HTTPError as exc:
            response_body = _read_limited_response(exc)
            raise CaptureError(_http_error_message(exc.code, response_body)) from exc
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            reason_name = reason.__class__.__name__ if reason is not None else "error"
            raise CaptureError(f"Could not reach the backend ({reason_name}).") from exc
        return _decode_json_object(response_body, "backend response")


def _validate_query_image_basics(path: Path) -> None:
    if path.suffix.lower() not in ALLOWED_QUERY_IMAGE_EXTENSIONS:
        raise CaptureError("query image has an unsupported file extension")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CaptureError("query image metadata could not be read") from exc
    if size < 1:
        raise CaptureError("query image must not be empty")
    if size > MAX_QUERY_IMAGE_BYTES:
        raise CaptureError("query image exceeded the capture size limit")


def _copy_query_image(source: BinaryIO, destination: BinaryIO) -> None:
    copied = 0
    while chunk := source.read(64 * 1024):
        copied += len(chunk)
        if copied > MAX_QUERY_IMAGE_BYTES:
            raise CaptureError("query image exceeded the capture size limit")
        destination.write(chunk)


def _verify_query_image(path: Path) -> None:
    _validate_query_image_basics(path)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                actual_format = (image.format or "").upper()
                width, height = image.size
                image.verify()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise CaptureError("query image failed Pillow validation") from exc
    if actual_format not in ALLOWED_QUERY_IMAGE_FORMATS:
        raise CaptureError("query image content format is not supported")
    if width < 1 or height < 1:
        raise CaptureError("query image dimensions must be positive")
    if width * height > MAX_QUERY_IMAGE_PIXELS:
        raise CaptureError("query image exceeded the pixel limit")


def _remove_staged_image(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        print(
            "capture warning: could not remove the staged query image; "
            "inspect the configured staging directory",
            file=sys.stderr,
        )


class QueryImageStager:
    """Copies a query image to a direct child of the backend query mount."""

    def __init__(self, host_root: str | Path, backend_root: str | None = None) -> None:
        root = Path(host_root).expanduser()
        try:
            self.host_root = root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("query staging directory does not exist") from exc
        if not self.host_root.is_dir():
            raise ValueError("query staging path must be a directory")
        self.backend_root = backend_root or str(self.host_root)
        if not _is_absolute_backend_path(self.backend_root):
            raise ValueError("backend query root must be an absolute path")

    @contextmanager
    def stage(self, source: Path) -> Iterator[str]:
        try:
            resolved_source = source.expanduser().resolve(strict=True)
        except OSError as exc:
            raise CaptureError("query image does not exist") from exc
        if not resolved_source.is_file():
            raise CaptureError("query image must be a file")

        _validate_query_image_basics(resolved_source)

        extension = resolved_source.suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,15}", extension):
            raise CaptureError("query image has an unsupported file extension")
        staged_name = f"capture-{uuid.uuid4().hex}{extension}"
        staged_path = self.host_root / staged_name
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_BINARY", 0)
            with resolved_source.open("rb") as source_stream:
                descriptor = os.open(staged_path, flags, 0o600)
                try:
                    destination_stream = os.fdopen(descriptor, "wb")
                except BaseException:
                    os.close(descriptor)
                    raise
                with destination_stream:
                    _copy_query_image(source_stream, destination_stream)
            if os.name != "nt":
                staged_path.chmod(0o600)
            if staged_path.resolve(strict=True).parent != self.host_root:
                raise CaptureError("staged query image escaped its staging directory")
            _verify_query_image(staged_path)
            yield _join_backend_path(self.backend_root, staged_name)
        finally:
            _remove_staged_image(staged_path)


def _read_limited_response(response: Any) -> bytes:
    value = response.read(MAX_RESPONSE_BYTES + 1)
    if len(value) > MAX_RESPONSE_BYTES:
        raise CaptureError("Backend response exceeded the capture size limit.")
    return value


def _decode_json_object(value: bytes, description: str) -> dict[str, Any]:
    try:
        payload = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"{description} was not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise CaptureError(f"{description} must be a JSON object.")
    return payload


def _http_error_message(status: int, value: bytes) -> str:
    code = "request_failed"
    message = "The backend rejected the request."
    try:
        payload = _decode_json_object(value, "backend error response")
    except CaptureError:
        payload = {}
    error = payload.get("error")
    if isinstance(error, dict):
        if isinstance(error.get("code"), str) and error["code"].strip():
            code = error["code"].strip()
        if isinstance(error.get("message"), str) and error["message"].strip():
            message = error["message"].strip()
    return f"Backend request failed with HTTP {status} ({code}): {message}"


def _is_absolute_backend_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _join_backend_path(root: str, name: str) -> str:
    if PureWindowsPath(root).is_absolute() and not root.startswith("/"):
        return ntpath.join(root, name)
    return posixpath.join(root, name)


def _read_dataset(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read dataset JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("dataset JSON root must be an object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _expect_job(payload: dict[str, Any]) -> dict[str, Any]:
    job = payload.get("job")
    if not isinstance(job, dict):
        raise CaptureError("Backend response did not contain a job object.")
    job_id = job.get("id")
    status = job.get("status")
    if not isinstance(job_id, str) or not job_id.strip():
        raise CaptureError("Backend job did not contain a valid id.")
    if not isinstance(status, str) or not status.strip():
        raise CaptureError("Backend job did not contain a valid status.")
    return job


def _wait_for_search(
    client: BackendTransport,
    params: dict[str, Any],
    *,
    poll_interval: float,
    job_timeout: float,
) -> tuple[dict[str, Any], float, int]:
    started_at = time.perf_counter()
    request_start = client.request_count
    submitted = client.request_json(
        "POST",
        "/v1/jobs",
        {"command": "search", "params": params},
    )
    job = _expect_job(submitted)
    job_id = str(job["id"])
    deadline = time.monotonic() + job_timeout
    while str(job["status"]) not in TERMINAL_JOB_STATUSES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CaptureError(f"Search job {job_id} timed out.")
        time.sleep(min(poll_interval, remaining))
        job = _expect_job(client.request_json("GET", f"/v1/jobs/{job_id}"))

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    status = str(job["status"])
    if status != "succeeded":
        error = job.get("error")
        error_code = "job_failed"
        error_message = f"Search job ended with status {status}."
        if isinstance(error, dict):
            if isinstance(error.get("code"), str) and error["code"].strip():
                error_code = error["code"].strip()
            if isinstance(error.get("message"), str) and error["message"].strip():
                error_message = error["message"].strip()
        raise CaptureError(f"Search job failed ({error_code}): {error_message}")
    result = job.get("result")
    if not isinstance(result, dict):
        raise CaptureError("Successful search job had no result object.")
    return result, elapsed_ms, client.request_count - request_start


def _number(value: Any, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CaptureError(f"{description} must be numeric.")
    result = float(value)
    if not math.isfinite(result):
        raise CaptureError(f"{description} must be finite.")
    return result


def _non_negative_integer(value: Any, description: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CaptureError(f"{description} must be a non-negative integer.")
    return value


def _image_id(hit: dict[str, Any]) -> str:
    library_id = hit.get("library_id")
    relative_path = hit.get("relative_path")
    if not isinstance(library_id, str) or not library_id.strip():
        raise CaptureError("Search result is missing library_id.")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise CaptureError("Search result is missing relative_path.")
    if (
        PurePosixPath(relative_path).is_absolute()
        or PureWindowsPath(relative_path).is_absolute()
    ):
        raise CaptureError("Search result relative_path must not be absolute.")
    normalized = relative_path.replace("\\", "/")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise CaptureError("Search result relative_path is not canonical.")
    return f"{library_id.strip()}:{normalized}"


def _capture_results(mode: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    raw_results = result.get("results")
    if not isinstance(raw_results, list) or any(
        not isinstance(value, dict) for value in raw_results
    ):
        raise CaptureError("Search result must contain an array of result objects.")
    captured = []
    for index, hit in enumerate(raw_results, start=1):
        image_id = _image_id(hit)
        try:
            digest = image_identity.normalize_sha256(
                hit.get("sha256"), f"result {index}"
            )
        except image_identity.ImageIdentityError as exc:
            raise CaptureError(str(exc)) from exc
        raw_score = _number(
            hit.get("raw_score", hit.get("distance")),
            f"result {index} raw_score",
        )
        normalized_score = _number(
            hit.get("normalized_score"),
            f"result {index} normalized_score",
        )
        confidence = _number(
            hit.get("confidence"),
            f"result {index} confidence",
        )
        rank = hit.get("rank", index)
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise CaptureError(f"result {index} rank must be a positive integer.")
        match_state = hit.get("match_state")
        if match_state not in {"high", "possible", "weak"}:
            raise CaptureError(f"result {index} has an invalid match_state.")
        rank_source = hit.get("rank_source")
        if rank_source not in {"image", "text", "fused"}:
            raise CaptureError(f"result {index} has an invalid rank_source.")
        fused_score = hit.get("fused_score")
        captured_hit: dict[str, Any] = {
            "image_id": image_id,
            "rank": rank,
            "score": confidence if mode == "combined" else raw_score,
            "raw_score": raw_score,
            "normalized_score": normalized_score,
            "confidence": confidence,
            "match_state": match_state,
            "rank_source": rank_source,
        }
        if digest is not None:
            captured_hit["sha256"] = digest
        if fused_score is not None:
            captured_hit["fused_score"] = _number(
                fused_score,
                f"result {index} fused_score",
            )
        for name in (
            "ranking_confidence",
            "image_confidence",
            "text_confidence",
            "rank_agreement",
        ):
            value = hit.get(name)
            if value is not None:
                captured_hit[name] = _number(value, f"result {index} {name}")
        for name in ("image_rank", "text_rank"):
            value = hit.get(name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise CaptureError(
                        f"result {index} {name} must be a positive integer."
                    )
                captured_hit[name] = value
        captured.append(captured_hit)
    identity_index = image_identity.ImageIdentityIndex()
    references = []
    try:
        for index, captured_hit in enumerate(captured, start=1):
            references.append(
                identity_index.add(captured_hit, f"captured result {index}")
            )
        identity_index.require_unique(references, "captured search results")
    except image_identity.ImageIdentityError as exc:
        raise CaptureError(str(exc)) from exc
    return captured


def _library_params(scope: dict[str, Any]) -> dict[str, Any]:
    mode = scope["mode"]
    library_ids = [str(value).strip() for value in scope["library_ids"]]
    if mode == "all_enabled":
        return {}
    if not library_ids:
        raise ValueError("selected library scope must contain at least one library id")
    return {"library_ids": library_ids}


def _query_image_path(
    item: dict[str, Any],
    dataset_directory: Path,
    query_source_roots: tuple[Path, ...],
) -> Path:
    value = item["query"]["image"]
    path = Path(str(value)).expanduser()
    if path.is_absolute():
        return path

    roots: list[Path] = []
    seen_roots: set[str] = set()
    for raw_root in (dataset_directory, *query_source_roots):
        try:
            root = raw_root.expanduser().resolve(strict=True)
        except OSError as exc:
            raise CaptureError("query image source root does not exist") from exc
        if not root.is_dir():
            raise CaptureError("query image source root must be a directory")
        normalized_root = os.path.normcase(str(root))
        if normalized_root not in seen_roots:
            seen_roots.add(normalized_root)
            roots.append(root)

    existing: list[Path] = []
    seen: set[str] = set()
    escaped = False
    for root in roots:
        candidate = root / path
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError:
            escaped = True
            continue
        except OSError:
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            escaped = True
            continue
        normalized = os.path.normcase(str(resolved))
        if normalized not in seen and resolved.is_file():
            seen.add(normalized)
            existing.append(resolved)
    if escaped:
        raise CaptureError("relative query image escaped an allowed source root")
    if not existing:
        raise CaptureError(
            "query image was not found under the dataset directory or configured "
            "source roots"
        )
    if len(existing) > 1:
        raise CaptureError(
            "relative query image is ambiguous across the configured source roots"
        )
    return existing[0]


def _validate_capture_annotation_metadata(items: list[dict[str, Any]]) -> None:
    for item in items:
        item_id = str(item["id"])
        annotation = item["annotation"]
        notes = annotation.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError(f"dataset item {item_id}: annotation notes must be text")
        if annotation["status"] != "human_verified":
            continue
        annotator = annotation.get("annotator")
        annotated_at = annotation.get("annotated_at")
        if not isinstance(annotator, str) or not annotator.strip():
            raise ValueError(f"dataset item {item_id}: human_verified needs annotator")
        if not isinstance(annotated_at, str) or not annotated_at.strip():
            raise ValueError(
                f"dataset item {item_id}: human_verified needs annotated_at"
            )
        try:
            datetime.fromisoformat(annotated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"dataset item {item_id}: annotated_at must be ISO-8601"
            ) from exc


def _capture_case(
    item: dict[str, Any],
    *,
    dataset_directory: Path,
    query_source_roots: tuple[Path, ...],
    client: BackendTransport,
    stager: QueryImageStager | None,
    top_k: int,
    candidate_k: int,
    poll_interval: float,
    job_timeout: float,
) -> dict[str, Any]:
    mode = str(item["mode"])
    params: dict[str, Any] = {
        "top_k": top_k,
        "candidate_k": candidate_k,
        **_library_params(item["library_scope"]),
    }
    query = item["query"]
    if mode in {"text", "combined"}:
        params["text"] = str(query["text"]).strip()

    if mode in {"image", "combined"}:
        if stager is None:
            raise ValueError(
                "image cases require --query-staging-root and a backend "
                "query-root mapping"
            )
        with stager.stage(
            _query_image_path(item, dataset_directory, query_source_roots)
        ) as backend_path:
            params["image"] = backend_path
            result, elapsed_ms, backend_requests = _wait_for_search(
                client,
                params,
                poll_interval=poll_interval,
                job_timeout=job_timeout,
            )
    else:
        result, elapsed_ms, backend_requests = _wait_for_search(
            client,
            params,
            poll_interval=poll_interval,
            job_timeout=job_timeout,
        )

    request_ids = result.get("request_ids", [])
    if not isinstance(request_ids, list) or any(
        not isinstance(value, str) for value in request_ids
    ):
        raise CaptureError("Search result request_ids must be an array of strings.")
    status = result.get("status", "ok")
    if not isinstance(status, str) or not status.strip():
        raise CaptureError("Search result status must be a non-empty string.")
    latency_value = result.get("latency_ms", elapsed_ms)
    latency_ms = _number(latency_value, "search latency_ms")
    if latency_ms < 0:
        raise CaptureError("search latency_ms must be non-negative.")
    library_ids = result.get("library_ids", [])
    if not isinstance(library_ids, list) or any(
        not isinstance(value, str) or not value.strip() for value in library_ids
    ):
        raise CaptureError("Search result library_ids must be an array of strings.")
    captured_case = {
        "id": item["id"],
        "mode": mode,
        "status": status.strip(),
        "candidate_count": _non_negative_integer(
            result.get("candidate_count", len(result.get("results", []))),
            "search candidate_count",
        ),
        "filtered_count": _non_negative_integer(
            result.get("filtered_count", 0),
            "search filtered_count",
        ),
        "latency_ms": latency_ms,
        "api_requests": len([value for value in request_ids if value]),
        "backend_requests": backend_requests,
        "library_ids": library_ids,
        "results": _capture_results(mode, result),
    }
    ranking_mode = result.get("ranking_mode")
    if isinstance(ranking_mode, str) and ranking_mode.strip():
        captured_case["ranking_mode"] = ranking_mode.strip()
    search_quality = result.get("search_quality")
    if isinstance(search_quality, dict):
        captured_case["search_quality"] = search_quality
    return captured_case


def capture_dataset(
    dataset: dict[str, Any],
    *,
    dataset_directory: Path,
    query_source_roots: tuple[Path, ...] = (),
    client: BackendTransport,
    stager: QueryImageStager | None,
    top_k: int = 10,
    candidate_k: int = 50,
    poll_interval: float = 0.25,
    job_timeout: float = 180.0,
    allow_pending_draft: bool = False,
    run_name: str | None = None,
) -> dict[str, Any]:
    if top_k < 1 or candidate_k < 1:
        raise ValueError("top_k and candidate_k must be positive")
    if poll_interval <= 0 or job_timeout <= 0:
        raise ValueError("poll interval and job timeout must be positive")
    items = quality_evaluate.validate_dataset(dataset)
    _validate_capture_annotation_metadata(items)
    pending_ids = [
        str(item["id"]) for item in items if item["annotation"]["status"] == "pending"
    ]
    if pending_ids and not allow_pending_draft:
        raise ValueError(
            "Dataset contains pending annotations; finish human review or pass "
            "--allow-pending-draft for a non-baseline draft capture."
        )
    if any(item["mode"] in {"image", "combined"} for item in items) and stager is None:
        raise ValueError("image cases require a query staging directory")

    health = client.request_json("GET", "/health")
    if health.get("service_ready") is not True:
        raise CaptureError("Persistent backend is not ready for search capture.")

    cases = [
        _capture_case(
            item,
            dataset_directory=dataset_directory,
            query_source_roots=query_source_roots,
            client=client,
            stager=stager,
            top_k=top_k,
            candidate_k=candidate_k,
            poll_interval=poll_interval,
            job_timeout=job_timeout,
        )
        for item in items
    ]
    annotation_counts = Counter(str(item["annotation"]["status"]) for item in items)
    baseline_eligible = bool(items) and annotation_counts == {
        "human_verified": len(items)
    }
    generated_at = datetime.now(timezone.utc)
    resolved_name = run_name or (
        f"{dataset['name']}-capture-{generated_at.strftime('%Y%m%dT%H%M%SZ')}"
    )
    run = {
        "schema_version": quality_evaluate.RUN_SCHEMA_VERSION,
        "name": resolved_name,
        "score_semantics": {
            "text": "lower_is_better",
            "image": "lower_is_better",
            "combined": "higher_is_better",
        },
        "score_fields": {
            "text": "raw_score",
            "image": "raw_score",
            "combined": "confidence",
        },
        "dataset": {
            "name": dataset["name"],
            "fingerprint": quality_evaluate.query_corpus_fingerprint(dataset),
            "fingerprint_algorithm": (
                quality_evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM
            ),
            "case_count": len(items),
        },
        "baseline_eligible": baseline_eligible,
        "draft": bool(pending_ids),
        "capture": {
            "kind": "zvec-persistent-backend-capture",
            "generated_at": generated_at.isoformat(),
            "top_k": top_k,
            "candidate_k": candidate_k,
            "annotation_statuses": dict(sorted(annotation_counts.items())),
            "pending_case_count": len(pending_ids),
            "baseline_eligible": baseline_eligible,
            "backend_protocol_version": health.get("protocol_version"),
        },
        "cases": cases,
    }
    split_metadata = _capture_split_metadata(dataset)
    if split_metadata is not None:
        run["split"] = copy.deepcopy(split_metadata)
    return run


def _read_backend_token(token_env: str, token_file: str | None) -> str:
    if token_file:
        source = Path(token_file).expanduser()
        try:
            if source.stat().st_size > MAX_TOKEN_BYTES:
                raise ValueError("backend token file is unexpectedly large")
            value = source.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError("could not read backend token file") from exc
    else:
        upper_name = token_env.upper()
        if any(marker in upper_name for marker in SENSITIVE_TOKEN_ENV_MARKERS):
            raise ValueError(
                "backend token environment variable must not be an API-key variable"
            )
        value = os.environ.get(token_env, "").strip()
    if not value:
        raise ValueError("backend Bearer token is not configured")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture search-quality candidate runs from a persistent backend."
    )
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--token-env",
        default="ZVEC_BACKEND_TOKEN",
        help="Environment variable containing the backend Bearer token.",
    )
    parser.add_argument(
        "--token-file",
        help="UTF-8 file containing only the backend Bearer token.",
    )
    parser.add_argument(
        "--query-staging-root",
        help="Host directory already mounted/configured as the backend query root.",
    )
    parser.add_argument(
        "--query-source-root",
        action="append",
        default=[],
        help=(
            "Additional root for dataset-relative query images; repeat for "
            "multiple roots."
        ),
    )
    parser.add_argument(
        "--backend-query-root",
        help="Same host staging directory as used by the native backend.",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--candidate-k", type=int, default=50)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--job-timeout", type=float, default=180.0)
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--name")
    parser.add_argument("--allow-pending-draft", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        dataset_path = Path(args.dataset).expanduser().resolve(strict=True)
        output_path = Path(args.output).expanduser().resolve(strict=False)
        if dataset_path == output_path:
            raise ValueError("output path must differ from the dataset path")
        dataset = _read_dataset(dataset_path)
        stager = (
            QueryImageStager(args.query_staging_root, args.backend_query_root)
            if args.query_staging_root
            else None
        )
        if args.backend_query_root and stager is None:
            raise ValueError("--backend-query-root requires --query-staging-root")
        query_source_roots = tuple(
            Path(value).expanduser().resolve(strict=True)
            for value in args.query_source_root
        )
        if any(not value.is_dir() for value in query_source_roots):
            raise ValueError("every query source root must be a directory")
        token = _read_backend_token(args.token_env, args.token_file)
        client = BackendClient(
            args.base_url,
            token,
            timeout=args.http_timeout,
        )
        run = capture_dataset(
            dataset,
            dataset_directory=dataset_path.parent,
            query_source_roots=query_source_roots,
            client=client,
            stager=stager,
            top_k=args.top_k,
            candidate_k=args.candidate_k,
            poll_interval=args.poll_interval,
            job_timeout=args.job_timeout,
            allow_pending_draft=args.allow_pending_draft,
            run_name=args.name,
        )
        quality_evaluate.validate_run(run)
        _write_json(output_path, run)
    except (CaptureError, OSError, ValueError) as exc:
        print(f"capture failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"Captured {len(run['cases'])} search-quality cases to {output_path} "
        f"(baseline_eligible={str(run['baseline_eligible']).lower()})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
