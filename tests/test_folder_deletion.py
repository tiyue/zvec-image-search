from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from image_vector_service.config import ServiceConfig
from image_vector_service.folder_deletion import (
    FolderDeletionError,
    FolderDeletionManager,
)
from image_vector_service.library_browser import LibraryBrowser
from image_vector_service.service import ImageVectorService
from image_vector_service.state import IndexState
from tests.test_image_service import FakeEmbeddingClient


@dataclass(frozen=True)
class _Document:
    id: str


class _Repository:
    collection_uuid = "collection-folder-delete"

    def __init__(self, doc_ids: tuple[str, ...]) -> None:
        self.documents = {doc_id: _Document(doc_id) for doc_id in doc_ids}
        self.embedding_api_requests = 0

    def contains(self, doc_id: str) -> bool:
        return doc_id in self.documents

    def snapshot_documents(
        self, doc_ids: list[str]
    ) -> tuple[dict[str, _Document], dict[str, str]]:
        return (
            {
                doc_id: self.documents[doc_id]
                for doc_id in doc_ids
                if doc_id in self.documents
            },
            {},
        )

    def delete_resilient(self, doc_ids: list[str]) -> tuple[list[str], dict[str, str]]:
        deleted: list[str] = []
        for doc_id in doc_ids:
            if self.documents.pop(doc_id, None) is not None:
                deleted.append(doc_id)
        return deleted, {}

    def delete(self, doc_ids: Iterable[str]) -> tuple[list[str], dict[str, str]]:
        return self.delete_resilient(list(doc_ids))

    def restore_documents(
        self, documents: list[_Document]
    ) -> tuple[list[str], dict[str, str]]:
        restored: list[str] = []
        for document in documents:
            self.documents[document.id] = document
            restored.append(document.id)
        return restored, {}


class _BulkState:
    def __init__(self, doc_ids: Iterable[str]) -> None:
        self.doc_ids = set(doc_ids)
        self.remove_batch_sizes: list[int] = []

    def root_path(self, _root_id: str) -> str | None:
        return None

    def iter_folder_entries(self, *_args: Any, **_kwargs: Any):
        return iter(())

    def remove_many(self, doc_ids: Iterable[str]) -> None:
        self.remove_many_for_folder_delete(doc_ids)

    def remove_many_for_folder_delete(self, doc_ids: Iterable[str]) -> int:
        values = list(doc_ids)
        self.remove_batch_sizes.append(len(values))
        before = len(self.doc_ids)
        self.doc_ids.difference_update(values)
        return before - len(self.doc_ids)

    def get_many(self, doc_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        return {
            doc_id: {"doc_id": doc_id} for doc_id in doc_ids if doc_id in self.doc_ids
        }


class _BulkRepository(_Repository):
    def __init__(self, doc_ids: tuple[str, ...]) -> None:
        super().__init__(doc_ids)
        self.snapshot_batch_sizes: list[int] = []
        self.delete_batch_sizes: list[int] = []

    def snapshot_documents(
        self, doc_ids: list[str]
    ) -> tuple[dict[str, _Document], dict[str, str]]:
        self.snapshot_batch_sizes.append(len(doc_ids))
        return super().snapshot_documents(doc_ids)

    def delete_resilient(self, doc_ids: list[str]) -> tuple[list[str], dict[str, str]]:
        self.delete_batch_sizes.append(len(doc_ids))
        return super().delete_resilient(doc_ids)


class _NoFilesystemFolderDeletionManager(FolderDeletionManager):
    def _stage(self, operation: Mapping[str, Any], *, recovering: bool) -> None:
        del operation, recovering

    def _staged_file(self, operation: Mapping[str, Any], relative_path: str) -> Path:
        del operation
        return self.config.workspace / "already-absent" / relative_path

    def _purge_stage(self, operation: Mapping[str, Any]) -> list[dict[str, str]]:
        del operation
        return []


class FolderDeletionManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.images = self.root / "images"
        self.workspace = self.root / "workspace"
        self.results = self.root / "results"
        self.folder = self.images / "characters"
        self.folder.mkdir(parents=True)
        self.workspace.mkdir()
        self.results.mkdir()
        self.source = self.folder / "portrait.jpg"
        self.source.write_bytes(b"indexed-image")
        (self.folder / "notes.txt").write_text("user sidecar", encoding="utf-8")

        self.config = ServiceConfig(
            workspace=self.workspace,
            results_directory=self.results,
        )
        self.state = IndexState(self.config.state_path)
        self.addCleanup(self.state.close)
        self.state.ensure_collection_uuid(_Repository.collection_uuid)
        self.root_id = self.state.ensure_root(str(self.images), True)
        metadata = self.source.stat()
        self.state.set_many(
            [
                {
                    "doc_id": "doc-portrait",
                    "root_id": self.root_id,
                    "relative_path": "characters/portrait.jpg",
                    "file_name": self.source.name,
                    "extension": ".jpg",
                    "mime_type": "image/jpeg",
                    "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                    "size_bytes": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                    "width": 100,
                    "height": 200,
                    "tags": ["人物"],
                    "folder_tags": ["characters"],
                    "accepted_auto_tags": [],
                    "inherited_tags": [],
                }
            ]
        )
        with LibraryBrowser(
            library_id="library-a", state_path=self.config.state_path
        ) as browser:
            folders = browser.list_folders(limit=100)["folders"]
        self.folder_key = next(
            str(item["folder_key"])
            for item in folders
            if item["relative_folder"] == "characters"
        )
        self.repository = _Repository(("doc-portrait",))

    def _manager(
        self,
        mutation_observer: Callable[[str, int], None] | None = None,
    ) -> FolderDeletionManager:
        return FolderDeletionManager(
            config=self.config,
            state=self.state,
            repository=self.repository,
            library_id="library-a",
            mutation_observer=mutation_observer,
        )

    def _root_folder_key(self) -> str:
        with LibraryBrowser(
            library_id="library-a", state_path=self.config.state_path
        ) as browser:
            roots = browser.list_folders(limit=1)["roots"]
        return str(roots[0]["folder_key"])

    def _add_indexed_image(self, index: int) -> str:
        doc_id = f"doc-{index:04d}"
        source = self.folder / f"image-{index:04d}.jpg"
        source.write_bytes(f"image-{index}".encode())
        metadata = source.stat()
        self.state.set_many(
            [
                {
                    "doc_id": doc_id,
                    "root_id": self.root_id,
                    "relative_path": f"characters/{source.name}",
                    "file_name": source.name,
                    "extension": ".jpg",
                    "mime_type": "image/jpeg",
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "size_bytes": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                    "width": 10,
                    "height": 20,
                    "tags": [],
                    "folder_tags": ["characters"],
                    "accepted_auto_tags": [],
                    "inherited_tags": [],
                }
            ]
        )
        self.repository.documents[doc_id] = _Document(doc_id)
        return doc_id

    def test_preview_then_commit_removes_only_after_explicit_confirmation(self) -> None:
        observed: list[tuple[str, int]] = []
        manager = self._manager(
            lambda operation_id, deleted: observed.append((operation_id, deleted))
        )
        self.addCleanup(manager.close)

        preview = manager.preview(folder_key=self.folder_key)

        self.assertEqual(preview["image_count"], 1)
        self.assertEqual(preview["file_count"], 2)
        self.assertEqual(preview["other_file_count"], 1)
        self.assertEqual(preview["folder_name"], "characters")
        self.assertEqual(preview["confirmation_phrase"], "characters")
        self.assertEqual(preview["api_requests"], 0)
        self.assertTrue(self.folder.is_dir())
        self.assertIn("doc-portrait", self.repository.documents)

        with self.assertRaises(FolderDeletionError):
            manager.commit(
                operation_id=str(preview["operation_id"]),
                confirmation_token=str(preview["confirmation_token"]),
                confirm=False,
            )
        self.assertTrue(self.folder.is_dir())

        result = manager.commit(
            operation_id=str(preview["operation_id"]),
            confirmation_token=str(preview["confirmation_token"]),
            confirm=True,
        )

        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["indexed_deleted"], 1)
        self.assertEqual(result["api_requests"], 0)
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.state.get_many(["doc-portrait"]), {})
        self.assertNotIn("doc-portrait", self.repository.documents)
        self.assertEqual(self.repository.embedding_api_requests, 0)
        self.assertEqual(
            observed,
            [(str(preview["operation_id"]), 1)],
        )

    def test_commit_rejects_changes_made_after_the_preview(self) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)
        preview = manager.preview(folder_key=self.folder_key)
        self.source.write_bytes(b"changed-after-preview")

        with self.assertRaisesRegex(FolderDeletionError, "changed after preview"):
            manager.commit(
                operation_id=str(preview["operation_id"]),
                confirmation_token=str(preview["confirmation_token"]),
                confirm=True,
            )

        self.assertTrue(self.source.is_file())
        self.assertIn("doc-portrait", self.repository.documents)
        self.assertIn("doc-portrait", self.state.get_many(["doc-portrait"]))
        operation = manager.journal.operation(str(preview["operation_id"]))
        assert operation is not None
        self.assertEqual(operation["status"], "prepared")

    def test_commit_rejects_expired_or_forged_cross_library_previews(self) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)
        preview = manager.preview(folder_key=self.folder_key)
        operation_id = str(preview["operation_id"])

        with self.assertRaisesRegex(FolderDeletionError, "token is invalid"):
            manager.commit(
                operation_id=operation_id,
                confirmation_token="forged-token",
                confirm=True,
            )

        other_library = FolderDeletionManager(
            config=self.config,
            state=self.state,
            repository=self.repository,
            library_id="library-b",
        )
        self.addCleanup(other_library.close)
        with self.assertRaisesRegex(FolderDeletionError, "different library"):
            other_library.commit(
                operation_id=operation_id,
                confirmation_token=str(preview["confirmation_token"]),
                confirm=True,
            )

        replaced_repository = _Repository(("doc-portrait",))
        replaced_repository.collection_uuid = "replacement-collection"
        replaced_collection = FolderDeletionManager(
            config=self.config,
            state=self.state,
            repository=replaced_repository,
            library_id="library-a",
        )
        self.addCleanup(replaced_collection.close)
        with self.assertRaisesRegex(FolderDeletionError, "Collection changed"):
            replaced_collection.commit(
                operation_id=operation_id,
                confirmation_token=str(preview["confirmation_token"]),
                confirm=True,
            )

        manager.journal.connection.execute(
            "UPDATE operations SET expires_at = ? WHERE operation_id = ?",
            (time.time() - 1, operation_id),
        )
        manager.journal.connection.commit()
        with self.assertRaisesRegex(FolderDeletionError, "preview expired"):
            manager.commit(
                operation_id=operation_id,
                confirmation_token=str(preview["confirmation_token"]),
                confirm=True,
            )
        self.assertTrue(self.source.is_file())
        self.assertIn("doc-portrait", self.repository.documents)

    def test_recovery_resumes_an_operation_after_physical_staging(self) -> None:
        manager = self._manager()
        preview = manager.preview(folder_key=self.folder_key)
        operation_id = str(preview["operation_id"])
        operation = manager.journal.operation(operation_id)
        assert operation is not None
        manager.journal.update_operation(operation_id, status="staging")
        manager._stage(operation, recovering=False)
        manager.close()

        self.assertFalse(self.folder.exists())
        self.assertIn("doc-portrait", self.repository.documents)

        observed: list[tuple[str, int]] = []
        recovered_manager = self._manager(
            lambda recovered_id, deleted: observed.append((recovered_id, deleted))
        )
        self.addCleanup(recovered_manager.close)
        report = recovered_manager.recover_incomplete()

        self.assertEqual(
            report,
            {
                "recovered": 1,
                "failed": 0,
                "failures": [],
                "api_requests": 0,
            },
        )
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.state.get_many(["doc-portrait"]), {})
        self.assertNotIn("doc-portrait", self.repository.documents)
        self.assertEqual(self.repository.embedding_api_requests, 0)
        self.assertEqual(observed, [(operation_id, 1)])

    def test_recovery_finishes_sqlite_when_collection_delete_already_committed(
        self,
    ) -> None:
        manager = self._manager()
        preview = manager.preview(folder_key=self.folder_key)
        operation_id = str(preview["operation_id"])
        operation = manager.journal.operation(operation_id)
        assert operation is not None
        manager.journal.update_operation(operation_id, status="staging")
        manager._stage(operation, recovering=False)
        manager.journal.mark_items(operation_id, ["doc-portrait"], status="staged")
        manager.journal.update_operation(operation_id, status="committing")
        self.repository.documents.pop("doc-portrait")
        manager.close()

        recovered_manager = self._manager()
        self.addCleanup(recovered_manager.close)
        report = recovered_manager.recover_incomplete()

        self.assertEqual(report["recovered"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(self.state.get_many(["doc-portrait"]), {})
        self.assertFalse(self.folder.exists())

    def test_missing_collection_document_and_sqlite_failure_needs_attention(
        self,
    ) -> None:
        manager = self._manager()
        preview = manager.preview(folder_key=self.folder_key)
        operation_id = str(preview["operation_id"])
        operation = manager.journal.operation(operation_id)
        assert operation is not None
        manager.journal.update_operation(operation_id, status="staging")
        manager._stage(operation, recovering=False)
        manager.journal.mark_items(operation_id, ["doc-portrait"], status="staged")
        manager.journal.update_operation(operation_id, status="committing")
        self.repository.documents.pop("doc-portrait")
        manager.close()
        with self.state.connection:
            self.state.connection.execute(
                "CREATE TRIGGER block_folder_delete BEFORE DELETE ON entries "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )

        recovered_manager = self._manager()
        self.addCleanup(recovered_manager.close)
        report = recovered_manager.recover_incomplete()
        operation = recovered_manager.journal.operation(operation_id)
        assert operation is not None

        self.assertEqual(report["recovered"], 0)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(operation["status"], "needs_attention")
        self.assertTrue(operation["result"]["needs_attention"])
        self.assertIn("rollback was unavailable", repr(operation["result"]))
        self.assertIn("doc-portrait", self.state.get_many(["doc-portrait"]))
        self.assertNotIn("doc-portrait", self.repository.documents)
        self.assertTrue(self.folder.is_dir())

    def test_sqlite_failure_restores_collection_and_physical_folder(self) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)
        preview = manager.preview(folder_key=self.folder_key)
        with self.state.connection:
            self.state.connection.execute(
                "CREATE TRIGGER block_folder_delete BEFORE DELETE ON entries "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )

        result = manager.commit(
            operation_id=str(preview["operation_id"]),
            confirmation_token=str(preview["confirmation_token"]),
            confirm=True,
        )

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["needs_attention"])
        self.assertEqual(result["failed"], 1)
        self.assertIn("doc-portrait", self.repository.documents)
        self.assertIn("doc-portrait", self.state.get_many(["doc-portrait"]))
        self.assertTrue(self.source.is_file())
        self.assertTrue((self.folder / "notes.txt").is_file())

    def test_staged_directory_cleanup_failure_is_reported_as_needs_attention(
        self,
    ) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)
        preview = manager.preview(folder_key=self.folder_key)
        with patch(
            "image_vector_service.folder_deletion._delete_owned_stage",
            side_effect=OSError("directory is locked"),
        ):
            result = manager.commit(
                operation_id=str(preview["operation_id"]),
                confirmation_token=str(preview["confirmation_token"]),
                confirm=True,
            )

        self.assertEqual(result["status"], "needs_attention")
        self.assertTrue(result["needs_attention"])
        self.assertGreaterEqual(result["failed"], 1)
        self.assertIn("cleanup failed", repr(result["failures"]))
        self.assertEqual(self.state.get_many(["doc-portrait"]), {})
        self.assertNotIn("doc-portrait", self.repository.documents)
        operation = manager.journal.operation(str(preview["operation_id"]))
        assert operation is not None
        self.assertEqual(operation["status"], "needs_attention")

    def test_changed_reparse_and_config_home_overlaps_are_blocked(self) -> None:
        self.source.write_bytes(b"changed-after-index")
        manager = self._manager()
        changed = manager.preview(folder_key=self.folder_key)
        manager.close()
        self.assertTrue(changed["blocked"])
        self.assertIn("indexed_file_changed", changed["blocked_reasons"])

        metadata = self.source.stat()
        entry = self.state.get_many(["doc-portrait"])["doc-portrait"]
        self.state.set_many(
            [
                {
                    **entry,
                    "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
                    "size_bytes": metadata.st_size,
                    "mtime_ns": metadata.st_mtime_ns,
                }
            ]
        )
        marker = self.folder / "link"
        marker.write_text("not followed", encoding="utf-8")
        manager = self._manager()
        with patch(
            "image_vector_service.folder_deletion._stat_is_reparse",
            side_effect=lambda path, _metadata: path.name == "link",
        ):
            reparse = manager.preview(folder_key=self.folder_key)
        manager.close()
        self.assertTrue(reparse["blocked"])
        self.assertIn("reparse_point", reparse["blocked_reasons"])

        marker.unlink()
        config_home = self.folder / "config-home"
        with patch.dict(os.environ, {"ZVEC_CONFIG_HOME": str(config_home)}):
            manager = self._manager()
            protected = manager.preview(folder_key=self.folder_key)
            manager.close()
        self.assertTrue(protected["blocked"])
        self.assertIn("protected_path_overlap", protected["blocked_reasons"])

    def test_library_root_is_recreated_after_its_contents_are_cleared(self) -> None:
        # On Windows CI the filesystem (or Defender) may mutate file metadata
        # between preview() and commit(), causing _validate_snapshot to reject
        # the operation.  Retry the whole preview-commit cycle a few times.
        result = None
        for attempt in range(5):
            manager = self._manager()
            try:
                preview = manager.preview(folder_key=self._root_folder_key())
                result = manager.commit(
                    operation_id=str(preview["operation_id"]),
                    confirmation_token=str(preview["confirmation_token"]),
                    confirm=True,
                )
                break
            except FolderDeletionError:
                manager.close()
                if attempt == 4:
                    raise
                time.sleep(0.5)
        assert result is not None

        self.assertTrue(result["is_library_root"])
        self.assertTrue(result["root_preserved"])
        # On Windows CI the filesystem may need a moment to recreate the directory.
        for _ in range(10):
            if self.images.is_dir():
                break
            time.sleep(0.2)
        self.assertTrue(self.images.is_dir())
        self.assertEqual(list(self.images.iterdir()), [])

    def test_large_folder_is_committed_in_multiple_bounded_batches(self) -> None:
        for index in range(260):
            self._add_indexed_image(index)
        manager = self._manager()
        self.addCleanup(manager.close)
        preview = manager.preview(folder_key=self.folder_key)
        result = manager.commit(
            operation_id=str(preview["operation_id"]),
            confirmation_token=str(preview["confirmation_token"]),
            confirm=True,
        )

        self.assertEqual(preview["image_count"], 261)
        self.assertEqual(result["selected"], 261)
        self.assertEqual(result["indexed_deleted"], 261)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.state.count(), 0)
        self.assertEqual(self.repository.documents, {})
        self.assertEqual(self.repository.embedding_api_requests, 0)

    def test_ten_thousand_items_never_exceed_the_bounded_commit_batch(self) -> None:
        doc_ids = tuple(f"bulk-{index:05d}" for index in range(10_001))
        state = _BulkState(doc_ids)
        repository = _BulkRepository(doc_ids)
        config = ServiceConfig(
            workspace=self.root / "bulk-workspace",
            results_directory=self.root / "bulk-results",
        )
        manager = _NoFilesystemFolderDeletionManager(
            config=config,
            state=state,
            repository=repository,
            library_id="library-bulk",
        )
        self.addCleanup(manager.close)
        operation_id = uuid.uuid4().hex
        operation_data = {
            "operation_id": operation_id,
            "library_id": "library-bulk",
            "collection_uuid": repository.collection_uuid,
            "folder_key": "bulk-folder",
            "root_id": "bulk-root",
            "relative_folder": "bulk",
            "root_path": str(self.root / "bulk-images"),
            "target_path": str(self.root / "bulk-images" / "bulk"),
            "stage_path": str(self.root / "bulk-stage"),
            "token_hash": "unused",
            "snapshot_digest": "unused",
            "expires_at": time.time() + 60,
            "preview": {"operation_id": operation_id},
        }
        manager.journal.create(
            operation_data,
            (
                {
                    "doc_id": doc_id,
                    "relative_path": f"bulk/{doc_id}.jpg",
                    "sha256": "",
                    "size_bytes": 1,
                    "mtime_ns": 1,
                }
                for doc_id in doc_ids
            ),
        )
        operation = manager.journal.operation(operation_id)
        assert operation is not None

        result = manager._execute(operation)

        self.assertEqual(result["selected"], 10_001)
        self.assertEqual(result["indexed_deleted"], 10_001)
        self.assertEqual(result["api_requests"], 0)
        self.assertEqual(state.doc_ids, set())
        self.assertEqual(repository.documents, {})
        self.assertGreater(len(repository.delete_batch_sizes), 1)
        self.assertLessEqual(max(repository.snapshot_batch_sizes), 128)
        self.assertLessEqual(max(repository.delete_batch_sizes), 128)
        self.assertLessEqual(max(state.remove_batch_sizes), 128)
        self.assertEqual(repository.embedding_api_requests, 0)

    def test_journal_prunes_only_expired_or_old_safe_terminal_operations(self) -> None:
        manager = self._manager()
        self.addCleanup(manager.close)

        def create_operation(status: str, expires_at: float) -> str:
            operation_id = uuid.uuid4().hex
            manager.journal.create(
                {
                    "operation_id": operation_id,
                    "library_id": "library-a",
                    "collection_uuid": self.repository.collection_uuid,
                    "folder_key": "folder",
                    "root_id": self.root_id,
                    "relative_folder": "characters",
                    "root_path": str(self.images),
                    "target_path": str(self.folder),
                    "stage_path": str(self.root / "stage" / operation_id),
                    "token_hash": "unused",
                    "snapshot_digest": "unused",
                    "expires_at": expires_at,
                    "preview": {"operation_id": operation_id},
                },
                [
                    {
                        "doc_id": f"item-{operation_id}",
                        "relative_path": "characters/item.jpg",
                        "sha256": "",
                        "size_bytes": 1,
                        "mtime_ns": 1,
                    }
                ],
            )
            if status != "prepared":
                manager.journal.update_operation(operation_id, status=status)
            return operation_id

        expired = create_operation("prepared", time.time() - 1)
        recoverable = create_operation("committing", time.time() - 1)
        attention = create_operation("needs_attention", time.time() - 1)
        old_committed = create_operation("committed", time.time() + 60)
        manager.journal.connection.execute(
            "UPDATE operations SET updated_at = datetime('now', '-31 days') "
            "WHERE operation_id = ?",
            (old_committed,),
        )
        manager.journal.connection.commit()

        report = manager.journal.prune()

        self.assertEqual(report, {"operations": 2, "items": 2})
        self.assertIsNone(manager.journal.operation(expired))
        self.assertIsNone(manager.journal.operation(old_committed))
        self.assertIsNotNone(manager.journal.operation(recoverable))
        self.assertIsNotNone(manager.journal.operation(attention))

    def test_commit_rechecks_cancellation_immediately_before_staging(self) -> None:
        armed = False
        commit_checks = 0

        def cancel_check() -> None:
            nonlocal commit_checks
            if not armed:
                return
            commit_checks += 1
            if commit_checks == 2:
                raise RuntimeError("cancelled before staging")

        manager = FolderDeletionManager(
            config=self.config,
            state=self.state,
            repository=self.repository,
            library_id="library-a",
            cancel_check=cancel_check,
        )
        self.addCleanup(manager.close)
        preview = manager.preview(folder_key=self.folder_key)
        armed = True

        with self.assertRaisesRegex(RuntimeError, "cancelled before staging"):
            manager.commit(
                operation_id=str(preview["operation_id"]),
                confirmation_token=str(preview["confirmation_token"]),
                confirm=True,
            )

        self.assertEqual(commit_checks, 2)
        self.assertTrue(self.folder.is_dir())
        self.assertTrue(self.source.is_file())
        self.assertIn("doc-portrait", self.state.get_many(["doc-portrait"]))
        self.assertIn("doc-portrait", self.repository.documents)


class FolderDeletionRealCollectionIntegrationTest(unittest.TestCase):
    """Exercise the safety saga against temporary SQLite and Zvec stores."""

    def setUp(self) -> None:
        # Zvec owns one process-global file logger on Windows. Its first log file
        # can remain locked until pytest exits, so cleanup must tolerate that one
        # diagnostic file while all Collection handles are still destroyed below.
        self.temporary = tempfile.TemporaryDirectory(
            prefix="zvec_folder_delete_real_", ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.images = self.root / "images"
        self.folder = self.images / "characters"
        self.folder.mkdir(parents=True)
        self.source = self.folder / "portrait.png"
        Image.new("RGB", (16, 16), (80, 120, 160)).save(self.source)
        (self.folder / "notes.txt").write_text("keep rollback exact", encoding="utf-8")
        self.config = ServiceConfig(
            workspace=self.root / "workspace",
            results_directory=self.root / "results",
        )
        self.embedding_client = FakeEmbeddingClient(self.config.dimension)
        self.service = ImageVectorService(
            config=self.config,
            embedding_client=self.embedding_client,
        )
        self.addCleanup(self._close_service)
        indexed = self.service.index_folder(str(self.images))
        self.assertEqual(indexed.inserted, 1)
        entry = self.service.state.find_entry_for_path(self.source)
        assert entry is not None
        self.doc_id = str(entry["doc_id"])
        self.requests_after_index = self.embedding_client.request_count
        with LibraryBrowser(
            library_id="default", state_path=self.config.state_path
        ) as browser:
            folders = browser.list_folders(limit=100)["folders"]
        self.folder_key = next(
            str(item["folder_key"])
            for item in folders
            if item["relative_folder"] == "characters"
        )

    def _close_service(self) -> None:
        # Zvec keeps RocksDB handles open until the Collection is destroyed.
        # Release them deterministically so Windows can remove the temp tree.
        try:
            self.service.repository.collection.destroy()
        finally:
            self.service.close()

    def _operation(self, operation_id: str) -> dict[str, Any]:
        manager = self.service._folder_deletion_manager("default")
        try:
            operation = manager.journal.operation(operation_id)
        finally:
            manager.close()
        assert operation is not None
        return operation

    def _vectors(self) -> dict[str, list[float]]:
        fetched = self.service.repository.collection.fetch(
            self.doc_id,
            output_fields=[],
            include_vector=True,
        )
        document = fetched[self.doc_id]
        return {
            str(name): list(vector) for name, vector in dict(document.vectors).items()
        }

    def test_preview_commit_stages_then_cleans_real_collection_without_model_calls(
        self,
    ) -> None:
        preview = self.service.preview_folder_deletion(
            library_id="default",
            folder_key=self.folder_key,
        )
        operation = self._operation(str(preview["operation_id"]))
        stage = Path(str(operation["stage_path"]))

        # Preview is read-only: neither source data nor database rows move yet.
        self.assertTrue(self.source.is_file())
        self.assertFalse(stage.exists())
        self.assertTrue(self.service.repository.contains(self.doc_id))
        self.assertIn(self.doc_id, self.service.state.get_many([self.doc_id]))

        result = self.service.commit_folder_deletion(
            library_id="default",
            operation_id=str(preview["operation_id"]),
            confirmation_token=str(preview["confirmation_token"]),
            confirm=True,
        )

        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["indexed_deleted"], 1)
        self.assertEqual(result["api_requests"], 0)
        self.assertFalse(self.folder.exists())
        self.assertFalse(stage.exists())
        self.assertFalse(self.service.repository.contains(self.doc_id))
        self.assertEqual(self.service.state.get_many([self.doc_id]), {})
        self.assertEqual(self.embedding_client.request_count, self.requests_after_index)

    def test_real_collection_and_payload_are_rolled_back_when_sqlite_commit_fails(
        self,
    ) -> None:
        preview = self.service.preview_folder_deletion(
            library_id="default",
            folder_key=self.folder_key,
        )
        vectors_before = self._vectors()
        with self.service.state.connection:
            self.service.state.connection.execute(
                "CREATE TRIGGER block_real_folder_delete BEFORE DELETE ON entries "
                "BEGIN SELECT RAISE(ABORT, 'simulated SQLite failure'); END"
            )

        result = self.service.commit_folder_deletion(
            library_id="default",
            operation_id=str(preview["operation_id"]),
            confirmation_token=str(preview["confirmation_token"]),
            confirm=True,
        )

        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["needs_attention"])
        self.assertEqual(result["indexed_deleted"], 0)
        self.assertTrue(self.source.is_file())
        self.assertTrue((self.folder / "notes.txt").is_file())
        self.assertTrue(self.service.repository.contains(self.doc_id))
        self.assertIn(self.doc_id, self.service.state.get_many([self.doc_id]))
        self.assertEqual(self._vectors(), vectors_before)
        self.assertEqual(self.embedding_client.request_count, self.requests_after_index)

    def test_recovery_after_staging_finishes_real_collection_and_sqlite_commit(
        self,
    ) -> None:
        preview = self.service.preview_folder_deletion(
            library_id="default",
            folder_key=self.folder_key,
        )
        operation_id = str(preview["operation_id"])
        manager = self.service._folder_deletion_manager("default")
        operation = manager.journal.operation(operation_id)
        assert operation is not None
        manager.journal.update_operation(operation_id, status="staging")
        manager._stage(operation, recovering=False)
        stage = Path(str(operation["stage_path"]))
        manager.close()

        # This is the crash boundary: files moved, both databases still intact.
        self.assertFalse(self.folder.exists())
        self.assertTrue((stage / "payload" / "portrait.png").is_file())
        self.assertTrue(self.service.repository.contains(self.doc_id))
        self.assertIn(self.doc_id, self.service.state.get_many([self.doc_id]))

        # A newly opened manager reads the durable journal, as backend startup does.
        report = self.service.recover_folder_deletions(library_id="default")

        self.assertEqual(report["recovered"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["api_requests"], 0)
        self.assertFalse(self.folder.exists())
        self.assertFalse(stage.exists())
        self.assertFalse(self.service.repository.contains(self.doc_id))
        self.assertEqual(self.service.state.get_many([self.doc_id]), {})
        self.assertEqual(self.embedding_client.request_count, self.requests_after_index)


if __name__ == "__main__":
    unittest.main()
