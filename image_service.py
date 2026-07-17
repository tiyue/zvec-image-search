from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from image_vector_service import ImageVectorService
from image_vector_service.backend_instance_lock import BackendInstanceLockError
from image_vector_service.config import ConfigurationError, ServiceConfig
from image_vector_service.dashscope_client import DashScopeError
from image_vector_service.model_catalog import load_active_model_configuration
from image_vector_service.path_migration import migrate_schema


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local image vectorization and search with DashScope and Zvec."
    )
    parser.add_argument(
        "--workspace",
        help=(
            "Runtime workspace. Defaults to the project directory or "
            "ZVEC_IMAGE_WORKSPACE."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command, help_text in (
        ("index", "Add or update images in a folder."),
        ("sync", "Mirror a folder and delete missing image records."),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("folder", help="Local image folder path.")
        subparser.add_argument(
            "--no-recursive",
            action="store_true",
            help="Do not scan subfolders.",
        )
        subparser.add_argument(
            "--verify-hash",
            action="store_true",
            help=(
                "Recompute SHA-256 even when size and modification time are unchanged."
            ),
        )

    sync_parser = subparsers.choices["sync"]
    index_parser = subparsers.choices["index"]
    index_parser.add_argument(
        "tags",
        nargs="*",
        help="Tags applied only to images first added by this index run.",
    )
    index_parser.add_argument(
        "--clear-tags",
        action="store_true",
        help="Remove tags from all existing images in this indexed root.",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report stale records without deleting them.",
    )
    sync_parser.add_argument(
        "--allow-scope-change",
        action="store_true",
        help="Allow changing the recursive scope recorded for this root.",
    )

    search = subparsers.add_parser("search", help="Search by text, image, or both.")
    search.add_argument("--text", help="Text query.")
    search.add_argument("--image", help="Local query image path.")
    search.set_defaults(tk=10)
    search.add_argument("--tk", dest="tk", type=int, help="Number of images.")
    search.add_argument(
        "--top-k",
        dest="tk",
        type=int,
        help=argparse.SUPPRESS,
    )
    search.add_argument(
        "--tags",
        nargs="+",
        help="Only return documents matching these tags.",
    )
    search.add_argument(
        "--tag-mode",
        choices=("all", "any"),
        default="all",
        help="Require all tags or any tag (default: all).",
    )
    search.add_argument(
        "--search-mode",
        choices=("semantic", "tags"),
        default="semantic",
        help=(
            "Use semantic text embeddings or fuzzy local tag matching "
            "(default: semantic)."
        ),
    )
    search.add_argument("--image-weight", type=float, default=0.5)
    search.add_argument("--text-weight", type=float, default=0.5)
    search.add_argument("--include-self", action="store_true")
    search.add_argument(
        "--show-low-confidence",
        action="store_true",
        help=(
            "Return low-confidence candidates without threshold/gap filtering. "
            "Results are diagnostic and must not be treated as reliable matches."
        ),
    )
    search.add_argument(
        "--show-all-series",
        action="store_true",
        help=(
            "Disable the default series-diversity rerank and preserve the "
            "original relevance order."
        ),
    )

    subparsers.add_parser("stats", help="Show collection statistics.")
    subparsers.add_parser("roots", help="List registered image roots.")
    rebind = subparsers.add_parser(
        "rebind-root", help="Bind an indexed root to a new local folder."
    )
    rebind.add_argument("root_id", help="Stable root identifier shown by roots.")
    rebind.add_argument("folder", help="New local folder path.")
    for command in ("migrate-schema", "migrate-path-schema"):
        migrate = subparsers.add_parser(
            command,
            help=(
                "Migrate the Collection without regenerating embeddings."
                if command == "migrate-schema"
                else argparse.SUPPRESS
            ),
        )
        migrate.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and preview the migration without changing data.",
        )
    subparsers.add_parser("cache-clear", help="Delete cached query embeddings.")
    metadata_backfill = subparsers.add_parser(
        "metadata-backfill",
        help=(
            "Explicitly generate pending description embeddings and resume from "
            "already committed documents."
        ),
    )
    metadata_backfill.add_argument(
        "--max-images",
        type=int,
        default=200,
        help="Maximum model requests in this run (default: 200, maximum: 10000).",
    )
    clean = subparsers.add_parser(
        "clean-results", help="Delete old search result directories."
    )
    clean.add_argument(
        "--days",
        type=int,
        default=7,
        help="Delete result directories older than this many days (default: 7).",
    )
    clean.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report matching directories.",
    )
    serve = subparsers.add_parser(
        "serve", help="Run the persistent authenticated local HTTP backend."
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Listen address (default: 127.0.0.1).",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Listen port (default: 8765).",
    )
    serve.add_argument(
        "--token",
        help="Bearer token; prefer the ZVEC_BACKEND_TOKEN environment variable.",
    )
    serve.add_argument(
        "--query-root",
        help=(
            "Directory containing permitted search query images; defaults to "
            "<workspace>/query-staging."
        ),
    )
    serve.add_argument(
        "--libraries-config",
        help=(
            "Read-only schema v3 runtime library manifest; may also be set with "
            "ZVEC_LIBRARIES_CONFIG."
        ),
    )
    serve.add_argument(
        "--instance-id",
        help="Stable desktop backend instance identity; generated when omitted.",
    )
    serve.add_argument(
        "--config-fingerprint",
        help="Fingerprint of the effective desktop configuration.",
    )
    serve.add_argument(
        "--instance-lock-path",
        help=("Cross-process backend lock file; defaults to the active ConfigHome."),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = None
    try:
        model_configuration = load_active_model_configuration()
        config = (
            ServiceConfig(
                workspace=Path(args.workspace).expanduser().resolve(),
                model_configuration=model_configuration,
            )
            if args.workspace
            else ServiceConfig(model_configuration=model_configuration)
        )
        if args.command in {"migrate-schema", "migrate-path-schema"}:
            print(
                json.dumps(
                    migrate_schema(config, dry_run=args.dry_run),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "serve":
            from image_vector_service.backend_server import serve_backend

            token = (args.token or os.getenv("ZVEC_BACKEND_TOKEN", "")).strip()
            if not token:
                raise ConfigurationError(
                    "ZVEC_BACKEND_TOKEN or --token is required for serve."
                )
            serve_backend(
                host=args.host,
                port=args.port,
                token=token,
                instance_id=(args.instance_id or os.getenv("ZVEC_BACKEND_INSTANCE_ID")),
                config_fingerprint=(
                    args.config_fingerprint
                    or os.getenv("ZVEC_BACKEND_CONFIG_FINGERPRINT")
                ),
                instance_lock_path=(
                    args.instance_lock_path or os.getenv("ZVEC_BACKEND_LOCK_PATH")
                ),
                config=config,
                query_root=args.query_root,
                libraries_config=(
                    args.libraries_config or os.getenv("ZVEC_LIBRARIES_CONFIG")
                ),
            )
            return 0
        service = ImageVectorService(
            config=config,
            progress=lambda message: print(message, flush=True),
        )
        if args.command in {"index", "sync"}:
            if args.command == "index":
                if args.clear_tags and args.tags:
                    raise ValueError("Tags cannot be combined with --clear-tags.")
                index_report = service.index_folder(
                    args.folder,
                    recursive=not args.no_recursive,
                    verify_hash=args.verify_hash,
                    tags=[] if args.clear_tags else (args.tags or None),
                )
            else:
                index_report = service.sync_folder(
                    args.folder,
                    recursive=not args.no_recursive,
                    verify_hash=args.verify_hash,
                    dry_run=args.dry_run,
                    allow_scope_change=args.allow_scope_change,
                )
            print(json.dumps(index_report.to_dict(), ensure_ascii=False, indent=2))
            return 4 if index_report.failed else 0

        if args.command == "stats":
            print(json.dumps(service.stats(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "roots":
            print(json.dumps(service.list_roots(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "rebind-root":
            print(
                json.dumps(
                    service.rebind_root(args.root_id, args.folder),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "cache-clear":
            print(
                json.dumps(
                    service.clear_embedding_cache(), ensure_ascii=False, indent=2
                )
            )
            return 0

        if args.command == "metadata-backfill":
            report = service.backfill_metadata_embeddings(
                max_images=args.max_images,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 4 if report["failed"] else 0

        if args.command == "clean-results":
            print(
                json.dumps(
                    service.clean_results(args.days, dry_run=args.dry_run),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if not args.text and not args.image:
            raise ValueError("search requires --text, --image, or both.")
        if args.search_mode == "tags":
            if not args.text or args.image:
                raise ValueError(
                    "tag-only search requires --text and does not accept --image."
                )
            search_report = service.search_by_tags(
                args.text,
                top_k=args.tk,
                tags=args.tags,
                tag_mode=args.tag_mode,
                show_low_confidence=args.show_low_confidence,
                diversify_results=not args.show_all_series,
            )
        elif args.text and args.image:
            search_report = service.search_by_image_and_text(
                image_path=args.image,
                text=args.text,
                top_k=args.tk,
                image_weight=args.image_weight,
                text_weight=args.text_weight,
                include_self=args.include_self,
                tags=args.tags,
                tag_mode=args.tag_mode,
                show_low_confidence=args.show_low_confidence,
                diversify_results=not args.show_all_series,
            )
        elif args.image:
            search_report = service.search_by_image(
                image_path=args.image,
                top_k=args.tk,
                include_self=args.include_self,
                tags=args.tags,
                tag_mode=args.tag_mode,
                show_low_confidence=args.show_low_confidence,
                diversify_results=not args.show_all_series,
            )
        else:
            search_report = service.search_by_text(
                args.text,
                top_k=args.tk,
                tags=args.tags,
                tag_mode=args.tag_mode,
                show_low_confidence=args.show_low_confidence,
                diversify_results=not args.show_all_series,
            )
        print(json.dumps(search_report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed records remain indexed.", file=sys.stderr)
        return 130
    except BackendInstanceLockError as exc:
        print(
            json.dumps(
                {"event": "backend_start_failed", "error": exc.to_dict()},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 6
    except (ConfigurationError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1
    except DashScopeError as exc:
        print(f"DashScope error: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        print(f"Path error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 5
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
