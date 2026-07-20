from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.verify_webview_preview_release import (
    WebviewReleaseVerificationError,
    verify_extracted_installer,
    verify_portable_archive,
    write_release_checksums,
)
from scripts.webview_preview_packaging import (
    ENTRY_POINTS,
    PRODUCT_DIRECTORY,
    write_payload_manifest,
    write_portable_archive,
)


def _write(root: Path, relative: str, content: bytes = b"preview") -> None:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _payload(root: Path) -> Path:
    payload = root / PRODUCT_DIRECTORY
    for entry_point in ENTRY_POINTS:
        _write(payload, entry_point)
    for relative in (
        "_internal/python312.dll",
        "_internal/pythonnet/runtime/Python.Runtime.dll",
        "_internal/webview/lib/Microsoft.Web.WebView2.Core.dll",
        "_internal/webview/lib/Microsoft.Web.WebView2.WinForms.dll",
        "_internal/webview/lib/WebBrowserInterop.x64.dll",
        "_internal/webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
        "_internal/webview/js/api.js",
        "_internal/webview/js/finish.js",
        "_internal/model-catalog.default.json",
        "_internal/assets/Zvec.AppIcon.ico",
        "_internal/zvec/_zvec.cp312-win_amd64.pyd",
        "_internal/PIL/_imaging.cp312-win_amd64.pyd",
        "_internal/zvec_webview/frontend_dist/assets/index-test.js",
        "_internal/zvec_webview/frontend_dist/assets/index-test.css",
    ):
        _write(payload, relative)
    _write(
        payload,
        "_internal/zvec_webview/frontend_dist/index.html",
        (
            b'<!doctype html><script type="module" '
            b'src="./assets/index-test.js"></script>'
            b'<link rel="stylesheet" href="./assets/index-test.css">'
        ),
    )
    _write(
        payload,
        "_internal/zvec_webview/frontend_dist/.vite/manifest.json",
        b'{"index.html":{"file":"assets/index-test.js","isEntry":true,"css":["assets/index-test.css"]}}',
    )
    write_payload_manifest(payload, version="0.4.0")
    return payload


class WebviewPreviewReleaseVerificationTest(unittest.TestCase):
    def test_release_checksums_cover_verified_artifacts_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            portable = root / "Zvec-Webview-Preview-portable.zip"
            installer = root / "Zvec-Webview-Preview-setup.exe"
            report = root / "webview-preview-release-verification.json"
            portable.write_bytes(b"portable")
            installer.write_bytes(b"installer")
            report.write_bytes(b"report")

            output = root / "SHA256SUMS.txt"
            write_release_checksums(output, (portable, installer, report))

            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(
                {line.split("  ", 1)[1] for line in lines},
                {portable.name, installer.name, report.name},
            )

    def test_release_checksums_reject_duplicate_basenames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first" / "artifact.zip"
            second = root / "second" / "artifact.zip"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")

            with self.assertRaisesRegex(
                WebviewReleaseVerificationError,
                "Duplicate release artifact basename",
            ):
                write_release_checksums(root / "SHA256SUMS.txt", (first, second))

    def test_portable_archive_matches_every_payload_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _payload(root)
            archive = root / "portable.zip"
            write_portable_archive(payload, archive)

            result = verify_portable_archive(payload, archive)

        self.assertGreater(result["verified_files"], 3)

    def test_portable_archive_rejects_an_extra_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _payload(root)
            archive = root / "portable.zip"
            write_portable_archive(payload, archive)
            with zipfile.ZipFile(archive, "a") as package:
                package.writestr("unexpected.txt", b"unexpected")

            with self.assertRaisesRegex(
                WebviewReleaseVerificationError,
                "inventory mismatch",
            ):
                verify_portable_archive(payload, archive)

    def test_extracted_installer_matches_manifest_owned_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _payload(root)
            extracted = root / "extracted"
            shutil.copytree(payload, extracted)

            result = verify_extracted_installer(payload, extracted)

        self.assertGreater(result["verified_files"], 3)

    def test_extracted_installer_accepts_generated_uninstaller(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _payload(root)
            extracted = root / "extracted"
            shutil.copytree(payload, extracted)
            (extracted / "Uninstall.exe").write_bytes(b"generated by NSIS")

            result = verify_extracted_installer(payload, extracted)

        self.assertIn("Uninstall.exe", result["support_files"])

    def test_extracted_installer_rejects_a_tampered_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _payload(root)
            extracted = root / "extracted"
            shutil.copytree(payload, extracted)
            (extracted / "Zvec.WebviewPreview.exe").write_bytes(b"tampered")

            with self.assertRaisesRegex(
                WebviewReleaseVerificationError,
                "payload mismatch",
            ):
                verify_extracted_installer(payload, extracted)

    def test_extracted_installer_rejects_an_unexpected_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _payload(root)
            extracted = root / "extracted"
            shutil.copytree(payload, extracted)
            (extracted / "unexpected-tool.exe").write_bytes(b"unexpected")

            with self.assertRaisesRegex(
                WebviewReleaseVerificationError,
                "unexpected files",
            ):
                verify_extracted_installer(payload, extracted)


if __name__ == "__main__":
    unittest.main()
