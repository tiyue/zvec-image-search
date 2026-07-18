from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from zvec_desktop.model_settings import ModelSettingsError, ModelSettingsService


class ModelSettingsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config" / "models.json"
        self.service = ModelSettingsService(self.path)

    def test_load_creates_valid_default_without_credentials(self) -> None:
        snapshot = self.service.load()

        self.assertTrue(self.path.is_file())
        self.assertEqual(snapshot.embedding_model, "qwen3-vl-embedding")
        self.assertEqual(snapshot.auto_tag_primary_model, "qwen3-vl-flash")
        self.assertEqual(snapshot.auto_tag_escalation_model, "qwen3-vl-plus")
        self.assertEqual(
            [choice.model_id for choice in snapshot.embedding_choices],
            ["qwen3-vl-embedding"],
        )
        self.assertNotIn("api_key", snapshot.json_text.lower())

    def test_assign_roles_uses_catalog_validation_and_persists_atomically(self) -> None:
        self.service.load()
        snapshot = self.service.assign_roles(
            embedding_model="qwen3-vl-embedding",
            auto_tag_primary_model="qwen3-vl-plus",
            auto_tag_escalation_model="qwen3-vl-flash",
        )

        self.assertEqual(snapshot.auto_tag_primary_model, "qwen3-vl-plus")
        self.assertEqual(snapshot.auto_tag_escalation_model, "qwen3-vl-flash")
        reloaded = self.service.load(create=False)
        self.assertEqual(reloaded.auto_tag_primary_model, "qwen3-vl-plus")

    def test_role_assignment_rejects_incompatible_model(self) -> None:
        self.service.load()
        with self.assertRaises(ModelSettingsError):
            self.service.assign_roles(
                embedding_model="qwen3-vl-flash",
                auto_tag_primary_model="qwen3-vl-flash",
                auto_tag_escalation_model="qwen3-vl-plus",
            )

    def test_replace_json_supports_user_added_aliyun_model(self) -> None:
        snapshot = self.service.load()
        payload = json.loads(snapshot.json_text)
        payload["models"].append(
            {
                "id": "qwen-vl-custom",
                "display_name": "Custom Qwen VL",
                "roles": ["auto_tag_primary", "auto_tag_escalation"],
                "protocol": "dashscope_multimodal_conversation",
                "enabled": True,
                "pricing": {
                    "input_yuan_per_million": 0.2,
                    "output_yuan_per_million": 2.0,
                    "effective_from": "2026-07-18",
                },
            }
        )
        payload["roles"]["auto_tag_primary"] = "qwen-vl-custom"

        changed = self.service.replace_json(
            json.dumps(payload, ensure_ascii=False, indent=2)
        )

        self.assertEqual(changed.auto_tag_primary_model, "qwen-vl-custom")
        self.assertIn(
            "qwen-vl-custom",
            [choice.model_id for choice in changed.auto_tag_primary_choices],
        )

    def test_replace_json_rejects_duplicates_and_keeps_old_file(self) -> None:
        before = self.service.load().json_text
        with self.assertRaisesRegex(ModelSettingsError, "重复字段"):
            self.service.replace_json(
                '{"schema_version":1,"provider":"aliyun_dashscope",'
                '"provider":"aliyun_dashscope","models":[],"roles":{}}'
            )
        self.assertEqual(self.service.load(create=False).json_text, before)

    def test_replace_json_rejects_unknown_provider(self) -> None:
        snapshot = self.service.load()
        payload = json.loads(snapshot.json_text)
        payload["provider"] = "not-aliyun"
        with self.assertRaises(ModelSettingsError):
            self.service.replace_json(json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
