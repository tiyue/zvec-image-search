from __future__ import annotations

# HTML/CSS templates are kept inline so the loopback server never loads external
# assets; wrapping every template line would make the security-sensitive markup
# harder to audit.
# ruff: noqa: E402, E501
import argparse
import hashlib
import hmac
import html
import io
import json
import os
import secrets
import socket
import stat
import sys
import threading
import warnings
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from image_vector_service.image_scanner import SUPPORTED_EXTENSIONS

try:
    from tests.search_quality import evaluate, review
except ModuleNotFoundError:  # Direct execution from tests/search_quality.
    import evaluate  # type: ignore[no-redef]
    import review  # type: ignore[no-redef]


SCRIPT_DIR = Path(__file__).resolve().parent
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
DEFAULT_MAX_LIBRARY_IMAGES = 5000
MAX_REQUEST_BODY_BYTES = 64 * 1024
MAX_FORM_FIELDS = 256
MAX_SELECTED_IMAGES = 200
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
THUMBNAIL_SIZE = (1600, 1600)
MAX_THUMBNAIL_BYTES = 12 * 1024 * 1024
ALLOWED_IMAGE_TYPES = dict(SUPPORTED_EXTENSIONS)


@dataclass(frozen=True)
class LibraryRoot:
    library_id: str
    name: str
    root: Path
    enabled: bool


@dataclass(frozen=True)
class MediaAsset:
    path: Path
    size_bytes: int
    width: int
    height: int


@dataclass(frozen=True)
class CandidateView:
    image_id: str
    media_key: str | None
    suggested: bool
    rank: int | None = None
    confidence: float | None = None
    match_state: str = ""
    rank_source: str = ""


class ReviewConflictError(review.ReviewError):
    """Raised when the dataset changed after the page was rendered."""


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise review.ReviewError(f"could not read {label} JSON") from exc
    if not isinstance(value, dict):
        raise review.ReviewError(f"{label} JSON root must be an object")
    return value


def _resolve_local_json(raw_path: str | Path, label: str) -> Path:
    path = Path(raw_path).expanduser().resolve(strict=True)
    try:
        path.relative_to(SCRIPT_DIR)
    except ValueError as exc:
        raise review.ReviewError(f"{label} must stay inside {SCRIPT_DIR.name}") from exc
    if not path.name.endswith(".local.json"):
        raise review.ReviewError(f"{label} must be an ignored *.local.json file")
    return path


def _path_under(root: Path, relative_path: str) -> Path | None:
    parts = PurePosixPath(relative_path).parts
    try:
        candidate = root.joinpath(*parts).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    if not candidate.is_file() or candidate.suffix.lower() not in ALLOWED_IMAGE_TYPES:
        return None
    try:
        if candidate.stat().st_size > MAX_MEDIA_BYTES:
            return None
    except OSError:
        return None
    return candidate


def _inspect_image(path: Path) -> tuple[int, int, int] | None:
    try:
        size_bytes = path.stat().st_size
        if size_bytes < 1 or size_bytes > MAX_MEDIA_BYTES:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
            return None
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None
    return size_bytes, width, height


def _render_thumbnail(asset: MediaAsset) -> bytes:
    if _inspect_image(asset.path) is None:
        raise review.ReviewError("image preview no longer passes validation")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(asset.path) as source:
                image = ImageOps.exif_transpose(source)
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise review.ReviewError("image preview exceeds the pixel limit")
                image.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
                if image.mode != "RGB":
                    background = Image.new("RGB", image.size, "white")
                    if "A" in image.getbands():
                        background.paste(image, mask=image.getchannel("A"))
                    else:
                        background.paste(image.convert("RGB"))
                    image = background
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=86, optimize=True)
        payload = output.getvalue()
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise review.ReviewError("image preview decoding failed") from exc
    if not payload or len(payload) > MAX_THUMBNAIL_BYTES:
        raise review.ReviewError("image preview exceeds the response limit")
    return payload


def _dataset_revision(path: Path) -> str:
    for _attempt in range(3):
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
        if (
            before.st_mtime_ns == after.st_mtime_ns
            and before.st_size == after.st_size == len(payload)
        ):
            digest = hashlib.sha256(payload).hexdigest()
            return f"{after.st_mtime_ns}:{after.st_size}:{digest}"
    raise review.ReviewError("dataset changed while it was being read; reload")


def _stable_file_sha256(path: Path) -> str:
    if _inspect_image(path) is None:
        raise review.ReviewError("selected image no longer passes validation")
    for _attempt in range(3):
        try:
            before = path.stat()
            digest = hashlib.sha256()
            total = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_MEDIA_BYTES:
                        raise review.ReviewError(
                            "selected image exceeds the size limit"
                        )
                    digest.update(chunk)
            after = path.stat()
        except OSError as exc:
            raise review.ReviewError("selected image could not be hashed") from exc
        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if total == before.st_size and all(
            getattr(before, name, None) == getattr(after, name, None)
            for name in stable_fields
        ):
            return digest.hexdigest()
    raise review.ReviewError("selected image changed while it was being hashed")


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(reparse_flag and attributes & reparse_flag)
    except OSError:
        return True


def _scan_library_images(library: LibraryRoot, maximum: int) -> list[tuple[str, Path]]:
    values: list[tuple[str, Path]] = []
    walk_errors: list[OSError] = []

    def record_error(error: OSError) -> None:
        walk_errors.append(error)

    for current_raw, directories, files in os.walk(
        library.root,
        topdown=True,
        followlinks=False,
        onerror=record_error,
    ):
        current = Path(current_raw)
        safe_directories: list[str] = []
        for name in sorted(directories, key=str.casefold):
            directory = current / name
            if _is_reparse_point(directory):
                continue
            try:
                directory.resolve(strict=True).relative_to(library.root)
            except (OSError, ValueError):
                continue
            safe_directories.append(name)
        directories[:] = safe_directories

        for name in sorted(files, key=str.casefold):
            path = current / name
            if path.suffix.lower() not in ALLOWED_IMAGE_TYPES or _is_reparse_point(
                path
            ):
                continue
            try:
                resolved = path.resolve(strict=True)
                relative = resolved.relative_to(library.root).as_posix()
            except (OSError, ValueError):
                continue
            values.append((f"{library.library_id}:{relative}", resolved))
            if len(values) > maximum:
                raise review.ReviewError(
                    f"library {library.library_id} exceeds the configured "
                    f"{maximum}-image review limit"
                )
    if walk_errors:
        raise review.ReviewError(
            f"library {library.library_id} could not be scanned completely"
        )
    return values


def _load_libraries(path: Path) -> dict[str, LibraryRoot]:
    payload = _read_json_object(path, "libraries")
    raw_libraries = payload.get("libraries")
    if not isinstance(raw_libraries, list) or not raw_libraries:
        raise review.ReviewError("libraries file needs a non-empty libraries array")
    libraries: dict[str, LibraryRoot] = {}
    for index, value in enumerate(raw_libraries, start=1):
        if not isinstance(value, dict):
            raise review.ReviewError(f"libraries[{index}] must be an object")
        library_id = value.get("id")
        image_root = value.get("image_root")
        name = value.get("name", library_id)
        enabled = value.get("enabled", True)
        if (
            not isinstance(library_id, str)
            or not library_id.strip()
            or "/" in library_id
            or "\\" in library_id
            or ":" in library_id
        ):
            raise review.ReviewError(f"libraries[{index}].id is invalid")
        if library_id in libraries:
            raise review.ReviewError(f"duplicate library id: {library_id}")
        if not isinstance(name, str) or not name.strip():
            raise review.ReviewError(f"libraries[{index}].name is invalid")
        if not isinstance(enabled, bool):
            raise review.ReviewError(f"libraries[{index}].enabled must be boolean")
        if not isinstance(image_root, str) or not image_root.strip():
            raise review.ReviewError(f"libraries[{index}].image_root is invalid")
        try:
            root = Path(image_root).expanduser().resolve(strict=True)
        except OSError as exc:
            raise review.ReviewError(
                f"configured image root for {library_id} is unavailable"
            ) from exc
        if not root.is_dir():
            raise review.ReviewError(
                f"configured image root for {library_id} is not a directory"
            )
        libraries[library_id] = LibraryRoot(library_id, name.strip(), root, enabled)
    return libraries


class ReviewWebApplication:
    def __init__(
        self,
        *,
        dataset_path: Path,
        run_path: Path,
        libraries_path: Path,
        query_source_roots: tuple[Path, ...] = (),
        session_token: str | None = None,
        max_library_images: int = DEFAULT_MAX_LIBRARY_IMAGES,
    ):
        self.dataset_path = review._resolve_dataset_path(dataset_path)
        self.run_path = _resolve_local_json(run_path, "run")
        self.libraries_path = _resolve_local_json(libraries_path, "libraries")
        self.session_token = session_token or secrets.token_urlsafe(32)
        if len(self.session_token) < 16:
            raise review.ReviewError(
                "session token must contain at least 16 characters"
            )
        if (
            isinstance(max_library_images, bool)
            or not isinstance(max_library_images, int)
            or not 1 <= max_library_images <= 50_000
        ):
            raise review.ReviewError("max_library_images must be between 1 and 50000")
        self.max_library_images = max_library_images
        self._save_lock = threading.Lock()
        self._libraries = _load_libraries(self.libraries_path)
        self._query_source_roots = self._resolve_query_roots(query_source_roots)
        self._media: dict[str, MediaAsset] = {}
        self._media_keys_by_path: dict[Path, str] = {}
        self._candidate_views: dict[str, tuple[CandidateView, ...]] = {}
        self._library_views: dict[str, tuple[CandidateView, ...]] = {}
        self._allowed_image_ids: dict[str, frozenset[str]] = {}
        self._query_media: dict[str, str | None] = {}
        self._query_image_ids: dict[str, frozenset[str]] = {}
        self._view_revision = ""
        self._build_views()

    @staticmethod
    def _resolve_query_roots(values: tuple[Path, ...]) -> tuple[Path, ...]:
        roots: list[Path] = []
        seen: set[Path] = set()
        for value in values:
            try:
                root = value.expanduser().resolve(strict=True)
            except OSError as exc:
                raise review.ReviewError("a query source root is unavailable") from exc
            if not root.is_dir():
                raise review.ReviewError("query source roots must be directories")
            if root not in seen:
                seen.add(root)
                roots.append(root)
        return tuple(roots)

    def _register_media(self, path: Path) -> str | None:
        existing = self._media_keys_by_path.get(path)
        if existing is not None:
            return existing
        inspected = _inspect_image(path)
        if inspected is None:
            return None
        size_bytes, width, height = inspected
        key = secrets.token_urlsafe(18)
        while key in self._media:
            key = secrets.token_urlsafe(18)
        asset = MediaAsset(path, size_bytes, width, height)
        self._media[key] = asset
        self._media_keys_by_path[path] = key
        return key

    def _candidate_path(self, image_id: str) -> Path | None:
        validated = review._validate_image_id(image_id, "candidate image_id")
        library_id, relative_path = validated.split(":", 1)
        library = self._libraries.get(library_id)
        if library is None:
            return None
        return _path_under(library.root, relative_path)

    def _selection_reference(self, image_id: str) -> dict[str, str]:
        path = self._candidate_path(image_id)
        if path is None:
            raise review.ReviewError("selected image is no longer available")
        return {
            "image_id": image_id,
            "sha256": _stable_file_sha256(path),
        }

    def _query_path(self, item: dict[str, Any]) -> Path | None:
        raw_path = item.get("query", {}).get("image")
        if not isinstance(raw_path, str):
            return None
        relative_path = review._validate_relative_path(
            raw_path, f"dataset item {item['id']}: query.image"
        )
        roots = list(self._query_source_roots)
        scope = item.get("library_scope", {})
        library_ids = scope.get("library_ids", [])
        if scope.get("mode") == "all_enabled" or not library_ids:
            roots.extend(library.root for library in self._libraries.values())
        else:
            roots.extend(
                self._libraries[library_id].root
                for library_id in library_ids
                if library_id in self._libraries
            )
        roots.append(self.dataset_path.parent)
        matches: list[Path] = []
        for root in dict.fromkeys(roots):
            candidate = _path_under(root, relative_path)
            if candidate is not None and candidate not in matches:
                matches.append(candidate)
        return matches[0] if len(matches) == 1 else None

    def _build_views(self) -> None:
        revision = _dataset_revision(self.dataset_path)
        dataset = review.load_dataset(self.dataset_path)
        if revision != _dataset_revision(self.dataset_path):
            raise review.ReviewError("dataset changed while views were being built")
        run = _read_json_object(self.run_path, "run")
        try:
            cases = evaluate.validate_run(run)
        except ValueError as exc:
            raise review.ReviewError(str(exc)) from exc
        known_ids = {str(item["id"]) for item in dataset["items"]}
        unknown = sorted(set(cases) - known_ids)
        if unknown:
            raise review.ReviewError("run contains cases not present in the dataset")

        self._media.clear()
        self._media_keys_by_path.clear()
        self._candidate_views.clear()
        self._library_views.clear()
        self._allowed_image_ids.clear()
        self._query_media.clear()
        self._query_image_ids.clear()
        for library in self._libraries.values():
            library_views: list[CandidateView] = []
            if library.enabled:
                for image_id, path in _scan_library_images(
                    library, self.max_library_images
                ):
                    library_views.append(
                        CandidateView(
                            image_id=image_id,
                            media_key=self._register_media(path),
                            suggested=False,
                        )
                    )
            self._library_views[library.library_id] = tuple(library_views)
        for item in dataset["items"]:
            item_id = str(item["id"])
            query_image_ids = self._query_image_ids_for_item(item)
            self._query_image_ids[item_id] = frozenset(query_image_ids)
            suggestions = [
                str(value["image_id"])
                for value in item.get("suggested_relevant_images", [])
                if str(value["image_id"]) not in query_image_ids
            ]
            case = cases.get(item_id, {"results": []})
            results = case.get("results", [])
            by_id = {
                str(value["image_id"]): value
                for value in results
                if isinstance(value, dict) and isinstance(value.get("image_id"), str)
            }
            ordered_ids = [
                image_id
                for image_id in dict.fromkeys(
                    [
                        *suggestions,
                        *(str(value) for value in by_id),
                        *(
                            str(value["image_id"])
                            for value in item.get("relevant_images", [])
                        ),
                    ]
                )
                if image_id not in query_image_ids
            ]
            if len(ordered_ids) > MAX_SELECTED_IMAGES:
                raise review.ReviewError(
                    f"dataset item {item_id} exposes more than "
                    f"{MAX_SELECTED_IMAGES} candidates"
                )
            views: list[CandidateView] = []
            for image_id in ordered_ids:
                review._validate_image_id(image_id, f"dataset item {item_id} candidate")
                result = by_id.get(image_id, {})
                candidate_path = self._candidate_path(image_id)
                media_key = (
                    self._register_media(candidate_path)
                    if candidate_path is not None
                    else None
                )
                rank = result.get("rank")
                confidence = result.get("confidence")
                views.append(
                    CandidateView(
                        image_id=image_id,
                        media_key=media_key,
                        suggested=image_id in suggestions,
                        rank=rank
                        if isinstance(rank, int) and not isinstance(rank, bool)
                        else None,
                        confidence=(
                            float(confidence)
                            if isinstance(confidence, (int, float))
                            and not isinstance(confidence, bool)
                            else None
                        ),
                        match_state=(
                            str(result.get("match_state") or "")
                            if isinstance(result, dict)
                            else ""
                        ),
                        rank_source=(
                            str(result.get("rank_source") or "")
                            if isinstance(result, dict)
                            else ""
                        ),
                    )
                )
            self._candidate_views[item_id] = tuple(views)
            scoped_library_ids = self._scoped_library_ids(item)
            scoped_library_id_set = set(scoped_library_ids)
            verified_candidate_ids = {
                value.image_id
                for value in views
                if value.media_key is not None
                and value.image_id.split(":", 1)[0] in scoped_library_id_set
            }
            verified_library_image_ids = {
                value.image_id
                for library_id in scoped_library_ids
                for value in self._library_views[library_id]
                if value.media_key is not None
            }
            self._allowed_image_ids[item_id] = frozenset(
                {*verified_candidate_ids, *verified_library_image_ids} - query_image_ids
            )
            query_path = self._query_path(item)
            self._query_media[item_id] = (
                self._register_media(query_path) if query_path is not None else None
            )
        self._view_revision = revision

    def _scoped_library_ids(self, item: dict[str, Any]) -> tuple[str, ...]:
        scope = item.get("library_scope", {})
        requested = scope.get("library_ids", [])
        if scope.get("mode") == "all_enabled" or not requested:
            enabled = tuple(
                library_id
                for library_id, library in self._libraries.items()
                if library.enabled
            )
            if not enabled:
                raise review.ReviewError("no enabled libraries are available")
            return enabled
        missing = [value for value in requested if value not in self._libraries]
        if missing:
            raise review.ReviewError(
                f"dataset item {item['id']} references an unconfigured library"
            )
        disabled = [value for value in requested if not self._libraries[value].enabled]
        if disabled:
            raise review.ReviewError(
                f"dataset item {item['id']} explicitly references a disabled library"
            )
        return tuple(str(value) for value in requested)

    def _query_image_ids_for_item(self, item: dict[str, Any]) -> set[str]:
        if item.get("mode") not in {"image", "combined"}:
            return set()
        raw_path = item.get("query", {}).get("image")
        if not isinstance(raw_path, str):
            return set()
        relative_path = review._validate_relative_path(
            raw_path, f"dataset item {item['id']}: query.image"
        )
        return {
            f"{library_id}:{relative_path}"
            for library_id in self._scoped_library_ids(item)
            if f"{library_id}:{relative_path}"
            in {value.image_id for value in self._library_views[library_id]}
        }

    def _refresh_views_if_changed(self) -> None:
        revision = _dataset_revision(self.dataset_path)
        if revision == self._view_revision:
            return
        with self._save_lock:
            if _dataset_revision(self.dataset_path) != self._view_revision:
                self._build_views()

    def media_asset(self, key: str) -> MediaAsset | None:
        return self._media.get(key)

    def _media_url(self, key: str) -> str:
        return f"/media/{quote(key)}?{urlencode({'token': self.session_token})}"

    def _page_url(self, case_id: str | None = None, *, saved: str | None = None) -> str:
        values = {"token": self.session_token}
        if case_id is not None:
            values["case"] = case_id
        if saved is not None:
            values["saved"] = saved
        return f"/?{urlencode(values)}"

    @staticmethod
    def _one(values: dict[str, list[str]], name: str, maximum: int) -> str:
        entries = values.get(name)
        if entries is None or len(entries) != 1:
            raise review.ReviewError(f"form field {name} is required exactly once")
        value = entries[0]
        if len(value) > maximum:
            raise review.ReviewError(f"form field {name} is too long")
        return value

    def confirm(self, values: dict[str, list[str]]) -> str | None:
        csrf_token = self._one(values, "csrf_token", 256)
        if not hmac.compare_digest(csrf_token, self.session_token):
            raise PermissionError("invalid CSRF token")
        if self._one(values, "human_confirmation", 16) != "yes":
            raise review.ReviewError("explicit human confirmation is required")
        item_id = self._one(values, "item_id", 128)
        annotator = self._one(values, "annotator", 200).strip()
        notes = self._one(values, "notes", 4000)
        submitted_revision = self._one(values, "dataset_revision", 256)
        selected = values.get("image_id", [])
        if len(selected) > MAX_SELECTED_IMAGES or any(
            len(value) > 1024 for value in selected
        ):
            raise review.ReviewError("too many or oversized image selections")
        allowed = self._allowed_image_ids.get(item_id)
        if allowed is None:
            raise review.ReviewError("unknown dataset item")
        if any(image_id not in allowed for image_id in selected):
            raise review.ReviewError("selection contains an image outside this case")
        no_answer_values = values.get("confirm_no_answer", [])
        if len(no_answer_values) > 1 or any(
            value != "yes" for value in no_answer_values
        ):
            raise review.ReviewError("invalid no-answer confirmation")
        no_answer = no_answer_values == ["yes"]

        with self._save_lock:
            current_revision = _dataset_revision(self.dataset_path)
            if not hmac.compare_digest(submitted_revision, current_revision):
                raise ReviewConflictError(
                    "dataset changed after this page loaded; reload before saving"
                )
            dataset = review.load_dataset(self.dataset_path)
            current_item = review._find_item(dataset, item_id)
            selected_references = [
                self._selection_reference(image_id) for image_id in selected
            ]
            query_path = self._query_path(current_item)
            if query_path is not None:
                query_sha256 = _stable_file_sha256(query_path)
                if any(
                    value.get("sha256") == query_sha256 for value in selected_references
                ):
                    raise review.ReviewError(
                        "selection contains the query image content"
                    )
            review.confirm_human_review(
                dataset,
                self.dataset_path,
                item_id=item_id,
                annotator=annotator,
                notes=notes,
                image_references=selected_references,
                no_answer=no_answer,
                accept_suggested=False,
                use_staged_draft=False,
            )
            refreshed = review.load_dataset(self.dataset_path)
            self._view_revision = _dataset_revision(self.dataset_path)
        pending = [
            str(item["id"])
            for item in refreshed["items"]
            if item["annotation"]["status"] == "pending"
        ]
        return pending[0] if pending else None

    def render_page(self, requested_case: str | None, saved: str | None = None) -> str:
        self._refresh_views_if_changed()
        dataset = review.load_dataset(self.dataset_path)
        items = dataset["items"]
        by_id = {str(item["id"]): item for item in items}
        if requested_case is not None and requested_case not in by_id:
            raise KeyError(requested_case)
        current = by_id.get(requested_case or "") or next(
            (item for item in items if item["annotation"]["status"] == "pending"),
            items[0] if items else None,
        )
        if current is None:
            raise review.ReviewError("dataset has no review cases")
        item_id = str(current["id"])
        summary = review._summary(items)
        sidebar = "".join(
            (
                f'<a class="case {html.escape(str(item["annotation"]["status"]))}" '
                f'href="{html.escape(self._page_url(str(item["id"])), quote=True)}">'
                f"<span>{html.escape(str(item['id']))}</span>"
                f"<small>{html.escape(str(item['mode']))} · "
                f"{html.escape(str(item['annotation']['status']))}</small></a>"
            )
            for item in items
        )
        query = current.get("query", {})
        query_text = query.get("text")
        query_image_key = self._query_media.get(item_id)
        query_image = (
            f'<img class="query-image" src="{html.escape(self._media_url(query_image_key), quote=True)}" '
            'alt="查询参考图">'
            if query_image_key is not None
            else (
                '<div class="missing">参考图不可用或路径存在歧义</div>'
                if current["mode"] in {"image", "combined"}
                else ""
            )
        )
        text_block = (
            f'<p class="query-text">{html.escape(str(query_text))}</p>'
            if isinstance(query_text, str)
            else ""
        )
        views = self._candidate_views.get(item_id, ())
        suggestions = [value for value in views if value.suggested]
        candidates = [value for value in views if not value.suggested]
        displayed_ids = {value.image_id for value in views}
        library_views = [
            value
            for library_id in self._scoped_library_ids(current)
            for value in self._library_views[library_id]
            if value.image_id not in displayed_ids
            and value.image_id not in self._query_image_ids.get(item_id, ())
        ]
        selectable = (
            current["annotation"]["status"] == "pending"
            and current["query_type"] != "no-answer"
        )

        def render_candidate(value: CandidateView) -> str:
            media_key = value.media_key
            media_verified = media_key is not None
            image = (
                f'<img loading="lazy" src="{html.escape(self._media_url(media_key), quote=True)}" '
                f'alt="{html.escape(value.image_id, quote=True)}">'
                if media_key is not None
                else '<div class="missing">本地图片不可验证，不可选择</div>'
            )
            can_select = selectable and media_verified
            checkbox = (
                '<input type="checkbox" name="image_id" '
                f'value="{html.escape(value.image_id, quote=True)}" '
                'form="review-form" '
                'aria-label="标记为 relevant">'
                if can_select
                else ""
            )
            details: list[str] = []
            if value.rank is not None:
                details.append(f"排名 {value.rank}")
            if value.confidence is not None:
                details.append(f"置信度 {value.confidence:.3f}")
            if value.match_state:
                details.append(value.match_state)
            if value.rank_source:
                details.append(value.rank_source)
            detail_text = " · ".join(details) or "无候选诊断"
            badge = (
                '<strong class="suggestion">AI 建议（未验证）</strong>'
                if value.suggested
                else ""
            )
            unavailable = (
                '<strong class="unavailable">不可验证 / 不可选择</strong>'
                if not media_verified
                else ""
            )
            element = "label" if can_select else "article"
            candidate_class = "candidate" + (
                " candidate-unavailable" if not media_verified else ""
            )
            return (
                f'<{element} class="{candidate_class}">'
                f'{checkbox}{image}<span class="candidate-id">'
                f"{html.escape(value.image_id)}</span><small>{html.escape(detail_text)}</small>"
                f"{badge}{unavailable}</{element}>"
            )

        suggestion_html = "".join(render_candidate(value) for value in suggestions)
        candidate_html = "".join(render_candidate(value) for value in candidates)
        library_html = "".join(render_candidate(value) for value in library_views)
        if not suggestion_html:
            suggestion_html = '<p class="muted">没有 AI 建议。</p>'
        if not candidate_html:
            candidate_html = '<p class="muted">本次 run 没有其他候选。</p>'
        if not library_html:
            library_html = '<p class="muted">当前图库没有可复核图片。</p>'

        annotation = current["annotation"]
        if annotation["status"] == "pending":
            no_answer_control = (
                '<label class="explicit"><input type="checkbox" '
                'name="confirm_no_answer" value="yes" required> '
                "我已人工检查候选，并确认图库中没有答案</label>"
                if current["query_type"] == "no-answer"
                else '<p class="muted">至少勾选一张人工确认相关的图片。</p>'
            )
            form = (
                f'<form id="review-form" method="post" action="/confirm?{urlencode({"token": self.session_token})}">'
                f'<input type="hidden" name="csrf_token" value="{html.escape(self.session_token, quote=True)}">'
                f'<input type="hidden" name="item_id" value="{html.escape(item_id, quote=True)}">'
                f'<input type="hidden" name="dataset_revision" value="{html.escape(self._view_revision, quote=True)}">'
                '<label>Annotator<input name="annotator" maxlength="200" required '
                'autocomplete="name"></label>'
                '<label>Notes<textarea name="notes" maxlength="4000" rows="3"></textarea></label>'
                f"{no_answer_control}"
                '<label class="explicit"><input type="checkbox" '
                'name="human_confirmation" value="yes" required> '
                "这是我本人逐图复核后的结论；AI 建议未被自动接受</label>"
                '<button type="submit">确认此条为 human_verified</button></form>'
            )
        else:
            relevant = (
                "".join(
                    f"<li>{html.escape(str(value['image_id']))}</li>"
                    for value in current.get("relevant_images", [])
                )
                or "<li>无答案</li>"
            )
            form = (
                '<section class="verified"><h3>已人工确认</h3>'
                f"<p>Annotator: {html.escape(str(annotation.get('annotator') or ''))}</p>"
                f"<p>Notes: {html.escape(str(annotation.get('notes') or ''))}</p>"
                f"<ul>{relevant}</ul></section>"
            )
        saved_banner = (
            f'<div class="saved">已保存 {html.escape(saved)}；状态仅因刚才的人工确认操作而变更。</div>'
            if saved
            else ""
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Search Quality Human Review</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;font:14px/1.5 system-ui,sans-serif;background:#f4f6fb;color:#172033}}
.layout{{display:grid;grid-template-columns:260px 1fr;min-height:100vh}}aside{{background:#15213b;color:#fff;padding:20px;overflow:auto}}
aside h1{{font-size:18px;margin-top:0}}.progress{{padding:10px;background:#243354;border-radius:10px;margin-bottom:14px}}
.case{{display:block;color:#dce5ff;text-decoration:none;padding:8px;border-radius:8px;margin:3px 0}}.case:hover{{background:#2a3a60}}
.case.human_verified{{opacity:.58}}.case span,.case small{{display:block}}main{{padding:24px;min-width:0}}
.panel{{background:#fff;border:1px solid #dce2ef;border-radius:14px;padding:18px;margin-bottom:18px;box-shadow:0 5px 20px #1c2b4d0d}}
.query{{display:grid;grid-template-columns:minmax(0,1fr) minmax(220px,420px);gap:18px}}.query-image{{width:100%;max-height:420px;object-fit:contain;background:#eef1f8;border-radius:10px}}
.query-text{{font-size:20px;white-space:pre-wrap}}.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}}
.candidate{{display:flex;position:relative;flex-direction:column;gap:6px;padding:10px;border:1px solid #dce2ef;border-radius:12px;background:#fbfcff;overflow:hidden}}
.candidate>input{{position:absolute;top:12px;left:12px;width:22px;height:22px;z-index:2}}.candidate img,.candidate .missing{{width:100%;height:190px;object-fit:contain;background:#eef1f8;border-radius:8px}}
.candidate-id{{overflow-wrap:anywhere;font-size:12px}}.suggestion{{color:#9a4b00}}.unavailable{{color:#a12727}}.candidate-unavailable{{border-style:dashed;opacity:.82}}.muted{{color:#68738a}}.missing{{display:grid;place-items:center;color:#7a8498}}
form{{display:grid;gap:12px}}form label{{display:grid;gap:5px;font-weight:600}}input,textarea{{font:inherit;padding:9px;border:1px solid #bfc8da;border-radius:8px}}
.explicit{{display:flex!important;align-items:center;gap:8px!important;font-weight:500!important}}.explicit input{{width:20px;height:20px}}
button{{justify-self:start;padding:11px 18px;border:0;border-radius:9px;background:#245fd6;color:#fff;font-weight:700;cursor:pointer}}
.saved{{background:#daf5e5;color:#175b35;padding:12px;border-radius:10px;margin-bottom:15px}}.verified{{background:#eef8f2;padding:14px;border-radius:10px}}
@media(max-width:800px){{.layout{{grid-template-columns:1fr}}aside{{max-height:35vh}}.query{{grid-template-columns:1fr}}}}
</style></head><body><div class="layout"><aside><h1>人工搜索质量复核</h1>
<div class="progress">总计 {summary["total"]} · Pending {summary["pending"]} · Human {summary["human_verified"]}</div>{sidebar}</aside>
<main>{saved_banner}<section class="panel"><h2>{html.escape(item_id)}</h2><p>模式：{html.escape(str(current["mode"]))} · 状态：{html.escape(str(annotation["status"]))}</p>
<div class="query"><div><h3>查询文字</h3>{text_block or '<p class="muted">无文字查询</p>'}</div><div><h3>查询参考图</h3>{query_image or '<p class="muted">无参考图</p>'}</div></div></section>
<section class="panel"><h3>AI 建议图</h3><p class="muted">默认全部未勾选；必须由人逐张判断。</p><div class="grid">{suggestion_html}</div></section>
<section class="panel"><h3>本次检索候选</h3><div class="grid">{candidate_html}</div></section>
<section class="panel"><h3>图库其他图片</h3><p class="muted">与上方建议/候选合计覆盖当前 case scope 的整个图库；此处用于发现 Top-K 之外的遗漏图。</p><div class="grid">{library_html}</div></section>
<section class="panel"><h3>人工结论</h3>{form}</section></main></div></body></html>"""


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        application: ReviewWebApplication,
    ):
        self.application = application
        super().__init__(server_address, ReviewRequestHandler)

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        error = sys.exc_info()[1]
        if isinstance(
            error,
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            return
        super().handle_error(request, client_address)


class ReviewRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZvecReview/1"
    sys_version = ""

    @property
    def application(self) -> ReviewWebApplication:
        return cast(ReviewHTTPServer, self.server).application

    def log_message(self, _format: str, *args: object) -> None:
        # Paths contain the session token, so the default access log is disabled.
        return

    def _valid_host(self) -> bool:
        value = self.headers.get("Host", "")
        port = cast(tuple[str, int], self.server.server_address)[1]
        return value in {LOOPBACK_HOST, f"{LOOPBACK_HOST}:{port}"}

    def _valid_query_token(self, query: str) -> bool:
        try:
            values = parse_qs(query, keep_blank_values=True, max_num_fields=8)
        except ValueError:
            return False
        tokens = values.get("token")
        return (
            tokens is not None
            and len(tokens) == 1
            and hmac.compare_digest(tokens[0], self.application.session_token)
        )

    def _headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status: HTTPStatus, message: str) -> None:
        self._send_bytes(status, message.encode("utf-8"))

    def _authorize(self, query: str) -> bool:
        if not self._valid_host():
            self._send_text(HTTPStatus.FORBIDDEN, "loopback Host required")
            return False
        if not self._valid_query_token(query):
            self._send_text(HTTPStatus.FORBIDDEN, "valid session token required")
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if len(self.path) > 4096:
            self._send_text(HTTPStatus.REQUEST_URI_TOO_LONG, "request URI too long")
            return
        parsed = urlsplit(self.path)
        if not self._authorize(parsed.query):
            return
        if parsed.path == "/":
            values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=8)
            case_values = values.get("case", [])
            saved_values = values.get("saved", [])
            case_id = case_values[0] if len(case_values) == 1 else None
            saved = saved_values[0] if len(saved_values) == 1 else None
            try:
                body = self.application.render_page(case_id, saved).encode("utf-8")
            except KeyError:
                self._send_text(HTTPStatus.NOT_FOUND, "unknown review case")
                return
            except (review.ReviewError, OSError):
                self._send_text(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "review data unavailable"
                )
                return
            self._send_bytes(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/media/"):
            key = parsed.path.removeprefix("/media/")
            if not key or "/" in key or "\\" in key or len(key) > 128:
                self._send_text(HTTPStatus.NOT_FOUND, "media not found")
                return
            asset = self.application.media_asset(key)
            if asset is None:
                self._send_text(HTTPStatus.NOT_FOUND, "media not found")
                return
            try:
                payload = _render_thumbnail(asset)
            except (OSError, review.ReviewError):
                self._send_text(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "media preview unavailable"
                )
                return
            self.send_response(HTTPStatus.OK)
            self._headers("image/jpeg", len(payload))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._send_text(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path != "/confirm":
            self._send_text(HTTPStatus.NOT_FOUND, "not found")
            return
        if not self._authorize(parsed.query):
            return
        if self.headers.get("Transfer-Encoding"):
            self._send_text(HTTPStatus.BAD_REQUEST, "chunked requests are not accepted")
            return
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError:
            self._send_text(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
            return
        if length < 1:
            self._send_text(HTTPStatus.BAD_REQUEST, "request body required")
            return
        if length > MAX_REQUEST_BODY_BYTES:
            self._send_text(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body too large"
            )
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/x-www-form-urlencoded":
            self._send_text(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "unsupported form type")
            return
        body = self.rfile.read(length)
        try:
            values = parse_qs(
                body.decode("utf-8"),
                keep_blank_values=True,
                max_num_fields=MAX_FORM_FIELDS,
            )
        except (UnicodeDecodeError, ValueError):
            self._send_text(HTTPStatus.BAD_REQUEST, "invalid form body")
            return
        try:
            item_id = self.application._one(values, "item_id", 128)
            next_item = self.application.confirm(values)
        except PermissionError:
            self._send_text(HTTPStatus.FORBIDDEN, "invalid CSRF token")
            return
        except ReviewConflictError as exc:
            self._send_text(HTTPStatus.CONFLICT, str(exc))
            return
        except review.ReviewError as exc:
            self._send_text(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except OSError:
            self._send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "atomic save failed")
            return
        location = self.application._page_url(next_item, saved=item_id)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self._headers("text/plain; charset=utf-8", 0)
        self.end_headers()


def create_server(
    application: ReviewWebApplication, port: int = DEFAULT_PORT
) -> ReviewHTTPServer:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise review.ReviewError("port must be between 0 and 65535")
    return ReviewHTTPServer((LOOPBACK_HOST, port), application)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local loopback-only visual human review for search-quality cases."
    )
    parser.add_argument("--dataset", default=str(review.DEFAULT_DATASET))
    parser.add_argument("--run", required=True, help="captured *.local.json run")
    parser.add_argument("--libraries", required=True, help="libraries.local.json")
    parser.add_argument(
        "--query-source-root",
        action="append",
        default=[],
        help="explicit local root for dataset-relative query reference images",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--max-library-images",
        type=int,
        default=DEFAULT_MAX_LIBRARY_IMAGES,
        help="fail closed when any configured library exceeds this image count",
    )
    parser.add_argument("--open-browser", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        application = ReviewWebApplication(
            dataset_path=review._resolve_dataset_path(args.dataset),
            run_path=_resolve_local_json(args.run, "run"),
            libraries_path=_resolve_local_json(args.libraries, "libraries"),
            query_source_roots=tuple(Path(value) for value in args.query_source_root),
            max_library_images=args.max_library_images,
        )
        server = create_server(application, args.port)
    except (OSError, review.ReviewError) as exc:
        print(f"review web error: {exc}", file=sys.stderr)
        return 2
    port = cast(tuple[str, int], server.server_address)[1]
    url = f"http://{LOOPBACK_HOST}:{port}{application._page_url()}"
    print("Local human-review server; images stay on this computer.")
    print(url)
    if args.open_browser:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nReview server stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
