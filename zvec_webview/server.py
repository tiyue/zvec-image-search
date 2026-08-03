"""Token-scoped loopback HTTP gateway for the local HTML application."""

from __future__ import annotations

import io
import json
import mimetypes
import secrets
import tempfile
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast
from urllib.parse import parse_qs, unquote, urlsplit

from PIL import Image, UnidentifiedImageError

from image_vector_service.raw_selection import RawSelectionService
from zvec_host.recommendation_service import (
    RecommendationService,
    RecommendationServiceError,
)

from .diagnostics import (
    DiagnosticValidationError,
    FrontendDiagnosticLog,
    validate_frontend_diagnostic,
)
from .facade import FacadeError, PreviewFacade
from .frontend_assets import FrontendAssetError, validate_frontend_build
from .image_registry import ImageRegistryError, ImageVariant

_MAX_JSON_BYTES: Final = 2 * 1024 * 1024
_MAX_DIAGNOSTIC_JSON_BYTES: Final = 64 * 1024
_MAX_QUERY_IMAGE_BYTES: Final = 128 * 1024 * 1024
_MAX_ASSET_BYTES: Final = 8 * 1024 * 1024
_QUERY_IMAGE_SUFFIXES: Final = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "GIF": ".gif",
    "TIFF": ".tiff",
}
_ASSET_TYPES: Final = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class GatewayError(RuntimeError):
    """The local gateway could not start or serve a safe request."""


@dataclass(frozen=True, slots=True)
class GatewayAddress:
    host: str
    port: int
    token: str

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def base_path(self) -> str:
        return f"/{self.token}/"

    @property
    def url(self) -> str:
        return f"{self.origin}{self.base_path}"


class GatewayServer:
    """Serve local assets and a narrow API from one random loopback origin."""

    def __init__(
        self,
        facade: PreviewFacade,
        *,
        assets_directory: str | Path | None = None,
        token: str | None = None,
        diagnostic_log: FrontendDiagnosticLog | None = None,
    ) -> None:
        if not isinstance(facade, PreviewFacade):
            # Tests can use a structural fake, but production should always pass
            # the real facade.  Avoid an isinstance-only runtime dependency there.
            required = ("bootstrap", "settings", "latest_results")
            if any(not callable(getattr(facade, name, None)) for name in required):
                raise TypeError("facade does not implement the preview contract")
        root = (
            Path(assets_directory).expanduser()
            if assets_directory is not None
            else Path(__file__).resolve().parent / "frontend_dist"
        )
        try:
            frontend = validate_frontend_build(root)
        except FrontendAssetError as exc:
            raise GatewayError(f"前端构建资源无效：{exc}") from exc
        self._assets = frontend.root
        supplied_token = token or secrets.token_urlsafe(32)
        if (
            not isinstance(supplied_token, str)
            or len(supplied_token) < 24
            or len(supplied_token) > 128
            or any(character.isspace() for character in supplied_token)
            or "/" in supplied_token
        ):
            raise ValueError("gateway token must be 24-128 URL-safe characters")
        self._facade = facade
        self._token = supplied_token
        config_home = getattr(facade, "config_home", None)
        self._diagnostic_log: FrontendDiagnosticLog | None
        if diagnostic_log is not None:
            self._diagnostic_log = diagnostic_log
        elif isinstance(config_home, str | Path):
            self._diagnostic_log = FrontendDiagnosticLog(
                Path(config_home) / "logs" / "frontend-diagnostics.jsonl",
                redactions=(supplied_token,),
            )
        else:
            # Structural test facades and intentionally minimal embedders can
            # run without a filesystem sink. Valid events are still accepted.
            self._diagnostic_log = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._address: GatewayAddress | None = None
        self._lock = threading.RLock()
        self._query_images: tempfile.TemporaryDirectory[str] | None = None
        self._query_image_ids: set[str] = set()
        self._recommendations: RecommendationService | None = None
        self._raw_selection: RawSelectionService | None = None

    def _record_frontend_diagnostic(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and persist a browser event without endangering the UI."""

        writer = self._diagnostic_log
        redactions: list[str] = [self._token]
        redaction_provider = getattr(self._facade, "diagnostic_redactions", None)
        if callable(redaction_provider):
            with suppress(Exception):
                supplied = redaction_provider()
                if isinstance(supplied, Sequence) and not isinstance(
                    supplied, (str, bytes, bytearray)
                ):
                    redactions.extend(
                        value
                        for value in supplied[:32]
                        if isinstance(value, str) and 0 < len(value) <= 4_096
                    )
        try:
            event = validate_frontend_diagnostic(
                payload,
                redactions=tuple(dict.fromkeys(redactions)),
            )
        except DiagnosticValidationError as exc:
            raise FacadeError(
                "invalid_diagnostic",
                str(exc),
                status=400,
            ) from exc
        response: dict[str, Any]
        if writer is None:
            response = {
                "accepted": True,
                "persisted": False,
                "code": "diagnostic_log_unavailable",
            }
        else:
            try:
                writer.record(event)
            except Exception:
                # Frontend logging is best-effort. A read-only directory,
                # antivirus lock, or full disk must never break other APIs.
                response = {
                    "accepted": True,
                    "persisted": False,
                    "code": "diagnostic_write_failed",
                    "log_file": str(writer.path),
                }
            else:
                response = {
                    "accepted": True,
                    "persisted": True,
                    "log_file": str(writer.path),
                }

        recorder = getattr(self._facade, "record_frontend_activity", None)
        if callable(recorder):
            # The bounded text log and HTTP acknowledgement remain valid even
            # when the optional structured activity mirror is unavailable.
            with suppress(Exception):
                recorder(dict(event))
        return response

    def _activity_method(self, name: str) -> Any:
        method = getattr(self._facade, name, None)
        if not callable(method):
            raise FacadeError(
                "activity_unavailable",
                "任务历史与操作日志暂不可用。",
                status=503,
            )
        return method

    @property
    def address(self) -> GatewayAddress:
        address = self._address
        if address is None:
            raise GatewayError("本地页面网关尚未启动。")
        return address

    @property
    def url(self) -> str:
        return self.address.url

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return self._httpd is not None and thread is not None and thread.is_alive()

    def start(self) -> GatewayAddress:
        with self._lock:
            if self.is_running:
                return self.address
            handler = _handler_type(self)
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            except OSError as exc:
                raise GatewayError("无法启动本地页面网关。") from exc
            httpd.daemon_threads = True
            httpd.allow_reuse_address = False
            host, port = httpd.server_address[:2]
            address = GatewayAddress(str(host), int(port), self._token)
            self._httpd = httpd
            self._address = address
            thread = threading.Thread(
                target=httpd.serve_forever,
                kwargs={"poll_interval": 0.1},
                name="zvec-webview-gateway",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return address

    def stop(self) -> None:
        with self._lock:
            httpd = self._httpd
            thread = self._thread
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)
        with self._lock:
            if self._httpd is httpd:
                self._httpd = None
                self._thread = None
                self._address = None
            query_images = self._query_images
            query_image_ids = tuple(self._query_image_ids)
            self._query_images = None
            self._query_image_ids.clear()
        for image_id in query_image_ids:
            try:
                self._facade.image_registry.forget(image_id)
            except ImageRegistryError:
                continue
        if query_images is not None:
            query_images.cleanup()
        raw_selection = self._raw_selection
        self._raw_selection = None
        if raw_selection is not None:
            with suppress(Exception):
                raw_selection.close()

    def _store_query_image(
        self,
        content: bytes,
        *,
        requested_name: str,
    ) -> dict[str, Any]:
        """Validate a dropped/pasted image and return only an opaque ID."""

        try:
            with Image.open(io.BytesIO(content)) as source:
                image_format = str(source.format or "").upper()
                source.verify()
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            raise FacadeError(
                "invalid_query_image",
                "拖放或粘贴的内容不是可用图片。",
                status=400,
            ) from exc
        suffix = _QUERY_IMAGE_SUFFIXES.get(image_format)
        if suffix is None:
            raise FacadeError(
                "unsupported_query_image",
                "暂不支持此图片格式。",
                status=415,
            )
        display_name = _safe_query_image_name(requested_name, suffix)
        with self._lock:
            temporary = self._query_images
            if temporary is None:
                temporary = tempfile.TemporaryDirectory(prefix="zvec-query-images-")
                self._query_images = temporary
            root = Path(temporary.name)
        path = root / f"{secrets.token_hex(20)}{suffix}"
        try:
            path.write_bytes(content)
            metadata = self._facade.image_registry.register(path)
        except (OSError, ImageRegistryError) as exc:
            path.unlink(missing_ok=True)
            raise FacadeError(
                "query_image_registration_failed",
                "无法读取拖放或粘贴的图片。",
                status=400,
            ) from exc
        with self._lock:
            self._query_image_ids.add(metadata.image_id)
        thumbnail_url = f"api/image/{metadata.image_id}?variant=thumbnail"
        return {
            "ok": True,
            "id": metadata.image_id,
            "image_id": metadata.image_id,
            "name": display_name,
            "thumbnail_url": thumbnail_url,
            "image": {
                "id": metadata.image_id,
                "name": display_name,
                "thumbnail_url": thumbnail_url,
            },
        }

    def _recommendation_service(self) -> RecommendationService:
        with self._lock:
            if self._recommendations is not None:
                return self._recommendations
            config_home = getattr(self._facade, "config_home", None)
            client_provider = getattr(self._facade, "_ready_client", None)
            if not isinstance(config_home, Path) or not callable(client_provider):
                raise FacadeError(
                    "recommendations_unavailable",
                    "图片推荐暂不可用，请确认本地服务已经就绪。",
                    status=503,
                )
            self._recommendations = RecommendationService(
                client_provider,
                self._facade.image_registry,
                config_home,
            )
            return self._recommendations

    def _raw_selection_service(self) -> RawSelectionService:
        with self._lock:
            if self._raw_selection is not None:
                return self._raw_selection
            config_home = getattr(self._facade, "config_home", None)
            if not isinstance(config_home, str | Path):
                raise FacadeError(
                    "raw_selection_unavailable",
                    "ARW 选片暂不可用，请确认本地服务已经就绪。",
                    status=503,
                )
            data_dir = Path(config_home) / "raw-selection"
            self._raw_selection = RawSelectionService(data_dir)
            self._raw_selection.recover_pending_deletes()
            return self._raw_selection

    def _asset(self, relative: str) -> tuple[bytes, str]:
        decoded = unquote(relative).replace("\\", "/")
        pure = PurePosixPath(decoded)
        if pure.is_absolute() or ".." in pure.parts or "" in pure.parts:
            raise FacadeError("asset_not_found", "资源不存在。", status=404)
        requested = self._assets.joinpath(*pure.parts)
        try:
            resolved = requested.resolve(strict=True)
            resolved.relative_to(self._assets)
            size = resolved.stat().st_size
        except (OSError, ValueError) as exc:
            raise FacadeError("asset_not_found", "资源不存在。", status=404) from exc
        if not resolved.is_file() or resolved.is_symlink():
            raise FacadeError("asset_not_found", "资源不存在。", status=404)
        content_type = _ASSET_TYPES.get(resolved.suffix.casefold())
        if content_type is None:
            guessed, _encoding = mimetypes.guess_type(resolved.name)
            if guessed is None or not guessed.startswith(("image/", "font/")):
                raise FacadeError("asset_not_found", "资源类型不受支持。", status=404)
            content_type = guessed
        if size <= 0 or size > _MAX_ASSET_BYTES:
            raise FacadeError("asset_not_found", "资源大小无效。", status=404)
        try:
            return resolved.read_bytes(), content_type
        except OSError as exc:
            raise FacadeError(
                "asset_read_failed", "无法读取页面资源。", status=500
            ) from exc


def _handler_type(gateway: GatewayServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ZvecPreview/1"
        sys_version = ""

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def do_PATCH(self) -> None:  # noqa: N802
            self._dispatch("PATCH")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

        def do_OPTIONS(self) -> None:  # noqa: N802
            # The application is same-origin and never needs CORS preflight.
            self._json_error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method_not_allowed",
                "不支持跨来源请求。",
            )

        def log_message(self, _format: str, *args: Any) -> None:
            del args

        def _dispatch(self, method: str) -> None:
            try:
                self._validate_authority()
                split = urlsplit(self.path)
                relative = self._relative_path(split.path)
                if not relative or relative == "index.html":
                    if method != "GET":
                        raise FacadeError(
                            "method_not_allowed", "请求方法不受支持。", status=405
                        )
                    self._send_asset("index.html")
                    return
                if relative.startswith("api/"):
                    self._route_api(method, relative[4:], split.query)
                    return
                if method != "GET":
                    raise FacadeError(
                        "method_not_allowed", "请求方法不受支持。", status=405
                    )
                self._send_asset(relative)
            except FacadeError as exc:
                self._json(
                    exc.status,
                    {"error": exc.to_dict()},
                )
            except RecommendationServiceError as exc:
                self._json_error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "recommendations_unavailable",
                    str(exc),
                )
            except ImageRegistryError as exc:
                self._json_error(HTTPStatus.NOT_FOUND, "image_not_found", str(exc))
            except (ConnectionError, BrokenPipeError):
                return
            except Exception:
                # Never expose stack traces, local paths, or backend credentials.
                self._json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "本地页面请求处理失败。",
                )

        def _route_api(self, method: str, route: str, query: str) -> None:
            segments = tuple(
                unquote(segment) for segment in route.strip("/").split("/") if segment
            )
            query_values = parse_qs(query, keep_blank_values=False)
            if method == "GET" and segments == ("bootstrap",):
                self._json(HTTPStatus.OK, gateway._facade.bootstrap())
                return
            if method == "POST" and segments == ("recommendations",):
                body = self._read_json()
                if set(body) != {"request_id"}:
                    raise FacadeError("invalid_request", "推荐请求字段无效。")
                self._json(
                    HTTPStatus.OK,
                    gateway._recommendation_service().create_recommendations(
                        _recommendation_text(body, "request_id", maximum=160)
                    ),
                )
                return
            if (
                method == "POST"
                and len(segments) == 3
                and segments[0] == "recommendations"
                and segments[2] in {"shown", "actions"}
            ):
                batch_id = _recommendation_text(
                    {"batch_id": segments[1]}, "batch_id", maximum=160
                )
                body = self._read_json()
                action_status: dict[str, object] = {}
                if segments[2] == "shown":
                    if set(body) != {"event_id"}:
                        raise FacadeError("invalid_request", "推荐展示记录字段无效。")
                    event_id = _recommendation_text(body, "event_id", maximum=160)
                    gateway._recommendation_service().mark_recommendations_shown(
                        batch_id, event_id
                    )
                else:
                    if set(body) - {"event_id", "item_id", "action", "metadata"} or (
                        not {"event_id", "item_id", "action"}.issubset(body)
                    ):
                        raise FacadeError("invalid_request", "推荐操作字段无效。")
                    event_id = _recommendation_text(body, "event_id", maximum=160)
                    item_id = _recommendation_text(body, "item_id", maximum=160)
                    action = _recommendation_text(body, "action", maximum=32)
                    if action not in {"open", "like", "export", "dislike"}:
                        raise FacadeError("invalid_request", "推荐操作无效。")
                    metadata = _recommendation_metadata(body.get("metadata"))
                    action_status = (
                        gateway._recommendation_service().record_recommendation_action(
                            batch_id, event_id, item_id, action, metadata
                        )
                    )
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "event_id": event_id, **action_status},
                )
                return
            if method == "POST" and segments == ("diagnostics", "frontend"):
                self._json(
                    HTTPStatus.ACCEPTED,
                    gateway._record_frontend_diagnostic(
                        self._read_json(maximum=_MAX_DIAGNOSTIC_JSON_BYTES)
                    ),
                )
                return
            if method == "GET" and segments == ("results", "latest"):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.latest_results(
                        page=_query_int(query_values, "page", 1, 1, 100_000),
                        page_size=_query_int(query_values, "page_size", 15, 1, 100),
                    ),
                )
                return
            if method == "GET" and segments == ("results", "history"):
                _reject_unknown_query(query_values, {"limit"})
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.search_history(
                        limit=_query_int(query_values, "limit", 12, 1, 50),
                    ),
                )
                return
            if (
                method == "GET"
                and len(segments) == 3
                and segments[:2] == ("results", "history")
            ):
                _reject_unknown_query(query_values, {"page", "page_size"})
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.historical_results(
                        segments[2],
                        page=_query_int(query_values, "page", 1, 1, 100_000),
                        page_size=_query_int(query_values, "page_size", 15, 1, 100),
                    ),
                )
                return
            if method == "POST" and segments == ("search",):
                self._json(
                    HTTPStatus.ACCEPTED,
                    gateway._facade.submit_search(self._read_json()),
                )
                return
            if method == "POST" and segments == ("query-image",):
                content = self._read_query_image()
                requested_name = _query_text(
                    query_values,
                    "name",
                    "粘贴图片",
                    maximum=180,
                )
                self._json(
                    HTTPStatus.CREATED,
                    gateway._store_query_image(
                        content,
                        requested_name=requested_name,
                    ),
                )
                return
            if len(segments) == 2 and segments[0] == "search":
                if method == "GET":
                    self._json(
                        HTTPStatus.OK,
                        gateway._facade.search(
                            segments[1],
                            page=_query_int(query_values, "page", 1, 1, 100_000),
                            page_size=_query_int(query_values, "page_size", 15, 1, 100),
                        ),
                    )
                    return
                if method == "DELETE":
                    self._json(
                        HTTPStatus.ACCEPTED,
                        gateway._facade.cancel_search(segments[1]),
                    )
                    return
            if method == "POST" and segments == ("search-feedback",):
                self._json(
                    HTTPStatus.CREATED,
                    gateway._facade.record_search_feedback(self._read_json()),
                )
                return
            if method == "GET" and segments == ("search-feedback",):
                _reject_unknown_query(
                    query_values,
                    {"cursor", "limit", "session_id", "action", "active_only"},
                )
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.search_feedback(
                        cursor=_query_optional_text(
                            query_values, "cursor", "", maximum=1_024
                        )
                        or None,
                        limit=_query_int(query_values, "limit", 50, 1, 200),
                        session_id=_query_optional_text(
                            query_values, "session_id", "", maximum=160
                        )
                        or None,
                        action=_query_optional_text(
                            query_values, "action", "", maximum=64
                        )
                        or None,
                        active_only=_query_bool(query_values, "active_only", True),
                    ),
                )
                return
            if (
                method == "DELETE"
                and len(segments) == 2
                and segments[0] == "search-feedback"
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.revoke_search_feedback(segments[1]),
                )
                return
            if method == "GET" and segments == ("search-learning", "status"):
                _reject_unknown_query(query_values, set())
                self._json(HTTPStatus.OK, gateway._facade.search_learning_status())
                return
            if method == "GET" and segments == ("search-learning", "settings"):
                _reject_unknown_query(query_values, set())
                self._json(HTTPStatus.OK, gateway._facade.search_learning_settings())
                return
            if method == "PUT" and segments == ("search-learning", "settings"):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.update_search_learning_settings(self._read_json()),
                )
                return
            if method == "GET" and segments == ("search-learning", "sessions"):
                _reject_unknown_query(query_values, {"cursor", "limit", "query_type"})
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.search_learning_sessions(
                        cursor=_query_optional_text(
                            query_values, "cursor", "", maximum=1_024
                        )
                        or None,
                        limit=_query_int(query_values, "limit", 50, 1, 200),
                        query_type=_query_optional_text(
                            query_values, "query_type", "", maximum=128
                        )
                        or None,
                    ),
                )
                return
            if method == "POST" and segments == (
                "search-learning",
                "fixed-evaluation",
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.install_search_learning_evaluation(
                        self._read_json()
                    ),
                )
                return
            if method == "POST" and segments == ("search-learning", "train"):
                body = self._read_json()
                if body:
                    raise FacadeError(
                        "invalid_request",
                        "Search-learning training does not accept fields.",
                        details={"unknown_fields": sorted(body)},
                    )
                self._json(
                    HTTPStatus.ACCEPTED,
                    gateway._facade.train_search_learning(),
                )
                return
            if len(segments) == 3 and segments[:2] == ("search-learning", "train"):
                if method == "GET":
                    self._json(
                        HTTPStatus.OK,
                        gateway._facade.search_learning_job(segments[2]),
                    )
                    return
                if method == "DELETE":
                    self._json(
                        HTTPStatus.ACCEPTED,
                        gateway._facade.cancel_search_learning_job(segments[2]),
                    )
                    return
            if method == "POST" and segments == (
                "search-learning",
                "activate",
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.activate_search_learning(self._read_json()),
                )
                return
            if method == "POST" and segments == (
                "search-learning",
                "rollback",
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.rollback_search_learning(self._read_json()),
                )
                return
            if method == "DELETE" and segments == (
                "search-learning",
                "data",
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.clear_search_learning(self._read_json()),
                )
                return
            if method == "GET" and segments == (
                "search-learning",
                "export",
            ):
                _reject_unknown_query(query_values, set())
                self._json(HTTPStatus.OK, gateway._facade.export_search_learning())
                return
            if method == "POST" and segments == (
                "data-migrations",
                "precheck",
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.precheck_data_migration(self._read_json()),
                )
                return
            if method == "POST" and segments == ("data-migrations",):
                self._json(
                    HTTPStatus.ACCEPTED,
                    {
                        "migration": gateway._facade.submit_data_migration(
                            self._read_json()
                        )
                    },
                )
                return
            if method == "GET" and segments == (
                "data-migrations",
                "recovery",
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.data_migration_recovery(),
                )
                return
            if method == "POST" and segments == (
                "data-migrations",
                "recovery",
                "restore",
            ):
                self._json(
                    HTTPStatus.ACCEPTED,
                    {
                        "migration": gateway._facade.submit_data_migration_recovery(
                            self._read_json()
                        )
                    },
                )
                return
            if len(segments) == 2 and segments[0] == "data-migrations":
                if method == "GET":
                    self._json(
                        HTTPStatus.OK,
                        {"migration": gateway._facade.data_migration(segments[1])},
                    )
                    return
                if method == "DELETE":
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {
                            "migration": gateway._facade.cancel_data_migration(
                                segments[1]
                            )
                        },
                    )
                    return
            if method == "GET" and segments == ("jobs",):
                self._json(HTTPStatus.OK, gateway._facade.list_jobs())
                return
            if method == "GET" and segments == ("job-history",):
                _reject_unknown_query(
                    query_values,
                    {
                        "cursor",
                        "limit",
                        "status",
                        "task_type",
                        "library_id",
                        "query",
                    },
                )
                history = gateway._activity_method("job_history")
                self._json(
                    HTTPStatus.OK,
                    history(
                        cursor=_query_optional_text(
                            query_values, "cursor", "", maximum=1_024
                        )
                        or None,
                        limit=_query_int(query_values, "limit", 50, 1, 200),
                        status=_query_optional_text(
                            query_values, "status", "", maximum=128
                        )
                        or None,
                        task_type=_query_optional_text(
                            query_values, "task_type", "", maximum=128
                        )
                        or None,
                        library_id=_query_optional_text(
                            query_values, "library_id", "", maximum=128
                        )
                        or None,
                        query=_query_optional_text(
                            query_values, "query", "", maximum=256
                        )
                        or None,
                    ),
                )
                return
            if method == "GET" and segments == ("activity-logs",):
                _reject_unknown_query(
                    query_values,
                    {
                        "cursor",
                        "limit",
                        "level",
                        "category",
                        "library_id",
                        "job_id",
                        "query",
                    },
                )
                activity_logs = gateway._activity_method("activity_logs")
                self._json(
                    HTTPStatus.OK,
                    activity_logs(
                        cursor=_query_optional_text(
                            query_values, "cursor", "", maximum=1_024
                        )
                        or None,
                        limit=_query_int(query_values, "limit", 50, 1, 200),
                        level=_query_optional_text(
                            query_values, "level", "", maximum=128
                        )
                        or None,
                        category=_query_optional_text(
                            query_values, "category", "", maximum=128
                        )
                        or None,
                        library_id=_query_optional_text(
                            query_values, "library_id", "", maximum=128
                        )
                        or None,
                        job_id=_query_optional_text(
                            query_values, "job_id", "", maximum=128
                        )
                        or None,
                        query=_query_optional_text(
                            query_values, "query", "", maximum=256
                        )
                        or None,
                    ),
                )
                return
            if method == "POST" and segments == ("jobs",):
                self._json(
                    HTTPStatus.ACCEPTED,
                    {"job": gateway._facade.submit_job(self._read_json())},
                )
                return
            if len(segments) == 2 and segments[0] == "jobs":
                if method == "GET":
                    self._json(
                        HTTPStatus.OK,
                        {"job": gateway._facade.job(segments[1])},
                    )
                    return
                if method == "DELETE":
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"job": gateway._facade.cancel_job(segments[1])},
                    )
                    return
            if method == "GET" and segments == ("settings",):
                self._json(HTTPStatus.OK, gateway._facade.settings())
                return
            if method == "GET" and segments == ("lan-access",):
                _reject_unknown_query(query_values, set())
                self._json(HTTPStatus.OK, gateway._facade.lan_access_status())
                return
            if method == "PUT" and segments == ("lan-access",):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.update_lan_access(self._read_json()),
                )
                return
            if method == "POST" and segments == ("lan-access", "start"):
                self._json(HTTPStatus.OK, gateway._facade.start_lan_access())
                return
            if method == "POST" and segments == ("lan-access", "stop"):
                self._json(HTTPStatus.OK, gateway._facade.stop_lan_access())
                return
            if (
                method == "POST"
                and len(segments) == 4
                and segments[:2] == ("lan-access", "pairings")
                and segments[3] == "approve"
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.approve_lan_pairing(segments[2]),
                )
                return
            if (
                method == "POST"
                and len(segments) == 4
                and segments[:2] == ("lan-access", "pairings")
                and segments[3] == "reject"
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.reject_lan_pairing(segments[2]),
                )
                return
            if method == "DELETE" and segments == ("lan-access", "device"):
                self._json(HTTPStatus.OK, gateway._facade.revoke_lan_device())
                return
            if method == "PUT" and segments == ("models",):
                body = self._read_json()
                if "json_text" in body:
                    response = gateway._facade.replace_model_json(body)
                else:
                    response = gateway._facade.assign_models(body)
                self._json(HTTPStatus.OK, response)
                return
            if method == "POST" and segments == ("credentials",):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.save_credentials(self._read_json()),
                )
                return
            if method == "DELETE" and segments == ("credentials",):
                self._json(HTTPStatus.OK, gateway._facade.delete_credentials())
                return
            if method == "POST" and segments == ("libraries",):
                self._json(
                    HTTPStatus.CREATED,
                    gateway._facade.add_library(self._read_json()),
                )
                return
            if (
                method == "GET"
                and len(segments) == 3
                and segments[0] == "libraries"
                and segments[2] == "folders"
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.tag_folders(
                        segments[1],
                        query=_query_optional_text(
                            query_values, "query", "", maximum=256
                        ),
                        offset=_query_int(query_values, "offset", 0, 0, 2**31 - 1),
                        limit=_query_int(query_values, "limit", 200, 1, 500),
                    ),
                )
                return
            if (
                method == "GET"
                and len(segments) == 3
                and segments[0] == "libraries"
                and segments[2] == "folder-images"
            ):
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.folder_images(
                        segments[1],
                        folder_key=_query_required_text(
                            query_values, "folder_key", maximum=32_768
                        ),
                        page=_query_int(query_values, "page", 1, 1, 100_000),
                        page_size=_query_int(query_values, "page_size", 100, 1, 200),
                        include_subfolders=_query_bool(
                            query_values, "include_subfolders", False
                        ),
                    ),
                )
                return
            if (
                method == "POST"
                and len(segments) == 4
                and segments[0] == "libraries"
                and segments[2:4] == ("folder-name-tags", "preview")
            ):
                body = self._read_json()
                unknown = sorted(set(body) - {"selection"})
                if unknown:
                    raise FacadeError(
                        "invalid_request",
                        "文件夹名称标签预览包含不支持的字段。",
                        details={"unknown_fields": unknown},
                    )
                selection = body.get("selection")
                if not isinstance(selection, Mapping):
                    raise FacadeError(
                        "invalid_request",
                        "selection 必须是对象。",
                        status=400,
                    )
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.preview_folder_name_tags(
                        segments[1],
                        selection=selection,
                    ),
                )
                return
            if (
                method == "POST"
                and len(segments) == 4
                and segments[0] == "libraries"
                and segments[2:4] == ("folder-delete", "preview")
            ):
                body = self._read_json()
                unknown = sorted(set(body) - {"folder_key", "include_subfolders"})
                if unknown:
                    raise FacadeError(
                        "invalid_request",
                        "文件夹清理预览包含不支持的字段。",
                        details={"unknown_fields": unknown},
                    )
                folder_key = body.get("folder_key")
                include_subfolders = body.get("include_subfolders", True)
                if not isinstance(folder_key, str) or not folder_key.strip():
                    raise FacadeError(
                        "invalid_request", "folder_key 不能为空。", status=400
                    )
                if not isinstance(include_subfolders, bool):
                    raise FacadeError(
                        "invalid_request",
                        "include_subfolders 必须是布尔值。",
                        status=400,
                    )
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.preview_folder_delete(
                        segments[1],
                        folder_key=folder_key.strip(),
                        include_subfolders=include_subfolders,
                    ),
                )
                return
            if (
                method == "POST"
                and len(segments) == 4
                and segments[0] == "libraries"
                and segments[2:4] == ("folder-delete", "commit")
            ):
                body = self._read_json()
                unknown = sorted(
                    set(body) - {"operation_id", "confirmation_token", "confirm"}
                )
                if unknown:
                    raise FacadeError(
                        "invalid_request",
                        "文件夹清理提交包含不支持的字段。",
                        details={"unknown_fields": unknown},
                    )
                operation_id = body.get("operation_id")
                confirmation_token = body.get("confirmation_token")
                confirm = body.get("confirm", False)
                if not isinstance(operation_id, str) or not operation_id.strip():
                    raise FacadeError(
                        "invalid_request", "operation_id 不能为空。", status=400
                    )
                if (
                    not isinstance(confirmation_token, str)
                    or not confirmation_token.strip()
                ):
                    raise FacadeError(
                        "invalid_request",
                        "confirmation_token 不能为空。",
                        status=400,
                    )
                if not isinstance(confirm, bool):
                    raise FacadeError(
                        "invalid_request", "confirm 必须是布尔值。", status=400
                    )
                self._json(
                    HTTPStatus.ACCEPTED,
                    gateway._facade.commit_folder_delete(
                        segments[1],
                        operation_id=operation_id.strip(),
                        confirmation_token=confirmation_token.strip(),
                        confirm=confirm,
                    ),
                )
                return
            if len(segments) == 2 and segments[0] == "libraries" and method == "PUT":
                self._json(
                    HTTPStatus.OK,
                    gateway._facade.update_library(segments[1], self._read_json()),
                )
                return
            if len(segments) == 3 and segments[0] == "libraries" and method == "PUT":
                body = self._read_json()
                if segments[2] == "enabled":
                    enabled = body.get("enabled")
                    if not isinstance(enabled, bool):
                        raise FacadeError(
                            "invalid_request",
                            "enabled 必须是布尔值。",
                            status=400,
                        )
                    response = gateway._facade.set_library_enabled(segments[1], enabled)
                elif segments[2] == "default":
                    response = gateway._facade.set_default_library(segments[1])
                else:
                    raise FacadeError("route_not_found", "接口不存在。", status=404)
                self._json(HTTPStatus.OK, response)
                return
            if len(segments) == 2 and segments[0] == "image" and method == "GET":
                variant_values = query_values.get("variant", ["thumbnail"])
                raw_variant = variant_values[0]
                if raw_variant not in {"thumbnail", "preview"}:
                    raise FacadeError(
                        "invalid_variant", "图片尺寸参数无效。", status=400
                    )
                # The runtime allow-list above narrows the value for users; this
                # cast communicates the same invariant to static type checkers.
                variant = cast(ImageVariant, raw_variant)
                payload = gateway._facade.image_registry.payload(
                    segments[1],
                    variant,
                )
                if self.headers.get("If-None-Match") == payload.etag:
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self._security_headers(cache="private, max-age=300")
                    self.send_header("ETag", payload.etag)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._bytes(
                    HTTPStatus.OK,
                    payload.content,
                    payload.content_type,
                    cache="private, max-age=300",
                    etag=payload.etag,
                )
                return
            # ------------------------------------------------------------------
            # ARW Selection module routes
            # ------------------------------------------------------------------
            if segments and segments[0] == "raw-selection":
                self._route_raw_selection(method, segments[1:], query_values)
                return
            raise FacadeError("route_not_found", "接口不存在。", status=404)

        def _route_raw_selection(
            self,
            method: str,
            segments: tuple[str, ...],
            query_values: dict[str, list[str]],
        ) -> None:
            svc = gateway._raw_selection_service()

            # GET raw-selection/projects — list projects
            if method == "GET" and segments == ("projects",):
                self._json(HTTPStatus.OK, {"projects": svc.list_projects()})
                return

            # POST raw-selection/projects — create project
            if method == "POST" and segments == ("projects",):
                body = self._read_json()
                name = body.get("name", "")
                if not isinstance(name, str) or not name.strip():
                    raise FacadeError(
                        "invalid_request",
                        "项目名称不能为空。",
                        status=400,
                    )
                self._json(HTTPStatus.CREATED, svc.create_project(name))
                return

            # GET raw-selection/projects/{id} — get project
            if method == "GET" and len(segments) == 2 and segments[0] == "projects":
                project = svc.get_project(segments[1])
                if project is None:
                    raise FacadeError(
                        "not_found", "项目不存在。", status=404
                    )
                self._json(HTTPStatus.OK, project)
                return

            # PATCH raw-selection/projects/{id} — rename project
            if method == "PATCH" and len(segments) == 2 and segments[0] == "projects":
                body = self._read_json()
                name = body.get("name", "")
                if not isinstance(name, str) or not name.strip():
                    raise FacadeError(
                        "invalid_request",
                        "项目名称不能为空。",
                        status=400,
                    )
                project = svc.rename_project(segments[1], name)
                if project is None:
                    raise FacadeError(
                        "not_found", "项目不存在。", status=404
                    )
                self._json(HTTPStatus.OK, project)
                return

            # DELETE raw-selection/projects/{id} — delete project
            if method == "DELETE" and len(segments) == 2 and segments[0] == "projects":
                if not svc.delete_project(segments[1]):
                    raise FacadeError(
                        "not_found", "项目不存在。", status=404
                    )
                self._json(HTTPStatus.OK, {"ok": True})
                return

            # POST raw-selection/projects/{id}/import-folder
            if (
                method == "POST"
                and len(segments) == 3
                and segments[0] == "projects"
                and segments[2] == "import-folder"
            ):
                body = self._read_json()
                folder = body.get("path", "")
                if not isinstance(folder, str) or not folder.strip():
                    raise FacadeError(
                        "invalid_request",
                        "文件夹路径不能为空。",
                        status=400,
                    )
                self._json(
                    HTTPStatus.OK,
                    svc.import_folder(segments[1], folder),
                )
                return

            # POST raw-selection/projects/{id}/import-files
            if (
                method == "POST"
                and len(segments) == 3
                and segments[0] == "projects"
                and segments[2] == "import-files"
            ):
                body = self._read_json()
                paths = body.get("paths", [])
                if not isinstance(paths, list) or not paths:
                    raise FacadeError(
                        "invalid_request",
                        "文件路径列表不能为空。",
                        status=400,
                    )
                self._json(
                    HTTPStatus.OK,
                    svc.import_files(segments[1], paths),
                )
                return

            # GET raw-selection/projects/{id}/members
            if (
                method == "GET"
                and len(segments) == 3
                and segments[0] == "projects"
                and segments[2] == "members"
            ):
                self._json(
                    HTTPStatus.OK,
                    svc.list_members(
                        segments[1],
                        offset=_query_int(query_values, "offset", 0, 0, 1_000_000),
                        limit=_query_int(query_values, "limit", 1000, 1, 10_000),
                        star_mode=_query_optional_text(
                            query_values, "star_mode", "", maximum=20
                        ) or "none",
                        star_value=_query_int(query_values, "star_value", 0, 0, 5),
                        color_labels=_query_optional_text(
                            query_values, "color_labels", "", maximum=200
                        ) or "",
                        filename_contains=_query_optional_text(
                            query_values, "filename", "", maximum=500
                        ) or "",
                        rated_filter=_query_optional_text(
                            query_values, "rated", "", maximum=20
                        ) or "all",
                        sort_field=_query_optional_text(
                            query_values, "sort", "", maximum=30
                        ) or "filename",
                        sort_direction=_query_optional_text(
                            query_values, "dir", "", maximum=10
                        ) or "asc",
                    ),
                )
                return

            # GET raw-selection/members/{id}
            if method == "GET" and len(segments) == 2 and segments[0] == "members":
                member = svc.get_member(segments[1])
                if member is None:
                    raise FacadeError(
                        "not_found", "图片不存在。", status=404
                    )
                self._json(HTTPStatus.OK, member)
                return

            # GET raw-selection/looks — list A7M4 creative looks
            if method == "GET" and segments == ("looks",):
                self._json(HTTPStatus.OK, {"looks": svc.list_creative_looks()})
                return

            # GET raw-selection/members/{id}/thumbnail
            if (
                method == "GET"
                and len(segments) == 3
                and segments[0] == "members"
                and segments[2] == "thumbnail"
            ):
                result = svc.get_thumbnail_bytes(segments[1])
                if result.error:
                    raise FacadeError(
                        "decode_failed",
                        result.error,
                        status=500,
                    )
                self._bytes(
                    HTTPStatus.OK,
                    result.data,
                    result.content_type,
                    cache="private, max-age=300",
                )
                return

            # GET raw-selection/members/{id}/preview
            if (
                method == "GET"
                and len(segments) == 3
                and segments[0] == "members"
                and segments[2] == "preview"
            ):
                result = svc.get_preview_bytes(
                    segments[1],
                    display_width=_query_int(query_values, "dw", 0, 0, 10_000),
                    display_height=_query_int(query_values, "dh", 0, 0, 10_000),
                    look=_query_optional_text(
                        query_values, "look", "as_shot", maximum=20
                    ) or "as_shot",
                )
                if result.error:
                    raise FacadeError(
                        "decode_failed",
                        result.error,
                        status=500,
                    )
                self._bytes(
                    HTTPStatus.OK,
                    result.data,
                    result.content_type,
                    cache="private, max-age=300",
                )
                return

            # PATCH raw-selection/members/{id}/rating
            if (
                method == "PATCH"
                and len(segments) == 3
                and segments[0] == "members"
                and segments[2] == "rating"
            ):
                body = self._read_json()
                star = body.get("star_rating")
                color = body.get("color_label", "none")
                if not isinstance(star, int) or not (0 <= star <= 5):
                    raise FacadeError(
                        "invalid_request",
                        "星级必须为 0-5 的整数。",
                        status=400,
                    )
                if not svc.update_rating(segments[1], star, color):
                    raise FacadeError(
                        "not_found", "图片不存在。", status=404
                    )
                self._json(HTTPStatus.OK, {"ok": True})
                return

            # PATCH raw-selection/members/{id}/creative-look
            if (
                method == "PATCH"
                and len(segments) == 3
                and segments[0] == "members"
                and segments[2] == "creative-look"
            ):
                body = self._read_json()
                look = body.get("creative_look", "as_shot")
                if not svc.update_creative_look(segments[1], look):
                    raise FacadeError(
                        "not_found", "图片不存在。", status=404
                    )
                self._json(HTTPStatus.OK, {"ok": True})
                return

            # GET raw-selection/projects/{id}/workspace
            if (
                method == "GET"
                and len(segments) == 3
                and segments[0] == "projects"
                and segments[2] == "workspace"
            ):
                ws = svc.get_workspace_state(segments[1])
                self._json(HTTPStatus.OK, ws or {})
                return

            # PUT raw-selection/projects/{id}/workspace
            if (
                method == "PUT"
                and len(segments) == 3
                and segments[0] == "projects"
                and segments[2] == "workspace"
            ):
                body = self._read_json()
                svc.save_workspace_state(
                    segments[1],
                    last_member_id=body.get("last_member_id"),
                    filter_star_mode=body.get("filter_star_mode", "none"),
                    filter_star_value=body.get("filter_star_value", 0),
                    filter_color_labels=body.get("filter_color_labels", ""),
                    filter_filename=body.get("filter_filename", ""),
                    filter_rated=body.get("filter_rated", "all"),
                    sort_field=body.get("sort_field", "filename"),
                    sort_direction=body.get("sort_direction", "asc"),
                    filmstrip_scroll=body.get("filmstrip_scroll", 0.0),
                )
                self._json(HTTPStatus.OK, {"ok": True})
                return

            # POST raw-selection/projects/{id}/export
            if (
                method == "POST"
                and len(segments) == 3
                and segments[0] == "projects"
                and segments[2] == "export"
            ):
                body = self._read_json()
                member_ids = body.get("member_ids", [])
                destination = body.get("destination", "")
                if not isinstance(member_ids, list) or not member_ids:
                    raise FacadeError(
                        "invalid_request",
                        "请选择要导出的图片。",
                        status=400,
                    )
                if not isinstance(destination, str) or not destination.strip():
                    raise FacadeError(
                        "invalid_request",
                        "请选择导出目标目录。",
                        status=400,
                    )
                self._json(
                    HTTPStatus.OK,
                    svc.export_files(member_ids, destination),
                )
                return

            # POST raw-selection/members/remove
            if method == "POST" and segments == ("members", "remove"):
                body = self._read_json()
                member_ids = body.get("member_ids", [])
                if not isinstance(member_ids, list) or not member_ids:
                    raise FacadeError(
                        "invalid_request",
                        "请选择要移出的图片。",
                        status=400,
                    )
                removed = svc.remove_members(member_ids)
                self._json(HTTPStatus.OK, {"removed": removed})
                return

            # POST raw-selection/members/delete-permanent
            if method == "POST" and segments == ("members", "delete-permanent"):
                body = self._read_json()
                member_ids = body.get("member_ids", [])
                confirmed = body.get("confirmed", False)
                if not isinstance(member_ids, list) or not member_ids:
                    raise FacadeError(
                        "invalid_request",
                        "请选择要删除的图片。",
                        status=400,
                    )
                if not confirmed:
                    raise FacadeError(
                        "invalid_request",
                        "永久删除需要明确确认。",
                        status=400,
                    )
                self._json(
                    HTTPStatus.OK,
                    svc.permanent_delete(member_ids, confirmed=True),
                )
                return

            # POST raw-selection/projects/{id}/clear-cache
            if (
                method == "POST"
                and len(segments) == 3
                and segments[0] == "projects"
                and segments[2] == "clear-cache"
            ):
                svc.clear_project_cache(segments[1])
                self._json(HTTPStatus.OK, {"ok": True})
                return

            raise FacadeError("route_not_found", "接口不存在。", status=404)

        def _validate_authority(self) -> None:
            address = gateway.address
            expected_host = f"{address.host}:{address.port}"
            if self.headers.get("Host", "") != expected_host:
                raise FacadeError("invalid_host", "请求主机无效。", status=403)
            origin = self.headers.get("Origin")
            if origin is not None and origin.rstrip("/") != address.origin:
                raise FacadeError("invalid_origin", "请求来源无效。", status=403)

        def _relative_path(self, path: str) -> str:
            prefix = gateway.address.base_path
            if path == prefix[:-1]:
                return ""
            if not path.startswith(prefix):
                raise FacadeError("route_not_found", "页面不存在。", status=404)
            return path[len(prefix) :]

        def _read_json(self, *, maximum: int = _MAX_JSON_BYTES) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise FacadeError(
                    "length_required", "请求必须包含 Content-Length。", status=411
                )
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise FacadeError(
                    "invalid_length", "Content-Length 无效。", status=400
                ) from exc
            if length <= 0 or length > maximum:
                raise FacadeError("invalid_length", "请求正文为空或过大。", status=413)
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.strip().casefold() != "application/json":
                raise FacadeError(
                    "unsupported_media_type",
                    "请求正文必须是 application/json。",
                    status=415,
                )
            body = self.rfile.read(length)
            if len(body) != length:
                raise FacadeError("incomplete_body", "请求正文不完整。", status=400)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FacadeError(
                    "invalid_json", "请求 JSON 无效。", status=400
                ) from exc
            if not isinstance(payload, dict):
                raise FacadeError("invalid_json", "请求 JSON 必须是对象。", status=400)
            return payload

        def _read_query_image(self) -> bytes:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise FacadeError(
                    "length_required", "请求必须包含 Content-Length。", status=411
                )
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise FacadeError(
                    "invalid_length", "Content-Length 无效。", status=400
                ) from exc
            if length <= 0 or length > _MAX_QUERY_IMAGE_BYTES:
                raise FacadeError(
                    "invalid_query_image_size",
                    "图片为空或超过 128 MiB。",
                    status=413,
                )
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            normalized_type = content_type.strip().casefold()
            if (
                normalized_type != "application/octet-stream"
                and not normalized_type.startswith("image/")
            ):
                raise FacadeError(
                    "unsupported_media_type",
                    "查询图片请求必须使用 image/* 内容类型。",
                    status=415,
                )
            body = self.rfile.read(length)
            if len(body) != length:
                raise FacadeError("incomplete_body", "请求正文不完整。", status=400)
            return body

        def _send_asset(self, relative: str) -> None:
            content, content_type = gateway._asset(relative)
            self._bytes(
                HTTPStatus.OK,
                content,
                content_type,
                cache="no-cache",
            )

        def _json_error(self, status: int, code: str, message: str) -> None:
            self._json(status, {"error": {"code": code, "message": message}})

        def _json(self, status: int, payload: Mapping[str, Any]) -> None:
            content = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self._bytes(
                status,
                content,
                "application/json; charset=utf-8",
                cache="no-store",
            )

        def _bytes(
            self,
            status: int,
            content: bytes,
            content_type: str,
            *,
            cache: str,
            etag: str | None = None,
        ) -> None:
            self.send_response(status)
            self._security_headers(cache=cache)
            self.send_header("Content-Type", content_type)
            if etag is not None:
                self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def _security_headers(self, *, cache: str) -> None:
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; form-action 'self'; "
                "frame-ancestors 'none'; object-src 'none'; "
                "img-src 'self' data: blob:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'",
            )

    return Handler


def _reject_unknown_query(
    query: Mapping[str, list[str]],
    allowed: set[str],
) -> None:
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise FacadeError(
            "invalid_query",
            "查询参数包含不支持的字段。",
            details={"unknown_fields": unknown},
        )


def _query_int(
    query: Mapping[str, list[str]],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    values = query.get(name)
    if not values:
        return default
    if len(values) != 1:
        raise FacadeError("invalid_query", f"{name} 只能出现一次。")
    try:
        value = int(values[0])
    except ValueError as exc:
        raise FacadeError("invalid_query", f"{name} 必须是整数。") from exc
    if not minimum <= value <= maximum:
        raise FacadeError(
            "invalid_query", f"{name} 必须在 {minimum} 到 {maximum} 之间。"
        )
    return value


def _query_text(
    query: Mapping[str, list[str]],
    name: str,
    default: str,
    *,
    maximum: int,
) -> str:
    values = query.get(name)
    if not values:
        return default
    if len(values) != 1:
        raise FacadeError("invalid_query", f"{name} 只能出现一次。")
    value = values[0].strip()
    invalid_control = any(ord(character) < 32 for character in value)
    if not value or len(value) > maximum or invalid_control:
        raise FacadeError("invalid_query", f"{name} 无效。")
    return value


def _query_optional_text(
    query: Mapping[str, list[str]],
    name: str,
    default: str,
    *,
    maximum: int,
) -> str:
    values = query.get(name)
    if not values:
        return default
    if len(values) != 1:
        raise FacadeError("invalid_query", f"{name} may only appear once.")
    value = values[0].strip()
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise FacadeError("invalid_query", f"{name} is invalid.")
    return value


def _query_required_text(
    query: Mapping[str, list[str]],
    name: str,
    *,
    maximum: int,
) -> str:
    values = query.get(name)
    if not values:
        raise FacadeError("invalid_query", f"{name} is required.")
    return _query_text(query, name, "", maximum=maximum)


def _recommendation_text(payload: Mapping[str, Any], name: str, *, maximum: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise FacadeError("invalid_request", f"{name} 无效。")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(character in normalized for character in "\r\n\0/")
    ):
        raise FacadeError("invalid_request", f"{name} 无效。")
    return normalized


def _recommendation_metadata(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise FacadeError("invalid_request", "推荐操作元数据无效。")
    return dict(value)


def _query_bool(
    query: Mapping[str, list[str]],
    name: str,
    default: bool,
) -> bool:
    values = query.get(name)
    if not values:
        return default
    if len(values) != 1:
        raise FacadeError("invalid_query", f"{name} may only appear once.")
    value = values[0].strip().casefold()
    if value == "true":
        return True
    if value == "false":
        return False
    raise FacadeError("invalid_query", f"{name} must be true or false.")


def _safe_query_image_name(requested_name: str, suffix: str) -> str:
    leaf = Path(requested_name).name.strip().strip(". ")
    stem = Path(leaf).stem.strip().strip(". ") if leaf else ""
    invalid_name_characters = frozenset('<>:"/\\|?*')
    safe_stem = "".join(
        character
        for character in stem
        if ord(character) >= 32 and character not in invalid_name_characters
    ).strip()
    if not safe_stem:
        safe_stem = "粘贴图片"
    return f"{safe_stem[:140]}{suffix}"


__all__ = ["GatewayAddress", "GatewayError", "GatewayServer"]
