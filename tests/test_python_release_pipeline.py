from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.assemble_python_release import (
    ReleaseAssemblyError,
    assemble_release,
)
from scripts.generate_python_release_materials import (
    ReleaseMaterialsError,
    generate_release_materials,
)
from scripts.prepare_python_release import (
    ReleaseRequestError,
    validate_release_request,
)
from scripts.python_preview_packaging import (
    ENTRY_POINTS,
    repository_root,
    write_payload_manifest,
    write_portable_archive,
)
from scripts.smoke_python_desktop_installer import _resolve_installer


def _write(root: Path, relative: str, content: bytes = b"release") -> None:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _valid_payload(output: Path) -> Path:
    payload = output / "Zvec-Desktop"
    for entry_point in ENTRY_POINTS:
        _write(payload, entry_point)
    for relative in (
        "model-catalog.default.json",
        "assets/Zvec.AppIcon.ico",
        "_internal/python312.dll",
        "_internal/_tkinter.pyd",
        "_internal/tcl86t.dll",
        "_internal/tk86t.dll",
        "_internal/_tcl_data/init.tcl",
        "_internal/_tk_data/tk.tcl",
        "_internal/PIL/_imaging.cp312-win_amd64.pyd",
        "_internal/zvec/_zvec.cp312-win_amd64.pyd",
        "_internal/zvec/data/jieba_dict/jieba.dict.utf8",
        "_internal/zvec/data/jieba_dict/hmm_model.utf8",
    ):
        _write(payload, relative)
    write_payload_manifest(
        payload,
        version="0.4.0",
        pyinstaller_version="6.16.0",
        signing_status="unsigned",
    )
    return payload


def _write_checksums(directory: Path) -> None:
    lines = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            lines.append(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            )
    (directory / "SHA256SUMS.txt").write_text("".join(lines), encoding="utf-8")


def _revision(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


class PythonReleaseMaterialsTest(unittest.TestCase):
    def test_generates_offline_spdx_provenance_manifest_and_checksums(self) -> None:
        root = repository_root()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            payload = _valid_payload(output)
            write_portable_archive(
                payload,
                output / "Zvec-Desktop-0.4.0-win-x64-portable.zip",
            )
            _write(
                output,
                "Zvec-Desktop-0.4.0-win-x64-unsigned-setup.exe",
                b"synthetic installer",
            )

            result = generate_release_materials(
                repository_root=root,
                output_root=output,
                expected_revision=_revision(root),
            )

            self.assertEqual(result["status"], "ok")
            release = json.loads(
                (output / "desktop-release.json").read_text(encoding="utf-8")
            )
            self.assertEqual(release["product"], "Zvec Desktop")
            self.assertFalse(release["runtime"]["powershell_required"])
            self.assertFalse(release["runtime"]["dotnet_required"])
            self.assertFalse(release["runtime"]["docker_required"])
            sbom = json.loads(
                (output / "Zvec-Desktop-0.4.0.spdx.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
            package_names = {package["name"] for package in sbom["packages"]}
            for expected in ("Pillow", "CPython", "PyInstaller", "NSIS"):
                self.assertIn(expected, package_names)
            self.assertIn(
                "BUILD_DEPENDENCY_OF",
                {
                    relationship["relationshipType"]
                    for relationship in sbom["relationships"]
                },
            )
            provenance = json.loads(
                (output / "Zvec-Desktop-0.4.0.provenance.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                provenance["predicateType"],
                "https://slsa.dev/provenance/v1",
            )
            checksums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
            self.assertIn("Zvec-Desktop-0.4.0-win-x64-portable.zip", checksums)
            self.assertIn("Zvec-Desktop-0.4.0.spdx.json", checksums)
            self.assertEqual(
                release["supply_chain"]["packaging_lock"]["file"],
                "requirements-packaging.txt",
            )

    def test_rejects_signing_status_that_disagrees_with_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            _valid_payload(output)
            with self.assertRaisesRegex(ReleaseMaterialsError, "signing_status"):
                generate_release_materials(
                    repository_root=repository_root(),
                    output_root=output,
                    signing_status="authenticode",
                )


class PythonReleasePolicyTest(unittest.TestCase):
    def test_branch_preview_is_explicit_and_uses_python_desktop_policy(self) -> None:
        root = repository_root()
        outputs = validate_release_request(
            repository_root=root,
            version="0.4.0",
            requested_prerelease=False,
            search_quality_gate_path="",
            allow_uncertified_preview=True,
            require_authenticode=False,
            github_ref="refs/heads/codex/collection",
            github_sha=_revision(root),
        )
        self.assertEqual(outputs["exact_tag_ref"], "false")
        self.assertEqual(outputs["search_quality_certified"], "false")

    def test_uncertified_release_must_be_explicit(self) -> None:
        with self.assertRaisesRegex(ReleaseRequestError, "formal search-quality"):
            validate_release_request(
                repository_root=repository_root(),
                version="0.4.0",
                requested_prerelease=False,
                search_quality_gate_path="",
                allow_uncertified_preview=False,
                require_authenticode=False,
                github_ref="refs/heads/main",
                github_sha=_revision(repository_root()),
            )


class PythonReleaseAssemblyTest(unittest.TestCase):
    def test_assembles_unsigned_candidate_as_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            desktop = root / "desktop"
            webview = root / "webview"
            native = root / "native"
            desktop.mkdir()
            webview.mkdir()
            native.mkdir()
            _write(desktop, "desktop.zip")
            _write(webview, "Zvec-Webview-Preview-0.4.0-win-x64-portable.zip")
            _write(native, "package.whl")
            _write_checksums(desktop)
            _write_checksums(webview)
            _write_checksums(native)

            outputs = assemble_release(
                desktop_directory=desktop,
                webview_directory=webview,
                native_directory=native,
                output_directory=root / "candidate",
                version="0.4.0",
                revision="a" * 40,
                signing_status="unsigned",
                version_prerelease=False,
                exact_tag_ref=False,
                license_present=False,
                search_quality_certified=False,
            )

            self.assertEqual(outputs["effective_prerelease"], "true")
            policy = json.loads(
                (root / "candidate/release-assets/RELEASE-POLICY.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(policy["desktop_runtime"]["powershell_required"])
            self.assertFalse(policy["webview_preview"]["powershell_required"])
            self.assertFalse(policy["stable_channel_eligible"])
            self.assertTrue(
                (
                    root
                    / "candidate/release-assets"
                    / "Zvec-Webview-Preview-0.4.0-win-x64-portable.zip"
                ).is_file()
            )

    def test_rejects_duplicate_asset_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("desktop", "webview", "native"):
                directory = root / name
                directory.mkdir()
                _write(directory, "same.bin")
                _write_checksums(directory)
            with self.assertRaisesRegex(ReleaseAssemblyError, "Duplicate"):
                assemble_release(
                    desktop_directory=root / "desktop",
                    webview_directory=root / "webview",
                    native_directory=root / "native",
                    output_directory=root / "candidate",
                    version="0.4.0",
                    revision="a" * 40,
                    signing_status="unsigned",
                    version_prerelease=False,
                    exact_tag_ref=False,
                    license_present=False,
                    search_quality_certified=False,
                )

    def test_rejects_tampered_upstream_artifact_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("desktop", "webview", "native"):
                directory = root / name
                directory.mkdir()
                _write(directory, f"{name}.bin")
                _write_checksums(directory)
            (root / "desktop/desktop.bin").write_bytes(b"tampered")
            with self.assertRaisesRegex(ReleaseAssemblyError, "SHA-256 mismatch"):
                assemble_release(
                    desktop_directory=root / "desktop",
                    webview_directory=root / "webview",
                    native_directory=root / "native",
                    output_directory=root / "candidate",
                    version="0.4.0",
                    revision="a" * 40,
                    signing_status="unsigned",
                    version_prerelease=False,
                    exact_tag_ref=False,
                    license_present=False,
                    search_quality_certified=False,
                )
            self.assertFalse((root / "candidate").exists())


class PurePythonWorkflowContractTest(unittest.TestCase):
    def test_default_ci_and_release_use_only_the_python_build_chain(self) -> None:
        root = repository_root()
        for relative in (".github/workflows/ci.yml", ".github/workflows/release.yml"):
            with self.subTest(relative=relative):
                content = (root / relative).read_text(encoding="utf-8")
                folded = content.casefold()
                for forbidden in (
                    "choco install",
                    ".ps1",
                    "docker build",
                    "docker run",
                    "docker compose",
                    "dotnet",
                    "pwsh",
                    "powershell",
                ):
                    self.assertNotIn(forbidden, folded)
                self.assertIn("build_python_desktop.py", content)
                self.assertIn("build_webview_preview.py", content)
                self.assertIn("verify_webview_preview_release.py", content)
                self.assertIn("requirements-webview-preview-lock.txt", content)
                self.assertIn("--use-existing-frontend", content)
                self.assertIn("provision_nsis.py", content)

        release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("Smoke signed portable", release)
        self.assertIn("smoke_python_desktop_installer.py", release)
        self.assertIn("webview-preview-${{ inputs.version }}-unsigned", release)
        self.assertIn("--webview-directory candidate/webview", release)

        ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn(
            "git diff --exit-code -- zvec_webview/frontend_dist",
            ci,
        )
        self.assertIn("needs: webview-frontend", ci)

        assembly = (root / "scripts/assemble_python_release.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('assets / "WEBVIEW-SHA256SUMS.txt"', assembly)
        self.assertIn("WebView Preview: frozen payload", assembly)

    def test_formal_icon_and_installer_do_not_read_the_wpf_tree(self) -> None:
        root = repository_root()
        inputs = (
            root / "release/python_preview/zvec_python_preview.spec",
            root / "installer/Zvec.PythonDesktop.nsi",
            root / "scripts/python_preview_packaging.py",
        )
        for path in inputs:
            with self.subTest(path=path.name):
                folded = path.read_text(encoding="utf-8").casefold()
                self.assertNotIn("desktop/zvec.desktop/assets", folded)
                self.assertNotIn("desktop\\zvec.desktop\\assets", folded)
                self.assertIn("assets", folded)


class PurePythonRepositoryHygieneContractTest(unittest.TestCase):
    def test_legacy_desktop_tree_and_launchers_are_absent(self) -> None:
        root = repository_root()

        for relative in (
            "desktop",
            "global.json",
            "bin/zvec.cmd",
            "installer/Zvec.Desktop.nsi",
            "installer/Zvec.PythonPreview.nsi",
        ):
            with self.subTest(relative=relative):
                self.assertFalse((root / relative).exists())

    def test_repository_contains_no_powershell_scripts(self) -> None:
        root = repository_root()

        discovered = tuple(
            path.relative_to(root).as_posix()
            for directory in ("scripts", "tests", "installer")
            for path in (root / directory).rglob("*")
            if path.is_file() and path.suffix.casefold() == ".ps1"
        )
        self.assertEqual(discovered, ())


class PythonInstallerSmokeContractTest(unittest.TestCase):
    def test_resolves_unsigned_and_signed_installer_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsigned = root / "Zvec-Desktop-0.4.0-win-x64-unsigned-setup.exe"
            unsigned.write_bytes(b"unsigned")
            self.assertEqual(_resolve_installer(None, root), unsigned)
            unsigned.unlink()

            signed = root / "Zvec-Desktop-0.4.0-win-x64-setup.exe"
            signed.write_bytes(b"signed")
            self.assertEqual(_resolve_installer(None, root), signed)


if __name__ == "__main__":
    unittest.main()
