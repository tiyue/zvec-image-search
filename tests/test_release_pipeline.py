from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.assemble_python_release import (
    ReleaseAssemblyError,
    assemble_release,
)
from scripts.prepare_python_release import (
    ReleaseRequestError,
    validate_release_request,
)
from scripts.webview_preview_packaging import (
    public_version,
    read_project_version,
    repository_root,
)


def _write(root: Path, relative: str, content: bytes = b"release") -> None:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_checksums(directory: Path) -> None:
    lines = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.name}\n")
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


def _artifact_directories(root: Path, version: str) -> tuple[Path, Path]:
    webview = root / "webview"
    android = root / "android"
    webview.mkdir()
    android.mkdir()
    display_version = public_version(version)
    _write(
        webview,
        f"YaoLens-{display_version}-win-x64-portable.zip",
    )
    _write(
        webview,
        f"YaoLens-{display_version}-win-x64-setup.exe",
    )
    _write(webview, "WINDOWS-RELEASE-VERIFICATION.json")
    _write(
        android,
        f"YaoLens-{display_version}-android.apk",
    )
    _write_checksums(webview)
    _write_checksums(android)
    return webview, android


class ReleaseRequestPolicyTest(unittest.TestCase):
    def test_branch_release_can_explicitly_waive_search_certification(self) -> None:
        root = repository_root()
        version = read_project_version(root)
        outputs = validate_release_request(
            repository_root=root,
            version=version,
            requested_prerelease=False,
            search_quality_gate_path="",
            allow_uncertified_search_quality=True,
            github_ref="refs/heads/codex/collection",
            github_sha=_revision(root),
        )
        self.assertEqual(outputs["exact_tag_ref"], "false")
        self.assertEqual(outputs["search_quality_certified"], "false")
        self.assertEqual(outputs["public_version"], "0.1")

    def test_uncertified_release_must_be_explicit(self) -> None:
        root = repository_root()
        version = read_project_version(root)
        with self.assertRaisesRegex(ReleaseRequestError, "formal search-quality"):
            validate_release_request(
                repository_root=root,
                version=version,
                requested_prerelease=False,
                search_quality_gate_path="",
                allow_uncertified_search_quality=False,
                github_ref="refs/heads/main",
                github_sha=_revision(root),
            )

    def test_prerelease_version_is_rejected(self) -> None:
        root = repository_root()
        with self.assertRaisesRegex(ReleaseRequestError, "Stable releases"):
            validate_release_request(
                repository_root=root,
                version="0.1.0-rc.1",
                requested_prerelease=True,
                search_quality_gate_path="",
                allow_uncertified_search_quality=True,
                github_ref="refs/heads/main",
                github_sha=_revision(root),
            )


class ReleaseAssemblyTest(unittest.TestCase):
    def test_assembles_only_webview_and_android_products(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            webview, android = _artifact_directories(root, "0.1.0")

            outputs = assemble_release(
                webview_directory=webview,
                android_directory=android,
                output_directory=root / "candidate",
                version="0.1.0",
                revision="a" * 40,
                version_prerelease=False,
                exact_tag_ref=True,
                license_present=True,
                search_quality_certified=True,
            )

            self.assertEqual(outputs["release_title"], "YaoLens 0.1")
            assets = root / "candidate/release-assets"
            policy = json.loads(
                (assets / "RELEASE-POLICY.json").read_text(encoding="utf-8")
            )
            self.assertEqual(policy["schema_version"], 4)
            self.assertEqual(policy["version"], "0.1.0")
            self.assertEqual(policy["public_version"], "0.1")
            self.assertEqual(policy["release_channel"], "stable")
            self.assertFalse(policy["prerelease"])
            self.assertNotIn("desktop_runtime", policy)
            names = {path.name for path in assets.iterdir() if path.is_file()}
            self.assertIn("YaoLens-0.1-win-x64-portable.zip", names)
            self.assertIn("YaoLens-0.1-win-x64-setup.exe", names)
            self.assertIn("YaoLens-0.1-android.apk", names)
            self.assertIn("WEBVIEW-SHA256SUMS.txt", names)
            self.assertIn("ANDROID-SHA256SUMS.txt", names)
            self.assertFalse(any("Desktop" in name for name in names))
            self.assertFalse(any(name.endswith(".whl") for name in names))
            public_text = (
                (assets / "RELEASE-POLICY.json").read_text(encoding="utf-8")
                + (root / "candidate/RELEASE-NOTES.md").read_text(encoding="utf-8")
                + "\n".join(names)
            ).casefold()
            for forbidden in ("preview", "debug-preview", "unsigned", "未签名"):
                self.assertNotIn(forbidden, public_text)
            self.assertNotRegex(public_text, r"\bsigned\b")

    def test_rejects_duplicate_asset_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            webview, android = _artifact_directories(root, "0.1.0")
            duplicate = "YaoLens-0.1-android.apk"
            _write(webview, duplicate)
            _write_checksums(webview)

            with self.assertRaisesRegex(ReleaseAssemblyError, "approved release set"):
                assemble_release(
                    webview_directory=webview,
                    android_directory=android,
                    output_directory=root / "candidate",
                    version="0.1.0",
                    revision="a" * 40,
                    version_prerelease=False,
                    exact_tag_ref=True,
                    license_present=True,
                    search_quality_certified=True,
                )

    def test_rejects_tampered_upstream_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            webview, android = _artifact_directories(root, "0.1.0")
            portable = webview / "YaoLens-0.1-win-x64-portable.zip"
            portable.write_bytes(b"tampered")

            with self.assertRaisesRegex(ReleaseAssemblyError, "SHA-256 mismatch"):
                assemble_release(
                    webview_directory=webview,
                    android_directory=android,
                    output_directory=root / "candidate",
                    version="0.1.0",
                    revision="a" * 40,
                    version_prerelease=False,
                    exact_tag_ref=True,
                    license_present=True,
                    search_quality_certified=True,
                )
            self.assertFalse((root / "candidate").exists())

    def test_rejects_android_apk_with_an_unapproved_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            webview = root / "webview"
            android = root / "android"
            webview.mkdir()
            android.mkdir()
            _write(webview, "YaoLens-0.1-win-x64-portable.zip")
            _write(webview, "YaoLens-0.1-win-x64-setup.exe")
            _write(webview, "WINDOWS-RELEASE-VERIFICATION.json")
            _write(android, "YaoLens-0.1-mobile.apk")
            _write_checksums(webview)
            _write_checksums(android)

            with self.assertRaisesRegex(
                ReleaseAssemblyError,
                "YaoLens-0.1-android.apk",
            ):
                assemble_release(
                    webview_directory=webview,
                    android_directory=android,
                    output_directory=root / "candidate",
                    version="0.1.0",
                    revision="a" * 40,
                    version_prerelease=False,
                    exact_tag_ref=True,
                    license_present=True,
                    search_quality_certified=True,
                )


class ReleaseWorkflowContractTest(unittest.TestCase):
    def test_release_publishes_only_webview_and_android(self) -> None:
        root = repository_root()
        release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        folded = release.casefold()

        self.assertIn("build_webview_preview.py", release)
        self.assertIn("name: Publish YaoLens", release)
        self.assertIn("YaoLens-%PUBLIC_VERSION%-win-x64-portable.zip", release)
        self.assertIn("YaoLens-%PUBLIC_VERSION%-win-x64-setup.exe", release)
        self.assertIn('apk_name="YaoLens-${PUBLIC_VERSION}-android.apk"', release)
        self.assertIn("name: yaolens-windows-${{ inputs.version }}", release)
        self.assertIn("name: yaolens-android-${{ inputs.version }}", release)
        self.assertNotIn("--draft", release)
        self.assertNotIn("arguments+=(--prerelease)", release)
        self.assertNotIn("draft release", folded)
        self.assertIn(
            "Stable Releases require workflow dispatch from exact ${tag}.",
            release,
        )
        visible_fields = "\n".join(
            line
            for line in release.splitlines()
            if re.match(r"^\s*(?:name|description):", line)
        ).casefold()
        for forbidden in ("preview", "debug-preview", "unsigned", "未签名"):
            self.assertNotIn(forbidden, visible_fields)
        for forbidden in (
            "desktop-package",
            "desktop-python-",
            "build_python_desktop.py",
            "native-package:",
            "native-${{ inputs.version }}-wheel",
            "--desktop-directory",
            "--native-directory",
            "require_authenticode",
            "zvec-desktop",
        ):
            self.assertNotIn(forbidden, folded)

    def test_ci_keeps_native_validation_without_desktop_packaging(self) -> None:
        root = repository_root()
        ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        folded = ci.casefold()

        self.assertIn("native-python-compatibility", ci)
        self.assertIn("tests.test_native_package", ci)
        self.assertIn("windows-package", ci)
        self.assertIn("YaoLens-ci-${GITHUB_SHA}-android.apk", ci)
        visible_fields = "\n".join(
            line
            for line in ci.splitlines()
            if re.match(r"^\s*(?:name|description):", line)
        ).casefold()
        for forbidden in ("preview", "debug-preview", "unsigned", "未签名"):
            self.assertNotIn(forbidden, visible_fields)
        self.assertNotIn("desktop-package:", folded)
        self.assertNotIn("build_python_desktop.py", folded)

    def test_retired_desktop_release_tree_is_absent(self) -> None:
        root = repository_root()
        for relative in (
            "installer/Zvec.PythonDesktop.nsi",
            "scripts/build_python_desktop.py",
            "scripts/build_python_preview.py",
            "scripts/generate_python_release_materials.py",
            "scripts/python_preview_packaging.py",
            "scripts/smoke_python_desktop_installer.py",
        ):
            with self.subTest(relative=relative):
                self.assertFalse((root / relative).exists())

        for relative in ("zvec_desktop", "release/python_preview"):
            with self.subTest(relative=relative):
                self.assertFalse(any((root / relative).rglob("*.py")))

        project = (root / "pyproject.toml").read_text(encoding="utf-8")
        lock = (root / "requirements-lock.txt").read_text(encoding="utf-8")
        self.assertNotIn("zvec-desktop", project)
        self.assertNotIn("pystray", project.casefold())
        self.assertNotIn("pystray", lock.casefold())
        self.assertNotIn("six==", lock.casefold())
        self.assertIn("zvec_host*", project)

    def test_public_version_and_android_version_are_synchronized(self) -> None:
        root = repository_root()
        self.assertEqual(read_project_version(root), "0.1.0")
        self.assertEqual(public_version(read_project_version(root)), "0.1")
        android = (root / "android/app/build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn("versionCode = 500005", android)
        self.assertIn('versionName = "0.1"', android)

    def test_public_release_documents_use_stable_yaolens_names(self) -> None:
        root = repository_root()
        documents = "\n".join(
            (root / relative).read_text(encoding="utf-8")
            for relative in ("README.md", "RELEASE.md", "SUPPLY_CHAIN.md")
        )
        folded = documents.casefold()
        for expected in (
            "YaoLens-0.1-win-x64-portable.zip",
            "YaoLens-0.1-win-x64-setup.exe",
            "YaoLens-0.1-android.apk",
        ):
            self.assertIn(expected, documents)
        for forbidden in (
            "preview",
            "预览版",
            "debug-preview",
            "unsigned",
            "未签名",
            "已签名",
        ):
            self.assertNotIn(forbidden, folded)
        self.assertNotRegex(folded, r"\bsigned\b")

        spec = (root / "docs/spec.md").read_text(encoding="utf-8")
        self.assertIn("版本：0.1（机器 SemVer：0.1.0）", spec)
        self.assertIn("公开主程序 `YaoLens.exe`", spec)


if __name__ == "__main__":
    unittest.main()
