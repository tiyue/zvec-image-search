from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_SERVER = Path(__file__).with_name("mock_dashscope_server.py")
APP_UID = "10001"
APP_GID = "10001"


class SmokeFailure(RuntimeError):
    pass


def _run(
    command: list[str],
    *,
    check: bool = True,
    timeout: float = 180,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        rendered = subprocess.list2cmdline(command)
        raise SmokeFailure(
            f"Command failed ({result.returncode}): {rendered}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _docker(*arguments: str, check: bool = True, timeout: float = 180):
    return _run(["docker", *arguments], check=check, timeout=timeout)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _json_output(output: str) -> Any:
    decoder = json.JSONDecoder()
    for index, character in enumerate(output):
        if character not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if not output[index + end :].strip():
            return value
    raise SmokeFailure(f"No trailing JSON value found in output:\n{output}")


def _png_bytes(red: int, green: int, blue: int) -> bytes:
    width = 4
    height = 4

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", checksum)
        )

    row = bytes((red, green, blue)) * width
    pixels = b"".join(b"\x00" + row for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bind(source: Path, target: str, *, read_only: bool = False) -> str:
    mount = f"type=bind,source={source.resolve()},target={target}"
    return f"{mount},readonly" if read_only else mount


class DockerSmoke:
    def __init__(self, image: str, max_size_mib: float):
        suffix = uuid.uuid4().hex
        self.image = image
        self.max_size_bytes = int(max_size_mib * 1024 * 1024)
        self.network = f"zvec-smoke-{suffix}"
        self.volume = f"zvec-smoke-workspace-{suffix}"
        self.mock_name = f"zvec-smoke-mock-{suffix}"
        self.lock_name = f"zvec-smoke-lock-{suffix}"
        self.interrupt_name = f"zvec-smoke-interrupt-{suffix}"
        self.temp_root = Path(tempfile.mkdtemp(prefix="zvec-docker-smoke-"))
        self.images = self.temp_root / "\u56fe\u5e93 space"
        self.queries = self.temp_root / "\u67e5\u8be2 space"
        self.results = self.temp_root / "results"
        self.images.mkdir()
        self.queries.mkdir()
        self.results.mkdir()
        if os.name != "nt":
            self.images.chmod(0o755)
            self.queries.chmod(0o755)
            self.results.chmod(0o777)

    def cleanup(self) -> None:
        for container in (self.interrupt_name, self.lock_name, self.mock_name):
            _docker("rm", "--force", container, check=False, timeout=30)
        _docker("network", "rm", self.network, check=False, timeout=30)
        _docker("volume", "rm", "--force", self.volume, check=False, timeout=30)
        shutil.rmtree(self.temp_root, ignore_errors=True)

    def app_command(
        self,
        arguments: list[str],
        *,
        extra_mounts: list[str] | None = None,
        entrypoint: str | None = None,
        remove: bool = True,
        detach: bool = False,
        name: str | None = None,
    ) -> list[str]:
        command = ["docker", "run"]
        if remove:
            command.append("--rm")
        if detach:
            command.append("--detach")
        if name:
            command.extend(("--name", name))
        command.extend(
            (
                "--init",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=256m",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--network",
                self.network,
                "--env",
                "DASHSCOPE_API_KEY=test-key-not-real",
                "--env",
                f"DASHSCOPE_API_URL=http://{self.mock_name}:8000/embeddings",
                "--mount",
                _bind(self.images, "/data/roots/main", read_only=True),
                "--mount",
                f"type=volume,source={self.volume},target=/data/workspace",
                "--mount",
                _bind(self.results, "/data/results"),
            )
        )
        for mount in extra_mounts or []:
            command.extend(("--mount", mount))
        if entrypoint:
            command.extend(("--entrypoint", entrypoint))
        command.append(self.image)
        command.extend(arguments)
        return command

    def run_app(
        self,
        *arguments: str,
        extra_mounts: list[str] | None = None,
        entrypoint: str | None = None,
        check: bool = True,
        timeout: float = 180,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            self.app_command(
                list(arguments), extra_mounts=extra_mounts, entrypoint=entrypoint
            ),
            check=check,
            timeout=timeout,
        )

    def start_resources(self) -> None:
        _docker("network", "create", self.network)
        _docker("volume", "create", self.volume)
        _docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "/bin/sh",
            "--mount",
            f"type=volume,source={self.volume},target=/data/workspace",
            self.image,
            "-c",
            f"chown {APP_UID}:{APP_GID} /data/workspace",
        )
        _docker(
            "run",
            "--detach",
            "--name",
            self.mock_name,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=32m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--network",
            self.network,
            "--mount",
            _bind(MOCK_SERVER, "/mock/server.py", read_only=True),
            "--entrypoint",
            "python",
            self.image,
            "/mock/server.py",
        )
        for _ in range(40):
            logs = _docker("logs", self.mock_name, check=False).stdout
            if "mock-ready" in logs:
                return
            time.sleep(0.25)
        raise SmokeFailure("Mock DashScope server did not become ready.")

    def verify_image(self) -> dict[str, Any]:
        inspect = _json_output(
            _docker("inspect", "--type", "image", self.image).stdout
        )[0]
        size = int(inspect["Size"])
        _require(
            size <= self.max_size_bytes,
            f"Image is {size / 1024 / 1024:.2f} MiB; limit is "
            f"{self.max_size_bytes / 1024 / 1024:.2f} MiB.",
        )
        _require(inspect["Config"]["User"] == "app:app", "Unexpected image user.")
        _require(
            not any(
                value.startswith("DASHSCOPE_API_KEY=")
                for value in inspect["Config"].get("Env", [])
            ),
            "DashScope API key is baked into the image config.",
        )
        uid = _docker(
            "run", "--rm", "--entrypoint", "id", self.image, "-u"
        ).stdout.strip()
        _require(uid == APP_UID, f"Expected UID {APP_UID}, received {uid!r}.")
        _docker("run", "--rm", self.image, "--help")

        versions_code = (
            "import json; from importlib.metadata import version; "
            "print(json.dumps({n: version(n) for n in "
            "['zvec-image-search', 'zvec', 'numpy', 'Pillow']}))"
        )
        versions = _json_output(
            _docker(
                "run",
                "--rm",
                "--entrypoint",
                "python",
                self.image,
                "-c",
                versions_code,
            ).stdout
        )
        expected = {
            "zvec-image-search": "0.4.0",
            "zvec": "0.5.1",
            "numpy": "2.5.1",
            "Pillow": "12.3.0",
        }
        _require(versions == expected, f"Unexpected package versions: {versions}")

        formats_code = """
from pathlib import Path
from PIL import Image

root = Path("/tmp/formats")
root.mkdir()
specs = [
    ("jpg", "JPEG", (32, 32)),
    ("png", "PNG", (32, 32)),
    ("webp", "WEBP", (32, 32)),
    ("bmp", "BMP", (32, 32)),
    ("tiff", "TIFF", (32, 32)),
    ("ico", "ICO", (64, 64)),
    ("dib", "DIB", (32, 32)),
    ("icns", "ICNS", (512, 512)),
    ("sgi", "SGI", (32, 32)),
]
for extension, image_format, size in specs:
    path = root / f"test.{extension}"
    Image.new("RGB", size, (120, 30, 220)).save(path, format=image_format)
    with Image.open(path) as image:
        image.load()
        print(image.format)
"""
        formats = _docker(
            "run",
            "--rm",
            "--entrypoint",
            "python",
            self.image,
            "-c",
            formats_code,
        ).stdout.splitlines()
        _require(
            formats
            == ["JPEG", "PNG", "WEBP", "BMP", "TIFF", "ICO", "DIB", "ICNS", "SGI"],
            f"Supported image format check failed: {formats}",
        )

        content_code = (
            "from pathlib import Path; "
            "root=Path('/usr/local/lib/python3.12/site-packages'); "
            "print(sum(1 for _ in root.rglob('*.pyc'))); "
            "print(int(Path('/wheels').exists())); "
            "print(sum(1 for base in [Path('/data/workspace'), Path('/data/results')] "
            "for path in base.rglob('*') if path.is_file())); "
            "print(int((root / 'pip').exists())); "
            "print(int((root / 'zvec' / 'data').exists())); "
            "print(int(any((root / 'pillow.libs').glob('libavif-*.so*'))))"
        )
        content_lines = _docker(
            "run",
            "--rm",
            "--entrypoint",
            "python",
            self.image,
            "-c",
            content_code,
        ).stdout.splitlines()
        _require(
            content_lines == ["0", "0", "0", "0", "0", "0"],
            "Unexpected image build data.",
        )
        env_files = _docker(
            "run",
            "--rm",
            "--user",
            "0:0",
            "--entrypoint",
            "find",
            self.image,
            "/",
            "-xdev",
            "-type",
            "f",
            "-name",
            ".env",
            "-print",
        ).stdout.strip()
        _require(not env_files, f"Unexpected .env file in image: {env_files}")
        return {"size_mib": round(size / 1024 / 1024, 2), "versions": versions}

    def verify_runtime(self) -> dict[str, Any]:
        red = self.images / "red.png"
        blue = self.images / "blue.png"
        external = self.queries / "external.png"
        red.write_bytes(_png_bytes(255, 0, 0))
        blue.write_bytes(_png_bytes(0, 0, 255))
        external.write_bytes(_png_bytes(0, 255, 0))
        original_hashes = {path.name: _sha256(path) for path in (red, blue, external)}
        blue_payload = blue.read_bytes()

        permission_script = (
            "test -r /data/roots/main && "
            "test ! -w /data/roots/main && "
            "test -w /data/workspace && "
            "test -w /data/results"
        )
        self.run_app("-c", permission_script, entrypoint="/bin/sh")

        first_stats = _json_output(self.run_app("stats").stdout)
        _require(
            first_stats["collection_stats"]["doc_count"] == 0,
            "New collection is not empty.",
        )

        indexed = _json_output(
            self.run_app("index", "/data/roots/main", "library", "primary").stdout
        )
        _require(indexed["inserted"] == 2, f"Unexpected index report: {indexed}")
        _require(indexed["failed"] == 0, f"Index failures: {indexed['failures']}")

        second_stats = _json_output(self.run_app("stats").stdout)
        third_stats = _json_output(self.run_app("stats").stdout)
        _require(
            second_stats["collection_stats"]["doc_count"] == 2, "Index count is not 2."
        )
        _require(second_stats["tracked_files"] == 2, "Tracked file count is not 2.")
        _require(
            second_stats["roots"][0]["tags"] == ["library", "primary"],
            "Indexed root tags did not persist.",
        )
        _require(
            second_stats["collection_uuid"] == third_stats["collection_uuid"],
            "Collection did not persist across containers.",
        )

        text_report = _json_output(
            self.run_app(
                "search",
                "--text",
                "red image",
                "--tk",
                "1",
                "--tags",
                "library",
                "primary",
            ).stdout
        )
        _require(text_report["result_count"] == 1, "Text search returned no result.")
        _require(
            text_report["embedding_sources"]["text"] == "api",
            "Text search did not use API.",
        )
        _require(
            text_report["results"][0]["tags"] == ["library", "primary"],
            "Search result did not include tags.",
        )
        no_tag_match = _json_output(
            self.run_app(
                "search",
                "--text",
                "red image",
                "--tk",
                "1",
                "--tags",
                "missing",
            ).stdout
        )
        _require(no_tag_match["result_count"] == 0, "Tag filter was not applied.")

        image_report = _json_output(
            self.run_app(
                "search", "--image", "/data/roots/main/red.png", "--tk", "1"
            ).stdout
        )
        _require(image_report["result_count"] == 1, "Image search returned no result.")
        _require(
            image_report["embedding_sources"]["image"] == "index",
            "Indexed image vector was not reused.",
        )

        query_mount = [_bind(self.queries, "/data/query", read_only=True)]
        external_report = _json_output(
            self.run_app(
                "search",
                "--image",
                "/data/query/external.png",
                "--tk",
                "1",
                extra_mounts=query_mount,
            ).stdout
        )
        _require(
            external_report["result_count"] == 1,
            "External image search returned no result.",
        )
        _require(
            external_report["embedding_sources"]["image"] == "api",
            "External image did not use API.",
        )

        mixed_report = _json_output(
            self.run_app(
                "search",
                "--image",
                "/data/query/external.png",
                "--text",
                "green image",
                "--tk",
                "1",
                extra_mounts=query_mount,
            ).stdout
        )
        _require(mixed_report["result_count"] == 1, "Mixed search returned no result.")
        _require(
            mixed_report["embedding_sources"]["text"] == "api",
            "Mixed text did not use API.",
        )

        for report in (
            text_report,
            no_tag_match,
            image_report,
            external_report,
            mixed_report,
        ):
            output_dir = self.results / Path(report["output_dir"]).name
            _require(
                (output_dir / "results.json").is_file(), "Missing search manifest."
            )
            if report["result_count"]:
                _require(
                    any(output_dir.glob("*.png")),
                    "Search result image was not copied.",
                )

        clean_report = _json_output(
            self.run_app("clean-results", "--days", "7", "--dry-run").stdout
        )
        _require(not clean_report["failures"], "Result cleanup dry-run failed.")
        cache_report = _json_output(self.run_app("cache-clear").stdout)
        _require(cache_report["deleted"] >= 3, "Expected cached query embeddings.")

        blue.unlink()
        dry_sync = _json_output(
            self.run_app("sync", "/data/roots/main", "--dry-run").stdout
        )
        _require(
            dry_sync["would_delete"] == 1, "Sync dry-run did not find stale record."
        )
        _require(
            _json_output(self.run_app("stats").stdout)["collection_stats"]["doc_count"]
            == 2,
            "Sync dry-run changed the collection.",
        )
        synced = _json_output(self.run_app("sync", "/data/roots/main").stdout)
        _require(synced["deleted"] == 1, "Sync did not delete stale record.")
        blue.write_bytes(blue_payload)
        restored = _json_output(self.run_app("index", "/data/roots/main").stdout)
        _require(restored["inserted"] == 1, "Restored image was not indexed.")

        self.verify_lock()
        self.verify_interrupt()

        final_stats = _json_output(self.run_app("stats").stdout)
        _require(
            final_stats["collection_stats"]["doc_count"] == 2,
            "Final collection count is not 2.",
        )
        for path in (red, blue, external):
            _require(
                _sha256(path) == original_hashes[path.name],
                f"Source image changed: {path.name}",
            )
        return {
            "collection_uuid": final_stats["collection_uuid"],
            "doc_count": final_stats["collection_stats"]["doc_count"],
            "result_directories": len(
                [path for path in self.results.iterdir() if path.is_dir()]
            ),
        }

    def verify_lock(self) -> None:
        code = (
            "import time; from pathlib import Path; "
            "from image_vector_service.process_lock import ProcessLock; "
            "lock=ProcessLock(Path('/data/workspace/.image_collection.lock')); "
            "lock.acquire(); print('locked', flush=True); time.sleep(30)"
        )
        command = [
            "docker",
            "run",
            "--detach",
            "--name",
            self.lock_name,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=32m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--mount",
            f"type=volume,source={self.volume},target=/data/workspace",
            "--entrypoint",
            "python",
            self.image,
            "-c",
            code,
        ]
        _run(command)
        try:
            for _ in range(40):
                if "locked" in _docker("logs", self.lock_name, check=False).stdout:
                    break
                time.sleep(0.25)
            else:
                raise SmokeFailure("Lock holder did not acquire the workspace lock.")
            contender = self.run_app("stats", check=False)
            _require(
                contender.returncode == 1, "Concurrent workspace access did not fail."
            )
            _require(
                "Another image service process" in contender.stderr,
                f"Unexpected lock error: {contender.stderr}",
            )
        finally:
            _docker("rm", "--force", self.lock_name, check=False, timeout=30)

    def verify_interrupt(self) -> None:
        command = self.app_command(
            ["search", "--text", "__slow__", "--tk", "1"],
            remove=False,
            detach=True,
            name=self.interrupt_name,
        )
        _run(command)
        try:
            for _ in range(60):
                logs = _docker("logs", self.mock_name, check=False).stdout
                if "mock-slow-request" in logs:
                    break
                time.sleep(0.25)
            else:
                raise SmokeFailure("Interrupt test did not reach the mock API.")
            _docker("kill", "--signal", "SIGINT", self.interrupt_name)
            exit_code = _docker("wait", self.interrupt_name).stdout.strip()
            logs = _docker("logs", self.interrupt_name, check=False)
            _require(
                exit_code == "130", f"SIGINT exit code was {exit_code!r}, not 130."
            )
            _require(
                "Interrupted. Completed records remain indexed." in logs.stderr,
                "SIGINT message was not emitted.",
            )
        finally:
            _docker("rm", "--force", self.interrupt_name, check=False, timeout=30)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real Docker smoke tests.")
    parser.add_argument("image", nargs="?", default="zvec-image-search:local")
    parser.add_argument("--max-size-mib", type=float, default=80.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    smoke = DockerSmoke(args.image, args.max_size_mib)
    try:
        print(f"Verifying image {args.image} ...", flush=True)
        image_report = smoke.verify_image()
        smoke.start_resources()
        print("Running isolated index/search/persistence checks ...", flush=True)
        runtime_report = smoke.verify_runtime()
        print(
            json.dumps(
                {"image": args.image, **image_report, **runtime_report},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (SmokeFailure, subprocess.TimeoutExpired) as exc:
        print(f"Docker smoke test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        smoke.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
