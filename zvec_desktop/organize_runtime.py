"""Runtime-controller adapter for the embeddable smart-organize panel."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from .backend_api import JsonObject
from .library_tasks import (
    AutoTagPendingRequest,
    AutoTagReviewRequest,
    AutoTagUndoRequest,
    IdentityConfirmationRequest,
    LibraryRequest,
    LowRiskBatchReviewRequest,
    TagAliasDeleteRequest,
    TagAliasListRequest,
    TagAliasUpsertRequest,
)

_SUCCESS_STATUSES = frozenset({"succeeded", "partial", "needs_attention"})


class OrganizeRuntimeError(RuntimeError):
    """A backend job could not produce a smart-organize result payload."""


@dataclass(eq=False)
class OrganizeJobError(OrganizeRuntimeError):
    command: str
    status: str
    job_id: str | None
    backend_error: Any = None

    def __post_init__(self) -> None:
        detail = ""
        if isinstance(self.backend_error, Mapping):
            message = self.backend_error.get("message")
            code = self.backend_error.get("code")
            if isinstance(message, str) and message:
                detail = message
            elif isinstance(code, str) and code:
                detail = code
        suffix = f"：{detail}" if detail else ""
        OrganizeRuntimeError.__init__(
            self,
            f"智能整理任务 {self.command} 以状态 {self.status} 结束{suffix}",
        )


class AsyncJobController(Protocol):
    def submit_job(
        self,
        command: str,
        params: Mapping[str, Any] | None = None,
        *,
        wait_for_completion: bool = True,
        poll_interval: float | None = None,
    ) -> Future[JsonObject]: ...


RequestT = TypeVar("RequestT", bound=LibraryRequest)


class RuntimeOrganizeOperations:
    """Translate validated organize requests into asynchronous result futures."""

    def __init__(self, controller: AsyncJobController) -> None:
        self._controller = controller

    def load_pending(self, request: AutoTagPendingRequest) -> Future[object]:
        return self._submit(request)

    def submit_review(self, request: AutoTagReviewRequest) -> Future[object]:
        return self._submit(request)

    def submit_low_risk_batch(
        self, request: LowRiskBatchReviewRequest
    ) -> Future[object]:
        return self._submit(request)

    def submit_identity_confirmation(
        self, request: IdentityConfirmationRequest
    ) -> Future[object]:
        return self._submit(request)

    def undo_low_risk_batch(self, request: AutoTagUndoRequest) -> Future[object]:
        return self._submit(request)

    def list_aliases(self, request: TagAliasListRequest) -> Future[object]:
        return self._submit(request)

    def upsert_alias(self, request: TagAliasUpsertRequest) -> Future[object]:
        return self._submit(request)

    def delete_alias(self, request: TagAliasDeleteRequest) -> Future[object]:
        return self._submit(request)

    def _submit(self, request: RequestT) -> Future[object]:
        if not hasattr(request, "command") or not hasattr(request, "to_params"):
            raise TypeError("request must implement the library request contract")
        command = request.command
        source = self._controller.submit_job(
            command,
            request.to_params(),
            wait_for_completion=True,
        )
        mapped: Future[object] = Future()

        def completed(future: Future[JsonObject]) -> None:
            if mapped.cancelled():
                return
            try:
                job = future.result()
                value = _job_result(command, job)
            except BaseException as exc:
                mapped.set_exception(exc)
            else:
                mapped.set_result(value)

        source.add_done_callback(completed)
        return mapped


def _job_result(command: str, job: Mapping[str, Any]) -> object:
    status_value = job.get("status")
    if not isinstance(status_value, str) or not status_value.strip():
        raise OrganizeRuntimeError(f"任务 {command} 没有返回有效状态。")
    status = status_value.strip().lower()
    job_id_value = job.get("id")
    job_id = job_id_value if isinstance(job_id_value, str) else None
    if status not in _SUCCESS_STATUSES:
        raise OrganizeJobError(command, status, job_id, copy.deepcopy(job.get("error")))
    result = job.get("result")
    if not isinstance(result, dict):
        raise OrganizeRuntimeError(f"任务 {command} 已完成，但没有返回结果对象。")
    return cast(object, copy.deepcopy(result))


__all__ = [
    "AsyncJobController",
    "OrganizeJobError",
    "OrganizeRuntimeError",
    "RuntimeOrganizeOperations",
]
