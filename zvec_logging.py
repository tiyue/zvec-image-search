from pathlib import Path

import zvec


LOG_DIR = Path(r"D:\code\zvec")
_INITIALIZED = False


def initialize_zvec() -> None:
    """Initialize Zvec with file logging, rotation, and retention."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    # Zvec does not create the log directory automatically.
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    zvec.init(
        log_type=zvec.LogType.FILE,
        log_level=zvec.LogLevel.INFO,
        log_dir=str(LOG_DIR),
        log_basename="zvec.log",
        log_file_size=1024,  # MB per file (1 GiB)
        log_overdue_days=7,
    )
    _INITIALIZED = True


if __name__ == "__main__":
    initialize_zvec()
    print(f"Zvec file logging initialized in {LOG_DIR}")
