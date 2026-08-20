from __future__ import annotations

import ast
import unittest
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# These APIs return a complete, materialized state table (or a complete root).
# Large-library production paths must use the corresponding iter_*/page_* APIs.
FULL_STATE_READ_APIS = frozenset(
    {
        "entries_for_root",
        "entries_for_index_run",
        "ids_for_root",
        "list_document_annotations",
        "list_entries",
    }
)
COLLECTION_MUTATION_APIS = frozenset(
    {
        "delete",
        "delete_resilient",
        "update_record_tags",
        "upsert_record_vectors",
        "upsert_records",
    }
)


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class _Source:
    relative_path: str
    tree: ast.Module


@dataclass(frozen=True)
class _CallSite:
    relative_path: str
    scope: str
    line: int
    expression: str


def _source(relative_path: str) -> _Source:
    path = PROJECT_ROOT / relative_path
    return _Source(
        relative_path=relative_path,
        tree=ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
    )


def _class(source: _Source, class_name: str) -> ast.ClassDef:
    for node in source.tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{source.relative_path} has no class {class_name}.")


def _method(source: _Source, class_name: str, method_name: str) -> FunctionNode:
    owner = _class(source, class_name)
    for node in owner.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        ):
            return node
    raise AssertionError(
        f"{source.relative_path} has no method {class_name}.{method_name}."
    )


def _function(source: _Source, function_name: str) -> FunctionNode:
    for node in source.tree.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ):
            return node
    raise AssertionError(f"{source.relative_path} has no function {function_name}.")


def _reachable_methods(
    source: _Source,
    class_name: str,
    entry_method: str,
) -> dict[str, FunctionNode]:
    """Return the intra-class call closure rooted at one production entry point."""

    owner = _class(source, class_name)
    methods = {
        node.name: node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if entry_method not in methods:
        raise AssertionError(
            f"{source.relative_path} has no method {class_name}.{entry_method}."
        )
    reachable: dict[str, FunctionNode] = {}
    pending = [entry_method]
    while pending:
        method_name = pending.pop()
        if method_name in reachable:
            continue
        method = methods[method_name]
        reachable[method_name] = method
        for candidate in ast.walk(method):
            if not isinstance(candidate, ast.Call):
                continue
            called = _call_name(candidate)
            if called is None or not called.startswith("self."):
                continue
            called_method = called.removeprefix("self.").split(".", maxsplit=1)[0]
            if called_method in methods and called_method not in reachable:
                pending.append(called_method)
    return reachable


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _call_name(node: ast.Call) -> str | None:
    return _dotted_name(node.func)


def _call_sites(node: ast.AST, called_name: str) -> list[ast.Call]:
    return [
        candidate
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.Call)
        and (_call_name(candidate) or "").split(".")[-1] == called_name
    ]


def _state_aliases(node: ast.AST) -> set[str]:
    """Track simple aliases such as ``state = self.state`` inside one method."""

    aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for candidate in ast.walk(node):
            targets: list[ast.expr]
            value: ast.expr | None
            if isinstance(candidate, ast.Assign):
                targets = candidate.targets
                value = candidate.value
            elif isinstance(candidate, ast.AnnAssign):
                targets = [candidate.target]
                value = candidate.value
            else:
                continue
            if value is None:
                continue
            value_name = _dotted_name(value)
            from_state = value_name == "self.state" or (
                isinstance(value, ast.Name) and value.id in aliases
            )
            if not from_state:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True
    return aliases


def _is_state_receiver(node: ast.AST, aliases: set[str]) -> bool:
    dotted = _dotted_name(node)
    return bool(
        dotted == "self.state"
        or (dotted is not None and dotted.endswith(".state"))
        or (isinstance(node, ast.Name) and node.id in aliases)
    )


def _full_state_accesses(node: ast.AST) -> list[tuple[int, str]]:
    """Find direct and getattr-based access to known full-table state APIs."""

    aliases = _state_aliases(node)
    found: set[tuple[int, str]] = set()
    for candidate in ast.walk(node):
        if (
            isinstance(candidate, ast.Attribute)
            and candidate.attr in FULL_STATE_READ_APIS
            and _is_state_receiver(candidate.value, aliases)
        ):
            found.add((candidate.lineno, candidate.attr))
            continue
        if not isinstance(candidate, ast.Call) or _call_name(candidate) != "getattr":
            continue
        if len(candidate.args) < 2 or not _is_state_receiver(
            candidate.args[0], aliases
        ):
            continue
        attribute = candidate.args[1]
        if (
            isinstance(attribute, ast.Constant)
            and isinstance(attribute.value, str)
            and attribute.value in FULL_STATE_READ_APIS
        ):
            found.add((candidate.lineno, attribute.value))
    return sorted(found)


def _format_accesses(accesses: Iterable[tuple[int, str]]) -> str:
    return ", ".join(f"line {line}: state.{name}" for line, name in accesses)


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next(
        (item.value for item in call.keywords if item.arg == name),
        None,
    )


def _is_name(node: ast.AST | None, expected: str) -> bool:
    return isinstance(node, ast.Name) and node.id == expected


def _is_false(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _module_integer_constants(source: _Source) -> dict[str, int]:
    values: dict[str, int] = {}
    for candidate in source.tree.body:
        if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
            continue
        raw_value = candidate.value
        if not isinstance(raw_value, ast.Constant):
            continue
        value = raw_value.value
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        targets = (
            candidate.targets
            if isinstance(candidate, ast.Assign)
            else [candidate.target]
        )
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value
    return values


def _positive_bounded_preview(
    node: ast.AST | None,
    constants: dict[str, int],
) -> bool:
    value: object
    if isinstance(node, ast.Constant):
        value = node.value
    elif isinstance(node, ast.Name):
        value = constants.get(node.id)
    else:
        return False
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100


def _dict_value(node: ast.AST, key: str) -> ast.expr | None:
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Dict):
            continue
        for raw_key, value in zip(candidate.keys, candidate.values, strict=True):
            if isinstance(raw_key, ast.Constant) and raw_key.value == key:
                return value
    return None


def _has_expanded_keyword(call: ast.Call, name: str) -> bool:
    return any(
        keyword.arg is None and _is_name(keyword.value, name)
        for keyword in call.keywords
    )


def _repository_mutations(node: ast.AST) -> list[tuple[int, str]]:
    aliases = {"repository"}
    for candidate in ast.walk(node):
        if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
            continue
        value = candidate.value
        if value is None or _dotted_name(value) != "self.repository":
            continue
        targets = (
            candidate.targets
            if isinstance(candidate, ast.Assign)
            else [candidate.target]
        )
        aliases.update(target.id for target in targets if isinstance(target, ast.Name))
    found: set[tuple[int, str]] = set()
    for candidate in ast.walk(node):
        if isinstance(candidate, ast.Attribute):
            if candidate.attr not in COLLECTION_MUTATION_APIS:
                continue
            receiver = _dotted_name(candidate.value)
            if receiver == "self.repository" or receiver in aliases:
                found.add((candidate.lineno, candidate.attr))
            continue
        if not isinstance(candidate, ast.Call) or _call_name(candidate) != "getattr":
            continue
        if len(candidate.args) < 2:
            continue
        receiver = _dotted_name(candidate.args[0])
        attribute = candidate.args[1]
        if (
            (receiver == "self.repository" or receiver in aliases)
            and isinstance(attribute, ast.Constant)
            and isinstance(attribute.value, str)
            and attribute.value in COLLECTION_MUTATION_APIS
        ):
            found.add((candidate.lineno, attribute.value))
    return sorted(found)


class _OptimizeCallVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.scope: list[str] = []
        self.calls: list[_CallSite] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        if isinstance(node.func, ast.Attribute) and node.func.attr == "optimize":
            self.calls.append(
                _CallSite(
                    relative_path=self.relative_path,
                    scope=".".join(self.scope),
                    line=node.lineno,
                    expression=ast.unparse(node.func),
                )
            )
        self.generic_visit(node)


class LargeLibraryArchitectureGuardrailTests(unittest.TestCase):
    def test_repository_mutation_guard_detects_direct_and_dynamic_bypasses(
        self,
    ) -> None:
        sample = ast.parse(
            """
def mutate(self):
    repository = self.repository
    repository.upsert_records([], [], [])
    getattr(self.repository, "delete")([])
"""
        )
        self.assertEqual(
            _repository_mutations(sample),
            [(4, "upsert_records"), (5, "delete")],
        )

    def test_large_service_paths_never_use_materializing_state_apis(self) -> None:
        source = _source("image_vector_service/service.py")
        methods = (
            "_index",
            "backfill_metadata_embeddings",
            "rebind_root",
            "cluster_images",
        )
        for method_name in methods:
            with self.subTest(method=method_name):
                reachable = _reachable_methods(
                    source,
                    "ImageVectorService",
                    method_name,
                )
                accesses = [
                    (reachable_name, line, api)
                    for reachable_name, reachable_method in reachable.items()
                    for line, api in _full_state_accesses(reachable_method)
                ]
                self.assertEqual(
                    accesses,
                    [],
                    f"ImageVectorService.{method_name} must stay bounded, but it "
                    "reaches full-table state access: "
                    + ", ".join(
                        f"{name} line {line}: state.{api}"
                        for name, line, api in accesses
                    )
                    + ". Replace the full-table "
                    "API with an IndexState iter_*/page_* keyset reader; do not add a "
                    "list_entries compatibility fallback to production code.",
                )

    def test_folder_failure_inheritance_never_falls_back_to_full_library(self) -> None:
        source = _source("image_vector_service/annotation_service.py")
        reachable = _reachable_methods(
            source,
            "AutoTaggingCoordinator",
            "_inherit_content_policy_failures",
        )
        method = reachable["_inherit_content_policy_failures"]
        accesses = [
            (reachable_name, line, api)
            for reachable_name, reachable_method in reachable.items()
            for line, api in _full_state_accesses(reachable_method)
        ]
        self.assertEqual(
            accesses,
            [],
            "Folder failure inheritance must query only requested folders in bounded "
            "pages, but it reaches: "
            + ", ".join(
                f"{name} line {line}: state.{api}" for name, line, api in accesses
            )
            + ". Require "
            "iter_entries_for_folders_with_annotations instead of scanning all "
            "entries.",
        )

        folder_reader_calls = _call_sites(method, "folder_reader")
        self.assertTrue(
            folder_reader_calls,
            "Folder inheritance must keep the bounded folder_reader production route.",
        )
        for call in folder_reader_calls:
            parent = next(
                (
                    candidate
                    for candidate in ast.walk(method)
                    if isinstance(candidate, ast.For) and candidate.iter is call
                ),
                None,
            )
            self.assertIsNotNone(
                parent,
                "Consume folder_reader as an iterator; never wrap it in "
                "list()/tuple().",
            )

        streaming_loop_lines = {call.lineno for call in folder_reader_calls}
        retained_lists = {
            target.id
            for candidate in ast.walk(method)
            if isinstance(candidate, (ast.Assign, ast.AnnAssign))
            for target in (
                candidate.targets
                if isinstance(candidate, ast.Assign)
                else [candidate.target]
            )
            if isinstance(target, ast.Name)
            and (
                isinstance(candidate.value, ast.List)
                or (
                    isinstance(candidate.value, ast.Call)
                    and _call_name(candidate.value) == "list"
                    and not candidate.value.args
                )
            )
            and candidate.lineno < min(streaming_loop_lines)
        }
        accumulating_calls = [
            candidate
            for outer in ast.walk(method)
            if isinstance(outer, ast.For)
            and isinstance(outer.iter, ast.Call)
            and _call_name(outer.iter) == "folder_reader"
            for statement in outer.body
            for candidate in ast.walk(statement)
            if isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Attribute)
            and candidate.func.attr in {"append", "extend", "insert"}
            and isinstance(candidate.func.value, ast.Name)
            and candidate.func.value.id in retained_lists
        ]
        self.assertEqual(
            accumulating_calls,
            [],
            "Do not append folder_reader members into a run-wide list. Consume each "
            "page into a bounded aggregate and discard the page before reading more.",
        )

        for candidate in ast.walk(method):
            if not isinstance(candidate, ast.Call):
                continue
            if _call_name(candidate) not in {"list", "tuple"} or not candidate.args:
                continue
            names = {
                item.id
                for item in ast.walk(candidate.args[0])
                if isinstance(item, ast.Name)
            }
            self.assertNotIn(
                "folder_reader",
                names,
                "Do not materialize every member returned by folder_reader. Aggregate "
                "bounded summaries while consuming its pages.",
            )

    def test_optimize_is_confined_to_idle_maintenance_and_migration(self) -> None:
        calls: list[_CallSite] = []
        roots = (
            PROJECT_ROOT / "image_vector_service",
            PROJECT_ROOT / "zvec_host",
            PROJECT_ROOT / "zvec_webview",
        )
        for root in roots:
            for path in sorted(root.rglob("*.py")):
                relative = path.relative_to(PROJECT_ROOT).as_posix()
                visitor = _OptimizeCallVisitor(relative)
                visitor.visit(
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                )
                calls.extend(visitor.calls)

        allowed = {
            (
                "image_vector_service/service.py",
                "ImageVectorService.run_idle_maintenance",
            ),
            (
                "image_vector_service/zvec_repository.py",
                "ZvecImageRepository.optimize",
            ),
        }
        violations = [
            call
            for call in calls
            if not call.relative_path.endswith("/path_migration.py")
            and (call.relative_path, call.scope) not in allowed
        ]
        self.assertEqual(
            violations,
            [],
            "Collection optimize must never run at the tail of an index/tag/delete "
            "task. Record pending mutations and let run_idle_maintenance compact when "
            "the backend is idle. Unexpected calls: "
            + "; ".join(
                f"{item.relative_path}:{item.line} {item.scope} ({item.expression})"
                for item in violations
            ),
        )
        self.assertTrue(
            any(
                call.relative_path == "image_vector_service/service.py"
                and call.scope == "ImageVectorService.run_idle_maintenance"
                for call in calls
            ),
            "run_idle_maintenance must remain the normal optimize execution point.",
        )

    def test_collection_writes_use_the_durable_coordinator(self) -> None:
        service = _source("image_vector_service/service.py")
        annotation = _source("image_vector_service/annotation_service.py")

        constructor = _method(service, "ImageVectorService", "__init__")
        self.assertTrue(
            _call_sites(constructor, "CollectionWriteCoordinator"),
            "ImageVectorService must create the durable CollectionWriteCoordinator "
            "before any index or tagging task can mutate Zvec.",
        )

        index_method = _method(service, "ImageVectorService", "_index")
        index_upsert = _method(service, "ImageVectorService", "_upsert_groups")
        for method, coordinator_call in (
            (index_method, "delete"),
            (index_upsert, "upsert"),
        ):
            calls = [
                call
                for call in _call_sites(method, coordinator_call)
                if (_call_name(call) or "").endswith(
                    f"collection_writes.{coordinator_call}"
                )
            ]
            self.assertTrue(
                calls,
                f"{method.name} must route Collection {coordinator_call} through "
                "self.collection_writes so the outbox can recover a crash.",
            )
            mutations = _repository_mutations(method)
            self.assertEqual(
                mutations,
                [],
                f"{method.name} bypasses CollectionWriteCoordinator at "
                f"{_format_accesses(mutations)}. Use PreparedCollectionUpsert and "
                "collection_writes.upsert/delete instead.",
            )

        auto_tag_constructors = [
            call
            for call in _call_sites(constructor, "AutoTaggingCoordinator")
            if _keyword(call, "collection_writes") is not None
        ]
        self.assertEqual(
            len(auto_tag_constructors),
            1,
            "ImageVectorService must construct AutoTaggingCoordinator once and inject "
            "the durable Collection write coordinator.",
        )
        auto_tag_constructor = auto_tag_constructors[0]
        injected_writer = _keyword(auto_tag_constructor, "collection_writes")
        self.assertIsNotNone(injected_writer)
        assert injected_writer is not None
        self.assertEqual(
            _dotted_name(injected_writer),
            "self.collection_writes",
            "ImageVectorService must inject its durable coordinator into auto tagging.",
        )

        manual_tags = _method(
            service,
            "ImageVectorService",
            "_apply_manual_tag_plans",
        )
        self.assertTrue(
            any(
                (_call_name(call) or "").endswith("collection_writes.upsert")
                for call in _call_sites(manual_tags, "upsert")
            ),
            "Manual-tag Collection writes must use the injected durable coordinator "
            "on the production ImageVectorService path.",
        )

        review_write = _method(
            annotation,
            "AutoTaggingCoordinator",
            "_write_review_document",
        )
        self.assertTrue(
            any(
                (_call_name(call) or "").endswith("collection_writes.upsert")
                for call in _call_sites(review_write, "upsert")
            ),
            "Auto-tag review writes must use collection_writes.upsert in production.",
        )
        annotation_mutations = _repository_mutations(
            _class(annotation, "AutoTaggingCoordinator")
        )
        self.assertEqual(
            annotation_mutations,
            [],
            "AutoTaggingCoordinator must never bypass the durable Collection writer. "
            "Unexpected direct repository mutations: "
            + _format_accesses(annotation_mutations),
        )

    def test_collection_write_batches_are_bounded_between_100_and_500(self) -> None:
        source = _source("image_vector_service/collection_write_coordinator.py")
        default_values: list[int] = []
        for node in source.tree.body:
            if not isinstance(node, ast.Assign) or not any(
                isinstance(target, ast.Name)
                and target.id == "DEFAULT_COLLECTION_WRITE_BATCH_SIZE"
                for target in node.targets
            ):
                continue
            if not isinstance(node.value, ast.Constant):
                continue
            raw_default = node.value.value
            if isinstance(raw_default, int) and not isinstance(raw_default, bool):
                default_values.append(raw_default)
        self.assertEqual(
            len(default_values),
            1,
            "Keep one literal DEFAULT_COLLECTION_WRITE_BATCH_SIZE so its memory and "
            "transaction bound remains reviewable.",
        )
        default = default_values[0]
        self.assertTrue(
            100 <= default <= 500,
            "Collection write batches must remain within 100..500 items. Smaller "
            "batches amplify transactions; larger batches break the memory/latency "
            f"budget. Current default: {default}.",
        )

        constructor = _method(source, "CollectionWriteCoordinator", "__init__")
        comparisons = [
            candidate
            for candidate in ast.walk(constructor)
            if isinstance(candidate, ast.Compare)
            and any(
                isinstance(item, ast.Name) and item.id == "batch_size"
                for item in ast.walk(candidate)
            )
        ]
        rendered = {ast.unparse(item).replace(" ", "") for item in comparisons}
        self.assertIn(
            "100<=batch_size<=500",
            rendered,
            "CollectionWriteCoordinator.__init__ must reject batch sizes outside "
            "100..500 instead of trusting callers.",
        )

        for method_name in ("upsert", "delete"):
            method = _method(source, "CollectionWriteCoordinator", method_name)
            self.assertTrue(
                any(
                    isinstance(candidate, ast.For)
                    and isinstance(candidate.iter, ast.Call)
                    and _call_name(candidate.iter) == "range"
                    and any(
                        _dotted_name(argument) == "self.batch_size"
                        for argument in candidate.iter.args
                    )
                    for candidate in ast.walk(method)
                ),
                f"CollectionWriteCoordinator.{method_name} must chunk work with "
                "self.batch_size; never enqueue an unbounded caller list as one write.",
            )

    def test_desktop_search_is_source_only_and_persisted_as_pages(self) -> None:
        backend = _source("image_vector_service/backend_server.py")
        service = _source("image_vector_service/service.py")
        federated = _source("image_vector_service/federated_search.py")
        exporter = _source("image_vector_service/result_exporter.py")
        backend_constants = _module_integer_constants(backend)

        single = _method(backend, "BackendJobManager", "_execute_single")
        self.assertTrue(
            _is_false(_dict_value(single, "copy_files")),
            "Desktop single-library search must set copy_files=False. The result page "
            "must reference source images instead of copying every hit.",
        )
        self.assertTrue(
            _positive_bounded_preview(
                _dict_value(single, "report_result_limit"),
                backend_constants,
            ),
            "Desktop single-library search must retain only a bounded inline preview; "
            "arbitrary pages belong in results.sqlite3.",
        )
        single_search_calls = [
            call
            for call in ast.walk(single)
            if isinstance(call, ast.Call)
            and (_call_name(call) or "").split(".")[-1]
            in {
                "search_by_image",
                "search_by_image_and_text",
                "search_by_tags",
                "search_by_text",
            }
        ]
        self.assertEqual(
            len(single_search_calls),
            4,
            "Update this guard when adding a desktop search mode, and pass the same "
            "source-only bounded options to the new mode.",
        )
        self.assertTrue(
            all(_has_expanded_keyword(call, "common") for call in single_search_calls),
            "Every desktop single-library search mode must receive the common "
            "copy_files=False and report_result_limit options.",
        )

        cross_library = _method(
            backend,
            "BackendJobManager",
            "_execute_federated_search",
        )
        exports = _call_sites(cross_library, "export_federated_search")
        self.assertEqual(len(exports), 1)
        self.assertTrue(
            _is_false(_keyword(exports[0], "copy_files")),
            "Desktop cross-Collection search must remain source-only.",
        )
        self.assertTrue(
            _positive_bounded_preview(
                _keyword(exports[0], "report_result_limit"),
                backend_constants,
            ),
            "Desktop cross-Collection search must keep a bounded inline preview.",
        )

        for method_name in (
            "search_by_text",
            "search_by_tags",
            "search_by_image",
            "search_by_image_and_text",
        ):
            method = _method(service, "ImageVectorService", method_name)
            export_calls = _call_sites(method, "export_results")
            self.assertEqual(
                len(export_calls),
                1,
                f"ImageVectorService.{method_name} must have one export boundary.",
            )
            self.assertTrue(
                _is_name(_keyword(export_calls[0], "copy_files"), "copy_files"),
                f"ImageVectorService.{method_name} must forward copy_files instead "
                "of silently restoring copied-result behavior.",
            )
            self.assertTrue(
                _is_name(
                    _keyword(export_calls[0], "report_result_limit"),
                    "report_result_limit",
                ),
                f"ImageVectorService.{method_name} must forward the preview bound.",
            )

        federated_export = _function(federated, "export_federated_search")
        export_calls = _call_sites(federated_export, "export_results")
        self.assertEqual(len(export_calls), 1)
        self.assertTrue(
            _is_name(_keyword(export_calls[0], "copy_files"), "copy_files")
            and _is_name(
                _keyword(export_calls[0], "report_result_limit"),
                "report_result_limit",
            ),
            "Federated export must forward source-only and inline-preview options to "
            "the common paged exporter.",
        )

        export_results = _function(exporter, "export_results")
        self.assertEqual(
            len(_call_sites(export_results, "write_result_store")),
            1,
            "export_results must persist the authoritative result sequence in "
            "results.sqlite3. Never put an unbounded result list back into JSON.",
        )


if __name__ == "__main__":
    unittest.main()
