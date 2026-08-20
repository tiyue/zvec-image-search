from __future__ import annotations

import gc
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import zvec_launcher
from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.service import ImageVectorService
from zvec_launcher import (
    LauncherError,
    LegacyDockerVolumeRequired,
    NativeLibrary,
    load_config,
    repair_and_verify_workspace,
    validate_native_config,
)


class FakeEmbeddingClient:
    def __init__(self, dimension: int):
        self.dimension = dimension
        self.request_count = 0

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        self.request_count += 1
        vectors = [
            [1.0, 0.01, *([0.0] * (self.dimension - 2))] for _path in image_paths
        ]
        return EmbeddingResponse(
            vectors=vectors,
            request_id=f"fake-{self.request_count}",
            usage={"images": len(image_paths)},
        )


class NativeLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec_native_launcher_test_"))
        self.config_home = self.root / "config"
        self.images = self.root / "images"
        self.workspace = self.root / "workspace"
        self.results = self.root / "results"
        self.images.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "ZVEC_CONFIG_HOME": str(self.config_home),
                "ZVEC_NATIVE_USE_SOURCE": "1",
            },
            clear=False,
        )
        self.environment.start()
        os.environ.pop("ZVEC_DOCKER_CONFIG_HOME", None)
        os.environ.pop("ZVEC_LIBRARY_ID", None)

    def tearDown(self) -> None:
        self.environment.stop()
        gc.collect()
        shutil.rmtree(self.root, ignore_errors=True)

    def write_config(self, payload: dict) -> Path:
        self.config_home.mkdir(parents=True, exist_ok=True)
        path = self.config_home / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def legacy_payload(self, workspace_type: str, workspace_source: str) -> dict:
        return {
            "schema_version": 2,
            "image_name": "zvec-image-search:legacy",
            "python_executable": "custom-python.exe",
            "results_directory": str(self.results.resolve()),
            "default_library_id": "library-main",
            "libraries": [
                {
                    "id": "library-main",
                    "name": "Main",
                    "image_root": str(self.images.resolve()),
                    "workspace_type": workspace_type,
                    "workspace_source": workspace_source,
                    "enabled": True,
                }
            ],
        }

    def test_init_writes_native_schema_and_forwards_host_paths(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = zvec_launcher.main(
                [
                    "init",
                    str(self.images),
                    "--workspace",
                    str(self.workspace),
                    "--results",
                    str(self.results),
                    "--skip-key",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads((self.config_home / "config.json").read_text())
        self.assertEqual(payload["schema_version"], 3)
        self.assertNotIn("python_executable", payload)
        self.assertNotIn("image_name", payload)
        self.assertNotIn("workspace_type", payload["libraries"][0])
        self.assertNotIn("workspace_source", payload["libraries"][0])
        self.assertEqual(
            payload["libraries"][0]["workspace_directory"],
            str(self.workspace.resolve()),
        )

        query = self.root / "query image.jpg"
        query.write_bytes(b"not-opened-by-routing-test")
        with patch.object(zvec_launcher, "_run_core", return_value=23) as run_core:
            code = zvec_launcher.main(["search-image", str(query), "--tk", "4"])
        self.assertEqual(code, 23)
        forwarded = run_core.call_args.args[2]
        self.assertEqual(forwarded[:3], ["search", "--image", str(query.resolve())])
        self.assertEqual(forwarded[3:], ["--tk", "4"])

    def test_legacy_command_experience_routes_to_native_core(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(
                zvec_launcher.main(
                    [
                        "init",
                        str(self.images),
                        "--workspace",
                        str(self.workspace),
                        "--results",
                        str(self.results),
                        "--skip-key",
                    ]
                ),
                0,
            )
        query = self.root / "query.jpg"
        query.write_bytes(b"routing-only")
        cases = (
            (
                ["index", "new", "featured"],
                ["index", str(self.images.resolve()), "new"],
            ),
            (
                ["sync", "--dry-run"],
                ["sync", str(self.images.resolve()), "--dry-run"],
            ),
            (["search", "sunset", "--tk", "3"], ["search", "--text", "sunset"]),
            (
                ["search-image", str(query), "--tk", "2"],
                ["search", "--image", str(query.resolve())],
            ),
            (
                ["search-mix", str(query), "portrait", "--tk", "5"],
                ["search", "--image", str(query.resolve()), "--text", "portrait"],
            ),
            (["stats"], ["stats"]),
            (["roots"], ["roots"]),
            (["cache-clear"], ["cache-clear"]),
            (["clean", "14", "--dry-run"], ["clean-results", "--days", "14"]),
            (["migrate-schema", "--dry-run"], ["migrate-schema", "--dry-run"]),
            (["raw", "serve", "--port", "9000"], ["serve", "--port", "9000"]),
        )
        for command, expected_prefix in cases:
            with (
                self.subTest(command=command),
                patch.object(zvec_launcher, "_run_core", return_value=0) as run_core,
            ):
                self.assertEqual(zvec_launcher.main(command), 0)
                forwarded = run_core.call_args.args[2]
                self.assertEqual(forwarded[: len(expected_prefix)], expected_prefix)

        # rebind-root keeps the stable root id but forwards a real host path.
        with patch.object(zvec_launcher, "_run_core", return_value=0) as run_core:
            self.assertEqual(zvec_launcher.main(["rebind-root", "root-1"]), 0)
        self.assertEqual(
            run_core.call_args.args[2],
            ["rebind-root", "root-1", str(self.images.resolve())],
        )

    def test_legacy_bind_workspace_is_migrated_without_docker(self) -> None:
        self.workspace.mkdir()
        path = self.write_config(
            self.legacy_payload("bind", str(self.workspace.resolve()))
        )
        with redirect_stderr(StringIO()):
            config = load_config()
        assert config is not None
        self.assertEqual(
            config.libraries[0].workspace_directory,
            self.workspace.resolve(),
        )
        migrated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["python_executable"], "custom-python.exe")
        self.assertTrue((self.config_home / "config.v2.docker.backup.json").is_file())
        manifests = list(
            (self.config_home / "migration-backups").glob(
                "*/migration-backup-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["api_requests"], 0)

    def test_schema_v1_bind_migration_creates_manifest_before_v3_write(self) -> None:
        self.workspace.mkdir()
        path = self.write_config(
            {
                "schema_version": 1,
                "image_name": "zvec-image-search:legacy",
                "image_root": str(self.images.resolve()),
                "workspace_type": "bind",
                "workspace_source": str(self.workspace.resolve()),
                "results_directory": str(self.results.resolve()),
            }
        )
        with redirect_stderr(StringIO()):
            config = load_config()
        assert config is not None
        self.assertEqual(json.loads(path.read_text())["schema_version"], 3)
        self.assertTrue((self.config_home / "config.v1.docker.backup.json").is_file())
        manifests = list(
            (self.config_home / "migration-backups").glob(
                "*/migration-backup-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)

    def test_python_executable_survives_schema_v3_cli_mutations(self) -> None:
        second_images = self.root / "second-images"
        second_workspace = self.root / "second-workspace"
        moved_images = self.root / "moved-images"
        updated_results = self.root / "updated-results"
        for path in (
            self.workspace,
            second_images,
            second_workspace,
            moved_images,
        ):
            path.mkdir()
        self.write_config(
            {
                "schema_version": 3,
                "python_executable": "  custom-python.exe  ",
                "results_directory": str(self.results.resolve()),
                "default_library_id": "library-main",
                "libraries": [
                    {
                        "id": "library-main",
                        "name": "Main",
                        "image_root": str(self.images.resolve()),
                        "workspace_directory": str(self.workspace.resolve()),
                        "enabled": True,
                    },
                    {
                        "id": "library-second",
                        "name": "Second",
                        "image_root": str(second_images.resolve()),
                        "workspace_directory": str(second_workspace.resolve()),
                        "enabled": True,
                    },
                ],
            }
        )

        def assert_preserved() -> None:
            payload = json.loads(
                (self.config_home / "config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["python_executable"], "custom-python.exe")

        with redirect_stdout(StringIO()):
            self.assertEqual(
                zvec_launcher.main(["library-rename", "library-main", "Renamed main"]),
                0,
            )
        assert_preserved()

        with (
            patch.object(zvec_launcher, "_run_core", return_value=0),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                zvec_launcher.main(
                    ["rebind-root", "root-1", str(moved_images.resolve())]
                ),
                0,
            )
        assert_preserved()

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(
                zvec_launcher.main(
                    [
                        "init",
                        str(moved_images.resolve()),
                        "--workspace",
                        str(self.workspace.resolve()),
                        "--results",
                        str(updated_results.resolve()),
                        "--skip-key",
                    ]
                ),
                0,
            )
        assert_preserved()

        with redirect_stdout(StringIO()):
            self.assertEqual(
                zvec_launcher.main(["library-default", "library-second"]), 0
            )
            self.assertEqual(zvec_launcher.main(["library-remove", "library-main"]), 0)
        assert_preserved()

    def test_legacy_bind_repair_failure_keeps_recoverable_v2_config(self) -> None:
        self.workspace.mkdir()
        path = self.write_config(
            self.legacy_payload("bind", str(self.workspace.resolve()))
        )
        with (
            patch.object(
                zvec_launcher,
                "repair_and_verify_workspace",
                side_effect=LauncherError("repair failed"),
            ),
            self.assertRaisesRegex(LauncherError, "repair failed"),
        ):
            load_config()
        self.assertEqual(json.loads(path.read_text())["schema_version"], 2)
        backup = self.config_home / "config.v2.docker.backup.json"
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(backup.read_text())["schema_version"], 2)

    def test_named_volume_requires_explicit_export(self) -> None:
        self.write_config(
            self.legacy_payload("volume", "zvec-image-workspace-existing")
        )
        with self.assertRaises(LegacyDockerVolumeRequired):
            load_config()

    def test_volume_export_rejects_docker_image_option_injection(self) -> None:
        destination = self.root / "malicious-export"
        with (
            patch.object(zvec_launcher, "_run_checked") as run_checked,
            self.assertRaisesRegex(LauncherError, "Invalid Docker image repository"),
        ):
            zvec_launcher.export_docker_volume(
                "zvec-image-workspace-existing",
                "--privileged",
                destination,
            )
        run_checked.assert_not_called()
        self.assertFalse(destination.exists())

    def test_volume_migration_rejects_library_id_path_traversal_before_export(
        self,
    ) -> None:
        payload = self.legacy_payload("volume", "zvec-image-workspace-existing")
        payload["default_library_id"] = "../../../outside"
        payload["libraries"][0]["id"] = "../../../outside"
        self.write_config(payload)
        with (
            patch.object(zvec_launcher, "export_docker_volume") as export,
            redirect_stderr(StringIO()),
        ):
            code = zvec_launcher.main(["migrate-docker-workspace"])
        self.assertEqual(code, 1)
        export.assert_not_called()
        self.assertFalse((self.root / "outside").exists())

    def test_volume_export_is_staged_and_bound_to_source_volume(self) -> None:
        destination = self.root / "safe-export"
        commands: list[list[str]] = []

        def fake_run_checked(
            command: list[str] | tuple[str, ...], _description: str
        ) -> subprocess.CompletedProcess[str]:
            values = list(command)
            commands.append(values)
            if values[1] == "cp":
                staging = Path(values[-1])
                (staging / "image_collection").mkdir()
                (staging / "image_collection.meta.json").write_text(
                    "{}", encoding="utf-8"
                )
                (staging / "image_collection.state.sqlite3").write_bytes(b"state")
            return subprocess.CompletedProcess(values, 0, "", "")

        with (
            patch.object(zvec_launcher, "_run_checked", fake_run_checked),
            patch.object(subprocess, "run") as cleanup,
        ):
            report = zvec_launcher.export_docker_volume(
                "zvec-image-workspace-existing",
                "registry.example.com:5000/zvec/image-search:legacy",
                destination,
            )

        self.assertEqual(report["status"], "exported")
        create = commands[0]
        separator = create.index("--")
        self.assertEqual(
            create[separator + 1],
            "registry.example.com:5000/zvec/image-search:legacy",
        )
        self.assertTrue((destination / "image_collection").is_dir())
        receipt = json.loads(
            (destination / zvec_launcher.DOCKER_EXPORT_RECEIPT).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["volume"], "zvec-image-workspace-existing")
        cleanup.assert_called_once()

        with self.assertRaisesRegex(LauncherError, "does not match Docker volume"):
            zvec_launcher.export_docker_volume(
                "different-volume",
                "zvec-image-search:legacy",
                destination,
            )

    def test_partial_export_without_receipt_is_never_reused(self) -> None:
        destination = self.root / "partial-export"
        (destination / "image_collection").mkdir(parents=True)
        (destination / "image_collection.meta.json").write_text("{}", encoding="utf-8")
        (destination / "image_collection.state.sqlite3").write_bytes(b"state")
        with self.assertRaisesRegex(LauncherError, "no verified export receipt"):
            zvec_launcher.export_docker_volume(
                "zvec-image-workspace-existing",
                "zvec-image-search:legacy",
                destination,
            )

    def test_native_config_rejects_runtime_data_inside_image_root(self) -> None:
        with self.assertRaisesRegex(LauncherError, "Results directory cannot overlap"):
            validate_native_config(
                {
                    "schema_version": 3,
                    "results_directory": str(self.images / "generated-results"),
                    "default_library_id": "library-main",
                    "libraries": [
                        {
                            "id": "library-main",
                            "name": "Main",
                            "image_root": str(self.images),
                            "workspace_directory": str(self.workspace),
                            "enabled": True,
                        }
                    ],
                }
            )

    def test_workspace_backup_command_supports_dry_run_and_creation(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(
                zvec_launcher.main(
                    [
                        "init",
                        str(self.images),
                        "--workspace",
                        str(self.workspace),
                        "--results",
                        str(self.results),
                        "--skip-key",
                    ]
                ),
                0,
            )
        destination = self.root / "manual-backup"
        output = StringIO()
        with redirect_stdout(output):
            code = zvec_launcher.main(
                [
                    "workspace-backup",
                    "--destination",
                    str(destination),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["plan"]["status"], "ready")
        self.assertFalse(destination.exists())

        output = StringIO()
        with redirect_stdout(output):
            code = zvec_launcher.main(
                ["workspace-backup", "--destination", str(destination)]
            )
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "created")
        self.assertEqual(report["api_requests"], 0)
        self.assertTrue((destination / "migration-backup-manifest.json").is_file())

    def test_explicit_volume_migration_writes_v3_after_verification(self) -> None:
        self.write_config(
            self.legacy_payload("volume", "zvec-image-workspace-existing")
        )
        destination = self.root / "exported-workspace"

        def fake_export(volume: str, image: str, output: Path) -> dict:
            output.mkdir(parents=True)
            return {
                "status": "exported",
                "volume": volume,
                "image": image,
                "destination": str(output),
            }

        with (
            patch.object(zvec_launcher, "export_docker_volume", fake_export),
            patch.object(
                zvec_launcher,
                "repair_and_verify_workspace",
                return_value={"status": "no_index", "api_requests": 0},
            ),
            redirect_stdout(StringIO()),
        ):
            code = zvec_launcher.main(
                [
                    "migrate-docker-workspace",
                    "--destination",
                    str(destination),
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads((self.config_home / "config.json").read_text())
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(
            payload["libraries"][0]["workspace_directory"],
            str(destination.resolve()),
        )
        manifests = list(
            (self.config_home / "migration-backups").glob(
                "*/migration-backup-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)

    def test_failed_volume_repair_still_preserves_legacy_config_backup(self) -> None:
        path = self.write_config(
            self.legacy_payload("volume", "zvec-image-workspace-existing")
        )
        destination = self.root / "failed-export"

        def fake_export(volume: str, image: str, output: Path) -> dict:
            output.mkdir(parents=True)
            return {
                "status": "exported",
                "volume": volume,
                "image": image,
                "destination": str(output),
            }

        with (
            patch.object(zvec_launcher, "export_docker_volume", fake_export),
            patch.object(
                zvec_launcher,
                "repair_and_verify_workspace",
                side_effect=LauncherError("verification failed"),
            ),
            redirect_stderr(StringIO()),
        ):
            code = zvec_launcher.main(
                [
                    "migrate-docker-workspace",
                    "--destination",
                    str(destination),
                ]
            )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(path.read_text())["schema_version"], 2)
        backup = self.config_home / "config.v2.docker.backup.json"
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(backup.read_text())["schema_version"], 2)

    def test_existing_vectors_are_verified_without_an_api_request(self) -> None:
        Image.new("RGB", (16, 16), (255, 0, 0)).save(self.images / "red.png")
        config = ServiceConfig(
            workspace=self.workspace,
            results_directory=self.results,
        )
        client = FakeEmbeddingClient(config.dimension)
        service = ImageVectorService(config=config, embedding_client=client)
        try:
            report = service.index_folder(str(self.images))
            self.assertEqual(report.inserted, 1)
            self.assertEqual(client.request_count, 1)
        finally:
            service.close()
            del service
            gc.collect()

        library = NativeLibrary(
            "library-main",
            "Main",
            self.images.resolve(),
            self.workspace.resolve(),
        )
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}, clear=False):
            verification = repair_and_verify_workspace(
                library,
                self.results.resolve(),
                dry_run=False,
            )
        self.assertEqual(verification["api_requests"], 0)
        self.assertEqual(verification["collection_documents"], 1)
        self.assertEqual(verification["sqlite_entries"], 1)
        self.assertEqual(verification["search_probe"], "passed")

        self.write_config(
            {
                "schema_version": 3,
                "results_directory": str(self.results.resolve()),
                "default_library_id": "library-main",
                "libraries": [library.to_dict()],
            }
        )
        backup = self.root / "verified-migration-backup"
        output = StringIO()
        with redirect_stdout(output):
            code = zvec_launcher.main(
                [
                    "migrate-docker-workspace",
                    "--backup-directory",
                    str(backup),
                ]
            )
        self.assertEqual(code, 0)
        migration = json.loads(output.getvalue())
        self.assertEqual(migration["api_requests"], 0)
        self.assertEqual(migration["collection_documents"], 1)
        self.assertEqual(migration["sqlite_entries"], 1)
        self.assertEqual(migration["search_probe"], "passed")
        self.assertTrue((backup / "migration-backup-manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
