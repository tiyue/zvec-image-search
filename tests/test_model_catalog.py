from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from image_vector_service import model_catalog
from image_vector_service.model_catalog import (
    AUTO_TAG_PRIMARY_ROLE,
    EMBEDDING_PROTOCOL,
    EMBEDDING_ROLE,
    MODEL_PROVIDER,
    ConfigurationError,
    default_model_configuration,
    ensure_user_model_configuration,
    load_active_model_configuration,
    load_model_configuration,
    parse_model_configuration,
    save_model_configuration,
)


class ModelCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="zvec-model-catalog-test-"
        )
        self.root = Path(self.temporary_directory.name)
        self.default_path = (
            Path(__file__).resolve().parents[1] / "model-catalog.default.json"
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _payload() -> dict[str, Any]:
        return copy.deepcopy(default_model_configuration().to_dict())

    def test_default_json_matches_python_defaults(self) -> None:
        default_payload = json.loads(self.default_path.read_text(encoding="utf-8"))
        python_payload = default_model_configuration().to_dict()

        self.assertEqual(default_payload, python_payload)
        self.assertEqual(
            load_model_configuration(self.default_path).to_dict(),
            python_payload,
        )
        self.assertEqual(python_payload["provider"], MODEL_PROVIDER)
        embedding = python_payload["models"][0]
        self.assertEqual(embedding["id"], "qwen3-vl-embedding")
        self.assertEqual(embedding["protocol"], EMBEDDING_PROTOCOL)
        self.assertEqual(embedding["dimension"], 1024)

    def test_unknown_fields_are_rejected_at_every_schema_level(self) -> None:
        root_payload = self._payload()
        root_payload["api_key"] = "must-not-be-accepted"
        with self.assertRaisesRegex(ConfigurationError, "unsupported fields: api_key"):
            parse_model_configuration(root_payload)

        model_payload = self._payload()
        model_payload["models"][0]["endpoint"] = "https://example.invalid"
        with self.assertRaisesRegex(ConfigurationError, "unsupported fields: endpoint"):
            parse_model_configuration(model_payload)

        pricing_payload = self._payload()
        pricing_payload["models"][1]["pricing"]["currency"] = "CNY"
        with self.assertRaisesRegex(ConfigurationError, "unsupported fields: currency"):
            parse_model_configuration(pricing_payload)

    def test_unknown_provider_protocol_and_roles_are_rejected(self) -> None:
        provider_payload = self._payload()
        provider_payload["provider"] = "unknown_provider"
        with self.assertRaisesRegex(ConfigurationError, "only Alibaba Cloud"):
            parse_model_configuration(provider_payload)

        protocol_payload = self._payload()
        protocol_payload["models"][0]["protocol"] = "unknown_protocol"
        with self.assertRaisesRegex(ConfigurationError, "protocol is not supported"):
            parse_model_configuration(protocol_payload)

        model_role_payload = self._payload()
        model_role_payload["models"][1]["roles"].append("caption")
        with self.assertRaisesRegex(ConfigurationError, "unknown role 'caption'"):
            parse_model_configuration(model_role_payload)

        assignment_payload = self._payload()
        assignment_payload["roles"]["caption"] = "qwen3-vl-flash"
        with self.assertRaisesRegex(ConfigurationError, "unsupported fields: caption"):
            parse_model_configuration(assignment_payload)

    def test_embedding_model_requires_1024_dimensions(self) -> None:
        for dimension in (None, 768, 1536):
            with self.subTest(dimension=dimension):
                payload = self._payload()
                if dimension is None:
                    payload["models"][0].pop("dimension")
                else:
                    payload["models"][0]["dimension"] = dimension
                with self.assertRaisesRegex(
                    ConfigurationError,
                    "dimension 1024",
                ):
                    parse_model_configuration(payload)

        configuration = parse_model_configuration(self._payload())
        self.assertEqual(
            configuration.model_for_role(EMBEDDING_ROLE).dimension,
            1024,
        )

    def test_visual_model_pricing_is_validated(self) -> None:
        configuration = default_model_configuration()
        flash = configuration.model_for_role(AUTO_TAG_PRIMARY_ROLE)
        self.assertIsNotNone(flash.pricing)
        assert flash.pricing is not None
        self.assertEqual(flash.pricing.input_yuan_per_million, 0.15)
        self.assertEqual(flash.pricing.output_yuan_per_million, 1.5)
        self.assertEqual(flash.pricing.effective_from, "2026-07-14")

        missing_payload = self._payload()
        missing_payload["models"][1].pop("pricing")
        with self.assertRaisesRegex(ConfigurationError, "requires pricing metadata"):
            parse_model_configuration(missing_payload)

        negative_payload = self._payload()
        negative_payload["models"][1]["pricing"]["input_yuan_per_million"] = -0.1
        with self.assertRaisesRegex(ConfigurationError, "non-negative number"):
            parse_model_configuration(negative_payload)

        invalid_date_payload = self._payload()
        invalid_date_payload["models"][1]["pricing"]["effective_from"] = "2026-02-30"
        with self.assertRaisesRegex(ConfigurationError, "valid calendar date"):
            parse_model_configuration(invalid_date_payload)

        wrong_date_format_payload = self._payload()
        wrong_date_format_payload["models"][1]["pricing"]["effective_from"] = "2026-7-1"
        with self.assertRaisesRegex(ConfigurationError, "YYYY-MM-DD"):
            parse_model_configuration(wrong_date_format_payload)

    def test_duplicate_json_keys_and_model_ids_are_rejected(self) -> None:
        duplicate_key_path = self.root / "duplicate-key.json"
        duplicate_key_path.write_text(
            '{"schema_version":1,"schema_version":1}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigurationError, "Duplicate JSON property"):
            load_model_configuration(duplicate_key_path)

        duplicate_model_payload = self._payload()
        duplicate_model_payload["models"].append(
            copy.deepcopy(duplicate_model_payload["models"][0])
        )
        with self.assertRaisesRegex(ConfigurationError, "Duplicate model id"):
            parse_model_configuration(duplicate_model_payload)

    def test_corrupt_json_and_invalid_utf8_are_rejected(self) -> None:
        malformed_path = self.root / "malformed.json"
        malformed_path.write_text('{"schema_version":', encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "not valid UTF-8 JSON"):
            load_model_configuration(malformed_path)

        invalid_utf8_path = self.root / "invalid-utf8.json"
        invalid_utf8_path.write_bytes(b"\xff\xfe\x00")
        with self.assertRaisesRegex(ConfigurationError, "not valid UTF-8 JSON"):
            load_model_configuration(invalid_utf8_path)

    def test_symbolic_link_configuration_is_rejected_without_touching_target(
        self,
    ) -> None:
        target = self.root / "target.json"
        target.write_text(
            json.dumps(default_model_configuration().to_dict()),
            encoding="utf-8",
        )
        original = target.read_bytes()
        link = self.root / "models.json"
        try:
            link.symlink_to(target)
        except OSError:
            # Windows commonly requires an unavailable privilege for test-created
            # symlinks. Exercise the same lstat/reparse rejection branch directly.
            link.write_bytes(original)
            reparse_guard = patch.object(
                model_catalog,
                "_is_reparse_point",
                return_value=True,
            )
        else:
            reparse_guard = patch.object(
                model_catalog,
                "_is_reparse_point",
                wraps=model_catalog._is_reparse_point,
            )

        with reparse_guard:
            with self.assertRaisesRegex(
                ConfigurationError,
                "symbolic link|reparse point",
            ):
                load_model_configuration(link)
            with self.assertRaisesRegex(
                ConfigurationError,
                "symbolic-link|reparse-point",
            ):
                ensure_user_model_configuration(link)
            with self.assertRaisesRegex(
                ConfigurationError,
                "symbolic-link|reparse-point",
            ):
                save_model_configuration(link, default_model_configuration())
            with (
                patch.dict(
                    os.environ,
                    {"ZVEC_MODELS_CONFIG": str(link)},
                    clear=True,
                ),
                self.assertRaisesRegex(
                    ConfigurationError,
                    "symbolic link|reparse point",
                ),
            ):
                load_active_model_configuration()
        self.assertEqual(target.read_bytes(), original)

    def test_missing_file_is_generated_from_defaults(self) -> None:
        path = self.root / "nested" / "models.json"
        created = ensure_user_model_configuration(path)

        self.assertEqual(created, path.absolute())
        self.assertTrue(path.is_file())
        self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.assertEqual(
            load_model_configuration(path).to_dict(),
            default_model_configuration().to_dict(),
        )
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_save_is_atomic_and_cleans_failed_temporary_file(self) -> None:
        path = self.root / "models.json"
        save_model_configuration(path, default_model_configuration())
        original = path.read_bytes()
        changed_payload = self._payload()
        changed_payload["roles"][AUTO_TAG_PRIMARY_ROLE] = "qwen3-vl-plus"
        changed = parse_model_configuration(changed_payload)

        with (
            patch.object(Path, "replace", side_effect=OSError("replace failed")),
            self.assertRaisesRegex(OSError, "replace failed"),
        ):
            save_model_configuration(path, changed)

        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

        save_model_configuration(path, changed)
        reloaded = load_model_configuration(path)
        self.assertEqual(reloaded.auto_tag_primary_model, "qwen3-vl-plus")
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
