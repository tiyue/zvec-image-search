from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from image_vector_service import ImageVectorService
from image_vector_service.config import ConfigurationError, ServiceConfig
from image_vector_service.dashscope_client import DashScopeError


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
    search.add_argument("--top-k", type=int, default=10, help="Number of images.")
    search.add_argument("--image-weight", type=float, default=0.5)
    search.add_argument("--text-weight", type=float, default=0.5)
    search.add_argument("--include-self", action="store_true")

    subparsers.add_parser("stats", help="Show collection statistics.")
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
        service = ImageVectorService(
            config=config,
            progress=lambda message: print(message, flush=True),
        )
        if args.command in {"index", "sync"}:
            if args.command == "index":
                index_report = service.index_folder(
                    args.folder,
                    recursive=not args.no_recursive,
                    verify_hash=args.verify_hash,
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
                top_k=args.top_k,
                image_weight=args.image_weight,
                text_weight=args.text_weight,
                include_self=args.include_self,
            )
        elif args.image:
            search_report = service.search_by_image(
                image_path=args.image,
                top_k=args.top_k,
                include_self=args.include_self,
            )
        else:
            search_report = service.search_by_text(args.text, top_k=args.top_k)
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
