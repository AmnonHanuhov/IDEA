#!/usr/bin/env python3
"""
Split large files into smaller chunks.

Usage:
    python split_files.py <directory> [--size SIZE_MB] [--pattern GLOB] [--dry-run]

Arguments:
    directory       Directory containing files to split
    --size SIZE_MB  Target chunk size in MB (default: 10)
    --pattern       Glob pattern to match files (default: '*')
    --dry-run       Show what would be done without making changes
"""

import argparse
import glob
import os
import sys


def split_file(filepath: str, chunk_size: int, dry_run: bool) -> list[str]:
    """Split a single file into chunks of at most chunk_size bytes.

    Returns the list of output paths created.
    """
    file_size = os.path.getsize(filepath)
    if file_size <= chunk_size:
        return []

    base = filepath
    outputs = []
    part = 0

    if not dry_run:
        with open(filepath, "rb") as src:
            while True:
                data = src.read(chunk_size)
                if not data:
                    break
                out_path = f"{base}.part{part:03d}"
                outputs.append(out_path)
                with open(out_path, "wb") as dst:
                    dst.write(data)
                part += 1
    else:
        num_parts = (file_size + chunk_size - 1) // chunk_size
        for i in range(num_parts):
            outputs.append(f"{base}.part{i:03d}")

    return outputs


def main():
    parser = argparse.ArgumentParser(description="Split large files into smaller chunks.")
    parser.add_argument("directory", help="Directory containing files to split")
    parser.add_argument(
        "--size", type=float, default=10.0, metavar="SIZE_MB",
        help="Target chunk size in MB (default: 10)",
    )
    parser.add_argument(
        "--pattern", default="*", help="Glob pattern to match files (default: '*')"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without changes"
    )
    args = parser.parse_args()

    directory = os.path.abspath(args.directory)
    if not os.path.isdir(directory):
        print(f"Error: '{directory}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    chunk_size = int(args.size * 1024 * 1024)
    pattern = os.path.join(directory, args.pattern)
    candidates = sorted(
        p for p in glob.glob(pattern) if os.path.isfile(p)
    )

    if not candidates:
        print(f"No files matched pattern '{args.pattern}' in '{directory}'.")
        return

    print(
        f"{'[DRY RUN] ' if args.dry_run else ''}Splitting {len(candidates)} file(s) "
        f"into {args.size} MB chunks in '{directory}'"
    )

    total_split = 0
    total_skipped = 0

    for i, filepath in enumerate(candidates, 1):
        file_size = os.path.getsize(filepath)
        mb = file_size / (1024 * 1024)
        name = os.path.basename(filepath)

        if file_size <= chunk_size:
            print(f"  [{i}/{len(candidates)}] SKIP {name} ({mb:.1f} MB) — already small enough")
            total_skipped += 1
            continue

        outputs = split_file(filepath, chunk_size, args.dry_run)
        print(
            f"  [{i}/{len(candidates)}] SPLIT {name} ({mb:.1f} MB) "
            f"→ {len(outputs)} parts"
        )

        if not args.dry_run:
            os.remove(filepath)

        total_split += 1

    print(
        f"\nDone. Split: {total_split}, Skipped (already small): {total_skipped}"
        + (" (dry run — no changes made)" if args.dry_run else "")
    )


if __name__ == "__main__":
    main()
