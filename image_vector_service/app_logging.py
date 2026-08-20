from __future__ import annotations

import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# The text log is only a last-resort diagnostic source.  Searchable product
# activity lives in activity.sqlite3, so keeping 1 GiB x 1000 text rotations
# would waste disk space without improving the UI.  Five 20 MiB generations
# are ample for support diagnostics and remain bounded on long-running hosts.
LOG_FILE_SIZE = 20 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_RETENTION_DAYS = 7


def get_app_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (log_dir / "image-service.log").resolve()
    logger = logging.getLogger(f"zvec-image-service:{log_path}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=LOG_FILE_SIZE,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(handler)
        _remove_expired_logs(log_dir, log_path)
    return logger


def close_app_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.flush()
        handler.close()


def _remove_expired_logs(log_dir: Path, active_log: Path) -> None:
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for path in log_dir.glob("image-service.log*"):
        if path.resolve() == active_log:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
