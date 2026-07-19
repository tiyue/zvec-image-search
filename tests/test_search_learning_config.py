from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from image_vector_service.learning_ranker import RANKING_MODEL_SCHEMA_VERSION
from image_vector_service.search_features import FEATURE_SCHEMA_VERSION
from image_vector_service.search_learning_config import (
    ACTIVE_CONFIG_FILENAME,
    ACTIVE_CONFIG_SCHEMA_VERSION,
    CALIBRATION_SCHEMA_VERSION,
    HARD_MINIMUM_CONFIDENCE,
    CalibrationRegistry,
    IsotonicCalibrator,
    PlattCalibrator,
    SearchLearningConfigurationError,
    calibration_registry_from_mapping,
    calibrator_from_mapping,
    load_search_learning,
)


class CalibrationInferenceTests(unittest.TestCase):
    def test_platt_is_monotonic_and_uses_the_hard_minimum(self) -> None:
        registry = CalibrationRegistry(
            calibration_version="calibration-v1",
            query_types={
                "text": PlattCalibrator(
                    a=4.0,
                    b=-2.0,
                    minimum_confidence=0.10,
                )
            },
            fallback_reason=None,
        )

        low = registry.calibrate(0.1, query_type="text")
        high = registry.calibrate(0.9, query_type="text")

        self.assertLess(low.confidence, high.confidence)
        self.assertEqual(low.scope, "query_type")
        self.assertEqual(
            low.effective_minimum_confidence,
            HARD_MINIMUM_CONFIDENCE,
        )
        self.assertFalse(low.passes_threshold)
        self.assertTrue(high.passes_threshold)

    def test_isotonic_interpolates_and_clamps_at_curve_endpoints(self) -> None:
        calibrator = IsotonicCalibrator(
            points=((0.3, 0.18), (0.5, 0.42), (0.7, 0.76), (0.9, 0.95)),
            minimum_confidence=0.38,
        )

        self.assertEqual(calibrator.calibrate(0.0), 0.18)
        self.assertAlmostEqual(calibrator.calibrate(0.6), 0.59)
        self.assertEqual(calibrator.calibrate(1.0), 0.95)

    def test_isotonic_rejects_non_monotonic_or_duplicate_points(self) -> None:
        with self.assertRaisesRegex(
            SearchLearningConfigurationError, "strictly increasing"
        ):
            IsotonicCalibrator(
                points=((0.2, 0.3), (0.2, 0.4)),
                minimum_confidence=0.2,
            )
        with self.assertRaisesRegex(SearchLearningConfigurationError, "non-decreasing"):
            IsotonicCalibrator(
                points=((0.2, 0.8), (0.9, 0.4)),
                minimum_confidence=0.2,
            )

    def test_hierarchy_is_collection_then_query_type_then_global(self) -> None:
        registry = CalibrationRegistry(
            calibration_version="calibration-v1",
            global_calibrator=_constant_platt(0.3),
            query_types={"identity": _constant_platt(0.5)},
            collections={
                "lib-exact": {"identity": _constant_platt(0.9)},
                "lib-any": {"global": _constant_platt(0.7)},
            },
            fallback_reason=None,
        )

        exact = registry.calibrate(
            0.5, query_type="identity", collection_id="lib-exact"
        )
        collection_global = registry.calibrate(
            0.5, query_type="identity", collection_id="lib-any"
        )
        query = registry.calibrate(
            0.5, query_type="identity", collection_id="lib-unknown"
        )
        global_value = registry.calibrate(0.5, query_type="action")

        self.assertAlmostEqual(exact.confidence, 0.9)
        self.assertEqual(exact.scope, "collection")
        self.assertAlmostEqual(collection_global.confidence, 0.7)
        self.assertEqual(collection_global.scope, "collection")
        self.assertAlmostEqual(query.confidence, 0.5)
        self.assertEqual(query.scope, "query_type")
        self.assertAlmostEqual(global_value.confidence, 0.3)
        self.assertEqual(global_value.scope, "global")

    def test_missing_mode_uses_identity_and_still_enforces_twenty_percent(self) -> None:
        registry = CalibrationRegistry(fallback_reason="calibration_unavailable")

        rejected = registry.calibrate(0.19, query_type="tag")
        accepted = registry.calibrate(0.20, query_type="tag")

        self.assertTrue(rejected.fallback)
        self.assertEqual(rejected.method, "identity")
        self.assertFalse(rejected.passes_threshold)
        self.assertTrue(accepted.passes_threshold)
        self.assertEqual(
            rejected.diagnostics()["hard_minimum_confidence"],
            HARD_MINIMUM_CONFIDENCE,
        )

    def test_parser_supports_flat_document_schema_and_all_domain_modes(self) -> None:
        payload: dict[str, object] = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "calibration_version": "calibration-domain-v1",
            "text": _platt_payload(0.31),
            "image": _isotonic_payload(0.32),
            "combined": _platt_payload(0.33),
            "tag": _platt_payload(0.34),
            "identity": _platt_payload(0.35),
            "identity_combined": _platt_payload(0.36),
            "action": _platt_payload(0.37),
            "expression": _platt_payload(0.38),
            "scene": _platt_payload(0.39),
            "action_expression": _platt_payload(0.40),
        }

        registry = calibration_registry_from_mapping(payload)

        self.assertEqual(len(registry.query_types), 10)
        self.assertEqual(
            registry.calibrate(0.8, query_type="image_text").minimum_confidence,
            0.33,
        )
        self.assertEqual(
            registry.calibrate(0.8, query_type="identity_combined").minimum_confidence,
            0.36,
        )

    def test_calibrator_parser_rejects_unknown_fields_and_bad_methods(self) -> None:
        payload = _platt_payload(0.2)
        payload["extra"] = True
        with self.assertRaisesRegex(SearchLearningConfigurationError, "unknown fields"):
            calibrator_from_mapping(payload)
        with self.assertRaisesRegex(
            SearchLearningConfigurationError, "platt or isotonic"
        ):
            calibrator_from_mapping({"method": "softmax", "minimum_confidence": 0.2})


class SearchLearningLoaderTests(unittest.TestCase):
    def test_loads_ranker_and_calibration_from_active_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / "search-learning"
            directory.mkdir()
            _write_json(directory / "ranker-v1.json", _ranker_payload())
            _write_json(directory / "calibration-v1.json", _calibration_payload())
            _write_json(
                directory / ACTIVE_CONFIG_FILENAME,
                {
                    "schema_version": ACTIVE_CONFIG_SCHEMA_VERSION,
                    "enabled": True,
                    "shadow_mode": True,
                    "ranker": "ranker-v1.json",
                    "calibration": "calibration-v1.json",
                },
            )

            bundle = load_search_learning(home)

        self.assertTrue(bundle.configured)
        self.assertTrue(bundle.enabled)
        self.assertTrue(bundle.shadow_mode)
        self.assertIsNotNone(bundle.ranker_model)
        self.assertEqual(bundle.ranker_model.model_version, "ranker-v1")  # type: ignore[union-attr]
        self.assertTrue(bundle.calibrations.configured)
        self.assertEqual(
            bundle.calibrations.calibration_version,
            "calibration-v1",
        )
        self.assertFalse(bundle.diagnostics()["ranking_fallback"])

    def test_corrupt_ranker_falls_back_without_disabling_valid_calibration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / "search-learning"
            directory.mkdir()
            (directory / "ranker-v1.json").write_text("{bad", encoding="utf-8")
            _write_json(directory / "calibration-v1.json", _calibration_payload())
            _write_json(
                directory / ACTIVE_CONFIG_FILENAME,
                {
                    "schema_version": ACTIVE_CONFIG_SCHEMA_VERSION,
                    "enabled": True,
                    "ranker": "ranker-v1.json",
                    "calibration": "calibration-v1.json",
                },
            )

            bundle = load_search_learning(home)

        self.assertTrue(bundle.enabled)
        self.assertIsNone(bundle.ranker_model)
        self.assertRegex(bundle.ranking_fallback_reason or "", "ranker_invalid")
        self.assertTrue(bundle.calibrations.configured)
        self.assertFalse(bundle.calibrations.calibrate(0.8, query_type="text").fallback)

    def test_incompatible_feature_version_is_a_safe_ranking_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / "search-learning"
            directory.mkdir()
            payload = _ranker_payload()
            payload["feature_schema_version"] = FEATURE_SCHEMA_VERSION + 1
            _write_json(directory / "ranker-v2.json", payload)
            _write_json(
                directory / ACTIVE_CONFIG_FILENAME,
                {
                    "schema_version": ACTIVE_CONFIG_SCHEMA_VERSION,
                    "enabled": True,
                    "ranker": "ranker-v2.json",
                },
            )

            bundle = load_search_learning(home)

        self.assertIsNone(bundle.ranker_model)
        self.assertTrue(bundle.build_ranker().rank(()).fallback)
        self.assertRegex(bundle.ranking_fallback_reason or "", "ranker_invalid")

    def test_bad_active_file_and_disabled_config_never_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / "search-learning"
            directory.mkdir()
            (directory / ACTIVE_CONFIG_FILENAME).write_text("[]", encoding="utf-8")
            bad = load_search_learning(home)
            _write_json(
                directory / ACTIVE_CONFIG_FILENAME,
                {
                    "schema_version": ACTIVE_CONFIG_SCHEMA_VERSION,
                    "enabled": False,
                },
            )
            disabled = load_search_learning(home)

        self.assertFalse(bad.enabled)
        self.assertRegex(bad.ranking_fallback_reason or "", "active_config_invalid")
        self.assertFalse(disabled.enabled)
        self.assertEqual(disabled.ranking_fallback_reason, "learning_disabled")

    def test_artifact_path_traversal_is_rejected_as_a_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            directory = home / "search-learning"
            directory.mkdir()
            _write_json(
                directory / ACTIVE_CONFIG_FILENAME,
                {
                    "schema_version": ACTIVE_CONFIG_SCHEMA_VERSION,
                    "enabled": True,
                    "ranker": "../outside.json",
                },
            )

            bundle = load_search_learning(home)

        self.assertTrue(bundle.enabled)
        self.assertIsNone(bundle.ranker_model)
        self.assertRegex(bundle.ranking_fallback_reason or "", "ranker_invalid")


def _constant_platt(confidence: float) -> PlattCalibrator:
    # sigmoid(log(p / (1-p))) == p for every input when a == 0.
    import math

    return PlattCalibrator(
        a=0.0,
        b=math.log(confidence / (1.0 - confidence)),
        minimum_confidence=0.2,
    )


def _platt_payload(minimum: float) -> dict[str, object]:
    return {
        "method": "platt",
        "a": 3.21,
        "b": -1.74,
        "minimum_confidence": minimum,
    }


def _isotonic_payload(minimum: float) -> dict[str, object]:
    return {
        "method": "isotonic",
        "points": [[0.3, 0.18], [0.5, 0.42], [0.7, 0.76], [0.9, 0.95]],
        "minimum_confidence": minimum,
    }


def _ranker_payload() -> dict[str, object]:
    return {
        "schema_version": RANKING_MODEL_SCHEMA_VERSION,
        "model_version": "ranker-v1",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "kind": "logistic",
        "intercept": -0.82,
        "weights": {
            "vector_confidence": 1.72,
            "tag_match_score": 1.31,
            "manual_tag_matches": 0.68,
            "folder_tag_matches": 0.49,
            "identity_match": 1.15,
            "image_text_agreement": 0.74,
        },
    }


def _calibration_payload() -> dict[str, object]:
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "calibration_version": "calibration-v1",
        "query_types": {
            "text": _platt_payload(0.34),
            "image": _isotonic_payload(0.38),
        },
        "global": _platt_payload(0.30),
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
