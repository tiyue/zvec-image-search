from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from image_vector_service.search_learning_config import load_search_learning
from image_vector_service.service import ImageVectorService


class SearchLearningHotReloadTests(unittest.TestCase):
    def test_single_library_service_reloads_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_home = Path(temporary)
            directory = config_home / "search-learning"
            directory.mkdir()
            active_path = directory / "active.json"
            active_path.write_text(
                json.dumps({"schema_version": 1, "enabled": False}),
                encoding="utf-8",
            )

            service = ImageVectorService.__new__(ImageVectorService)
            service.config = SimpleNamespace(config_home_path=config_home)
            service.search_learning = load_search_learning(config_home)
            service._search_learning_reload_lock = threading.Lock()
            service._search_learning_manifest_signature = (
                service._search_learning_active_signature()
            )
            initial = service._current_search_learning()
            self.assertIsNotNone(initial)
            self.assertFalse(initial.enabled)

            replacement = directory / ".active.next.json"
            replacement.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "enabled": True,
                        "shadow_mode": True,
                    }
                ),
                encoding="utf-8",
            )
            replacement.replace(active_path)

            reloaded = service._current_search_learning()
            self.assertIsNotNone(reloaded)
            self.assertTrue(reloaded.enabled)
            self.assertTrue(reloaded.shadow_mode)


if __name__ == "__main__":
    unittest.main()
