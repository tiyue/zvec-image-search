"""Run a small, isolated live benchmark of the auto-tag network scheduler."""

from __future__ import annotations

import argparse
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from image_vector_service.config import RuntimeCredentials, ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.model_catalog import load_active_model_configuration
from image_vector_service.service import ImageVectorService
from image_vector_service.vision_tagging_client import DashScopeVisionTaggingClient
from zvec_desktop.credentials import default_credential_store


class _OfflineEmbeddingClient:
    """Keep the benchmark focused on auto-tagging without embedding API calls."""

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension

    def embed_images(self, paths: list[Path]) -> EmbeddingResponse:
        return EmbeddingResponse(
            vectors=[[1.0] + [0.0] * (self.dimension - 1) for _path in paths],
            request_id="offline-benchmark-images",
            usage={},
        )

    def embed_text(self, _text: str) -> EmbeddingResponse:
        return EmbeddingResponse(
            vectors=[[1.0] + [0.0] * (self.dimension - 1)],
            request_id="offline-benchmark-text",
            usage={},
        )


class _PeakConcurrency:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        with self._lock:
            self.active -= 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark the production auto-tag scheduler against DashScope "
            "using generated images and an isolated temporary Collection."
        )
    )
    parser.add_argument("--concurrency", type=int, required=True, choices=range(1, 7))
    parser.add_argument("--images", type=int, default=6)
    parser.add_argument("--max-budget-cny", type=float, default=1.0)
    parser.add_argument(
        "--confirm-external-processing",
        action="store_true",
        help="confirm that generated benchmark images may be sent to DashScope",
    )
    return parser


def _create_images(root: Path, count: int) -> None:
    root.mkdir(parents=True)
    for index in range(count):
        image = Image.new(
            "RGB",
            (640, 480),
            (
                (37 + index * 31) % 256,
                (83 + index * 47) % 256,
                (131 + index * 59) % 256,
            ),
        )
        draw = ImageDraw.Draw(image)
        inset = 32 + index * 7
        draw.rectangle(
            (inset, inset, 640 - inset, 480 - inset),
            outline=(255, 255, 255),
            width=8,
        )
        draw.ellipse(
            (90 + index * 9, 80, 310 + index * 9, 300),
            fill=((index * 43) % 256, 210, 120),
        )
        draw.text((24, 440), f"zvec concurrency benchmark {index}", fill="white")
        image.save(root / f"benchmark-{index:02}.png")


def main() -> int:
    options = _parser().parse_args()
    if options.images < options.concurrency or options.images > 24:
        raise SystemExit("--images must be between --concurrency and 24")
    if options.max_budget_cny <= 0:
        raise SystemExit("--max-budget-cny must be positive")
    if not options.confirm_external_processing:
        raise SystemExit(
            "--confirm-external-processing is required for this live benchmark"
        )

    credential_store = default_credential_store()
    secret = credential_store.read_secret()
    if not secret:
        raise SystemExit("No DashScope credential is available.")
    credentials = RuntimeCredentials()
    credentials.configure(secret)
    secret = ""

    model_configuration = load_active_model_configuration()
    peak = _PeakConcurrency()
    original_tag_image = DashScopeVisionTaggingClient.tag_image

    def measured_tag_image(self, image_path, *, context=None):
        peak.enter()
        try:
            return original_tag_image(self, image_path, context=context)
        finally:
            peak.leave()

    with tempfile.TemporaryDirectory(
        prefix="zvec_concurrency_benchmark_",
        ignore_cleanup_errors=True,
    ) as raw:
        temporary = Path(raw)
        library = temporary / "library"
        _create_images(library, options.images)
        config = ServiceConfig(
            workspace=temporary / "workspace",
            config_home=temporary / "config",
            library_image_root=library,
            results_directory=temporary / "results",
            model_configuration=model_configuration,
            runtime_credentials=credentials,
            auto_tag_concurrency=options.concurrency,
        )
        service = ImageVectorService(
            config=config,
            embedding_client=_OfflineEmbeddingClient(config.dimension),
            progress=lambda message: print(
                json.dumps(
                    {"event": "progress", "message": message},
                    ensure_ascii=False,
                ),
                flush=True,
            ),
        )
        try:
            indexed = service.index_folder(str(library))
            started = time.perf_counter()
            with patch.object(
                DashScopeVisionTaggingClient,
                "tag_image",
                measured_tag_image,
            ):
                report = service.auto_tag_images(
                    scope="latest_index_run",
                    max_images=options.images,
                    max_budget_cny=options.max_budget_cny,
                    external_processing_confirmed=True,
                )
            elapsed = time.perf_counter() - started
        finally:
            service.close()
        # Emit the result before TemporaryDirectory cleanup. The native zvec
        # logger keeps its Windows file handle until process exit, so cleanup
        # is deliberately best-effort and a caller may remove any residue after
        # this short-lived process exits.
        print(
            json.dumps(
                {
                    "event": "result",
                    "concurrency": options.concurrency,
                    "images": options.images,
                    "indexed": indexed.inserted,
                    "elapsed_seconds": round(elapsed, 3),
                    "images_per_minute": round(
                        report["unique_processed"] / elapsed * 60,
                        3,
                    ),
                    "peak_active": peak.peak,
                    "succeeded": report["succeeded"],
                    "failed": report["failed"],
                    "api_request_count": report["api_request_count"],
                    "retry_count": report["retry_count"],
                    "flash_requests": report["flash_requests"],
                    "plus_requests": report["plus_requests"],
                    "input_tokens": report["input_tokens"],
                    "output_tokens": report["output_tokens"],
                    "actual_cost_cny": round(report["actual_cost_cny"], 6),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
