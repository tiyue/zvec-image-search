"""Offline training and version control for local search-learning feedback.

Training creates a candidate artifact in the background.  It never changes
online ranking weights.  A candidate can be activated only after an injected
fixed evaluation hook proves every quality gate; otherwise it remains visible
as a pending or failed candidate and the current ranker is untouched.
"""

from __future__ import annotations

import json
import math
import os
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Final

from .learning_ranker import RANKING_MODEL_SCHEMA_VERSION, RankingModel
from .search_features import FEATURE_SCHEMA_VERSION, NUMERIC_FEATURE_NAMES
from .search_learning_evaluator import FIXED_EVALUATION_FILENAME
from .search_learning_store import (
    SearchLearningStore,
    SearchLearningValidationError,
)

JsonObject = dict[str, Any]
EvaluationHook = Callable[[Mapping[str, Any]], Mapping[str, Any]]

MIN_QUERY_SESSIONS: Final = 100
MIN_EXPLICIT_SAMPLES: Final = 300
MAX_MODEL_FILE_BYTES: Final = 256 * 1024


class SearchLearningServiceError(RuntimeError):
    """The optional local learning workflow could not complete safely."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvaluationGateResult:
    status: str
    reasons: tuple[str, ...]
    metrics: Mapping[str, Any]

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def to_dict(self) -> JsonObject:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "metrics": dict(self.metrics),
        }


class FixedEvaluationGate:
    """Validate one candidate against the non-negotiable fixed quality set."""

    def evaluate(self, report: Mapping[str, Any]) -> EvaluationGateResult:
        if not isinstance(report, Mapping):
            return EvaluationGateResult("failed", ("invalid_report",), {})
        if report.get("fixed_evaluation_set") is not True:
            return EvaluationGateResult(
                "failed", ("fixed_evaluation_set_required",), dict(report)
            )
        evaluation_set_id = report.get("evaluation_set_id")
        if not isinstance(evaluation_set_id, str) or not evaluation_set_id.strip():
            return EvaluationGateResult(
                "failed", ("evaluation_set_id_required",), dict(report)
            )
        current = report.get("current")
        candidate = report.get("candidate")
        if not isinstance(current, Mapping) or not isinstance(candidate, Mapping):
            return EvaluationGateResult(
                "failed", ("current_and_candidate_metrics_required",), dict(report)
            )
        reasons: list[str] = []
        current_precision = _metric(current, "precision_at_15")
        candidate_precision = _metric(candidate, "precision_at_15")
        current_recall = _metric(current, "recall_at_15")
        candidate_recall = _metric(candidate, "recall_at_15")
        no_answer_rate = _metric(candidate, "no_answer_false_positive_rate")
        current_bias = _metric(current, "cross_collection_bias")
        candidate_bias = _metric(candidate, "cross_collection_bias")
        current_latency = _metric(current, "p95_latency_ms")
        candidate_latency = _metric(candidate, "p95_latency_ms")
        external_calls = _metric(candidate, "external_api_calls")
        if candidate_precision < current_precision:
            reasons.append("precision_at_15_regressed")
        if candidate_recall < current_recall - 0.03:
            reasons.append("recall_at_15_regressed_more_than_3pp")
        if no_answer_rate > 0.10:
            reasons.append("no_answer_false_positive_rate_above_10_percent")
        if candidate_bias > current_bias:
            reasons.append("cross_collection_bias_regressed")
        if current_latency <= 0.0:
            if candidate_latency > 0.0:
                reasons.append("invalid_latency_baseline")
        elif candidate_latency > current_latency * 1.15:
            reasons.append("p95_latency_increased_more_than_15_percent")
        if external_calls != 0.0:
            reasons.append("external_api_calls_not_zero")
        return EvaluationGateResult(
            "failed" if reasons else "passed",
            tuple(reasons),
            _json_safe_mapping(report),
        )


class SearchLearningService:
    """Coordinate feedback, offline candidate training, activation, and rollback."""

    def __init__(
        self,
        config_home: str | Path,
        *,
        store: SearchLearningStore | None = None,
        evaluator: EvaluationHook | None = None,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._config_home = Path(config_home).expanduser().resolve()
        self._directory = self._config_home / "search-learning"
        self._store = store or SearchLearningStore(self._config_home)
        self._owns_store = store is None
        self._evaluator = evaluator
        self._gate = FixedEvaluationGate()
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="zvec-search-learning"
        )
        self._owns_executor = executor is None
        self._lock = threading.RLock()
        self._futures: dict[str, Future[None]] = {}
        self._cancel_events: dict[str, threading.Event] = {}

    @property
    def store(self) -> SearchLearningStore:
        return self._store

    def status(self) -> JsonObject:
        payload = self._store.status()
        payload["model_versions"] = self._store.list_model_versions(limit=20)
        payload["training_running"] = any(
            not future.done() for future in tuple(self._futures.values())
        )
        payload["online_weight_updates"] = False
        payload["fixed_evaluation_gate_required"] = True
        evaluator_status = getattr(self._evaluator, "status", None)
        if callable(evaluator_status):
            try:
                payload["fixed_evaluation"] = _json_safe_mapping(evaluator_status())
            except Exception as exc:
                payload["fixed_evaluation"] = {
                    "available": False,
                    "status": "error",
                    "error_type": exc.__class__.__name__,
                }
        else:
            payload["fixed_evaluation"] = {
                "available": False,
                "status": "missing",
            }
        return payload

    def settings(self) -> JsonObject:
        return self._store.settings()

    def update_settings(self, payload: Mapping[str, Any]) -> JsonObject:
        allowed = {
            "learning_enabled",
            "implicit_feedback_enabled",
            "save_query_text",
        }
        _reject_unknown(payload, allowed)
        if not payload:
            raise SearchLearningServiceError(
                "invalid_learning_settings", "At least one setting is required."
            )
        try:
            settings = self._store.update_settings(
                learning_enabled=payload.get("learning_enabled"),
                implicit_feedback_enabled=payload.get("implicit_feedback_enabled"),
                save_query_text=payload.get("save_query_text"),
            )
        except SearchLearningValidationError as exc:
            raise SearchLearningServiceError(
                "invalid_learning_settings", str(exc)
            ) from exc
        # Disabling learning immediately restores fixed ranking. Enabling only
        # references the previously gate-approved active model, if one exists.
        active_version = settings.get("active_model_version")
        if active_version:
            model = self._store.model_version(str(active_version))
            self._write_active_manifest(
                enabled=bool(settings["learning_enabled"]),
                shadow_mode=bool(settings["shadow_mode"]),
                ranker=str(model["model_file"]),
            )
        elif not settings["learning_enabled"]:
            self._write_active_manifest(
                enabled=False,
                shadow_mode=True,
                ranker=None,
            )
        return settings

    def feedback(self, payload: Mapping[str, Any]) -> JsonObject:
        allowed = {"session_id", "library_id", "doc_id", "action", "source"}
        _reject_unknown(payload, allowed)
        missing = sorted(
            {"session_id", "library_id", "doc_id", "action"} - set(payload)
        )
        if missing:
            raise SearchLearningServiceError(
                "invalid_search_feedback",
                "Missing feedback fields: " + ", ".join(missing),
            )
        for name in ("session_id", "library_id", "doc_id", "action"):
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip():
                raise SearchLearningServiceError(
                    "invalid_search_feedback", f"{name} must be non-empty text."
                )
        action = str(payload["action"]).strip()
        source = payload.get("source") or (
            "explicit" if action in {"relevant", "not_relevant"} else "gallery"
        )
        if not isinstance(source, str) or not source.strip():
            raise SearchLearningServiceError(
                "invalid_search_feedback", "source must be non-empty text."
            )
        try:
            return self._store.record_feedback(
                session_id=str(payload["session_id"]).strip(),
                library_id=str(payload["library_id"]).strip(),
                doc_id=str(payload["doc_id"]).strip(),
                action=action,
                source=source.strip(),
            )
        except SearchLearningValidationError as exc:
            raise SearchLearningServiceError(
                "invalid_search_feedback", str(exc)
            ) from exc

    def revoke_feedback(self, event_id: str) -> JsonObject:
        try:
            return self._store.revoke_feedback(event_id)
        except SearchLearningValidationError as exc:
            raise SearchLearningServiceError(
                "search_feedback_not_found", str(exc)
            ) from exc

    def list_feedback(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        session_id: str | None = None,
        action: str | None = None,
        active_only: bool = True,
    ) -> JsonObject:
        return self._store.list_feedback(
            cursor=cursor,
            limit=limit,
            session_id=session_id,
            action=action,
            active_only=active_only,
        )

    def list_sessions(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        query_type: str | None = None,
    ) -> JsonObject:
        return self._store.list_sessions(
            cursor=cursor,
            limit=limit,
            query_type=query_type,
        )

    def train(self) -> JsonObject:
        counts = self._store.training_counts()
        self._validate_training_counts(counts)
        with self._lock:
            for job_id, future in self._futures.items():
                if not future.done():
                    raise SearchLearningServiceError(
                        "training_already_running",
                        f"A local training job is already running: {job_id}",
                    )
            job_id = f"learn-{uuid.uuid4().hex}"
            cancel_event = threading.Event()
            job = {
                "job_id": job_id,
                "status": "queued",
                "submitted_at": _now(),
                "message": "Waiting for the local training worker.",
            }
            self._store.record_learning_job(job)
            self._cancel_events[job_id] = cancel_event
            self._futures[job_id] = self._executor.submit(
                self._train_worker, job_id, cancel_event, counts
            )
        return self._store.learning_job(job_id)

    def training_job(self, job_id: str) -> JsonObject:
        return self._store.learning_job(job_id)

    def cancel_training(self, job_id: str) -> JsonObject:
        with self._lock:
            cancel_event = self._cancel_events.get(job_id)
            future = self._futures.get(job_id)
            if cancel_event is None or future is None:
                return self._store.learning_job(job_id)
            cancel_event.set()
            if future.cancel():
                self._mark_job_cancelled(job_id)
        return self._store.learning_job(job_id)

    def activate(self, model_version: str, *, shadow_mode: bool = True) -> JsonObject:
        if not isinstance(shadow_mode, bool):
            raise SearchLearningServiceError(
                "invalid_activation", "shadow_mode must be a boolean."
            )
        if not self._store.settings()["learning_enabled"]:
            raise SearchLearningServiceError(
                "search_learning_disabled",
                "Enable search learning before activating a candidate.",
            )
        try:
            model = self._store.model_version(model_version)
        except SearchLearningValidationError as exc:
            raise SearchLearningServiceError("model_not_found", str(exc)) from exc
        if model["gate_status"] != "passed":
            raise SearchLearningServiceError(
                "evaluation_gate_not_passed",
                "The candidate has not passed the fixed evaluation set.",
            )
        self._validate_model_artifact(str(model["model_file"]))
        previous_manifest = self._read_active_bytes()
        self._write_active_manifest(
            enabled=True,
            shadow_mode=shadow_mode,
            ranker=str(model["model_file"]),
        )
        try:
            return self._store.activate_model(model_version, shadow_mode=shadow_mode)
        except Exception:
            self._restore_active_bytes(previous_manifest)
            raise

    def rollback(self) -> JsonObject:
        settings = self._store.settings()
        if not settings["learning_enabled"]:
            raise SearchLearningServiceError(
                "search_learning_disabled",
                "Enable search learning before rolling back the active candidate.",
            )
        previous = settings.get("previous_model_version")
        if not previous:
            raise SearchLearningServiceError(
                "rollback_unavailable", "No previous model version is available."
            )
        model = self._store.model_version(str(previous))
        self._validate_model_artifact(str(model["model_file"]))
        previous_manifest = self._read_active_bytes()
        self._write_active_manifest(
            enabled=True,
            shadow_mode=False,
            ranker=str(model["model_file"]),
        )
        try:
            return self._store.rollback_model()
        except Exception:
            self._restore_active_bytes(previous_manifest)
            raise

    def clear(self, *, confirmed: bool) -> JsonObject:
        if confirmed is not True:
            raise SearchLearningServiceError(
                "confirmation_required",
                "Clearing search-learning data requires explicit confirmation.",
            )
        with self._lock:
            if any(not future.done() for future in self._futures.values()):
                raise SearchLearningServiceError(
                    "training_running", "Cancel local training before clearing data."
                )
            result = self._store.clear_learning_data()
            self._write_active_manifest(enabled=False, shadow_mode=True, ranker=None)
            if self._directory.exists():
                for child in self._directory.iterdir():
                    if (
                        child.name
                        in {
                            "active.json",
                            FIXED_EVALUATION_FILENAME,
                        }
                        or not child.is_file()
                    ):
                        continue
                    if child.suffix.casefold() == ".json":
                        try:
                            child.unlink()
                        except OSError:
                            # Database clearing remains valid if antivirus holds
                            # an obsolete artifact; active.json is already off.
                            result["artifact_cleanup_incomplete"] = True
        return result

    def anonymous_export(self) -> JsonObject:
        return self._store.anonymous_export()

    def install_fixed_evaluation(self, source_path: str | Path) -> JsonObject:
        installer = getattr(self._evaluator, "install", None)
        if not callable(installer):
            raise SearchLearningServiceError(
                "fixed_evaluator_unavailable",
                "This build does not provide a fixed evaluation installer.",
            )
        try:
            result = installer(source_path)
        except Exception as exc:
            raise SearchLearningServiceError(
                str(getattr(exc, "code", "fixed_evaluation_pack_invalid")),
                str(exc),
            ) from exc
        if not isinstance(result, Mapping):
            raise SearchLearningServiceError(
                "fixed_evaluation_pack_invalid",
                "Fixed evaluation installer returned an invalid result.",
            )
        return _json_safe_mapping(result)

    def close(self) -> None:
        with self._lock:
            for cancel_event in self._cancel_events.values():
                cancel_event.set()
            if self._owns_executor:
                self._executor.shutdown(wait=False, cancel_futures=True)

    def _train_worker(
        self,
        job_id: str,
        cancel_event: threading.Event,
        counts: Mapping[str, Any],
    ) -> None:
        started_at = _now()
        self._store.record_learning_job(
            {
                "job_id": job_id,
                "status": "running",
                "started_at": started_at,
                "message": "Training a local candidate model.",
            }
        )
        model_version = _model_version()
        try:
            examples = self._store.training_examples()
            self._validate_training_counts(self._counts_from_examples(examples))
            if cancel_event.is_set():
                raise _TrainingCancelled
            model = _train_logistic_model(examples, model_version, cancel_event)
            model_payload = model.to_dict()
            model_file = f"{model_version}.json"
            self._write_artifact(model_file, model_payload)
            if cancel_event.is_set():
                raise _TrainingCancelled
            gate_result = self._evaluate(model_payload)
            report_payload = {
                "schema_version": 1,
                "model_version": model_version,
                "generated_at": _now(),
                "gate": gate_result.to_dict(),
            }
            report_file = f"comparison-{model_version}.json"
            self._write_artifact(report_file, report_payload)
            self._store.register_model_version(
                {
                    "model_version": model_version,
                    "status": "candidate",
                    "created_at": started_at,
                    "parent_version": self._store.settings().get(
                        "active_model_version"
                    ),
                    "model_file": model_file,
                    "report_file": report_file,
                    **counts,
                    "gate_status": gate_result.status,
                    "metrics": gate_result.to_dict(),
                }
            )
            self._store.record_learning_job(
                {
                    "job_id": job_id,
                    "status": "succeeded",
                    "started_at": started_at,
                    "finished_at": _now(),
                    "model_version": model_version,
                    "message": (
                        "Candidate passed the fixed evaluation gate."
                        if gate_result.passed
                        else (
                            "Candidate created; activation remains blocked "
                            "by evaluation."
                        )
                    ),
                }
            )
        except _TrainingCancelled:
            self._mark_job_cancelled(job_id, started_at=started_at)
        except Exception as exc:
            with suppress(Exception):
                self._store.record_learning_job(
                    {
                        "job_id": job_id,
                        "status": "failed",
                        "started_at": started_at,
                        "finished_at": _now(),
                        "message": "Local candidate training failed.",
                        "error_code": getattr(exc, "code", "training_failed"),
                        "error_message": (
                            f"{exc.__class__.__name__}: local candidate training failed"
                        ),
                    }
                )
            # Learning failures are optional and must not affect search.

    def _evaluate(self, model_payload: Mapping[str, Any]) -> EvaluationGateResult:
        if self._evaluator is None:
            return EvaluationGateResult(
                "pending",
                ("fixed_evaluator_unavailable",),
                {"fixed_evaluation_set": False},
            )
        try:
            report = self._evaluator(model_payload)
        except Exception as exc:
            if getattr(exc, "code", None) == "fixed_evaluator_unavailable":
                return EvaluationGateResult(
                    "pending",
                    ("fixed_evaluator_unavailable",),
                    {"fixed_evaluation_set": False},
                )
            return EvaluationGateResult(
                "failed",
                ("fixed_evaluator_failed",),
                {"error_type": exc.__class__.__name__},
            )
        try:
            return self._gate.evaluate(report)
        except SearchLearningServiceError as exc:
            return EvaluationGateResult(
                "failed",
                (exc.code,),
                {"error_message": str(exc)[:256]},
            )

    def _validate_training_counts(self, counts: Mapping[str, Any]) -> None:
        sessions = int(counts.get("query_sessions") or 0)
        samples = int(counts.get("explicit_samples") or 0)
        positives = int(counts.get("positive_samples") or 0)
        negatives = int(counts.get("negative_samples") or 0)
        reasons = []
        if sessions < MIN_QUERY_SESSIONS:
            reasons.append(f"{MIN_QUERY_SESSIONS - sessions} more feedback sessions")
        if samples < MIN_EXPLICIT_SAMPLES:
            reasons.append(f"{MIN_EXPLICIT_SAMPLES - samples} more explicit labels")
        if positives < 1:
            reasons.append("at least one relevant label")
        if negatives < 1:
            reasons.append("at least one not-relevant label")
        if reasons:
            raise SearchLearningServiceError(
                "insufficient_training_data",
                "Training has not started; collect " + ", ".join(reasons) + ".",
            )

    @staticmethod
    def _counts_from_examples(examples: Sequence[Mapping[str, Any]]) -> JsonObject:
        explicit = [item for item in examples if item.get("explicit") is True]
        return {
            "query_sessions": len({str(item["session_id"]) for item in explicit}),
            "explicit_samples": len(explicit),
            "positive_samples": sum(int(item["label"]) == 1 for item in explicit),
            "negative_samples": sum(int(item["label"]) == 0 for item in explicit),
        }

    def _mark_job_cancelled(
        self, job_id: str, *, started_at: str | None = None
    ) -> None:
        self._store.record_learning_job(
            {
                "job_id": job_id,
                "status": "cancelled",
                "started_at": started_at,
                "finished_at": _now(),
                "message": "Local candidate training was cancelled.",
            }
        )

    def _validate_model_artifact(self, model_file: str) -> RankingModel:
        path = self._artifact_path(model_file)
        try:
            if path.stat().st_size > MAX_MODEL_FILE_BYTES:
                raise SearchLearningServiceError(
                    "invalid_model_artifact", "Model artifact is too large."
                )
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("model must be an object")
            return RankingModel(
                schema_version=int(payload["schema_version"]),
                model_version=str(payload["model_version"]),
                feature_schema_version=int(payload["feature_schema_version"]),
                kind=str(payload.get("kind") or "logistic"),
                intercept=float(payload["intercept"]),
                weights={
                    str(key): float(value)
                    for key, value in dict(payload["weights"]).items()
                },
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SearchLearningServiceError(
                "invalid_model_artifact", "Model artifact is missing or invalid."
            ) from exc

    def _write_artifact(self, name: str, payload: Mapping[str, Any]) -> None:
        path = self._artifact_path(name)
        self._atomic_json(path, payload)

    def _write_active_manifest(
        self,
        *,
        enabled: bool,
        shadow_mode: bool,
        ranker: str | None,
    ) -> None:
        payload: JsonObject = {
            "schema_version": 1,
            "enabled": enabled,
            "shadow_mode": shadow_mode,
        }
        if ranker is not None:
            payload["ranker"] = self._artifact_name(ranker)
        existing = self._read_active_mapping()
        calibration = existing.get("calibration") if existing else None
        if isinstance(calibration, str):
            with suppress(SearchLearningServiceError):
                payload["calibration"] = self._artifact_name(calibration)
        self._atomic_json(self._directory / "active.json", payload)

    def _read_active_mapping(self) -> JsonObject:
        path = self._directory / "active.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def _read_active_bytes(self) -> bytes | None:
        path = self._directory / "active.json"
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _restore_active_bytes(self, content: bytes | None) -> None:
        path = self._directory / "active.json"
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                self._directory.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_bytes(content)
                os.replace(temporary, path)
        except OSError:
            # The database operation has already failed; retaining a validated
            # ranker is safer than breaking the normal fixed-ranker fallback.
            pass

    def _artifact_path(self, name: str) -> Path:
        normalized = self._artifact_name(name)
        self._directory.mkdir(parents=True, exist_ok=True)
        return self._directory / normalized

    @staticmethod
    def _artifact_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise SearchLearningServiceError(
                "invalid_artifact_name", "Learning artifact name is invalid."
            )
        pure = PurePosixPath(name.replace("\\", "/"))
        if (
            pure.is_absolute()
            or len(pure.parts) != 1
            or ".." in pure.parts
            or pure.suffix.casefold() != ".json"
        ):
            raise SearchLearningServiceError(
                "invalid_artifact_name", "Learning artifacts must be local JSON files."
            )
        return pure.as_posix()

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_MODEL_FILE_BYTES:
            raise SearchLearningServiceError(
                "artifact_too_large", "Learning artifact is too large."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise SearchLearningServiceError(
                "artifact_write_failed", "Unable to save the learning artifact."
            ) from exc


class _TrainingCancelled(Exception):
    pass


def _train_logistic_model(
    examples: Sequence[Mapping[str, Any]],
    model_version: str,
    cancel_event: threading.Event,
) -> RankingModel:
    names = tuple(NUMERIC_FEATURE_NAMES)
    rows: list[list[float]] = []
    labels: list[float] = []
    sample_weights: list[float] = []
    for example in examples:
        if int(example.get("feature_schema_version") or 0) != FEATURE_SCHEMA_VERSION:
            continue
        features = example.get("features")
        if not isinstance(features, Mapping):
            continue
        row = [_finite_feature(features.get(name, 0.0)) for name in names]
        rows.append(row)
        labels.append(1.0 if int(example.get("label") or 0) == 1 else 0.0)
        raw_weight = example.get("feedback_weight")
        sample_weight = float(raw_weight) if raw_weight is not None else 1.0
        sample_weights.append(max(0.1, abs(sample_weight)))
    compatible_explicit = sum(
        item.get("explicit") is True
        and int(item.get("feature_schema_version") or 0) == FEATURE_SCHEMA_VERSION
        for item in examples
    )
    if compatible_explicit < MIN_EXPLICIT_SAMPLES:
        raise SearchLearningServiceError(
            "insufficient_compatible_features",
            "Too few feedback rows use the current feature schema.",
        )
    means = [
        math.fsum(row[index] for row in rows) / len(rows) for index in range(len(names))
    ]
    scales = []
    for index, mean in enumerate(means):
        variance = math.fsum((row[index] - mean) ** 2 for row in rows) / len(rows)
        scales.append(max(math.sqrt(variance), 1.0e-6))
    normalized = [
        [(value - means[index]) / scales[index] for index, value in enumerate(row)]
        for row in rows
    ]
    weights = [0.0] * len(names)
    positive_rate = min(1.0 - 1.0e-6, max(1.0e-6, math.fsum(labels) / len(labels)))
    intercept = math.log(positive_rate / (1.0 - positive_rate))
    learning_rate = 0.08
    regularization = 0.002
    weight_total = math.fsum(sample_weights)
    for iteration in range(400):
        if iteration % 8 == 0 and cancel_event.is_set():
            raise _TrainingCancelled
        gradient = [0.0] * len(weights)
        intercept_gradient = 0.0
        for row, label, sample_weight in zip(
            normalized, labels, sample_weights, strict=True
        ):
            decision = intercept + math.fsum(
                weight * value for weight, value in zip(weights, row, strict=True)
            )
            prediction = _sigmoid(decision)
            error = (prediction - label) * sample_weight
            intercept_gradient += error
            for index, value in enumerate(row):
                gradient[index] += error * value
        intercept -= learning_rate * intercept_gradient / weight_total
        for index in range(len(weights)):
            weights[index] -= learning_rate * (
                gradient[index] / weight_total + regularization * weights[index]
            )
        learning_rate *= 0.997
    raw_weights = {
        name: weights[index] / scales[index]
        for index, name in enumerate(names)
        if abs(weights[index] / scales[index]) > 1.0e-12
    }
    raw_intercept = intercept - math.fsum(
        weights[index] * means[index] / scales[index] for index in range(len(names))
    )
    if not raw_weights:
        # RankingModel requires at least one named weight. A zero coefficient is
        # still a valid explicit model and keeps the artifact schema complete.
        raw_weights[names[0]] = 0.0
    return RankingModel(
        schema_version=RANKING_MODEL_SCHEMA_VERSION,
        model_version=model_version,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        kind="logistic",
        intercept=raw_intercept,
        weights=raw_weights,
    )


def _metric(values: Mapping[str, Any], name: str) -> float:
    value = values.get(name)
    if value is None or isinstance(value, bool):
        raise SearchLearningServiceError(
            "invalid_evaluation_report", f"Metric {name} must be numeric."
        )
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SearchLearningServiceError(
            "invalid_evaluation_report", f"Metric {name} is required."
        ) from exc
    if not math.isfinite(result):
        raise SearchLearningServiceError(
            "invalid_evaluation_report", f"Metric {name} must be finite."
        )
    return result


def _finite_feature(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-min(value, 700.0)))
    exponential = math.exp(max(value, -700.0))
    return exponential / (1.0 + exponential)


def _json_safe_mapping(value: Mapping[str, Any]) -> JsonObject:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str]) -> None:
    if not isinstance(payload, Mapping):
        raise SearchLearningServiceError("invalid_request", "Body must be an object.")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise SearchLearningServiceError(
            "invalid_request", "Unsupported fields: " + ", ".join(unknown)
        )


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _model_version() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"ranker-{stamp}-{uuid.uuid4().hex[:8]}"


__all__ = [
    "EvaluationGateResult",
    "FixedEvaluationGate",
    "MIN_EXPLICIT_SAMPLES",
    "MIN_QUERY_SESSIONS",
    "SearchLearningService",
    "SearchLearningServiceError",
]
