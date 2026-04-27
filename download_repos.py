#!/usr/bin/env python3
"""
download_repos.py — Download and extract StereoCode benchmark repos from GitHub.

Usage examples:
  python download_repos.py --lang java
  python download_repos.py --lang java c#
  python download_repos.py --lang all --limit 50
  python download_repos.py --lang java --delay 2.5 --output-dir ./repos
  python download_repos.py --lang java --min-loc 1000 --max-size-kb 100000
  python download_repos.py --dry-run --lang all

Download uses the pinned SHA so results are reproducible regardless of branch movement.
Extraction runs in a background worker process so downloads continue uninterrupted.
The script is fully resumable: repos already downloaded (zip present) or already
extracted (target directory present) are skipped automatically.
"""

import argparse
import csv
import multiprocessing
import os
import random
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_LANGUAGES = {"java", "c#", "c++"}
CSV_DEFAULT = Path(__file__).parent / "repos.csv"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Download StereoCode benchmark repositories from GitHub.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=CSV_DEFAULT,
        metavar="FILE",
        help="Path to repos.csv (default: repos.csv next to this script)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("repos"),
        metavar="DIR",
        help="Root output directory (default: ./repos). "
             "Zips go into <DIR>/zips/<language>/, "
             "extracted repos into <DIR>/src/<language>/.",
    )
    parser.add_argument(
        "--lang",
        nargs="+",
        default=["java"],
        metavar="LANG",
        help="Language(s) to download: java, c#, c++, all "
             "(case-insensitive, default: java)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        metavar="SECS",
        help="Base delay between downloads in seconds (default: 2.0). "
             "Actual delay is jittered ±30%% to be polite.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Stop after downloading N repos (across all selected languages).",
    )
    parser.add_argument(
        "--min-loc",
        type=int,
        default=None,
        metavar="N",
        help="Skip repos with fewer than N lines of code.",
    )
    parser.add_argument(
        "--max-loc",
        type=int,
        default=None,
        metavar="N",
        help="Skip repos with more than N lines of code.",
    )
    parser.add_argument(
        "--min-stars",
        type=int,
        default=None,
        metavar="N",
        help="Skip repos with fewer than N stars.",
    )
    parser.add_argument(
        "--max-size-kb",
        type=int,
        default=None,
        metavar="KB",
        help="Skip repos larger than KB kilobytes (uncompressed size from CSV).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        metavar="N",
        help="Number of download retries on transient errors (default: 3).",
    )
    parser.add_argument(
        "--extract-workers",
        type=int,
        default=2,
        metavar="N",
        help="Number of parallel extraction worker processes (default: 2).",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="Download zips only; do not extract.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be downloaded without actually downloading.",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Randomise download order (useful to interleave languages).",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# CSV loading and filtering
# ---------------------------------------------------------------------------

def load_repos(csv_path: Path, langs: set[str], args) -> list[dict]:
    """Load and filter repos from CSV."""
    if not csv_path.exists():
        sys.exit(f"Error: CSV file not found: {csv_path}")

    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["language"].lower() not in langs:
                continue

            loc = int(row.get("loc") or 0)
            stars = int(row.get("stars") or 0)
            size_kb = int(row.get("size (kb)") or 0)

            if args.min_loc is not None and loc < args.min_loc:
                continue
            if args.max_loc is not None and loc > args.max_loc:
                continue
            if args.min_stars is not None and stars < args.min_stars:
                continue
            if args.max_size_kb is not None and size_kb > args.max_size_kb:
                continue

            rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def zip_path(output_dir: Path, row: dict) -> Path:
    lang_slug = row["language"].lower().replace("+", "p").replace("#", "sharp")
    return output_dir / "zips" / lang_slug / f"{row['name']}__{row['sha'][:12]}.zip"


def extract_dir(output_dir: Path, row: dict) -> Path:
    lang_slug = row["language"].lower().replace("+", "p").replace("#", "sharp")
    return output_dir / "src" / lang_slug / row["name"]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_url_for(row: dict) -> str:
    """
    GitHub archive URL pinned to the exact SHA recorded in the CSV.
    Format: https://github.com/<owner>/<repo>/archive/<sha>.zip
    """
    owner = row["owner"].strip()
    name = row["name"].strip()
    sha = row["sha"].strip()
    return f"https://github.com/{owner}/{name}/archive/{sha}.zip"


def download_zip(url: str, dest: Path, retries: int = 3) -> bool:
    """
    Download url to dest. Returns True on success, False on permanent failure.
    Partial downloads land in dest.part and are atomically renamed on success.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "stereocode-benchmark-downloader/1.0"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp, \
                 open(part, "wb") as f:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk = 65536
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    downloaded += len(data)
                    if total:
                        pct = downloaded * 100 // total
                        print(f"\r    {pct:3d}% ({downloaded // 1024} KB)", end="", flush=True)
            print()
            part.rename(dest)
            return True

        except urllib.error.HTTPError as e:
            if e.code in (404, 451):           # permanent failures
                print(f"    HTTP {e.code} — skipping {url}")
                part.unlink(missing_ok=True)
                return False
            print(f"    HTTP {e.code} on attempt {attempt}/{retries}, retrying…")
        except Exception as e:
            print(f"    Error on attempt {attempt}/{retries}: {e}")

        if attempt < retries:
            time.sleep(5 * attempt)

    part.unlink(missing_ok=True)
    return False


# ---------------------------------------------------------------------------
# Extraction worker
# ---------------------------------------------------------------------------

def extraction_worker(queue: multiprocessing.Queue, output_dir: Path):
    """
    Runs in a separate process. Pulls (zip_path, extract_dir) tuples off the
    queue and extracts them. Sentinel value None signals shutdown.
    """
    while True:
        item = queue.get()
        if item is None:
            break
        zp, ed = item
        try:
            ed.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zp, "r") as zf:
                # GitHub zips have a single top-level dir; strip it
                members = zf.namelist()
                prefix = members[0] if members else ""
                for member in members:
                    rel = member[len(prefix):]
                    if not rel:
                        continue
                    target = ed / rel
                    if member.endswith("/"):
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(target, "wb") as dst:
                            dst.write(src.read())
            print(f"  [extract] ✓ {ed.name}")
        except Exception as e:
            print(f"  [extract] ✗ {ed.name}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    # Resolve languages
    selected = {l.lower() for l in args.lang}
    if "all" in selected:
        selected = VALID_LANGUAGES
    unknown = selected - VALID_LANGUAGES
    if unknown:
        sys.exit(f"Error: unknown language(s): {unknown}. Valid: {VALID_LANGUAGES | {'all'}}")

    print(f"Languages : {', '.join(sorted(selected))}")
    print(f"CSV       : {args.csv}")
    print(f"Output    : {args.output_dir}")

    repos = load_repos(args.csv, selected, args)
    print(f"Repos     : {len(repos)} after filters")

    if args.shuffle:
        random.shuffle(repos)
    if args.limit:
        repos = repos[: args.limit]
        print(f"Limit     : {args.limit}")

    if not repos:
        print("Nothing to download.")
        return

    # Summary table
    print()
    print(f"{'#':>4}  {'Name':<35} {'Lang':<6} {'LOC':>8}  Status")
    print("-" * 70)

    to_download = []
    for i, row in enumerate(repos, 1):
        zp = zip_path(args.output_dir, row)
        ed = extract_dir(args.output_dir, row)
        loc = int(row.get("loc") or 0)

        if ed.exists():
            status = "already extracted"
        elif zp.exists():
            status = "zip exists (pending extract)"
        else:
            status = "to download"
            to_download.append(row)

        print(f"{i:>4}  {row['name']:<35} {row['language']:<6} {loc:>8,}  {status}")

    print()
    print(f"{len(to_download)} repos to download, "
          f"{len(repos) - len(to_download)} already present.")

    if args.dry_run:
        print("\n[dry-run] No files written.")
        return

    if not to_download:
        print("Nothing to download — all repos already present.")
        return

    # Start extraction workers
    extract_queue = None
    workers = []
    if not args.no_extract:
        extract_queue = multiprocessing.Queue()
        for _ in range(args.extract_workers):
            p = multiprocessing.Process(
                target=extraction_worker,
                args=(extract_queue, args.output_dir),
                daemon=True,
            )
            p.start()
            workers.append(p)

    # Enqueue already-downloaded-but-not-yet-extracted zips
    if extract_queue:
        for row in repos:
            zp = zip_path(args.output_dir, row)
            ed = extract_dir(args.output_dir, row)
            if zp.exists() and not ed.exists():
                extract_queue.put((zp, ed))

    # Download loop
    failed = []
    for idx, row in enumerate(to_download, 1):
        zp = zip_path(args.output_dir, row)
        ed = extract_dir(args.output_dir, row)
        url = download_url_for(row)

        print(f"[{idx}/{len(to_download)}] {row['name']} ({row['language']}) "
              f"— SHA {row['sha'][:12]}")
        print(f"    {url}")

        ok = download_zip(url, zp, retries=args.retries)
        if ok:
            print(f"    ✓ saved to {zp}")
            if extract_queue:
                extract_queue.put((zp, ed))
        else:
            failed.append(row["name"])

        # Polite delay with ±30% jitter (skip after last item)
        if idx < len(to_download):
            jitter = random.uniform(0.7, 1.3)
            sleep_for = args.delay * jitter
            time.sleep(sleep_for)

    # Shut down extraction workers
    if extract_queue:
        print("\nWaiting for extraction workers to finish…")
        for _ in workers:
            extract_queue.put(None)
        for p in workers:
            p.join()

    # Final report
    print("\n" + "=" * 50)
    print(f"Done. {len(to_download) - len(failed)} downloaded, "
          f"{len(failed)} failed.")
    if failed:
        print("Failed repos:")
        for name in failed:
            print(f"  {name}")


if __name__ == "__main__":
    main()
