from pathlib import Path

import zvec

_INITIALIZED = False
_LOG_DIR: Path | None = None


def initialize_zvec(log_dir: Path | str | None = None) -> None:
    """Initialize Zvec with file logging, rotation, and retention."""
    global _INITIALIZED, _LOG_DIR
    if _INITIALIZED:
        return

    resolved_log_dir = Path(log_dir or Path.cwd()).expanduser().resolve()
    # Zvec does not create the log directory automatically.
    resolved_log_dir.mkdir(parents=True, exist_ok=True)

    zvec.init(
        log_type=zvec.LogType.FILE,
        log_level=zvec.LogLevel.INFO,
        log_dir=str(resolved_log_dir),
        log_basename="zvec.log",
        log_file_size=1024,  # MB per file (1 GiB)
        log_overdue_days=7,
    )
    _INITIALIZED = True
    _LOG_DIR = resolved_log_dir


if __name__ == "__main__":
    initialize_zvec()
    print(f"Zvec file logging initialized in {_LOG_DIR}")
