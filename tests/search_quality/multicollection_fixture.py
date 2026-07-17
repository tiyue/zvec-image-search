from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import uuid
from array import array
from collections import Counter
from collections.abc import Iterator
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
repository_root_text = str(REPOSITORY_ROOT)
if not sys.path or sys.path[0] != repository_root_text:
    sys.path.insert(0, repository_root_text)

import zvec  # noqa: E402

from image_vector_service.config import ServiceConfig  # noqa: E402
from image_vector_service.federated_search import (  # noqa: E402
    LibraryCandidateSet,
    rank_federated_hits,
)
from image_vector_service.library_config import LibraryDefinition  # noqa: E402
from image_vector_service.logical_paths import logical_document_id  # noqa: E402
from image_vector_service.service import ImageVectorService  # noqa: E402
from image_vector_service.state import IndexState  # noqa: E402
from image_vector_service.zvec_repository import (  # noqa: E402
    COLLECTION_SCHEMA_VERSION,
    OUTPUT_FIELDS,
    collection_schema,
    empty_metadata_embedding,
)
from tests.search_quality import evaluate  # noqa: E402

EXPECTED_DIMENSION = 1024
SOURCE_FILES = (
    "image_collection",
    "image_collection.meta.json",
    ".image_collection.lock",
)


def _assert_local_project_imports() -> None:
    modules = {
        "image_vector_service.config": sys.modules[ServiceConfig.__module__],
        "tests.search_quality.evaluate": evaluate,
    }
    for label, module in modules.items():
        raw_path = getattr(module, "__file__", None)
        if not isinstance(raw_path, str):
            raise RuntimeError(f"cannot verify import source for {label}")
        path = Path(raw_path).resolve()
        try:
            path.relative_to(REPOSITORY_ROOT)
        except ValueError as exc:
            raise RuntimeError(
                f"{label} was imported outside this checkout: {path}"
            ) from exc


_assert_local_project_imports()


class FixtureBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceDocument:
    source_doc_id: str
    top_directory: str
    relative_path: str
    fields: dict[str, Any]
    vector: tuple[float, ...]
    state_entry: dict[str, Any]


@dataclass(frozen=True)
class SourceSnapshot:
    collection_uuid: str
    metadata: dict[str, Any]
    documents: tuple[SourceDocument, ...]
    cache_rows: tuple[tuple[Any, ...], ...]
    source_fingerprint: str


@dataclass(frozen=True)
class LibraryFixture:
    library_id: str
    name: str
    top_directory: str
    root_id: str
    image_root: Path
    workspace: Path
    documents: tuple[SourceDocument, ...]


class _NoApiEmbeddingClient:
    def __init__(self) -> None:
        self.calls = 0

    def embed_text(self, _text: str) -> Any:
        self.calls += 1
        raise AssertionError("fixture smoke attempted a text embedding API call")

    def embed_images(self, _paths: list[Path]) -> Any:
        self.calls += 1
        raise AssertionError("fixture smoke attempted an image embedding API call")


class _ReadOnlyWorkspaceLock:
    """Acquire the existing service lock without creating or writing it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def __enter__(self) -> _ReadOnlyWorkspaceLock:
        if not self.path.is_file() or self.path.stat().st_size < 1:
            raise FixtureBuildError(
                "source workspace needs an existing non-empty "
                ".image_collection.lock; refusing to create one in a read-only source"
            )
        handle = self.path.open("rb")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
        except OSError as exc:
            handle.close()
            raise FixtureBuildError(
                "source workspace is in use; retry after the image service stops"
            ) from exc
        self._handle = handle
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        assert self._handle is not None
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    self._handle.fileno(),
                    fcntl.LOCK_UN,  # type: ignore[attr-defined]
                )
        finally:
            self._handle.close()
            self._handle = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
    except PermissionError as exc:
        raise FixtureBuildError(f"source artifact cannot be read: {path}") from exc
    return digest.hexdigest()


def _tree_fingerprint(workspace: Path) -> str:
    digest = hashlib.sha256()
    paths = [workspace / name for name in SOURCE_FILES]
    state_path = workspace / "image_collection.state.sqlite3"
    if not state_path.is_file():
        raise FixtureBuildError(f"source workspace artifact is missing: {state_path}")
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(value for value in path.rglob("*") if value.is_file())
        elif path.is_file():
            files.append(path)
        else:
            raise FixtureBuildError(f"source workspace artifact is missing: {path}")
    for path in sorted(
        files, key=lambda value: value.relative_to(workspace).as_posix()
    ):
        relative = path.relative_to(workspace).as_posix()
        if relative in {"image_collection/LOCK", ".image_collection.lock"}:
            # These files coordinate ownership and may deny shared reads while held.
            # Neither carries collection data; the outer lock is actively held here.
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(path)))
    digest.update(b"state-logical\0")
    digest.update(bytes.fromhex(_state_logical_fingerprint(state_path, read_only=True)))
    return digest.hexdigest()


def _copy_sqlite_snapshot(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True)) as source_db,
        closing(sqlite3.connect(destination)) as destination_db,
    ):
        source_db.backup(destination_db)


def _state_logical_fingerprint(path: Path, *, read_only: bool = False) -> str:
    digest = hashlib.sha256()
    queries = (
        ("metadata", "SELECT * FROM metadata ORDER BY key"),
        ("entries", "SELECT * FROM entries ORDER BY doc_id"),
        ("roots", "SELECT * FROM roots ORDER BY root_id"),
        (
            "embedding_cache",
            "SELECT * FROM embedding_cache ORDER BY cache_key",
        ),
    )
    target: str | Path = (
        f"file:{path.resolve().as_posix()}?mode=ro" if read_only else path
    )
    with closing(sqlite3.connect(target, uri=read_only)) as connection:
        for table, query in queries:
            digest.update(table.encode())
            digest.update(b"\0")
            for row in connection.execute(query):
                for value in row:
                    if value is None:
                        payload = b"N"
                    elif isinstance(value, bytes):
                        payload = b"B" + value
                    else:
                        payload = b"T" + str(value).encode("utf-8")
                    digest.update(len(payload).to_bytes(8, "big"))
                    digest.update(payload)
    return digest.hexdigest()


@contextmanager
def _source_snapshot(workspace: Path) -> Iterator[tuple[Path, str]]:
    with tempfile.TemporaryDirectory(prefix="zvec-multicollection-source-") as raw:
        snapshot = Path(raw)
        with _ReadOnlyWorkspaceLock(workspace / ".image_collection.lock"):
            before = _tree_fingerprint(workspace)
            shutil.copytree(
                workspace / "image_collection",
                snapshot / "image_collection",
                copy_function=shutil.copy2,
                ignore=shutil.ignore_patterns("LOCK"),
            )
            (snapshot / "image_collection" / "LOCK").touch()
            shutil.copy2(
                workspace / "image_collection.meta.json",
                snapshot / "image_collection.meta.json",
            )
            _copy_sqlite_snapshot(
                workspace / "image_collection.state.sqlite3",
                snapshot / "image_collection.state.sqlite3",
            )
            state_fingerprint = _state_logical_fingerprint(
                snapshot / "image_collection.state.sqlite3"
            )
            after = _tree_fingerprint(workspace)
            if after != before:
                raise FixtureBuildError(
                    "source workspace changed while its read-only snapshot was copied"
                )
        combined = hashlib.sha256(
            f"physical:{before}\0state:{state_fingerprint}".encode()
        ).hexdigest()
        yield snapshot, combined


def _decode_tags(value: str) -> list[str]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or any(
        not isinstance(tag, str) for tag in decoded
    ):
        raise FixtureBuildError("source state contains invalid tags_json")
    return decoded


def _normalized_relative(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FixtureBuildError(f"unsafe relative image path: {value!r}")
    return path.as_posix()


def _split_top_directory(value: str) -> tuple[str, str]:
    normalized = _normalized_relative(value)
    parts = PurePosixPath(normalized).parts
    if len(parts) < 2:
        raise FixtureBuildError(
            f"indexed image is not below a top-level directory: {value!r}"
        )
    return parts[0], PurePosixPath(*parts[1:]).as_posix()


def _load_source_snapshot(snapshot: Path, source_fingerprint: str) -> SourceSnapshot:
    metadata_path = snapshot / "image_collection.meta.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureBuildError("source collection metadata is invalid") from exc
    collection_uuid = metadata.get("collection_uuid")
    if not isinstance(collection_uuid, str) or not collection_uuid.strip():
        raise FixtureBuildError("source collection metadata has no collection_uuid")
    if metadata.get("dimension") != EXPECTED_DIMENSION:
        raise FixtureBuildError(
            f"source collection dimension must be {EXPECTED_DIMENSION}"
        )

    state_path = snapshot / "image_collection.state.sqlite3"
    with closing(sqlite3.connect(state_path)) as connection:
        connection.row_factory = sqlite3.Row
        state_uuid_row = connection.execute(
            "SELECT value FROM metadata WHERE key='collection_uuid'"
        ).fetchone()
        if not state_uuid_row or str(state_uuid_row[0]) != collection_uuid:
            raise FixtureBuildError("source state and collection UUID do not match")
        rows = connection.execute(
            "SELECT doc_id, root_id, relative_path, file_name, extension, mime_type, "
            "sha256, size_bytes, mtime_ns, width, height, tags_json "
            "FROM entries ORDER BY relative_path, doc_id"
        ).fetchall()
        cache_rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT cache_key, modality, embedding, dimension, created_at, "
                "last_used_at, hit_count FROM embedding_cache ORDER BY cache_key"
            )
        )
    if not rows:
        raise FixtureBuildError("source collection has no indexed documents")

    collection = zvec.open(str(snapshot / "image_collection"))
    try:
        ids = [str(row["doc_id"]) for row in rows]
        if int(collection.stats.doc_count) != len(ids):
            raise FixtureBuildError(
                "source Zvec and state document counts do not match"
            )
        fetched = collection.fetch(
            ids, output_fields=OUTPUT_FIELDS, include_vector=True
        )
        if len(fetched) != len(ids):
            raise FixtureBuildError(
                "source state and Zvec document counts do not match"
            )
        documents: list[SourceDocument] = []
        for row in rows:
            doc_id = str(row["doc_id"])
            document = fetched.get(doc_id)
            if document is None:
                raise FixtureBuildError(f"source Zvec document is missing: {doc_id}")
            fields = dict(document.fields)
            vector = document.vectors.get("embedding")
            if vector is None or len(vector) != EXPECTED_DIMENSION:
                raise FixtureBuildError(f"source vector is invalid: {doc_id}")
            if str(fields.get("sha256")) != str(row["sha256"]):
                raise FixtureBuildError(f"source hash disagrees with state: {doc_id}")
            expected_fields: dict[str, Any] = {
                "root_id": str(row["root_id"]),
                "relative_path": str(row["relative_path"]),
                "file_name": str(row["file_name"]),
                "extension": str(row["extension"]),
                "mime_type": str(row["mime_type"]),
                "sha256": str(row["sha256"]),
                "size_bytes": int(row["size_bytes"]),
                "mtime_ns": int(row["mtime_ns"]),
                "width": int(row["width"]),
                "height": int(row["height"]),
                "model": str(metadata["model"]),
                "tags": _decode_tags(str(row["tags_json"])),
            }
            mismatched_fields = [
                name
                for name, expected in expected_fields.items()
                if fields.get(name) != expected
            ]
            if mismatched_fields:
                raise FixtureBuildError(
                    f"source Zvec fields disagree with state for {doc_id}: "
                    + ", ".join(mismatched_fields)
                )
            top_directory, relative_path = _split_top_directory(
                str(row["relative_path"])
            )
            entry = {
                "doc_id": doc_id,
                "root_id": str(row["root_id"]),
                "relative_path": str(row["relative_path"]),
                "file_name": str(row["file_name"]),
                "extension": str(row["extension"]),
                "mime_type": str(row["mime_type"]),
                "sha256": str(row["sha256"]),
                "size_bytes": int(row["size_bytes"]),
                "mtime_ns": int(row["mtime_ns"]),
                "width": int(row["width"]),
                "height": int(row["height"]),
                "tags": list(expected_fields["tags"]),
            }
            documents.append(
                SourceDocument(
                    source_doc_id=doc_id,
                    top_directory=top_directory,
                    relative_path=relative_path,
                    fields=fields,
                    vector=tuple(float(value) for value in vector),
                    state_entry=entry,
                )
            )
    finally:
        del collection
        gc.collect()
    return SourceSnapshot(
        collection_uuid=collection_uuid,
        metadata=metadata,
        documents=tuple(documents),
        cache_rows=cache_rows,
        source_fingerprint=source_fingerprint,
    )


def _cache_key(
    collection_uuid: str,
    model: str,
    dimension: int,
    modality: str,
    value: str,
) -> str:
    identity = "\0".join((collection_uuid, model, str(dimension), modality, value))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _library_id(source_library_id: str, top_directory: str) -> str:
    suffix = hashlib.sha256(
        f"{source_library_id}\0{top_directory}".encode()
    ).hexdigest()[:20]
    return f"lib-mc-{suffix}"


def _root_id(collection_uuid: str, top_directory: str) -> str:
    try:
        namespace = uuid.UUID(collection_uuid)
    except ValueError as exc:
        raise FixtureBuildError("source collection_uuid is not a UUID") from exc
    return str(uuid.uuid5(namespace, f"multicollection-fixture:{top_directory}"))


def _safe_image_path(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve(strict=True)
    candidate = (resolved_root / _normalized_relative(relative_path)).resolve(
        strict=True
    )
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise FixtureBuildError("image path escaped its top-level image root") from exc
    if not candidate.is_file():
        raise FixtureBuildError(f"indexed image is missing: {candidate}")
    return candidate


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _resolve_top_image_root(image_root: Path, top_directory: str) -> Path:
    parts = PurePosixPath(_normalized_relative(top_directory)).parts
    if len(parts) != 1:
        raise FixtureBuildError(
            "split top directory must contain exactly one component"
        )
    candidate = image_root / parts[0]
    if _is_reparse_point(candidate):
        raise FixtureBuildError(
            f"split top directory must not be a symlink or reparse point: {candidate}"
        )
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or resolved.parent != image_root:
        raise FixtureBuildError(
            f"split top directory must be a direct child of the image root: {candidate}"
        )
    return resolved


def _validate_private_images(fixtures: tuple[LibraryFixture, ...]) -> int:
    verified = 0
    for fixture in fixtures:
        for document in fixture.documents:
            path = _safe_image_path(fixture.image_root, document.relative_path)
            stat = path.stat()
            entry = document.state_entry
            if stat.st_size != int(entry["size_bytes"]):
                raise FixtureBuildError(f"indexed image size changed: {path}")
            if _sha256_file(path) != str(entry["sha256"]):
                raise FixtureBuildError(f"indexed image hash changed: {path}")
            verified += 1
    return verified


def _target_metadata(source: SourceSnapshot) -> dict[str, Any]:
    return {
        **source.metadata,
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "collection_uuid": source.collection_uuid,
        "fixture_lineage": {
            "kind": "read-only-multicollection-split",
            "source_fingerprint": source.source_fingerprint,
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_json_without_overwrite(path: Path, payload: dict[str, Any]) -> None:
    """Atomically publish a complete JSON file while preserving concurrent files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FixtureBuildError(
                f"output appeared concurrently; refusing to overwrite it: {path}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _build_workspace(source: SourceSnapshot, fixture: LibraryFixture) -> None:
    workspace = fixture.workspace
    workspace.mkdir(parents=True, exist_ok=False)
    config = ServiceConfig(workspace=workspace)
    collection = zvec.create_and_open(
        str(config.collection_path), collection_schema(config)
    )
    try:
        documents: list[zvec.Doc] = []
        target_entries: list[dict[str, Any]] = []
        for source_document in fixture.documents:
            new_doc_id = logical_document_id(
                fixture.root_id, source_document.relative_path
            )
            fields = {
                **source_document.fields,
                "root_id": fixture.root_id,
                "relative_path": source_document.relative_path,
                "metadata_text": "",
                "metadata_text_hash": "",
            }
            documents.append(
                zvec.Doc(
                    id=new_doc_id,
                    fields=fields,
                    vectors={
                        "embedding": list(source_document.vector),
                        "metadata_embedding": empty_metadata_embedding(
                            config.dimension
                        ),
                    },
                )
            )
            target_entries.append(
                {
                    **source_document.state_entry,
                    "doc_id": new_doc_id,
                    "root_id": fixture.root_id,
                    "relative_path": source_document.relative_path,
                }
            )
        for offset in range(0, len(documents), 25):
            statuses = collection.upsert(documents[offset : offset + 25])
            if not isinstance(statuses, list):
                statuses = [statuses]
            failures = [str(status) for status in statuses if not status.ok()]
            if failures:
                raise FixtureBuildError(
                    f"target Zvec upsert failed for {fixture.library_id}: {failures}"
                )
        collection.flush()
        if int(collection.stats.doc_count) != len(documents):
            raise FixtureBuildError(
                f"target Zvec count mismatch for {fixture.library_id}"
            )
    finally:
        del collection
        gc.collect()

    _write_json(config.collection_meta_path, _target_metadata(source))
    state = IndexState(config.state_path)
    try:
        state.ensure_collection_uuid(source.collection_uuid)
        state.record_root(fixture.root_id, str(fixture.image_root), True)
        state.set_many(target_entries)
        state.import_cache_rows(source.cache_rows)
        model = str(source.metadata["model"])
        dimension = int(source.metadata["dimension"])
        vectors_by_hash: dict[str, bytes] = {}
        for document in source.documents:
            content_hash = str(document.fields["sha256"])
            vector_bytes = _vector_bytes(document.vector)
            known_vector = vectors_by_hash.get(content_hash)
            if known_vector is not None and known_vector != vector_bytes:
                raise FixtureBuildError(
                    "duplicate source content has inconsistent indexed vectors: "
                    f"{content_hash}"
                )
            vectors_by_hash[content_hash] = vector_bytes
            state.set_cached_vector(
                _cache_key(
                    source.collection_uuid,
                    model,
                    dimension,
                    "image",
                    content_hash,
                ),
                "image",
                list(document.vector),
            )
        state.checkpoint()
    finally:
        state.close()


def _vector_bytes(values: list[float] | tuple[float, ...]) -> bytes:
    return array("f", values).tobytes()


def _validate_target(source: SourceSnapshot, fixture: LibraryFixture) -> dict[str, Any]:
    config = ServiceConfig(workspace=fixture.workspace)
    collection = zvec.open(str(config.collection_path))
    state = IndexState(config.state_path)
    try:
        if int(collection.stats.doc_count) != len(fixture.documents):
            raise FixtureBuildError(
                f"target document count changed: {fixture.library_id}"
            )
        if state.count() != len(fixture.documents):
            raise FixtureBuildError(f"target state count changed: {fixture.library_id}")
        if state.get_metadata("collection_uuid") != source.collection_uuid:
            raise FixtureBuildError(f"target state UUID changed: {fixture.library_id}")
        roots = state.list_roots()
        if len(roots) != 1 or Path(str(roots[0]["current_path"])) != fixture.image_root:
            raise FixtureBuildError(f"target root is invalid: {fixture.library_id}")
        expected_by_id = {
            logical_document_id(fixture.root_id, document.relative_path): document
            for document in fixture.documents
        }
        fetched = collection.fetch(
            list(expected_by_id), output_fields=OUTPUT_FIELDS, include_vector=True
        )
        if set(fetched) != set(expected_by_id):
            raise FixtureBuildError(f"target document IDs differ: {fixture.library_id}")
        for doc_id, expected in expected_by_id.items():
            actual = fetched[doc_id]
            if str(actual.fields.get("sha256")) != str(expected.fields["sha256"]):
                raise FixtureBuildError(f"target hash differs: {doc_id}")
            vector = actual.vectors.get("embedding")
            if vector is None or _vector_bytes(list(vector)) != _vector_bytes(
                expected.vector
            ):
                raise FixtureBuildError(f"target vector differs: {doc_id}")
            entry = state.get(doc_id)
            if entry is None or str(entry["sha256"]) != str(expected.fields["sha256"]):
                raise FixtureBuildError(f"target state entry differs: {doc_id}")
        cache_stats = state.cache_stats()
        image_cache_count = int(
            state.connection.execute(
                "SELECT COUNT(*) FROM embedding_cache WHERE modality='image'"
            ).fetchone()[0]
        )
        expected_image_keys = {
            _cache_key(
                source.collection_uuid,
                str(source.metadata["model"]),
                int(source.metadata["dimension"]),
                "image",
                str(document.fields["sha256"]),
            )
            for document in source.documents
        }
        present_image_keys = {
            str(row[0])
            for row in state.connection.execute(
                "SELECT cache_key FROM embedding_cache WHERE modality='image'"
            )
        }
        if not expected_image_keys.issubset(present_image_keys):
            raise FixtureBuildError(
                f"target image cache is incomplete: {fixture.library_id}"
            )
        for document in source.documents:
            key = _cache_key(
                source.collection_uuid,
                str(source.metadata["model"]),
                int(source.metadata["dimension"]),
                "image",
                str(document.fields["sha256"]),
            )
            row = state.connection.execute(
                "SELECT embedding, dimension FROM embedding_cache WHERE cache_key=?",
                (key,),
            ).fetchone()
            if (
                row is None
                or int(row["dimension"]) != len(document.vector)
                or bytes(row["embedding"]) != _vector_bytes(document.vector)
            ):
                raise FixtureBuildError(
                    f"target image cache vector differs: {fixture.library_id}"
                )
        return {
            "documents": len(expected_by_id),
            "cache_entries": cache_stats["entries"],
            "image_cache_entries": image_cache_count,
            "vectors_verified": len(expected_by_id),
            "hashes_verified": len(expected_by_id),
        }
    finally:
        state.close()
        del collection
        gc.collect()


def _source_library_id(dataset: dict[str, Any]) -> str:
    identifiers: set[str] = set()
    for item in dataset.get("items", []):
        for field in ("relevant_images", "suggested_relevant_images", "review_draft"):
            values = item.get(field, [])
            if not isinstance(values, list):
                continue
            for value in values:
                if not isinstance(value, dict):
                    continue
                image_id = value.get("image_id")
                if isinstance(image_id, str) and ":" in image_id:
                    identifiers.add(image_id.split(":", 1)[0])
    if len(identifiers) != 1:
        raise FixtureBuildError(
            "dataset must reference exactly one source library before splitting"
        )
    return next(iter(identifiers))


def _map_image_reference(
    value: dict[str, Any],
    *,
    source_library_id: str,
    by_top: dict[str, LibraryFixture],
) -> dict[str, Any]:
    image_id = value.get("image_id")
    if not isinstance(image_id, str) or ":" not in image_id:
        raise FixtureBuildError("dataset image reference has an invalid image_id")
    library_id, relative_path = image_id.split(":", 1)
    if library_id != source_library_id:
        raise FixtureBuildError(f"unexpected source library id: {library_id}")
    top_directory, child = _split_top_directory(relative_path)
    fixture = by_top.get(top_directory)
    if fixture is None:
        raise FixtureBuildError(
            f"dataset image reference uses an unknown top directory: {top_directory}"
        )
    indexed = {document.relative_path: document for document in fixture.documents}.get(
        child
    )
    if indexed is None:
        raise FixtureBuildError(f"dataset image reference is not indexed: {image_id}")
    reference_hash = value.get("sha256")
    if reference_hash is not None and reference_hash != indexed.fields["sha256"]:
        raise FixtureBuildError(
            f"dataset image reference SHA-256 disagrees with the index: {image_id}"
        )
    return {**value, "image_id": f"{fixture.library_id}:{child}"}


def _resolve_query_path(
    value: str,
    *,
    by_top: dict[str, LibraryFixture],
    dataset_directory: Path,
    query_source_roots: tuple[Path, ...],
) -> tuple[str, Path]:
    normalized = _normalized_relative(value)
    parts = PurePosixPath(normalized).parts
    if len(parts) >= 2 and parts[0] in by_top:
        fixture = by_top[parts[0]]
        child = PurePosixPath(*parts[1:]).as_posix()
        indexed_paths = {document.relative_path for document in fixture.documents}
        if child not in indexed_paths:
            raise FixtureBuildError(f"library query image is not indexed: {value!r}")
        library_matches: list[Path] = []
        for candidate_fixture in by_top.values():
            candidate = candidate_fixture.image_root / child
            try:
                resolved = _safe_image_path(candidate_fixture.image_root, child)
            except (FixtureBuildError, OSError):
                continue
            if candidate.is_file() and resolved not in library_matches:
                library_matches.append(resolved)
        if len(library_matches) != 1:
            raise FixtureBuildError(
                f"mapped query image is ambiguous across Collection roots: {child!r}"
            )
        return child, library_matches[0]

    external_matches: list[Path] = []
    for root in (dataset_directory, *query_source_roots):
        try:
            candidate = (root.resolve(strict=True) / normalized).resolve(strict=True)
            candidate.relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            continue
        if candidate.is_file() and candidate not in external_matches:
            external_matches.append(candidate)
    if len(external_matches) != 1:
        raise FixtureBuildError(
            f"query image must resolve exactly once across allowed roots: {value!r}"
        )
    return normalized, external_matches[0]


def _transform_dataset(
    dataset: dict[str, Any],
    *,
    fixtures: tuple[LibraryFixture, ...],
    dataset_directory: Path,
    query_source_roots: tuple[Path, ...],
) -> tuple[dict[str, Any], dict[str, Path]]:
    try:
        evaluate.validate_dataset(dataset)
    except ValueError as exc:
        raise FixtureBuildError(str(exc)) from exc
    source_library_id = _source_library_id(dataset)
    by_top = {fixture.top_directory: fixture for fixture in fixtures}
    transformed = deepcopy(dataset)
    query_paths: dict[str, Path] = {}
    for item in transformed["items"]:
        item_id = str(item["id"])
        annotation = item.get("annotation", {})
        if annotation.get("status") != "pending":
            raise FixtureBuildError(
                f"dataset item {item_id} is not pending; refusing to rewrite labels"
            )
        item["library_scope"] = {"mode": "all_enabled", "library_ids": []}
        for field in ("relevant_images", "suggested_relevant_images", "review_draft"):
            if field not in item:
                continue
            values = item[field]
            if not isinstance(values, list) or any(
                not isinstance(value, dict) for value in values
            ):
                raise FixtureBuildError(f"dataset item {item_id}: invalid {field}")
            item[field] = [
                _map_image_reference(
                    value,
                    source_library_id=source_library_id,
                    by_top=by_top,
                )
                for value in values
            ]
        query = item.get("query", {})
        if item.get("mode") in {"image", "combined"}:
            value = query.get("image")
            if not isinstance(value, str):
                raise FixtureBuildError(f"dataset item {item_id}: missing query image")
            mapped, resolved = _resolve_query_path(
                value,
                by_top=by_top,
                dataset_directory=dataset_directory,
                query_source_roots=query_source_roots,
            )
            query["image"] = mapped
            query_paths[item_id] = resolved
    transformed["name"] = f"{transformed['name']}-multicollection"
    split_note = (
        " Split into two enabled Collections without relabeling; pending human review."
    )
    transformed["description"] = (
        str(transformed.get("description", "")).rstrip() + split_note
    ).strip()
    try:
        evaluate.validate_dataset(transformed)
    except ValueError as exc:
        raise FixtureBuildError(f"transformed dataset is invalid: {exc}") from exc
    return transformed, query_paths


def _zero_api_smoke(
    fixtures: tuple[LibraryFixture, ...],
    dataset: dict[str, Any],
    query_paths: dict[str, Path],
    *,
    review_run_output: Path | None = None,
) -> dict[str, Any]:
    clients = {fixture.library_id: _NoApiEmbeddingClient() for fixture in fixtures}
    services: dict[str, ImageVectorService] = {}
    cases = evaluate.validate_dataset(dataset)
    prepared_count = 0
    candidate_sets = 0
    federated_cases = 0
    federated_results = 0
    dual_library_cases = 0
    confidence_order_verified_cases = 0
    ranking_modes: Counter[str] = Counter()
    legacy_ranking_modes: Counter[str] = Counter()
    review_cases: list[dict[str, Any]] = []
    try:
        for fixture in fixtures:
            services[fixture.library_id] = ImageVectorService(
                ServiceConfig(workspace=fixture.workspace),
                embedding_client=clients[fixture.library_id],
            )
        primary = services[fixtures[0].library_id]
        for item in cases:
            mode = str(item["mode"])
            query = item["query"]
            prepared = primary.prepare_search_query(
                text=str(query["text"]) if mode in {"text", "combined"} else None,
                image_path=(
                    str(query_paths[str(item["id"])])
                    if mode in {"image", "combined"}
                    else None
                ),
            )
            if prepared.request_ids or any(
                value == "api" for value in prepared.embedding_sources.values()
            ):
                raise FixtureBuildError(
                    f"fixture smoke used an API embedding: {item['id']}"
                )
            prepared_count += 1
            collections: list[LibraryCandidateSet] = []
            for fixture in fixtures:
                service = services[fixture.library_id]
                candidates = service.query_prepared_search(
                    prepared,
                    candidate_k=min(30, service.repository.doc_count),
                    include_self=False,
                )
                collections.append(
                    LibraryCandidateSet(
                        library=LibraryDefinition(
                            library_id=fixture.library_id,
                            name=fixture.name,
                            image_root=fixture.image_root,
                            workspace=fixture.workspace,
                            enabled=True,
                        ),
                        candidates=candidates,
                    )
                )
                candidate_sets += 1
            legacy_ranking = rank_federated_hits(
                collections,
                top_k=min(10, sum(len(value.documents) for value in fixtures)),
                show_low_confidence=True,
            )
            has_metadata = any(
                collection.candidates.metadata_hits for collection in collections
            )
            has_tags = any(collection.candidates.tag_hits for collection in collections)
            expected_legacy_mode = (
                "weighted_rrf_with_metadata"
                if prepared.query_type == "image_text" and has_metadata
                else "weighted_rrf"
                if prepared.query_type == "image_text"
                else "hybrid_tag_visual_metadata"
                if prepared.query_type == "text" and has_tags and has_metadata
                else "hybrid_tag_vector"
                if prepared.query_type == "text" and has_tags
                else "visual_metadata"
                if prepared.query_type == "text" and has_metadata
                else "distance"
            )
            if legacy_ranking.ranking_mode != expected_legacy_mode:
                raise FixtureBuildError(
                    f"unexpected legacy ranking mode for {item['id']}: "
                    f"{legacy_ranking.ranking_mode}"
                )
            legacy_ranking_modes[legacy_ranking.ranking_mode] += 1
            configured_collections = [
                replace(
                    collection,
                    candidates=replace(
                        collection.candidates,
                        quality_configured=True,
                        minimum_confidence=0.0,
                        possible_confidence=0.0,
                        high_confidence=0.0,
                        score_gap=1.0,
                        max_confidence_drop=1.0,
                        fusion_mode="confidence_v2",
                        fusion_options={},
                    ),
                )
                for collection in collections
            ]
            ranking = rank_federated_hits(
                configured_collections,
                top_k=min(10, sum(len(value.documents) for value in fixtures)),
                show_low_confidence=True,
            )
            if ranking.ranking_mode != "confidence_v2":
                raise FixtureBuildError(
                    f"unified smoke did not use confidence_v2: {item['id']}"
                )
            ranking_modes[ranking.ranking_mode] += 1
            federated_cases += 1
            federated_results += len(ranking.hits)
            hashes = [str(hit.fields.get("sha256") or "") for hit in ranking.hits]
            nonempty_hashes = [value for value in hashes if value]
            if len(nonempty_hashes) != len(set(nonempty_hashes)):
                raise FixtureBuildError(
                    f"federated smoke returned duplicate SHA-256 values: {item['id']}"
                )
            library_ids = {
                str(hit.fields.get("library_id") or "") for hit in ranking.hits
            }
            if {fixture.library_id for fixture in fixtures}.issubset(library_ids):
                dual_library_cases += 1
            confidences = [
                float(hit.confidence)
                for hit in ranking.hits
                if hit.confidence is not None
            ]
            if len(confidences) == len(ranking.hits):
                if any(
                    left + 1e-12 < right
                    for left, right in zip(confidences, confidences[1:], strict=False)
                ):
                    raise FixtureBuildError(
                        f"federated confidence order increased: {item['id']}"
                    )
                confidence_order_verified_cases += 1
            review_cases.append(
                _review_run_case(
                    item,
                    fixtures=fixtures,
                    ranking=ranking,
                )
            )
    except AssertionError as exc:
        raise FixtureBuildError(str(exc)) from exc
    finally:
        for service in services.values():
            service.close()
    api_calls = sum(client.calls for client in clients.values())
    if api_calls:
        raise FixtureBuildError(f"fixture smoke made {api_calls} embedding API calls")
    if not dual_library_cases:
        raise FixtureBuildError(
            "federated smoke did not produce any result set covering both Collections"
        )
    if review_run_output is not None:
        review_run = _review_run(dataset, review_cases)
        try:
            evaluate.validate_run(review_run)
        except ValueError as exc:
            raise FixtureBuildError(f"review run is invalid: {exc}") from exc
        _write_json(review_run_output, review_run)
    return {
        "cases": prepared_count,
        "candidate_sets": candidate_sets,
        "federated_cases": federated_cases,
        "federated_results": federated_results,
        "dual_library_cases": dual_library_cases,
        "confidence_order_verified_cases": confidence_order_verified_cases,
        "ranking_modes": dict(sorted(ranking_modes.items())),
        "legacy_compatibility_modes": dict(sorted(legacy_ranking_modes.items())),
        "embedding_api_calls": api_calls,
    }


def _review_run_case(
    item: dict[str, Any],
    *,
    fixtures: tuple[LibraryFixture, ...],
    ranking: Any,
) -> dict[str, Any]:
    mode = str(item["mode"])
    results: list[dict[str, Any]] = []
    for rank, hit in enumerate(ranking.hits, start=1):
        library_id = str(hit.fields.get("library_id") or "")
        relative_path = str(hit.fields.get("relative_path") or "").replace("\\", "/")
        if not library_id or not relative_path:
            raise FixtureBuildError(
                f"review run result is missing image identity: {item['id']}"
            )
        raw_score = float(hit.raw_score if hit.raw_score is not None else hit.distance)
        confidence = float(hit.confidence or 0.0)
        result: dict[str, Any] = {
            "image_id": f"{library_id}:{relative_path}",
            "rank": rank,
            "score": confidence if mode == "combined" else raw_score,
            "raw_score": raw_score,
            "normalized_score": float(hit.normalized_score or 0.0),
            "confidence": confidence,
            "match_state": str(hit.match_state or "weak"),
            "rank_source": str(hit.rank_source or "text"),
        }
        digest = str(hit.fields.get("sha256") or "")
        if digest:
            result["sha256"] = digest
        if hit.fused_score is not None:
            result["fused_score"] = float(hit.fused_score)
        for name in ("image_confidence", "text_confidence", "rank_agreement"):
            value = getattr(hit, name)
            if value is not None:
                result[name] = float(value)
        for name in ("image_rank", "text_rank"):
            value = getattr(hit, name)
            if value is not None:
                result[name] = int(value)
        results.append(result)
    return {
        "id": str(item["id"]),
        "mode": mode,
        "status": str(ranking.status),
        "candidate_count": int(ranking.candidate_count),
        "filtered_count": int(ranking.filtered_count),
        "latency_ms": 0.0,
        "api_requests": 0,
        "backend_requests": 0,
        "library_ids": [fixture.library_id for fixture in fixtures],
        "results": results,
        "ranking_mode": str(ranking.ranking_mode),
        "search_quality": {
            "configured": True,
            "ranking_mode": str(ranking.ranking_mode),
            "fixture": "zero_api_multicollection",
        },
    }


def _review_run(
    dataset: dict[str, Any], review_cases: list[dict[str, Any]]
) -> dict[str, Any]:
    items = evaluate.validate_dataset(dataset)
    if len(review_cases) != len(items):
        raise FixtureBuildError("review run does not cover the transformed dataset")
    return {
        "schema_version": evaluate.RUN_SCHEMA_VERSION,
        "name": f"{dataset['name']}-zero-api-review",
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
            "fingerprint": evaluate.query_corpus_fingerprint(dataset),
            "fingerprint_algorithm": evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM,
            "case_count": len(items),
        },
        "baseline_eligible": False,
        "draft": True,
        "capture": {
            "kind": "zero-api-multicollection-fixture",
            "top_k": 10,
            "candidate_k": 30,
            "pending_case_count": len(items),
            "baseline_eligible": False,
        },
        "cases": review_cases,
    }


def _run_zero_api_smoke_subprocess(
    fixtures: tuple[LibraryFixture, ...],
    dataset: dict[str, Any],
    query_paths: dict[str, Path],
    staging_root: Path,
) -> dict[str, Any]:
    spec_path = staging_root / ".zero-api-smoke.json"
    _write_json(
        spec_path,
        {
            "libraries": [
                {
                    "id": fixture.library_id,
                    "name": fixture.name,
                    "image_root": str(fixture.image_root),
                    "workspace": str(fixture.workspace),
                    "document_count": len(fixture.documents),
                }
                for fixture in fixtures
            ],
            "dataset": dataset,
            "query_paths": {
                case_id: str(path) for case_id, path in query_paths.items()
            },
            "review_run_output": str(staging_root / "review-run.json"),
        },
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--internal-zero-api-smoke",
                str(spec_path),
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            timeout=600,
            check=False,
        )
    finally:
        spec_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise FixtureBuildError(f"zero-API smoke subprocess failed: {detail}")
    prefix = "ZVEC_ZERO_API_SMOKE="
    lines = [line for line in completed.stdout.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise FixtureBuildError("zero-API smoke subprocess returned no result marker")
    try:
        result = json.loads(lines[0][len(prefix) :])
    except json.JSONDecodeError as exc:
        raise FixtureBuildError("zero-API smoke result marker is invalid") from exc
    if not isinstance(result, dict):
        raise FixtureBuildError("zero-API smoke result must be an object")
    return result


def _internal_zero_api_smoke(spec_path: Path) -> int:
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
        fixtures = tuple(
            LibraryFixture(
                library_id=str(value["id"]),
                name=str(value["name"]),
                top_directory=str(value["name"]),
                root_id="internal-smoke",
                image_root=Path(str(value["image_root"])),
                workspace=Path(str(value["workspace"])),
                documents=tuple(
                    SourceDocument(
                        source_doc_id="",
                        top_directory="",
                        relative_path="",
                        fields={},
                        vector=(),
                        state_entry={},
                    )
                    for _ in range(int(value["document_count"]))
                ),
            )
            for value in payload["libraries"]
        )
        result = _zero_api_smoke(
            fixtures,
            payload["dataset"],
            {
                str(case_id): Path(str(path))
                for case_id, path in payload["query_paths"].items()
            },
            review_run_output=Path(str(payload["review_run_output"])),
        )
    except Exception as exc:
        print(f"internal zero-API smoke failed: {exc}", file=sys.stderr)
        return 1
    print(
        "ZVEC_ZERO_API_SMOKE="
        + json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    )
    return 0


def _validate_destination(path: Path, *, label: str, source: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        common = Path(os.path.commonpath([str(resolved), str(source)]))
    except ValueError:
        common = Path()
    if common in {resolved, source}:
        raise FixtureBuildError(f"{label} must not overlap the source workspace")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise FixtureBuildError(f"{label} already exists and is not an empty directory")
    return resolved


def _require_new_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        raise FixtureBuildError(f"{label} already exists; refusing to overwrite it")
    return resolved


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def build_fixture(
    *,
    source_workspace: Path,
    image_root: Path,
    dataset_path: Path,
    output_root: Path,
    libraries_output: Path,
    dataset_output: Path,
    review_run_output: Path | None = None,
    query_source_roots: tuple[Path, ...] = (),
    run_smoke: bool = True,
) -> dict[str, Any]:
    source_workspace = source_workspace.expanduser().resolve(strict=True)
    image_root = image_root.expanduser().resolve(strict=True)
    dataset_path = dataset_path.expanduser().resolve(strict=True)
    if not source_workspace.is_dir() or not image_root.is_dir():
        raise FixtureBuildError("source workspace and image root must be directories")
    if not dataset_path.is_file():
        raise FixtureBuildError("dataset must be a file")
    output_root = _validate_destination(
        output_root, label="output root", source=source_workspace
    )
    libraries_output = _require_new_file(libraries_output, "libraries output")
    dataset_output = _require_new_file(dataset_output, "dataset output")
    if review_run_output is not None:
        review_run_output = _require_new_file(review_run_output, "review run output")
    if libraries_output == dataset_output:
        raise FixtureBuildError("libraries output and dataset output must differ")
    output_files: list[tuple[Path, str]] = [
        (libraries_output, "libraries output"),
        (dataset_output, "dataset output"),
    ]
    if review_run_output is not None:
        output_files.append((review_run_output, "review run output"))
    if len({path for path, _label in output_files}) != len(output_files):
        raise FixtureBuildError(
            "libraries, dataset, and review run outputs must differ"
        )
    for path, label in output_files:
        if _is_within(path, source_workspace):
            raise FixtureBuildError(f"{label} must not be inside the source workspace")
        if _is_within(path, output_root):
            raise FixtureBuildError(f"{label} must not be inside the output root")
        if path == dataset_path:
            raise FixtureBuildError(f"{label} must not overwrite the input dataset")
    query_source_roots = tuple(
        value.expanduser().resolve(strict=True) for value in query_source_roots
    )
    if any(not value.is_dir() for value in query_source_roots):
        raise FixtureBuildError("query source roots must be directories")
    try:
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureBuildError("dataset is not valid UTF-8 JSON") from exc
    if not isinstance(dataset, dict):
        raise FixtureBuildError("dataset must contain a JSON object")

    with _source_snapshot(source_workspace) as (snapshot_path, source_fingerprint):
        source = _load_source_snapshot(snapshot_path, source_fingerprint)
    top_directories = sorted({document.top_directory for document in source.documents})
    if len(top_directories) != 2:
        raise FixtureBuildError(
            "source collection must contain exactly two indexed top-level directories"
        )
    source_library_id = _source_library_id(dataset)
    staging_root = output_root.with_name(f".{output_root.name}.tmp-{uuid.uuid4().hex}")
    if staging_root.exists():
        raise FixtureBuildError(f"temporary output already exists: {staging_root}")
    staging_root.mkdir(parents=True)
    final_fixtures: tuple[LibraryFixture, ...] = tuple(
        LibraryFixture(
            library_id=_library_id(source_library_id, top_directory),
            name=top_directory,
            top_directory=top_directory,
            root_id=_root_id(source.collection_uuid, top_directory),
            image_root=_resolve_top_image_root(image_root, top_directory),
            workspace=output_root
            / "workspaces"
            / _library_id(source_library_id, top_directory),
            documents=tuple(
                document
                for document in source.documents
                if document.top_directory == top_directory
            ),
        )
        for top_directory in top_directories
    )
    staging_fixtures = tuple(
        LibraryFixture(
            **{
                **fixture.__dict__,
                "workspace": staging_root / fixture.workspace.relative_to(output_root),
            }
        )
        for fixture in final_fixtures
    )
    published_output_root = False
    published_libraries = False
    published_dataset = False
    published_review_run = False
    try:
        verified_private_images = _validate_private_images(staging_fixtures)
        transformed_dataset, query_paths = _transform_dataset(
            dataset,
            fixtures=staging_fixtures,
            dataset_directory=dataset_path.parent,
            query_source_roots=query_source_roots,
        )
        for fixture in staging_fixtures:
            _build_workspace(source, fixture)
        validations = {
            fixture.library_id: _validate_target(source, fixture)
            for fixture in staging_fixtures
        }
        smoke = (
            _run_zero_api_smoke_subprocess(
                staging_fixtures,
                transformed_dataset,
                query_paths,
                staging_root,
            )
            if run_smoke
            else {
                "cases": 0,
                "candidate_sets": 0,
                "federated_cases": 0,
                "federated_results": 0,
                "dual_library_cases": 0,
                "confidence_order_verified_cases": 0,
                "ranking_modes": {},
                "legacy_compatibility_modes": {},
                "embedding_api_calls": 0,
            }
        )
        report = {
            "schema_version": 1,
            "source_workspace": str(source_workspace),
            "source_fingerprint": source.source_fingerprint,
            "source_collection_uuid": source.collection_uuid,
            "source_document_count": len(source.documents),
            "source_cache_entries": len(source.cache_rows),
            "private_images_copied": 0,
            "private_images_verified": verified_private_images,
            "libraries": [
                {
                    "id": fixture.library_id,
                    "name": fixture.name,
                    "top_directory": fixture.top_directory,
                    "root_id": fixture.root_id,
                    "image_root": str(fixture.image_root),
                    "workspace": str(final.workspace),
                    **validations[fixture.library_id],
                }
                for fixture, final in zip(staging_fixtures, final_fixtures, strict=True)
            ],
            "zero_api_smoke": smoke,
            "review_run": (
                {
                    "path": str(review_run_output or output_root / "review-run.json"),
                    "case_count": len(transformed_dataset["items"]),
                    "draft": True,
                    "baseline_eligible": False,
                }
                if run_smoke
                else None
            ),
        }
        _write_json(staging_root / "fixture-report.json", report)
        (staging_root / "results").mkdir()
        if output_root.exists():
            output_root.rmdir()
        os.replace(staging_root, output_root)
        published_output_root = True
        libraries_payload = {
            "schema_version": 2,
            "default_library_id": final_fixtures[0].library_id,
            "results_directory": str(output_root / "results"),
            "libraries": [
                {
                    "id": fixture.library_id,
                    "name": fixture.name,
                    "image_root": str(fixture.image_root),
                    "workspace": str(fixture.workspace),
                    "enabled": True,
                }
                for fixture in final_fixtures
            ],
        }
        _publish_json_without_overwrite(libraries_output, libraries_payload)
        published_libraries = True
        _publish_json_without_overwrite(dataset_output, transformed_dataset)
        published_dataset = True
        if review_run_output is not None:
            review_run = json.loads(
                (output_root / "review-run.json").read_text(encoding="utf-8")
            )
            _publish_json_without_overwrite(review_run_output, review_run)
            published_review_run = True
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        if published_output_root:
            shutil.rmtree(output_root, ignore_errors=True)
        if published_libraries:
            libraries_output.unlink(missing_ok=True)
        if published_dataset:
            dataset_output.unlink(missing_ok=True)
        if published_review_run and review_run_output is not None:
            review_run_output.unlink(missing_ok=True)
        raise
    return {
        **report,
        "output_root": str(output_root),
        "libraries_output": str(libraries_output),
        "dataset_output": str(dataset_output),
        "review_run_output": (
            str(review_run_output) if review_run_output is not None else None
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Split one indexed Zvec workspace into two ignored evaluation "
            "Collections without calling an embedding API."
        )
    )
    parser.add_argument("--source-workspace", type=Path, required=True)
    parser.add_argument("--image-root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--libraries-output", type=Path, required=True)
    parser.add_argument("--dataset-output", type=Path, required=True)
    parser.add_argument(
        "--review-run-output",
        type=Path,
        help=(
            "Optional draft run output for review_web.py. Use a *.local.json path "
            "inside tests/search_quality for the hardened reviewer."
        ),
    )
    parser.add_argument(
        "--query-source-root",
        action="append",
        type=Path,
        default=[],
        help="Allowed root for non-library query images; repeat as needed.",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="Skip the zero-API search smoke (not recommended for real fixtures).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments[:1] == ["--internal-zero-api-smoke"]:
        if len(raw_arguments) != 2:
            print("internal zero-API smoke needs one spec path", file=sys.stderr)
            return 2
        return _internal_zero_api_smoke(Path(raw_arguments[1]))
    args = build_parser().parse_args(raw_arguments)
    try:
        report = build_fixture(
            source_workspace=args.source_workspace,
            image_root=args.image_root,
            dataset_path=args.dataset,
            output_root=args.output_root,
            libraries_output=args.libraries_output,
            dataset_output=args.dataset_output,
            review_run_output=args.review_run_output,
            query_source_roots=tuple(args.query_source_root),
            run_smoke=not args.skip_smoke,
        )
    except (FixtureBuildError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"multicollection fixture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
