from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.provision_nsis import (
    ArchiveIdentity,
    NsisProvisionError,
    extract_archive,
    provision_nsis,
    verify_archive,
)


def _write_archive(path: Path, *, member: str = "nsis-test/makensis.exe") -> bytes:
    content = b"portable-nsis"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
    return content


def _identity(path: Path) -> ArchiveIdentity:
    content = path.read_bytes()
    return ArchiveIdentity(
        size=len(content),
        md5=hashlib.md5(content).hexdigest(),  # noqa: S324 - test fixture
        sha256=hashlib.sha256(content).hexdigest(),
    )


class NsisProvisionTest(unittest.TestCase):
    def test_script_help_runs_without_an_installed_project(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                str(repository / "scripts" / "provision_nsis.py"),
                "--help",
            ],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Provision the pinned portable NSIS", completed.stdout)

    def test_archive_identity_is_verified_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "nsis.zip"
            _write_archive(archive)
            expected = _identity(archive)

            actual = verify_archive(
                archive,
                expected_size=expected.size,
                expected_md5=expected.md5,
                expected_sha256=expected.sha256,
            )

            self.assertEqual(actual, expected)
            with self.assertRaisesRegex(NsisProvisionError, "identity mismatch"):
                verify_archive(
                    archive,
                    expected_size=expected.size,
                    expected_md5=expected.md5,
                    expected_sha256="0" * 64,
                )

    def test_extraction_rejects_traversal_drive_and_symbolic_link(self) -> None:
        unsafe_members = ("../escape.exe", r"..\escape.exe", "C:/escape.exe")
        for member in unsafe_members:
            with self.subTest(member=member), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                archive = root / "unsafe.zip"
                _write_archive(archive, member=member)
                with self.assertRaisesRegex(NsisProvisionError, "unsafe path"):
                    extract_archive(archive, root / "output")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "link.zip"
            info = zipfile.ZipInfo("nsis-test/makensis.exe")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(info, "target")
            with self.assertRaisesRegex(NsisProvisionError, "symbolic link"):
                extract_archive(archive, root / "output")

    def test_official_archive_prefers_documented_root_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "nsis.zip"
            with zipfile.ZipFile(
                archive,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as package:
                package.writestr("nsis-3.12/makensis.exe", b"root-compiler")
                package.writestr("nsis-3.12/Bin/makensis.exe", b"bin-compiler")

            selected = extract_archive(archive, root / "output")

            self.assertEqual(selected.read_bytes(), b"root-compiler")
            self.assertEqual(
                selected.relative_to((root / "output").resolve()).parts,
                ("nsis-3.12", "makensis.exe"),
            )

    def test_provision_is_atomic_and_reuses_verified_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "nsis.zip"
            executable = _write_archive(archive)
            identity = _identity(archive)
            output = root / "toolchain"

            with (
                patch(
                    "scripts.provision_nsis.verify_archive",
                    return_value=identity,
                ),
                patch.multiple(
                    "scripts.provision_nsis",
                    NSIS_ARCHIVE_SIZE=identity.size,
                    NSIS_ARCHIVE_MD5=identity.md5,
                    NSIS_ARCHIVE_SHA256=identity.sha256,
                ),
            ):
                first = provision_nsis(output, archive_path=archive)
                second = provision_nsis(output, archive_path=archive)

            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(first.makensis_path.read_bytes(), executable)
            marker = json.loads(
                (output / ".zvec-nsis-provision.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["archive_sha256"], identity.sha256)

    def test_provision_canonicalizes_an_aliased_staging_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            physical_parent = root / "long-parent-name"
            aliased_parent = root / "alias"
            physical_parent.mkdir()
            try:
                aliased_parent.symlink_to(physical_parent, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory aliases are unavailable: {exc}")

            archive = root / "nsis.zip"
            executable = _write_archive(archive)
            identity = _identity(archive)
            output = aliased_parent / "toolchain"

            with (
                patch(
                    "scripts.provision_nsis.verify_archive",
                    return_value=identity,
                ),
                patch.multiple(
                    "scripts.provision_nsis",
                    NSIS_ARCHIVE_SIZE=identity.size,
                    NSIS_ARCHIVE_MD5=identity.md5,
                    NSIS_ARCHIVE_SHA256=identity.sha256,
                ),
            ):
                result = provision_nsis(output, archive_path=archive)

            self.assertEqual(result.output_directory, output.resolve())
            self.assertEqual(result.makensis_path.read_bytes(), executable)

    def test_unverified_existing_output_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "toolchain"
            output.mkdir()
            (output / "user-file.txt").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(NsisProvisionError, "Refusing to replace"):
                provision_nsis(output)

            self.assertEqual(
                (output / "user-file.txt").read_text(encoding="utf-8"),
                "keep",
            )


if __name__ == "__main__":
    unittest.main()
