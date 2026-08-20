from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from image_vector_service.auto_tagging_assets import (
    FIELD_SPECS,
    FIELD_TAG_LABELS,
    PROMPT_VERSION,
)
from image_vector_service.image_data_uri import DASHSCOPE_DATA_URI_MAX_BYTES
from image_vector_service.vision_tagging_client import (
    DashScopeVisionTaggingClient,
    TaggingBudget,
    TaggingBudgetExceeded,
    TaggingBudgetTracker,
    TaggingContext,
    VisionInputError,
    VisionPricing,
    VisionResponseError,
    VisionTaggingConfig,
    parse_annotation_json,
    parse_model_annotation_json,
    sanitize_generated_tag,
)


@dataclass
class MockReply:
    status: int
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class MockState:
    replies: list[MockReply]
    requests: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _handler_for(state: MockState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            raw_length = self.headers.get("Content-Length")
            length = int(raw_length or "0")
            body = self.rfile.read(length)
            with state.lock:
                state.requests.append(
                    {
                        "headers": dict(self.headers.items()),
                        "body": body,
                    }
                )
                reply = state.replies.pop(0)
            encoded = json.dumps(
                reply.payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


@contextmanager
def _mock_server(replies: list[MockReply]):
    state = MockState(list(replies))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/chat/completions", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _annotation_payload() -> dict[str, Any]:
    fields = {name: {"values": [], "confidence": 0.97} for name in FIELD_SPECS}
    fields["content_domain"]["values"] = ["domain_cosplay"]
    fields["pose"]["values"] = ["pose_standing"]
    fields["expression"]["values"] = ["expression_smile"]
    return {
        "schema_version": 2,
        "description": "刻晴户外写真",
        "fields": fields,
        "entities": {
            "real_person": [
                {
                    "name": "某明星",
                    "state": "confirmed",
                    "evidence": ["visual"],
                    "evidence_text": "外观相似",
                    "confidence": 0.99,
                }
            ],
            "cosplayer": [
                {
                    "name": "Alice",
                    "state": "confirmed",
                    "evidence": ["watermark"],
                    "evidence_text": "@Alice",
                    "confidence": 0.95,
                }
            ],
            "character": [
                {
                    "name": "刻晴",
                    "state": "confirmed",
                    "evidence": ["visual"],
                    "evidence_text": "服装和发型",
                    "confidence": 0.92,
                }
            ],
            "work": [
                {
                    "name": "原神",
                    "state": "confirmed",
                    "evidence": ["visual"],
                    "evidence_text": "角色视觉特征",
                    "confidence": 0.94,
                }
            ],
        },
    }


def _success_reply(
    *, request_id: str = "vision-request", annotation: dict[str, Any] | None = None
) -> MockReply:
    return MockReply(
        200,
        {
            "id": request_id,
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            annotation or _annotation_payload(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 1_000,
                "completion_tokens": 200,
                "total_tokens": 1_200,
            },
        },
    )


def _write_test_image(directory: str, *, mode: str = "RGB") -> Path:
    path = Path(directory) / "刻晴-001.png"
    color = (128, 64, 192, 255) if mode == "RGBA" else (128, 64, 192)
    Image.new(mode, (2, 2), color=color).save(path)
    return path


def _without_identity_review(payload: dict[str, Any]) -> dict[str, Any]:
    copied = json.loads(json.dumps(payload, ensure_ascii=False))
    copied["entities"] = {name: [] for name in copied["entities"]}
    return copied


class VisionTaggingClientTests(unittest.TestCase):
    def test_request_is_json_without_thinking_and_schema_v2_is_controlled(self) -> None:
        pricing = VisionPricing(0.15, 1.50, effective_from="test-only")
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = _write_test_image(temporary_directory)
            with _mock_server([_success_reply()]) as (url, state):
                client = DashScopeVisionTaggingClient(
                    VisionTaggingConfig(
                        api_url=url,
                        api_key="test-key-not-real",
                        max_retries=0,
                        pricing=pricing,
                    )
                )
                response = client.tag_image(
                    image_path,
                    context=TaggingContext(folder_name="原神-刻晴-120P-1.2GB"),
                )

        self.assertEqual(len(state.requests), 1)
        captured = state.requests[0]
        headers = captured["headers"]
        self.assertEqual(int(headers["Content-Length"]), len(captured["body"]))
        self.assertNotIn("Transfer-Encoding", headers)
        request_json = json.loads(captured["body"].decode("utf-8"))
        self.assertEqual(request_json["model"], "qwen3-vl-flash")
        self.assertIs(request_json["enable_thinking"], False)
        self.assertEqual(request_json["response_format"], {"type": "json_object"})
        image_part = request_json["messages"][1]["content"][0]
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/png"))

        self.assertEqual(response.request_id, "vision-request")
        self.assertEqual(response.usage.input_tokens, 1_000)
        self.assertEqual(response.usage.output_tokens, 200)
        self.assertAlmostEqual(response.cost_yuan or 0, 0.00045)
        self.assertEqual(response.annotation.schema_version, 2)
        self.assertEqual(response.annotation.prompt_version, PROMPT_VERSION)
        self.assertEqual(
            response.annotation.stable_fields["content_domain"].values,
            ("domain_cosplay",),
        )
        expected_tags = {
            FIELD_TAG_LABELS["domain_cosplay"],
            FIELD_TAG_LABELS["pose_standing"],
            FIELD_TAG_LABELS["expression_smile"],
        }
        self.assertEqual(set(response.annotation.controlled_tags), expected_tags)
        self.assertEqual(response.annotation.tags, response.annotation.controlled_tags)
        self.assertEqual(response.annotation.suggested_tags, ())
        for free_text in ("Alice", "刻晴", "原神", "某明星"):
            self.assertNotIn(free_text, response.annotation.tags)
        rejected_person = response.annotation.entities["real_person"][0]
        self.assertIsNone(rejected_person.name)
        self.assertEqual(rejected_person.state, "unable_to_confirm")
        rejected_cosplayer = response.annotation.entities["cosplayer"][0]
        self.assertIsNone(rejected_cosplayer.name)
        self.assertEqual(rejected_cosplayer.state, "unable_to_confirm")
        self.assertTrue(response.annotation.requires_review)
        self.assertFalse(response.annotation.plus_recommended)
        self.assertTrue(response.annotation.warnings)

    def test_identity_evidence_must_exist_in_the_supplied_context(self) -> None:
        cases = (
            (
                "watermark",
                TaggingContext(watermark_text="@SomeoneElse"),
                TaggingContext(watermark_text="摄影：@Alice"),
            ),
            (
                "ocr",
                TaggingContext(ocr_text="角色：刻晴"),
                TaggingContext(ocr_text="出镜：Alice"),
            ),
        )
        for evidence_source, missing_context, matched_context in cases:
            with self.subTest(evidence_source=evidence_source):
                payload = _without_identity_review(_annotation_payload())
                payload["entities"]["cosplayer"] = [
                    {
                        "name": "Alice",
                        "state": "confirmed",
                        "evidence": [evidence_source],
                        "evidence_text": "Alice",
                        "confidence": 0.95,
                    }
                ]

                missing = parse_annotation_json(
                    json.dumps(payload, ensure_ascii=False),
                    context=missing_context,
                )
                missing_identity = missing.entities["cosplayer"][0]
                self.assertIsNone(missing_identity.name)
                self.assertEqual(missing_identity.state, "unable_to_confirm")

                matched = parse_annotation_json(
                    json.dumps(payload, ensure_ascii=False),
                    context=matched_context,
                )
                matched_identity = matched.entities["cosplayer"][0]
                self.assertEqual(matched_identity.name, "Alice")
                self.assertEqual(matched_identity.state, "confirmed")

    def test_schema_v2_rejects_legacy_tags_unknown_fields_and_codes(self) -> None:
        for forbidden in ("tags", "suggested_tags", "unexpected"):
            payload = _annotation_payload()
            payload[forbidden] = []
            with (
                self.subTest(forbidden=forbidden),
                self.assertRaises(VisionResponseError),
            ):
                parse_annotation_json(json.dumps(payload, ensure_ascii=False))

        payload = _annotation_payload()
        payload["fields"]["pose"]["values"] = ["free_text_pose"]
        with self.assertRaises(VisionResponseError):
            parse_annotation_json(json.dumps(payload, ensure_ascii=False))

    def test_model_parser_repairs_common_schema_drift_before_strict_validation(
        self,
    ) -> None:
        payload = _annotation_payload()
        payload["description"] = (
            "这是一段明显超过二十个字符的模型图片描述，需要被安全截断"
        )
        payload["extra"] = "ignored"
        payload["fields"]["content_domain"] = ["domain_cosplay"]
        payload["fields"]["pose"]["values"] = ["pose_laying", "pose_laying"]
        payload["fields"]["clothing"]["values"] = [
            "clothing_tights",
            "unknown_clothing",
        ]
        payload["entities"]["character"] = "刻晴"
        payload["entities"]["cosplayer"][0]["state"] = "unable_to_confirm"

        parsed = parse_model_annotation_json(
            json.dumps(payload, ensure_ascii=False),
            context=TaggingContext(file_name="刻晴.png"),
        )

        self.assertLessEqual(len(parsed.description), 20)
        self.assertEqual(
            parsed.stable_fields["content_domain"].values,
            ("domain_cosplay",),
        )
        self.assertEqual(parsed.stable_fields["pose"].values, ("pose_lying",))
        self.assertEqual(
            parsed.stable_fields["legwear"].values,
            ("legwear_tights",),
        )
        self.assertEqual(parsed.entities["character"][0].state, "suggested")
        self.assertIsNone(parsed.entities["cosplayer"][0].name)
        self.assertTrue(
            any(warning.startswith("repair:") for warning in parsed.warnings)
        )

    def test_model_repair_never_invents_restricted_identity(self) -> None:
        payload = _annotation_payload()
        payload["entities"]["real_person"] = ["Visual Guess"]
        payload["entities"]["cosplayer"] = [
            {
                "name": "Guessed Coser",
                "state": "confirmed",
                "evidence": "visual",
                "confidence": "0.99",
            }
        ]

        parsed = parse_model_annotation_json(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(parsed.entities["real_person"], ())
        cosplayer = parsed.entities["cosplayer"][0]
        self.assertIsNone(cosplayer.name)
        self.assertEqual(cosplayer.state, "unable_to_confirm")

    def test_client_accepts_repairable_provider_output_without_retry(self) -> None:
        payload = _annotation_payload()
        payload["description"] = "超过二十个字符的描述会被修复且不应再次请求模型服务"
        payload["fields"]["content_domain"] = ["domain_cosplay"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = _write_test_image(temporary_directory)
            with _mock_server([_success_reply(annotation=payload)]) as (url, state):
                client = DashScopeVisionTaggingClient(
                    VisionTaggingConfig(
                        api_url=url,
                        api_key="test-key-not-real",
                        max_retries=2,
                        retry_base_seconds=0,
                    )
                )
                response = client.tag_image(image_path)

        self.assertEqual(len(state.requests), 1)
        self.assertLessEqual(len(response.annotation.description), 20)
        self.assertTrue(
            any(
                warning == "repair:description_truncated"
                for warning in response.annotation.warnings
            )
        )

    def test_fields_require_exact_structure_cardinality_and_confidence(self) -> None:
        mutations = []
        missing_confidence = _annotation_payload()
        del missing_confidence["fields"]["pose"]["confidence"]
        mutations.append(missing_confidence)
        extra_member = _annotation_payload()
        extra_member["fields"]["pose"]["note"] = "free text"
        mutations.append(extra_member)
        non_array = _annotation_payload()
        non_array["fields"]["pose"]["values"] = "pose_standing"
        mutations.append(non_array)
        duplicate = _annotation_payload()
        duplicate["fields"]["pose"]["values"] = ["pose_standing", "pose_standing"]
        mutations.append(duplicate)
        single_value = _annotation_payload()
        single_value["fields"]["people_count"]["values"] = [
            "count_single_person",
            "count_two_people",
        ]
        mutations.append(single_value)
        too_many = _annotation_payload()
        pose_spec = FIELD_SPECS["pose"]
        too_many["fields"]["pose"]["values"] = list(
            pose_spec.values[: pose_spec.max_items + 1]
        )
        mutations.append(too_many)
        boolean_confidence = _annotation_payload()
        boolean_confidence["fields"]["pose"]["confidence"] = True
        mutations.append(boolean_confidence)

        for index, payload in enumerate(mutations):
            with self.subTest(case=index), self.assertRaises(VisionResponseError):
                parse_annotation_json(json.dumps(payload, ensure_ascii=False))

    def test_empty_fields_do_not_trigger_review_or_plus(self) -> None:
        payload = _without_identity_review(_annotation_payload())
        for field_payload in payload["fields"].values():
            field_payload["values"] = []
            field_payload["confidence"] = 0.0

        annotation = parse_annotation_json(json.dumps(payload, ensure_ascii=False))

        self.assertFalse(annotation.requires_review)
        self.assertFalse(annotation.plus_recommended)
        self.assertEqual(annotation.controlled_tags, ())

    def test_entity_policy_downgrades_low_confidence_and_limits_plus_recommendation(
        self,
    ) -> None:
        low_character = _without_identity_review(_annotation_payload())
        low_character["entities"]["character"] = [
            {
                "name": "刻晴",
                "state": "confirmed",
                "evidence": ["visual"],
                "evidence_text": "服装",
                "confidence": 0.79,
            }
        ]
        parsed = parse_annotation_json(json.dumps(low_character, ensure_ascii=False))
        self.assertEqual(parsed.entities["character"][0].state, "suggested")
        self.assertTrue(parsed.requires_review)
        self.assertTrue(parsed.plus_recommended)

        visual_identity = _without_identity_review(_annotation_payload())
        visual_identity["entities"]["real_person"] = [
            {
                "name": "某明星",
                "state": "confirmed",
                "evidence": ["visual"],
                "evidence_text": "外观",
                "confidence": 0.99,
            }
        ]
        parsed = parse_annotation_json(json.dumps(visual_identity, ensure_ascii=False))
        self.assertEqual(parsed.entities["real_person"][0].state, "unable_to_confirm")
        self.assertIsNone(parsed.entities["real_person"][0].name)
        self.assertTrue(parsed.requires_review)
        self.assertFalse(parsed.plus_recommended)

    def test_conflict_and_low_confidence_field_policy_signals(self) -> None:
        conflict = _without_identity_review(_annotation_payload())
        conflict["entities"]["work"] = [
            {
                "name": "原神或崩坏：星穹铁道",
                "state": "conflict",
                "evidence": ["visual"],
                "evidence_text": "视觉证据冲突",
                "confidence": 0.90,
            }
        ]
        parsed = parse_annotation_json(json.dumps(conflict, ensure_ascii=False))
        self.assertTrue(parsed.plus_recommended)
        self.assertIn("entity:work:conflict", parsed.review_reasons)

        critical = _without_identity_review(_annotation_payload())
        critical["fields"]["content_domain"]["confidence"] = 0.64
        parsed = parse_annotation_json(json.dumps(critical, ensure_ascii=False))
        self.assertTrue(parsed.requires_review)
        self.assertTrue(parsed.plus_recommended)

        noncritical = _without_identity_review(_annotation_payload())
        noncritical["fields"]["pose"]["confidence"] = 0.64
        parsed = parse_annotation_json(json.dumps(noncritical, ensure_ascii=False))
        self.assertTrue(parsed.requires_review)
        self.assertFalse(parsed.plus_recommended)

    def test_entities_require_exact_structure_known_state_and_confidence(self) -> None:
        for mutation in ("missing_confidence", "unknown_state", "unable_with_name"):
            payload = _without_identity_review(_annotation_payload())
            entity = {
                "name": "刻晴",
                "state": "confirmed",
                "evidence": ["visual"],
                "evidence_text": "服装",
                "confidence": 0.92,
            }
            if mutation == "missing_confidence":
                del entity["confidence"]
            elif mutation == "unknown_state":
                entity["state"] = "unknown"
            else:
                entity["state"] = "unable_to_confirm"
            payload["entities"]["character"] = [entity]
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(VisionResponseError),
            ):
                parse_annotation_json(json.dumps(payload, ensure_ascii=False))

    def test_invalid_model_json_is_rejected(self) -> None:
        reply = MockReply(
            200,
            {
                "id": "bad-json",
                "choices": [{"message": {"content": "{not valid json"}}],
                "usage": {},
            },
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = _write_test_image(temporary_directory)
            with _mock_server([reply]) as (url, _state):
                client = DashScopeVisionTaggingClient(
                    VisionTaggingConfig(
                        api_url=url,
                        api_key="test-key-not-real",
                        max_retries=0,
                    )
                )
                with self.assertRaises(VisionResponseError):
                    client.tag_image(image_path)

    def test_retries_429_and_5xx_then_succeeds(self) -> None:
        replies = [
            MockReply(
                429,
                {"error": {"code": "RateLimit", "message": "slow down"}},
                {"Retry-After": "0"},
            ),
            MockReply(
                503,
                {"error": {"code": "Unavailable", "message": "retry"}},
            ),
            _success_reply(request_id="after-retries"),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = _write_test_image(temporary_directory)
            with _mock_server(replies) as (url, state):
                client = DashScopeVisionTaggingClient(
                    VisionTaggingConfig(
                        api_url=url,
                        api_key="test-key-not-real",
                        max_retries=2,
                        retry_base_seconds=0,
                    )
                )
                response = client.tag_image(image_path)

        self.assertEqual(response.request_id, "after-retries")
        self.assertEqual(client.request_count, 3)
        self.assertEqual(len(state.requests), 3)

    def test_budget_blocks_second_image_before_http_request(self) -> None:
        tracker = TaggingBudgetTracker(TaggingBudget(max_images=1))
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = _write_test_image(temporary_directory)
            with _mock_server([_success_reply()]) as (url, state):
                client = DashScopeVisionTaggingClient(
                    VisionTaggingConfig(
                        api_url=url,
                        api_key="test-key-not-real",
                        max_retries=0,
                    ),
                    budget_tracker=tracker,
                )
                client.tag_image(image_path)
                with self.assertRaises(TaggingBudgetExceeded):
                    client.tag_image(image_path)

        self.assertEqual(len(state.requests), 1)
        self.assertEqual(tracker.snapshot()["completed_images"], 1)

    def test_source_under_twenty_mib_is_transcoded_when_data_uri_is_oversized(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = _write_test_image(temporary_directory)
            with image_path.open("ab") as handle:
                handle.write(b"\0" * (16 * 1024 * 1024))
            source_size = image_path.stat().st_size
            self.assertLess(source_size, 20 * 1024 * 1024)
            self.assertGreater((source_size * 4) // 3, DASHSCOPE_DATA_URI_MAX_BYTES)
            with _mock_server([_success_reply()]) as (url, state):
                config = VisionTaggingConfig(
                    api_url=url,
                    api_key="test-key-not-real",
                    max_retries=0,
                )
                client = DashScopeVisionTaggingClient(config)
                client.tag_image(image_path)

            request = state.requests[0]["body"]
            payload = json.loads(request.decode("utf-8"))
            data_uri = payload["messages"][1]["content"][0]["image_url"]["url"]
            self.assertTrue(data_uri.startswith("data:image/jpeg;base64,"))
            self.assertLessEqual(
                len(data_uri.encode("ascii")), DASHSCOPE_DATA_URI_MAX_BYTES
            )
            self.assertLessEqual(len(request), config.max_request_bytes)
            self.assertEqual(image_path.stat().st_size, source_size)

    def test_provider_size_rejection_forces_one_smaller_transcode_retry(
        self,
    ) -> None:
        replies = [
            MockReply(
                400,
                {
                    "error": {
                        "code": "InvalidParameter",
                        "message": "Image size should be less than 10MB",
                    }
                },
            ),
            _success_reply(request_id="after-smaller-image"),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "oversized-retry.png"
            Image.effect_noise((512, 512), 100).convert("RGB").save(image_path)
            original = image_path.read_bytes()
            with _mock_server(replies) as (url, state):
                client = DashScopeVisionTaggingClient(
                    VisionTaggingConfig(
                        api_url=url,
                        api_key="test-key-not-real",
                        max_retries=0,
                    )
                )
                response = client.tag_image(image_path)

            first_payload = json.loads(state.requests[0]["body"].decode("utf-8"))
            second_payload = json.loads(state.requests[1]["body"].decode("utf-8"))
            first_uri = first_payload["messages"][1]["content"][0]["image_url"]["url"]
            second_uri = second_payload["messages"][1]["content"][0]["image_url"]["url"]
            after = image_path.read_bytes()

        self.assertEqual(response.request_id, "after-smaller-image")
        self.assertEqual(len(state.requests), 2)
        self.assertTrue(first_uri.startswith("data:image/png;base64,"))
        self.assertTrue(second_uri.startswith("data:image/jpeg;base64,"))
        self.assertLess(len(second_uri), len(first_uri))
        self.assertEqual(after, original)

    def test_transcoding_flattens_transparent_png_to_white(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = Path(temporary_directory) / "transparent.png"
            noise = Image.effect_noise((512, 512), 100).convert("L")
            transparent = Image.merge(
                "RGBA",
                (noise, noise, noise, Image.new("L", noise.size, 0)),
            )
            transparent.save(image_path)
            source_size = image_path.stat().st_size
            with _mock_server([_success_reply()]) as (url, state):
                config = VisionTaggingConfig(
                    api_url=url,
                    api_key="test-key-not-real",
                    max_retries=0,
                    max_image_bytes=source_size + 1,
                    max_request_bytes=source_size + 1,
                )
                DashScopeVisionTaggingClient(config).tag_image(image_path)

            request = state.requests[0]["body"]
            payload = json.loads(request.decode("utf-8"))
            data_uri = payload["messages"][1]["content"][0]["image_url"]["url"]
            self.assertTrue(data_uri.startswith("data:image/jpeg;base64,"))
            encoded = data_uri.split(",", 1)[1]
            with Image.open(BytesIO(base64.b64decode(encoded))) as decoded:
                self.assertEqual(decoded.mode, "RGB")
                pixel = decoded.getpixel((0, 0))
                self.assertTrue(all(channel >= 245 for channel in pixel))
            self.assertLessEqual(len(request), config.max_request_bytes)

    def test_uncompressible_request_and_pixel_guard_surface_clear_input_errors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            image_path = _write_test_image(temporary_directory)
            source_size = image_path.stat().st_size
            probe = DashScopeVisionTaggingClient(
                VisionTaggingConfig(
                    api_url="http://127.0.0.1:1/chat/completions",
                    api_key="test-key-not-real",
                    max_image_bytes=source_size + 1,
                    max_request_bytes=128 * 1024,
                )
            )
            context = TaggingContext(file_name=image_path.name)
            overhead = len(
                json.dumps(
                    probe._build_payload("", context),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            budget = max(source_size + 1, overhead + 10)
            client = DashScopeVisionTaggingClient(
                VisionTaggingConfig(
                    api_url="http://127.0.0.1:1/chat/completions",
                    api_key="test-key-not-real",
                    max_image_bytes=source_size + 1,
                    max_request_bytes=budget,
                    max_retries=0,
                )
            )
            with self.assertRaises(VisionInputError) as uncompressible:
                client.tag_image(image_path, context=context)
            self.assertIn("2x2", str(uncompressible.exception))
            self.assertIn("bytes", str(uncompressible.exception))

            with (
                patch("image_vector_service.image_data_uri._MAX_DECODE_PIXELS", 1),
                self.assertRaises(VisionInputError) as pixel_guard,
            ):
                client.tag_image(image_path, context=context)
            self.assertIn("safe decode limit", str(pixel_guard.exception))
            self.assertIn("2x2", str(pixel_guard.exception))

    def test_technical_metadata_is_removed_without_losing_name(self) -> None:
        cases = {
            "120P": None,
            "80张": None,
            "30 images": None,
            "1.5GB": None,
            "850MB": None,
            "1024KB": None,
            "原神-120P-1.2GB": "原神",
            "刻晴 80张 850MB": "刻晴",
            "原神120P写真1.2GB": "原神写真",
            "2B": "2B",
            "120Photoshop教程": "120Photoshop教程",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_generated_tag(raw), expected)


if __name__ == "__main__":
    unittest.main()
