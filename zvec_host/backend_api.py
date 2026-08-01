"""Small, dependency-free client for the persistent Zvec backend.

The local application host calls this module from background threads. Each request
uses a short-lived connection, so a client instance can safely be shared by
those workers without serialising all operations behind one HTTP connection.
"""

from __future__ import annotations

import http.client
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

JsonObject: TypeAlias = dict[str, Any]

_JSON_CONTENT_TYPE = "application/json"
_DEFAULT_TIMEOUT_SECONDS = 30.0


class BackendApiError(RuntimeError):
    """Base class for failures produced while talking to the backend."""


@dataclass(eq=False)
class BackendTimeoutError(BackendApiError):
    """The backend did not complete a request before the configured timeout."""

    method: str
    url: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        BackendApiError.__init__(
            self,
            f"Backend request {self.method} {self.url} timed out after "
            f"{self.timeout_seconds:g} seconds.",
        )


@dataclass(eq=False)
class BackendTransportError(BackendApiError):
    """The request could not be sent or its HTTP response could not be read."""

    method: str
    url: str
    reason: str

    def __post_init__(self) -> None:
        BackendApiError.__init__(
            self,
            f"Backend request {self.method} {self.url} failed: {self.reason}",
        )


@dataclass(eq=False)
class BackendHttpError(BackendApiError):
    """A complete HTTP response reported a non-success status."""

    method: str
    url: str
    status_code: int
    reason: str
    body: str
    code: str | None = None
    backend_message: str | None = None
    details: Any = None
    payload: JsonObject | None = None

    def __post_init__(self) -> None:
        detail = self.backend_message or self.body.strip()
        message = (
            f"Backend request {self.method} {self.url} failed with HTTP "
            f"{self.status_code} ({self.reason or 'Unknown'})."
        )
        if detail:
            message = f"{message} {detail}"
        BackendApiError.__init__(self, message)

    @property
    def status(self) -> int:
        """Compatibility alias useful to generic HTTP error handlers."""

        return self.status_code


@dataclass(eq=False)
class BackendProtocolError(BackendApiError):
    """The backend returned HTTP successfully but violated the JSON contract."""

    method: str
    url: str
    message: str
    status_code: int | None = None
    response_body: str = ""

    def __post_init__(self) -> None:
        BackendApiError.__init__(
            self,
            f"Invalid backend response for {self.method} {self.url}: {self.message}",
        )


class BackendApiClient:
    """Synchronous client for the authenticated persistent-backend API.

    The methods intentionally return JSON dictionaries.  Job results evolve as
    commands gain fields, and retaining the complete payload avoids coupling the
    Python UI to a large, brittle hierarchy of response data classes.
    """

    def __init__(
        self,
        base_url: str,
        session_token: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty absolute URL")
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("base_url must not contain a query string or fragment")
        if not isinstance(session_token, str) or not session_token.strip():
            raise ValueError("session_token must be non-empty")
        if "\r" in session_token or "\n" in session_token:
            raise ValueError("session_token must not contain line breaks")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a positive finite number")
        timeout_value = float(timeout)
        if timeout_value <= 0 or not math.isfinite(timeout_value):
            raise ValueError("timeout must be a positive finite number")

        base_path = parsed.path.rstrip("/") + "/"
        self._base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", ""))
        self._session_token = session_token.strip()
        self._timeout = timeout_value

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def timeout(self) -> float:
        return self._timeout

    def get_health(self) -> JsonObject:
        """Return health even while startup legitimately reports HTTP 503."""

        return self._request_json("GET", "health", expected_statuses={200, 503})

    def health(self) -> JsonObject:
        """Short alias for :meth:`get_health`."""

        return self.get_health()

    def shutdown_if_idle(self) -> JsonObject:
        """Ask the backend to stop, but only when no job is active.

        A busy backend responds with HTTP 409 and is surfaced as
        :class:`BackendHttpError`. Keeping that distinction lets process hosts
        avoid terminating work merely because a desktop window is closing.
        """

        payload = self._request_json(
            "POST",
            "v1/control/shutdown",
            json_body={"if_idle": True},
            expected_statuses={202},
        )
        if payload.get("accepted") is not True:
            raise self._protocol_error(
                "POST",
                "v1/control/shutdown",
                "the backend did not accept the idle shutdown request",
                status_code=202,
                payload=payload,
            )
        return payload

    def configure_credentials(
        self,
        dash_scope_api_key: str,
        api_url: str | None = None,
    ) -> JsonObject:
        if not isinstance(dash_scope_api_key, str) or not dash_scope_api_key.strip():
            raise ValueError("dash_scope_api_key must be non-empty")
        if api_url is not None and not isinstance(api_url, str):
            raise ValueError("api_url must be a string or None")
        payload = self._request_json(
            "PUT",
            "v1/session/credentials",
            json_body={
                "dashscope_api_key": dash_scope_api_key.strip(),
                "api_url": api_url.strip() if api_url and api_url.strip() else None,
            },
            expected_statuses={200},
        )
        if payload.get("credentials_configured") is not True:
            raise self._protocol_error(
                "PUT",
                "v1/session/credentials",
                "the backend did not confirm credential configuration",
                status_code=200,
                payload=payload,
            )
        return payload

    def create_recommendations(
        self,
        viewer_id: str,
        request_id: str,
    ) -> JsonObject:
        return self._request_json(
            "POST",
            "v1/recommendations",
            json_body={
                "viewer_id": self._validate_recommendation_id(viewer_id, "viewer_id"),
                "request_id": self._validate_recommendation_id(
                    request_id,
                    "request_id",
                ),
            },
            expected_statuses={200},
        )

    def mark_recommendations_shown(
        self,
        viewer_id: str,
        batch_id: str,
        event_id: str,
    ) -> JsonObject:
        normalized_batch = self._validate_recommendation_id(batch_id, "batch_id")
        return self._request_json(
            "POST",
            f"v1/recommendations/{quote(normalized_batch, safe='')}/shown",
            json_body={
                "viewer_id": self._validate_recommendation_id(viewer_id, "viewer_id"),
                "event_id": self._validate_recommendation_id(event_id, "event_id"),
            },
            expected_statuses={200},
        )

    def record_recommendation_action(
        self,
        viewer_id: str,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
        metadata: Mapping[str, str] | None = None,
    ) -> JsonObject:
        if action not in {"open", "like", "export", "dislike"}:
            raise ValueError("action is invalid")
        if metadata is not None and (
            not isinstance(metadata, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in metadata.items()
            )
        ):
            raise ValueError("metadata must be a string mapping or None")
        normalized_batch = self._validate_recommendation_id(batch_id, "batch_id")
        payload: JsonObject = {
            "viewer_id": self._validate_recommendation_id(viewer_id, "viewer_id"),
            "event_id": self._validate_recommendation_id(event_id, "event_id"),
            "item_id": self._validate_recommendation_id(item_id, "item_id"),
            "action": action,
        }
        if metadata is not None:
            payload["metadata"] = dict(metadata)
        return self._request_json(
            "POST",
            f"v1/recommendations/{quote(normalized_batch, safe='')}/actions",
            json_body=payload,
            expected_statuses={200},
        )

    def submit_job(
        self,
        command: str,
        params: Mapping[str, Any] | None = None,
    ) -> JsonObject:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be non-empty")
        if params is not None and not isinstance(params, Mapping):
            raise ValueError("params must be a mapping or None")
        payload = self._request_json(
            "POST",
            "v1/jobs",
            json_body={"command": command.strip(), "params": dict(params or {})},
            expected_statuses={202},
        )
        return self._extract_job("POST", "v1/jobs", payload)

    def get_job(self, job_id: str) -> JsonObject:
        normalized_id = self._validate_job_id(job_id)
        relative_path = f"v1/jobs/{quote(normalized_id, safe='')}"
        payload = self._request_json("GET", relative_path, expected_statuses={200})
        return self._extract_job(
            "GET", relative_path, payload, expected_job_id=normalized_id
        )

    def list_jobs(
        self,
        *,
        active: bool | None = None,
        limit: int = 100,
    ) -> JsonObject:
        if active is not None and not isinstance(active, bool):
            raise ValueError("active must be a boolean or None")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise ValueError("limit must be between 1 and 500")

        query: list[tuple[str, str | int]] = []
        if active is not None:
            query.append(("active", "true" if active else "false"))
        query.append(("limit", limit))
        relative_path = f"v1/jobs?{urlencode(query)}"
        payload = self._request_json("GET", relative_path, expected_statuses={200})
        jobs = payload.get("jobs")
        count = payload.get("count")
        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise self._protocol_error(
                "GET",
                relative_path,
                "the job list must contain an array of job objects",
                status_code=200,
                payload=payload,
            )
        if isinstance(count, bool) or not isinstance(count, int) or count != len(jobs):
            raise self._protocol_error(
                "GET",
                relative_path,
                "the job-list count must equal the number of returned jobs",
                status_code=200,
                payload=payload,
            )
        return payload

    def get_jobs(
        self,
        *,
        active: bool | None = None,
        limit: int = 100,
    ) -> JsonObject:
        """Compatibility alias matching the former desktop client vocabulary."""

        return self.list_jobs(active=active, limit=limit)

    def delete_job(self, job_id: str) -> JsonObject:
        normalized_id = self._validate_job_id(job_id)
        relative_path = f"v1/jobs/{quote(normalized_id, safe='')}"
        payload = self._request_json("DELETE", relative_path, expected_statuses={202})
        return self._extract_job(
            "DELETE", relative_path, payload, expected_job_id=normalized_id
        )

    def cancel_job(self, job_id: str) -> JsonObject:
        """Semantic alias for the backend's DELETE-based cancellation endpoint."""

        return self.delete_job(job_id)

    @staticmethod
    def _validate_job_id(job_id: str) -> str:
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be non-empty")
        return job_id.strip()

    @staticmethod
    def _validate_recommendation_id(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-empty")
        normalized = value.strip()
        if len(normalized) > 256 or any(
            ord(character) < 32 for character in normalized
        ):
            raise ValueError(f"{name} is invalid")
        return normalized

    def _extract_job(
        self,
        method: str,
        relative_path: str,
        payload: JsonObject,
        *,
        expected_job_id: str | None = None,
    ) -> JsonObject:
        job = payload.get("job")
        if not isinstance(job, dict):
            raise self._protocol_error(
                method,
                relative_path,
                "the response did not contain a job object",
                payload=payload,
            )
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise self._protocol_error(
                method,
                relative_path,
                "the job object did not contain a non-empty ID",
                payload=payload,
            )
        if expected_job_id is not None and job_id != expected_job_id:
            raise self._protocol_error(
                method,
                relative_path,
                f"the backend returned job {job_id!r} instead of {expected_job_id!r}",
                payload=payload,
            )
        return job

    def _request_json(
        self,
        method: str,
        relative_path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        expected_statuses: set[int],
    ) -> JsonObject:
        url = urljoin(self._base_url, relative_path)
        parsed = urlsplit(url)
        request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        headers = {
            "Accept": _JSON_CONTENT_TYPE,
            "Authorization": f"Bearer {self._session_token}",
        }
        body: bytes | None = None
        if json_body is not None:
            try:
                # Serialising first is intentional: the backend rejects chunked
                # request bodies and requires an exact Content-Length.
                body = json.dumps(
                    dict(json_body),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"request body is not valid JSON: {exc}") from exc
            headers["Content-Type"] = f"{_JSON_CONTENT_TYPE}; charset=utf-8"
            headers["Content-Length"] = str(len(body))

        connection_class = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        hostname = parsed.hostname
        if hostname is None:
            raise BackendProtocolError(
                method,
                url,
                "the resolved backend URL does not contain a host",
            )
        connection = connection_class(
            hostname,
            parsed.port,
            timeout=self._timeout,
        )
        try:
            connection.request(method, request_target, body=body, headers=headers)
            response = connection.getresponse()
            raw_body = response.read()
            status_code = response.status
            reason = response.reason or ""
            content_type = response.getheader("Content-Type", "")
        except TimeoutError as exc:
            raise BackendTimeoutError(method, url, self._timeout) from exc
        except (OSError, http.client.HTTPException) as exc:
            reason_text = str(exc) or type(exc).__name__
            raise BackendTransportError(method, url, reason_text) from exc
        finally:
            connection.close()

        if status_code not in expected_statuses:
            if not 200 <= status_code < 300:
                raise self._http_error(method, url, status_code, reason, raw_body)
            raise BackendProtocolError(
                method,
                url,
                f"unexpected HTTP status {status_code}",
                status_code=status_code,
                response_body=raw_body.decode("utf-8", errors="replace"),
            )
        if content_type.split(";", 1)[0].strip().lower() != _JSON_CONTENT_TYPE:
            raise BackendProtocolError(
                method,
                url,
                "expected application/json, received "
                f"{content_type or 'no Content-Type'}",
                status_code=status_code,
                response_body=raw_body.decode("utf-8", errors="replace"),
            )
        return self._decode_json_object(method, url, status_code, raw_body)

    @staticmethod
    def _decode_json_object(
        method: str,
        url: str,
        status_code: int,
        raw_body: bytes,
    ) -> JsonObject:
        try:
            response_body = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BackendProtocolError(
                method,
                url,
                "the response body is not valid UTF-8",
                status_code=status_code,
                response_body=raw_body.decode("utf-8", errors="replace"),
            ) from exc
        try:
            payload = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise BackendProtocolError(
                method,
                url,
                f"the response body is not valid JSON ({exc.msg})",
                status_code=status_code,
                response_body=response_body,
            ) from exc
        if not isinstance(payload, dict):
            raise BackendProtocolError(
                method,
                url,
                "the JSON response must be an object",
                status_code=status_code,
                response_body=response_body,
            )
        return payload

    @staticmethod
    def _http_error(
        method: str,
        url: str,
        status_code: int,
        reason: str,
        raw_body: bytes,
    ) -> BackendHttpError:
        body = raw_body.decode("utf-8", errors="replace")
        payload: JsonObject | None = None
        code: str | None = None
        backend_message: str | None = None
        details: Any = None
        try:
            candidate = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            candidate = None
        if isinstance(candidate, dict):
            payload = candidate
            error = candidate.get("error")
            if isinstance(error, dict):
                if isinstance(error.get("code"), str):
                    code = error["code"]
                if isinstance(error.get("message"), str):
                    backend_message = error["message"]
                details = error.get("details")
        return BackendHttpError(
            method=method,
            url=url,
            status_code=status_code,
            reason=reason,
            body=body,
            code=code,
            backend_message=backend_message,
            details=details,
            payload=payload,
        )

    def _protocol_error(
        self,
        method: str,
        relative_path: str,
        message: str,
        *,
        status_code: int | None = None,
        payload: JsonObject,
    ) -> BackendProtocolError:
        return BackendProtocolError(
            method,
            urljoin(self._base_url, relative_path),
            message,
            status_code=status_code,
            response_body=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )


__all__ = [
    "BackendApiClient",
    "BackendApiError",
    "BackendHttpError",
    "BackendProtocolError",
    "BackendTimeoutError",
    "BackendTransportError",
    "JsonObject",
]
