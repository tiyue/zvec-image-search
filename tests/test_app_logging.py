from __future__ import annotations

import os
import tempfile
import time
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

from image_vector_service.app_logging import (
    LOG_BACKUP_COUNT,
    LOG_FILE_SIZE,
    LOG_RETENTION_DAYS,
    close_app_logger,
    get_app_logger,
)


class AppLoggingTest(unittest.TestCase):
    def test_logger_uses_small_bounded_rotations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            logger = get_app_logger(Path(temporary))
            try:
                handlers = [
                    handler
                    for handler in logger.handlers
                    if isinstance(handler, RotatingFileHandler)
                ]
                self.assertEqual(len(handlers), 1)
                self.assertEqual(handlers[0].maxBytes, 20 * 1024 * 1024)
                self.assertEqual(handlers[0].backupCount, 5)
                self.assertEqual(LOG_FILE_SIZE, 20 * 1024 * 1024)
                self.assertEqual(LOG_BACKUP_COUNT, 5)
            finally:
                # Windows keeps the active log locked until the handler closes.
                close_app_logger(logger)

    def test_expired_rotations_are_removed_but_recent_files_remain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expired = root / "image-service.log.6"
            recent = root / "image-service.log.1"
            expired.write_text("old", encoding="utf-8")
            recent.write_text("recent", encoding="utf-8")
            old = time.time() - (LOG_RETENTION_DAYS + 1) * 86_400
            os.utime(expired, (old, old))

            logger = get_app_logger(root)
            close_app_logger(logger)

            self.assertFalse(expired.exists())
            self.assertTrue(recent.exists())
            self.assertTrue((root / "image-service.log").exists())


if __name__ == "__main__":
    unittest.main()
