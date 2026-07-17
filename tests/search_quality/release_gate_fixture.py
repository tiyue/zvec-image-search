from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from tests.search_quality import evaluate, quality_gate
from tests.search_quality import split as dataset_split


def _query(mode: str, case_id: str) -> dict[str, str]:
    if mode == "text":
        return {"text": case_id}
    if mode == "image":
        return {"image": f"fixtures/{case_id}.png"}
    return {"text": case_id, "image": f"fixtures/{case_id}.png"}


def _source_dataset() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    annotation = {
        "status": "human_verified",
        "annotator": "release-replay-test-fixture",
        "annotated_at": "2026-07-13T12:00:00+00:00",
        "notes": "Deterministic automated test fixture; not a production label.",
    }
    for mode in sorted(evaluate.MODES):
        for index in range(10):
            answer_id = f"{mode}-answer-{index}"
            no_answer_id = f"{mode}-no-answer-{index}"
            items.extend(
                [
                    {
                        "id": answer_id,
                        "query_type": mode,
                        "mode": mode,
                        "query": _query(mode, answer_id),
                        "library_scope": {
                            "mode": "selected",
                            "library_ids": ["large", "small"],
                        },
                        "relevant_images": [
                            {
                                "image_id": f"large:{answer_id}:relevant.jpg",
                                "library_id": "large",
                            },
                            {
                                "image_id": f"small:{answer_id}:relevant.jpg",
                                "library_id": "small",
                            },
                        ],
                        "annotation": dict(annotation),
                    },
                    {
                        "id": no_answer_id,
                        "query_type": "no-answer",
                        "mode": mode,
                        "query": _query(mode, no_answer_id),
                        "library_scope": {
                            "mode": "selected",
                            "library_ids": ["large", "small"],
                        },
                        "relevant_images": [],
                        "annotation": dict(annotation),
                    },
                ]
            )
    return {
        "schema_version": 1,
        "name": "release-gate-replay-test-fixture",
        "description": "Deterministic formal release gate replay fixture.",
        "items": items,
    }


def _run(dataset: dict[str, Any], *, name: str, improved: bool) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    for item in evaluate.validate_dataset(dataset):
        results: list[dict[str, Any]]
        if item["query_type"] == "no-answer":
            results = (
                []
                if improved
                else [
                    {
                        "image_id": f"large:{item['id']}:false.jpg",
                        "library_id": "large",
                    }
                ]
            )
        elif improved:
            results = [dict(record) for record in item["relevant_images"]]
        else:
            results = [
                {
                    "image_id": f"small:{item['id']}:noise-{index}.jpg",
                    "library_id": "small",
                }
                for index in range(5)
            ] + [dict(record) for record in item["relevant_images"]]
        for rank, result in enumerate(results, start=1):
            result["rank"] = rank
            result["score"] = float(rank)
        cases.append(
            {
                "id": item["id"],
                "library_ids": ["large", "small"],
                "latency_ms": 10.0,
                "api_requests": 0,
                "results": results,
            }
        )
    split = dataset["split"]
    return {
        "schema_version": 1,
        "name": name,
        "score_semantics": {
            "text": "lower_is_better",
            "image": "lower_is_better",
            "combined": "higher_is_better",
        },
        "dataset": {
            "name": dataset["name"],
            "fingerprint": evaluate.query_corpus_fingerprint(dataset),
            "fingerprint_algorithm": evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM,
            "case_count": len(dataset["items"]),
        },
        "draft": False,
        "baseline_eligible": True,
        "capture": {
            "kind": "zvec-persistent-backend-capture",
            "pending_case_count": 0,
            "baseline_eligible": True,
            "top_k": 10,
            "candidate_k": 50,
        },
        "split": {
            "kind": split["kind"],
            "role": split["role"],
            "manifest_fingerprint": split["manifest_fingerprint"],
            "manifest_fingerprint_algorithm": split["manifest_fingerprint_algorithm"],
        },
        "cases": cases,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _relative_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError as exc:
        raise ValueError(
            "release gate test fixture must stay inside repository"
        ) from exc


def build_release_gate_fixture(
    output_directory: str | Path,
    repository_root: str | Path,
) -> Path:
    root = Path(repository_root).resolve()
    output = Path(output_directory).resolve()
    output.mkdir(parents=True, exist_ok=False)
    _relative_to_root(output, root)

    source = _source_dataset()
    manifest, _, validation = dataset_split.build_split_artifacts(source)
    before = _run(validation, name="release-replay-before", improved=False)
    after = _run(validation, name="release-replay-after", improved=True)
    paths = {
        "source": output / "source-dataset.json",
        "manifest": output / "split-manifest.json",
        "dataset": output / "validation-dataset.json",
        "before": output / "before-run.json",
        "after": output / "after-run.json",
        "report": output / "report",
    }
    for key, value in (
        ("source", source),
        ("manifest", manifest),
        ("dataset", validation),
        ("before", before),
        ("after", after),
    ):
        _write_json(paths[key], value)

    previous_directory = Path.cwd()
    try:
        os.chdir(root)
        comparison = quality_gate.run_quality_gate(
            _relative_to_root(paths["source"], root),
            _relative_to_root(paths["before"], root),
            _relative_to_root(paths["after"], root),
            _relative_to_root(paths["report"], root),
            split_manifest_path=_relative_to_root(paths["manifest"], root),
        )
    finally:
        os.chdir(previous_directory)
    if comparison["quality_gate"]["passed"] is not True:
        raise ValueError("deterministic release replay fixture did not pass")
    return paths["report"] / "comparison.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic formal release-gate test fixture"
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repository-root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(build_release_gate_fixture(args.output_dir, args.repository_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
