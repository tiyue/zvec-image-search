from __future__ import annotations

import io
import json
import re
import unittest
from collections.abc import Mapping
from contextlib import redirect_stdout
from typing import cast
from unittest.mock import patch

from image_vector_service.backend_server import serve_backend
from image_vector_service.large_library_policy import (
    build_large_library_policy_snapshot,
    canonical_large_library_policy_sha256,
)


def _snapshot(
    *,
    queue_capacity_per_library: int = 64,
    queue_max_capacity_per_library: int = 10_000,
    queue_retry_after_seconds: float = 1,
    cooperative_checkpoint_items: int = 200,
    cooperative_max_checkpoint_items: int = 10_000,
    cooperative_max_interval_seconds: float = 0.25,
    cooperative_max_high_priority_calls: int = 4,
    result_preview_page_size: int = 15,
    optimize_idle_grace_seconds: float = 2.0,
) -> dict[str, object]:
    return build_large_library_policy_snapshot(
        queue_capacity_per_library=queue_capacity_per_library,
        queue_max_capacity_per_library=queue_max_capacity_per_library,
        queue_retry_after_seconds=queue_retry_after_seconds,
        cooperative_checkpoint_items=cooperative_checkpoint_items,
        cooperative_max_checkpoint_items=cooperative_max_checkpoint_items,
        cooperative_max_interval_seconds=cooperative_max_interval_seconds,
        cooperative_max_high_priority_calls=(cooperative_max_high_priority_calls),
        result_preview_page_size=result_preview_page_size,
        optimize_idle_grace_seconds=optimize_idle_grace_seconds,
    )


class LargeLibraryPolicySnapshotTests(unittest.TestCase):
    def test_snapshot_reports_authoritative_bounded_runtime_policy(self) -> None:
        snapshot = _snapshot(
            queue_capacity_per_library=7,
            cooperative_checkpoint_items=19,
        )

        self.assertEqual(snapshot["schema_version"], 1)
        queue = cast(Mapping[str, object], snapshot["queue"])
        self.assertEqual(
            queue,
            {
                "bounded": True,
                "priority_scheduling": True,
                "capacity_per_library": 7,
                "maximum_capacity_per_library": 10_000,
                "retry_after_seconds": 1.0,
                "cooperative_checkpoint_items": 19,
                "cooperative_maximum_checkpoint_items": 10_000,
                "cooperative_maximum_interval_seconds": 0.25,
                "cooperative_maximum_high_priority_calls": 4,
            },
        )
        writes = cast(Mapping[str, object], snapshot["collection_writes"])
        reads = cast(Mapping[str, object], snapshot["scan_and_reads"])
        results = cast(Mapping[str, object], snapshot["results"])
        self.assertEqual(writes["batch_size"], 256)
        self.assertEqual(reads["state_read_page_size"], 256)
        self.assertEqual(results["preview_page_size"], 15)
        folder = cast(Mapping[str, object], snapshot["folder_inheritance"])
        self.assertEqual(folder["tag_limit_per_source"], 512)
        self.assertEqual(folder["global_tag_limit"], 50_000)
        clustering = cast(Mapping[str, object], snapshot["large_clustering"])
        self.assertEqual(clustering["entry_threshold_exclusive"], 10_000)
        self.assertEqual(clustering["default_types"], ["exact", "perceptual"])
        optimize = cast(Mapping[str, object], snapshot["optimize"])
        self.assertEqual(optimize["change_threshold"], 2_000)
        self.assertEqual(optimize["delete_count_threshold"], 500)
        self.assertEqual(optimize["delete_ratio_threshold"], 0.05)
        self.assertEqual(optimize["idle_grace_seconds"], 2.0)

    def test_snapshot_is_small_canonical_and_contains_no_sensitive_field(self) -> None:
        snapshot = _snapshot()
        encoded = json.dumps(
            snapshot,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = canonical_large_library_policy_sha256(snapshot)

        self.assertLess(len(encoded), 8 * 1024)
        self.assertRegex(digest, re.compile(r"^[0-9a-f]{64}$"))
        reversed_snapshot = dict(reversed(tuple(snapshot.items())))
        self.assertEqual(
            canonical_large_library_policy_sha256(reversed_snapshot),
            digest,
        )
        lowered = encoded.decode("ascii").lower()
        for forbidden in (
            "authorization",
            "bearer",
            "cookie",
            "key",
            "password",
            "path",
            "secret",
            "token",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

    def test_invalid_runtime_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            _snapshot(queue_capacity_per_library=0)
        with self.assertRaisesRegex(ValueError, "configured maximum"):
            _snapshot(
                queue_capacity_per_library=101,
                queue_max_capacity_per_library=100,
            )
        with self.assertRaisesRegex(ValueError, "positive finite number"):
            _snapshot(optimize_idle_grace_seconds=float("nan"))


class BackendStartupPolicyLogTests(unittest.TestCase):
    def test_startup_event_uses_the_same_snapshot_and_digest(self) -> None:
        snapshot = _snapshot()
        digest = canonical_large_library_policy_sha256(snapshot)

        class Server:
            server_address = ("127.0.0.1", 12345)

            def serve_forever(self, *, poll_interval: float) -> None:
                self.poll_interval = poll_interval

            def server_close(self) -> None:
                self.closed = True

        class Manager:
            def version_info(self) -> dict[str, object]:
                return {
                    "instance_id": "instance-test",
                    "config_fingerprint": "f" * 64,
                    "large_library_policy": snapshot,
                    "large_library_policy_sha256": digest,
                }

            def close(self) -> None:
                self.closed = True

        server = Server()
        manager = Manager()
        output = io.StringIO()
        with (
            patch(
                "image_vector_service.backend_server.create_backend_server",
                return_value=(server, manager),
            ),
            redirect_stdout(output),
        ):
            serve_backend(host="127.0.0.1", port=0, token="not-logged")

        event = json.loads(output.getvalue())
        self.assertEqual(event["event"], "backend_listening")
        self.assertEqual(event["large_library_policy"], snapshot)
        self.assertEqual(event["large_library_policy_sha256"], digest)
        self.assertNotIn("not-logged", output.getvalue())


if __name__ == "__main__":
    unittest.main()
