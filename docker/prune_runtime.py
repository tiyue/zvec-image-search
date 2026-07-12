from __future__ import annotations

import shutil
import sysconfig
from pathlib import Path

SITE_PACKAGES = Path(sysconfig.get_paths()["purelib"])
STDLIB = Path(sysconfig.get_paths()["stdlib"])
PREFIX = Path(sysconfig.get_config_var("prefix") or "/usr/local")


def path_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    if path.is_dir():
        return sum(
            item.lstat().st_size
            for item in path.rglob("*")
            if item.is_file() or item.is_symlink()
        )
    return 0


def remove(path: Path) -> int:
    removed = path_size(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    return removed


def remove_patterns(root: Path, patterns: tuple[str, ...]) -> int:
    removed = 0
    matches = {
        path for pattern in patterns for path in root.rglob(pattern) if path.exists()
    }
    for path in sorted(matches, key=lambda item: len(item.parts), reverse=True):
        if path.exists():
            removed += remove(path)
    return removed


def main() -> None:
    removed = 0

    removed += remove_patterns(SITE_PACKAGES, ("pip", "pip-*.dist-info", "README.txt"))
    for relative in (
        "ensurepip",
        "idlelib",
        "lib2to3",
        "pydoc_data",
        "tkinter",
        "turtledemo",
        "venv",
    ):
        removed += remove(STDLIB / relative)
    removed += remove_patterns(
        PREFIX / "bin",
        ("2to3*", "idle*", "pip*", "pydoc*", "python*-config"),
    )
    removed += remove(PREFIX / "include")
    removed += remove(PREFIX / "share" / "man")

    removed += remove_patterns(SITE_PACKAGES, ("tests", "test", "__pycache__"))
    removed += remove_patterns(
        SITE_PACKAGES,
        ("*.pyi", "*.pxd", "*.h", "*.a", "*.c", "*.cpp", "py.typed"),
    )

    numpy = SITE_PACKAGES / "numpy"
    for relative in ("f2py", "typing", "testing", "_pyinstaller"):
        removed += remove(numpy / relative)

    zvec = SITE_PACKAGES / "zvec"
    removed += remove(zvec / "data")
    removed += remove(zvec / "libzvec_diskann_plugin.so")

    pillow = SITE_PACKAGES / "PIL"
    removed += remove_patterns(
        pillow,
        (
            "_avif*.so",
            "_imagingcms*.so",
            "_imagingft*.so",
            "AvifImagePlugin.py",
            "ImageCms.py",
            "ImageFont.py",
        ),
    )
    pillow_libs = SITE_PACKAGES / "pillow.libs"
    removed += remove_patterns(
        pillow_libs,
        (
            "libavif-*.so*",
            "libbrotlicommon-*.so*",
            "libbrotlidec-*.so*",
            "libfreetype-*.so*",
            "libharfbuzz-*.so*",
            "liblcms2-*.so*",
        ),
    )

    print(f"Pruned {removed / (1024 * 1024):.2f} MiB from runtime packages.")


if __name__ == "__main__":
    main()
