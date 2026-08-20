from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from tests.search_quality import evaluate
    from tests.search_quality.image_identity import (
        ImageIdentityError,
        ImageIdentityIndex,
        canonical_image_reference,
    )
except ModuleNotFoundError:  # Direct execution: python tests/search_quality/review.py
    import evaluate  # type: ignore[no-redef]
    from image_identity import (  # type: ignore[no-redef]
        ImageIdentityError,
        ImageIdentityIndex,
        canonical_image_reference,
    )


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = SCRIPT_DIR / "dataset.local.json"


class ReviewError(ValueError):
    """Raised when a review operation would make unsafe or invalid changes."""


def _resolve_dataset_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser().resolve()
    try:
        path.relative_to(SCRIPT_DIR)
    except ValueError as exc:
        raise ReviewError(
            f"dataset must stay inside {SCRIPT_DIR}; refusing path outside review area"
        ) from exc
    if not path.name.endswith(".local.json"):
        raise ReviewError("review only writes ignored *.local.json datasets")
    return path


def _validate_relative_path(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{context} must be a non-empty relative path")
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
        raise ReviewError(f"{context} must not be absolute")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReviewError(f"{context} contains path traversal or empty components")
    if "\x00" in normalized:
        raise ReviewError(f"{context} contains a NUL character")
    return normalized


def _validate_image_id(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{context} must be a non-empty image_id")
    image_id = value.strip()
    if ":" not in image_id:
        raise ReviewError(f"{context} must use library-id:relative/path format")
    library_id, relative_path = image_id.split(":", 1)
    if not library_id or "/" in library_id or "\\" in library_id:
        raise ReviewError(f"{context} has an invalid library id")
    _validate_relative_path(relative_path, f"{context} path")
    return image_id


def _validate_image_list(value: object, context: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ReviewError(f"{context} must be an array")
    validated: list[dict[str, str]] = []
    identity_index = ImageIdentityIndex()
    references = []
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            raise ReviewError(f"{context}[{index}] must be an object")
        item_context = f"{context}[{index}]"
        image_id = _validate_image_id(entry.get("image_id"), item_context)
        candidate = {**entry, "image_id": image_id}
        try:
            canonical = canonical_image_reference(candidate, item_context)
            references.append(identity_index.add(canonical, item_context))
        except ImageIdentityError as exc:
            raise ReviewError(str(exc)) from exc
        validated.append(canonical)
    try:
        identity_index.require_unique(references, context)
    except ImageIdentityError as exc:
        raise ReviewError(str(exc)) from exc
    return validated


def validate_review_dataset(dataset: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = dataset.get("items")
    if not isinstance(raw_items, list):
        raise ReviewError(
            "review requires an explicit items array; expand templates into "
            "dataset.local.json first"
        )
    for raw_item in raw_items:
        if (
            isinstance(raw_item, dict)
            and isinstance(raw_item.get("annotation"), dict)
            and raw_item["annotation"].get("status") == "synthetic_fixture"
        ):
            raise ReviewError(
                f"dataset item {raw_item.get('id', '<unknown>')}: "
                "synthetic_fixture cannot be human-reviewed"
            )
    try:
        items = evaluate.validate_dataset(dataset)
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc

    for item in items:
        item_id = str(item["id"])
        annotation = item["annotation"]
        status = annotation["status"]
        if status == "synthetic_fixture":
            raise ReviewError(
                f"dataset item {item_id}: synthetic_fixture cannot be human-reviewed"
            )
        if status not in {"pending", "human_verified"}:
            raise ReviewError(f"dataset item {item_id}: unsupported review status")

        notes = annotation.get("notes", "")
        if not isinstance(notes, str):
            raise ReviewError(f"dataset item {item_id}: annotation notes must be text")
        if status == "human_verified":
            annotator = annotation.get("annotator")
            annotated_at = annotation.get("annotated_at")
            if not isinstance(annotator, str) or not annotator.strip():
                raise ReviewError(
                    f"dataset item {item_id}: human_verified needs annotator"
                )
            if not isinstance(annotated_at, str) or not annotated_at.strip():
                raise ReviewError(
                    f"dataset item {item_id}: human_verified needs annotated_at"
                )
            try:
                datetime.fromisoformat(annotated_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ReviewError(
                    f"dataset item {item_id}: annotated_at must be ISO-8601"
                ) from exc

        query = item.get("query", {})
        if item["mode"] in {"image", "combined"}:
            _validate_relative_path(
                query.get("image"), f"dataset item {item_id}: query.image"
            )

        _validate_image_list(
            item.get("relevant_images"),
            f"dataset item {item_id}: relevant_images",
        )
        suggestions = item.get("suggested_relevant_images", [])
        _validate_image_list(
            suggestions,
            f"dataset item {item_id}: suggested_relevant_images",
        )

        draft = item.get("review_draft")
        if draft is not None:
            if status != "pending":
                raise ReviewError(
                    f"dataset item {item_id}: verified item cannot retain review_draft"
                )
            if item["query_type"] == "no-answer":
                raise ReviewError(
                    f"dataset item {item_id}: no-answer cannot stage relevant images"
                )
            if not isinstance(draft, dict):
                raise ReviewError(
                    f"dataset item {item_id}: review_draft must be an object"
                )
            selected = _validate_image_list(
                draft.get("relevant_images"),
                f"dataset item {item_id}: review_draft.relevant_images",
            )
            if not selected:
                raise ReviewError(
                    f"dataset item {item_id}: review_draft cannot be empty"
                )
    return items


def load_dataset(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReviewError(f"dataset does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReviewError(f"dataset is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewError("dataset root must be an object")
    validate_review_dataset(value)
    return value


def _atomic_write(path: Path, dataset: dict[str, Any]) -> None:
    validate_review_dataset(dataset)
    encoded = (json.dumps(dataset, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _find_item(dataset: dict[str, Any], item_id: str) -> dict[str, Any]:
    for item in dataset["items"]:
        if item.get("id") == item_id:
            return item
    raise ReviewError(f"unknown dataset item: {item_id}")


def _require_pending(item: dict[str, Any]) -> None:
    status = item["annotation"]["status"]
    if status != "pending":
        raise ReviewError(
            f"dataset item {item['id']} is {status}; only pending items can be changed"
        )


def _image_objects(values: Sequence[str], context: str) -> list[dict[str, str]]:
    result = [{"image_id": _validate_image_id(value, context)} for value in values]
    identifiers = [entry["image_id"] for entry in result]
    if len(identifiers) != len(set(identifiers)):
        raise ReviewError(f"{context} contains duplicate image_id values")
    return result


def _draft_selection(item: dict[str, Any]) -> list[dict[str, str]]:
    draft = item.get("review_draft")
    if not isinstance(draft, dict):
        return []
    selected = draft.get("relevant_images")
    if not isinstance(selected, list):
        return []
    return copy.deepcopy(selected)


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(item["annotation"]["status"]) for item in items)
    modes = Counter(str(item["mode"]) for item in items)
    ready = sum(
        item["annotation"]["status"] == "pending"
        and item["query_type"] != "no-answer"
        and bool(_draft_selection(item))
        for item in items
    )
    return {
        "total": len(items),
        "pending": statuses["pending"],
        "human_verified": statuses["human_verified"],
        "ready_to_confirm": ready,
        "pending_no_answer": sum(
            item["annotation"]["status"] == "pending"
            and item["query_type"] == "no-answer"
            for item in items
        ),
        "by_mode": dict(sorted(modes.items())),
    }


def _query_preview(item: dict[str, Any]) -> str:
    query = item["query"]
    text = query.get("text")
    image = query.get("image")
    values = [value for value in (text, image) if isinstance(value, str) and value]
    preview = " | ".join(values)
    return preview if len(preview) <= 100 else preview[:97] + "..."


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _cmd_list(dataset: dict[str, Any], args: argparse.Namespace) -> None:
    items = dataset["items"]
    if args.status != "all":
        items = [item for item in items if item["annotation"]["status"] == args.status]
    if args.json:
        _print_json(
            [
                {
                    "id": item["id"],
                    "query_type": item["query_type"],
                    "mode": item["mode"],
                    "status": item["annotation"]["status"],
                    "query": item["query"],
                    "suggested_count": len(item.get("suggested_relevant_images", [])),
                    "draft_count": len(_draft_selection(item)),
                }
                for item in items
            ]
        )
        return
    for item in items:
        print(
            f"{item['id']} [{item['mode']}] "
            f"suggested={len(item.get('suggested_relevant_images', []))} "
            f"draft={len(_draft_selection(item))} {_query_preview(item)}"
        )
    print(f"{len(items)} item(s)")


def _cmd_show(dataset: dict[str, Any], args: argparse.Namespace) -> None:
    item = _find_item(dataset, args.item_id)
    if args.json:
        _print_json(item)
        return
    print(f"ID: {item['id']}")
    print(f"Type: {item['query_type']} (mode={item['mode']})")
    print(f"Status: {item['annotation']['status']}")
    print("Query:")
    _print_json(item["query"])
    suggestions = item.get("suggested_relevant_images", [])
    print(f"Suggested relevant images ({len(suggestions)}; unverified):")
    for index, entry in enumerate(suggestions, start=1):
        print(f"  {index}. {entry['image_id']}")
    selected = _draft_selection(item)
    print(f"Draft selection ({len(selected)}; still pending):")
    for index, entry in enumerate(selected, start=1):
        print(f"  {index}. {entry['image_id']}")
    if item["annotation"].get("notes"):
        print(f"Notes: {item['annotation']['notes']}")


def _cmd_accept(dataset: dict[str, Any], args: argparse.Namespace) -> None:
    item = _find_item(dataset, args.item_id)
    _require_pending(item)
    if item["query_type"] == "no-answer":
        raise ReviewError("no-answer items must use explicit confirm --no-answer")
    suggestions = copy.deepcopy(item.get("suggested_relevant_images", []))
    if not suggestions:
        raise ReviewError(f"dataset item {item['id']} has no suggestions to accept")
    item["review_draft"] = {
        "source": "suggested_relevant_images",
        "relevant_images": suggestions,
    }
    _atomic_write(args.dataset_path, dataset)
    print(
        f"Saved {len(suggestions)} draft image(s) for {item['id']}; status remains "
        "pending. Run confirm for this item after human review."
    )


def _cmd_replace(dataset: dict[str, Any], args: argparse.Namespace) -> None:
    item = _find_item(dataset, args.item_id)
    _require_pending(item)
    if item["query_type"] == "no-answer":
        raise ReviewError("no-answer items cannot have relevant image IDs")
    selected = _image_objects(args.image_id, "--image-id")
    if not selected:
        raise ReviewError("answerable review selection cannot be empty")
    item["review_draft"] = {
        "source": "manual_replacement",
        "relevant_images": selected,
    }
    _atomic_write(args.dataset_path, dataset)
    print(
        f"Saved {len(selected)} draft image(s) for {item['id']}; status remains "
        "pending. Run confirm for this item after human review."
    )


def confirm_human_review(
    dataset: dict[str, Any],
    dataset_path: str | Path,
    *,
    item_id: str,
    annotator: str,
    notes: str = "",
    image_ids: Sequence[str] | None = None,
    image_references: Sequence[dict[str, Any]] | None = None,
    no_answer: bool = False,
    accept_suggested: bool = False,
    use_staged_draft: bool = True,
) -> dict[str, Any]:
    """Atomically confirm exactly one case after an explicit human action.

    Callers must opt in to suggestions or staged drafts. Passing ``image_ids``
    always means an explicit exact selection, including an empty selection that is
    rejected for answerable cases.
    """
    resolved_path = _resolve_dataset_path(dataset_path)
    item = _find_item(dataset, item_id)
    _require_pending(item)
    if not isinstance(annotator, str) or not annotator.strip():
        raise ReviewError("annotator must not be empty")
    if not isinstance(notes, str):
        raise ReviewError("notes must be text")
    explicit_sources = sum(value is not None for value in (image_ids, image_references))
    if accept_suggested and explicit_sources:
        raise ReviewError("cannot combine suggested and explicit image selection")
    if explicit_sources > 1:
        raise ReviewError("cannot combine image_ids and image_references")

    if item["query_type"] == "no-answer":
        if not no_answer:
            raise ReviewError(
                "no-answer confirmation requires the explicit --no-answer flag"
            )
        if accept_suggested or image_ids or image_references:
            raise ReviewError("no-answer items cannot have relevant image IDs")
        selected: list[dict[str, str]] = []
    else:
        if no_answer:
            raise ReviewError("--no-answer is only valid for no-answer dataset items")
        if accept_suggested:
            selected = copy.deepcopy(item.get("suggested_relevant_images", []))
        elif image_references is not None:
            selected = _validate_image_list(
                list(image_references), "explicit image references"
            )
        elif image_ids is not None:
            selected = _image_objects(image_ids, "--image-id")
        elif use_staged_draft:
            selected = _draft_selection(item)
        else:
            selected = []
        if not selected:
            raise ReviewError(
                "answerable confirmation needs non-empty relevance: stage "
                "accept/replace "
                "or pass --accept-suggested/--image-id"
            )

    item["relevant_images"] = selected
    item["annotation"] = {
        "status": "human_verified",
        "annotator": annotator.strip(),
        "annotated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "notes": notes,
    }
    item.pop("review_draft", None)
    _atomic_write(resolved_path, dataset)
    return copy.deepcopy(item)


def _cmd_confirm(dataset: dict[str, Any], args: argparse.Namespace) -> None:
    item = confirm_human_review(
        dataset,
        args.dataset_path,
        item_id=args.item_id,
        annotator=args.annotator,
        notes=args.notes,
        image_ids=args.image_id,
        no_answer=args.no_answer,
        accept_suggested=args.accept_suggested,
    )
    print(f"Human-verified exactly one item: {item['id']}")


def _cmd_status(dataset: dict[str, Any], args: argparse.Namespace) -> None:
    if args.item_id is None:
        _print_json(_summary(dataset["items"]))
        return
    item = _find_item(dataset, args.item_id)
    value = {
        "id": item["id"],
        "status": item["annotation"]["status"],
        "query_type": item["query_type"],
        "draft_count": len(_draft_selection(item)),
        "suggested_count": len(item.get("suggested_relevant_images", [])),
        "ready_to_confirm": item["query_type"] == "no-answer"
        or bool(_draft_selection(item)),
        "annotation": item["annotation"],
    }
    _print_json(value)


def _cmd_summary(dataset: dict[str, Any], _args: argparse.Namespace) -> None:
    _print_json(_summary(dataset["items"]))


def _cmd_validate(dataset: dict[str, Any], _args: argparse.Namespace) -> None:
    items = validate_review_dataset(dataset)
    summary = _summary(items)
    print(
        f"Valid review dataset: {summary['total']} item(s), "
        f"pending={summary['pending']}, "
        f"human_verified={summary['human_verified']}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely review one local search-quality annotation at a time."
    )
    parser.add_argument(
        "--dataset",
        default=str(DEFAULT_DATASET),
        help="ignored *.local.json inside tests/search_quality",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list review cases")
    list_parser.add_argument(
        "--status",
        choices=("pending", "human_verified", "all"),
        default="pending",
    )
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=_cmd_list)

    show_parser = subparsers.add_parser("show", help="show a query and suggestions")
    show_parser.add_argument("item_id")
    show_parser.add_argument("--json", action="store_true")
    show_parser.set_defaults(handler=_cmd_show)

    accept_parser = subparsers.add_parser(
        "accept", help="stage all suggestions; item remains pending"
    )
    accept_parser.add_argument("item_id")
    accept_parser.set_defaults(handler=_cmd_accept)

    replace_parser = subparsers.add_parser(
        "replace", help="stage replacement image IDs; item remains pending"
    )
    replace_parser.add_argument("item_id")
    replace_parser.add_argument("--image-id", action="append", required=True)
    replace_parser.set_defaults(handler=_cmd_replace)

    confirm_parser = subparsers.add_parser(
        "confirm", help="explicitly human-verify exactly one pending item"
    )
    confirm_parser.add_argument("item_id")
    confirm_parser.add_argument("--annotator", required=True)
    confirm_parser.add_argument("--notes", default="")
    selection = confirm_parser.add_mutually_exclusive_group()
    selection.add_argument("--accept-suggested", action="store_true")
    selection.add_argument("--image-id", action="append")
    selection.add_argument("--no-answer", action="store_true")
    confirm_parser.set_defaults(handler=_cmd_confirm)

    status_parser = subparsers.add_parser(
        "status", help="show one item status, or overall status"
    )
    status_parser.add_argument("item_id", nargs="?")
    status_parser.set_defaults(handler=_cmd_status)

    summary_parser = subparsers.add_parser("summary", help="summarize review progress")
    summary_parser.set_defaults(handler=_cmd_summary)

    validate_parser = subparsers.add_parser(
        "validate", help="validate schema, paths, labels, and review metadata"
    )
    validate_parser.set_defaults(handler=_cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.dataset_path = _resolve_dataset_path(args.dataset)
        dataset = load_dataset(args.dataset_path)
        args.handler(dataset, args)
    except (ReviewError, OSError) as exc:
        print(f"review error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
