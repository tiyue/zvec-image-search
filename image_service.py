from __future__ import annotations

import argparse
import json
import sys

from image_vector_service import ImageVectorService
from image_vector_service.config import ConfigurationError
from image_vector_service.dashscope_client import DashScopeError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local image vectorization and search with DashScope and Zvec."
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

    search = subparsers.add_parser("search", help="Search by text, image, or both.")
    search.add_argument("--text", help="Text query.")
    search.add_argument("--image", help="Local query image path.")
    search.add_argument("--top-k", type=int, default=10, help="Number of images.")
    search.add_argument("--image-weight", type=float, default=0.5)
    search.add_argument("--text-weight", type=float, default=0.5)
    search.add_argument("--include-self", action="store_true")

    subparsers.add_parser("stats", help="Show collection statistics.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        service = ImageVectorService(progress=lambda message: print(message, flush=True))
        if args.command in {"index", "sync"}:
            method = (
                service.index_folder if args.command == "index" else service.sync_folder
            )
            report = method(args.folder, recursive=not args.no_recursive)
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            return 4 if report.failed else 0

        if args.command == "stats":
            print(json.dumps(service.stats(), ensure_ascii=False, indent=2))
            return 0

        if not args.text and not args.image:
            raise ValueError("search requires --text, --image, or both.")
        if args.text and args.image:
            report = service.search_by_image_and_text(
                image_path=args.image,
                text=args.text,
                top_k=args.top_k,
                image_weight=args.image_weight,
                text_weight=args.text_weight,
                include_self=args.include_self,
            )
        elif args.image:
            report = service.search_by_image(
                image_path=args.image,
                top_k=args.top_k,
                include_self=args.include_self,
            )
        else:
            report = service.search_by_text(args.text, top_k=args.top_k)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
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


if __name__ == "__main__":
    raise SystemExit(main())
