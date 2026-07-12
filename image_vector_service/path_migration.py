from __future__ import annotations

import gc
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import zvec

from .config import ConfigurationError, ServiceConfig
from .logical_paths import logical_document_id, normalize_path
from .process_lock import ProcessLock
from .state import IndexState
from .zvec_repository import collection_schema

LEGACY_FIELDS = (
    "file_name",
    "extension",
    "mime_type",
    "sha256",
    "size_bytes",
    "mtime_ns",
    "width",
    "height",
)


def migrate_path_schema(config: ServiceConfig, dry_run: bool = False) -> dict[str, Any]:
    config.validate()
    lock = ProcessLock(config.lock_path)
    lock.acquire()
    try:
        return _migrate_locked(config, dry_run)
    finally:
        lock.release()


def _migrate_locked(config: ServiceConfig, dry_run: bool) -> dict[str, Any]:
    metadata = _read_metadata(config.collection_meta_path)
    schema_version = int(metadata.get("schema_version", 0))
    if schema_version == 2:
        return {
            "status": "already_v2",
            "dry_run": dry_run,
            "documents": _state_count(config.state_path),
            "api_requests": 0,
        }
    if schema_version != 1:
        raise ConfigurationError(f"Unsupported Collection schema: {schema_version}")
    expected_metadata = {
        "model": config.model,
        "mode": "independent",
        "dimension": config.dimension,
        "metric": config.metric,
    }
    mismatches = {
        key: (metadata.get(key), expected)
        for key, expected in expected_metadata.items()
        if metadata.get(key) != expected
    }
    if mismatches:
        raise ConfigurationError(f"V1 Collection metadata mismatch: {mismatches}")
    if not config.collection_path.is_dir() or not config.state_path.is_file():
        raise ConfigurationError("The V1 Collection or state database is missing.")

    legacy = sqlite3.connect(config.state_path, timeout=5)
    legacy.row_factory = sqlite3.Row
    try:
        state_version = _state_version(legacy)
        if state_version != "1":
            raise ConfigurationError(
                f"Expected V1 state database, found version {state_version!r}."
            )
        state_collection_uuid = legacy.execute(
            "SELECT value FROM metadata WHERE key='collection_uuid'"
        ).fetchone()
        if not state_collection_uuid or str(state_collection_uuid[0]) != str(
            metadata["collection_uuid"]
        ):
            raise ConfigurationError("V1 Collection/state identity mismatch.")
        entries = [dict(row) for row in legacy.execute("SELECT * FROM entries")]
        roots = _legacy_roots(legacy, entries, str(metadata["collection_uuid"]))
        cache_rows = []
        if _table_exists(legacy, "embedding_cache"):
            cache_rows = [
                tuple(row)
                for row in legacy.execute(
                    "SELECT cache_key, modality, embedding, dimension, created_at, "
                    "last_used_at, hit_count FROM embedding_cache"
                )
            ]
    finally:
        legacy.close()

    preview = {
        "status": "ready",
        "from_schema": 1,
        "to_schema": 2,
        "dry_run": dry_run,
        "documents": len(entries),
        "roots": [
            {
                "root_id": root["root_id"],
                "current_path": root["current_path"],
                "recursive": root["recursive"],
            }
            for root in roots.values()
        ],
        "cached_embeddings": len(cache_rows),
        "api_requests": 0,
    }
    if dry_run:
        return preview

    token = uuid.uuid4().hex
    temp_collection = config.workspace / f".image_collection.v2.{token}.tmp"
    temp_state = config.workspace / f".image_collection.state.v2.{token}.tmp.sqlite3"
    temp_meta = config.workspace / f".image_collection.meta.v2.{token}.tmp.json"
    old_collection = zvec.open(str(config.collection_path))
    old_count = int(old_collection.stats.doc_count)
    if old_count != len(entries):
        raise ConfigurationError(
            f"V1 Collection/state count mismatch: {old_count} != {len(entries)}."
        )

    new_collection = zvec.create_and_open(
        str(temp_collection), collection_schema(config)
    )
    migrated_entries: list[dict[str, Any]] = []
    try:
        for batch in _chunks(entries, 128):
            old_ids = [str(entry["doc_id"]) for entry in batch]
            old_documents = old_collection.fetch(
                old_ids, output_fields=[], include_vector=True
            )
            new_documents = []
            for entry in batch:
                old_doc = old_documents.get(str(entry["doc_id"]))
                vector = old_doc.vectors.get("embedding") if old_doc else None
                if vector is None or len(vector) != config.dimension:
                    raise ConfigurationError(
                        f"Missing or invalid vector for V1 document {entry['doc_id']}."
                    )
                root = roots[normalize_path(Path(str(entry["root_path"])))]
                new_id = logical_document_id(
                    str(root["root_id"]), str(entry["relative_path"])
                )
                fields = {
                    "root_id": root["root_id"],
                    "relative_path": str(entry["relative_path"]),
                    "model": config.model,
                    **{field: entry[field] for field in LEGACY_FIELDS},
                }
                new_documents.append(
                    zvec.Doc(
                        id=new_id,
                        fields=fields,
                        vectors={"embedding": list(vector)},
                    )
                )
                migrated_entries.append(
                    {
                        "doc_id": new_id,
                        **{
                            key: value
                            for key, value in fields.items()
                            if key != "model"
                        },
                    }
                )
            statuses = new_collection.upsert(new_documents)
            if not isinstance(statuses, list):
                statuses = [statuses]
            failures = [str(status) for status in statuses if not status.ok()]
            if failures:
                raise ConfigurationError(f"V2 Collection upsert failed: {failures}")
        new_collection.optimize()
        if int(new_collection.stats.doc_count) != len(entries):
            raise ConfigurationError("V2 Collection document-count validation failed.")

        new_state = IndexState(temp_state)
        try:
            new_state.ensure_collection_uuid(
                str(metadata["collection_uuid"]), reset_if_unbound=True
            )
            for root in roots.values():
                new_state.record_root(
                    str(root["root_id"]),
                    str(root["current_path"]),
                    bool(root["recursive"]),
                )
            new_state.set_many(migrated_entries)
            new_state.import_cache_rows(cache_rows)
            new_state.checkpoint()
        finally:
            new_state.close()

        migrated_metadata = {
            **metadata,
            "schema_version": 2,
            "migrated_from_schema": 1,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
        }
        temp_meta.write_text(
            json.dumps(migrated_metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        del new_collection
        del old_collection
        gc.collect()
        _remove_migration_artifacts(temp_collection, temp_state, temp_meta)
        raise

    del new_collection
    del old_collection
    gc.collect()
    backup_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_collection = config.workspace / f"image_collection.v1.backup_{backup_suffix}"
    backup_state = config.workspace / (
        f"image_collection.state.v1.backup_{backup_suffix}.sqlite3"
    )
    backup_meta = config.workspace / (
        f"image_collection.meta.v1.backup_{backup_suffix}.json"
    )
    moved: list[tuple[Path, Path]] = []
    try:
        for source, destination in (
            (config.collection_path, backup_collection),
            (config.state_path, backup_state),
            (config.collection_meta_path, backup_meta),
            (temp_collection, config.collection_path),
            (temp_state, config.state_path),
            (temp_meta, config.collection_meta_path),
        ):
            source.replace(destination)
            moved.append((destination, source))
    except Exception:
        for source, destination in reversed(moved):
            if source.exists() and not destination.exists():
                source.replace(destination)
        raise

    return {
        **preview,
        "status": "migrated",
        "backup_collection": str(backup_collection),
        "backup_state": str(backup_state),
        "backup_metadata": str(backup_meta),
    }


def _read_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigurationError(f"Collection metadata is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _state_version(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT value FROM metadata WHERE key='state_schema_version'"
    ).fetchone()
    return str(row[0]) if row else None


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def _state_count(path: Path) -> int:
    if not path.is_file():
        return 0
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT COUNT(*) FROM entries").fetchone()
        return int(row[0]) if row else 0
    finally:
        connection.close()


def _legacy_roots(
    connection: sqlite3.Connection,
    entries: list[dict[str, Any]],
    collection_uuid: str,
) -> dict[str, dict[str, Any]]:
    recursive_by_path = {
        normalize_path(Path(str(row["root_path"]))): bool(row["recursive"])
        for row in connection.execute("SELECT root_path, recursive FROM roots")
    }
    paths = {normalize_path(Path(str(entry["root_path"]))) for entry in entries} | set(
        recursive_by_path
    )
    namespace = uuid.UUID(collection_uuid)
    return {
        path: {
            "root_id": str(uuid.uuid5(namespace, path.casefold())),
            "current_path": path,
            "recursive": recursive_by_path.get(path, True),
        }
        for path in sorted(paths)
    }


def _chunks(values: list[dict[str, Any]], size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _remove_migration_artifacts(*paths: Path) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
            Path(f"{path}-wal").unlink(missing_ok=True)
            Path(f"{path}-shm").unlink(missing_ok=True)
