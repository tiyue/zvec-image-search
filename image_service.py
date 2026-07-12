from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from image_vector_service import ImageVectorService
from image_vector_service.config import ConfigurationError, ServiceConfig
from image_vector_service.dashscope_client import DashScopeError
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
        help="Tags applied to every indexed image in this root.",
    )
    index_parser.add_argument(
        "--clear-tags",
        action="store_true",
        help="Remove all tags from this indexed root.",
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
    search.add_argument("--image-weight", type=float, default=0.5)
    search.add_argument("--text-weight", type=float, default=0.5)
    search.add_argument("--include-self", action="store_true")

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = None
    try:
        config = (
            ServiceConfig(workspace=Path(args.workspace).expanduser().resolve())
            if args.workspace
            else ServiceConfig()
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
        if args.text and args.image:
            search_report = service.search_by_image_and_text(
                image_path=args.image,
                text=args.text,
                top_k=args.tk,
                image_weight=args.image_weight,
                text_weight=args.text_weight,
                include_self=args.include_self,
                tags=args.tags,
                tag_mode=args.tag_mode,
            )
        elif args.image:
            search_report = service.search_by_image(
                image_path=args.image,
                top_k=args.tk,
                include_self=args.include_self,
                tags=args.tags,
                tag_mode=args.tag_mode,
            )
        else:
            search_report = service.search_by_text(
                args.text,
                top_k=args.tk,
                tags=args.tags,
                tag_mode=args.tag_mode,
            )
        print(json.dumps(search_report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Completed records remain indexed.", file=sys.stderr)
        return 130
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
