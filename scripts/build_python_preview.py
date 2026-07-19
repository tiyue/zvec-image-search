"""Compatibility entry point for the renamed pure-Python desktop builder.

Use :mod:`scripts.build_python_desktop` in CI, Release, and new automation.
"""

from __future__ import annotations

from collections.abc import Sequence

if __package__:
    from scripts.build_python_desktop import main as _desktop_main
else:
    from build_python_desktop import main as _desktop_main


def main(argv: Sequence[str] | None = None) -> int:
    return _desktop_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
