#!/usr/bin/env python3
"""
run_analysis_split.py — Run the analysis jar over per-subrepo rows from split_failed.py.

For each row in the split CSV (repo, subrepo, source_path) executes:

    java -jar <jar> -f json -n <repo>__<subrepo> -i <source_path> -o <output_dir>/<repo>/

with <dataset_dir> as the working directory.

Resume: skips rows whose output file <output_dir>/<repo>/<repo>__<subrepo>.json exists.
Failures are logged to <output_dir>/_failed.txt as "<repo>__<subrepo>" tokens.

Usage:
  python run_analysis_split.py --csv failed_split.csv --jar saboroot.jar
  python run_analysis_split.py --csv failed_split.csv --jar saboroot.jar \\
      --dataset-dir repos/src/java --output-dir results
  python run_analysis_split.py --csv failed_split.csv --jar saboroot.jar \\
      --retry-failed --skip-failed
  python run_analysis_split.py --csv failed_split.csv --jar saboroot.jar \\
      --only owner__repoA owner__repoB
  python run_analysis_split.py --csv failed_split.csv --jar saboroot.jar \\
      --dry-run
"""

import argparse
import csv
import subprocess
import sys
import time
import random
from pathlib import Path


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Run analysis jar over subrepo rows from a split CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--csv",         type=Path, required=True,  metavar="FILE",
                   help="Split CSV from split_failed.py (repo, subrepo, source_path).")
    p.add_argument("--jar",         type=Path, required=True,  metavar="FILE",
                   help="Path to the analysis jar.")
    p.add_argument("--dataset-dir", type=Path, default=Path("repos/src/java"), metavar="DIR",
                   help="Working directory for java invocations (default: repos/src/java).")
    p.add_argument("--output-dir",  type=Path, default=Path("results"),        metavar="DIR",
                   help="Root output directory. Each repo gets a subdirectory. "
                        "(default: results)")
    p.add_argument("--timeout",     type=int,  default=600,   metavar="SECS",
                   help="Per-subrepo timeout in seconds (default: 600).")
    p.add_argument("--delay",       type=float, default=0.0,  metavar="SECS",
                   help="Base delay between runs (default: 0). Jittered ±30%%.")
    p.add_argument("--java",        type=str,  default="java", metavar="CMD",
                   help="Java executable (default: java).")
    p.add_argument("--extra-args",  type=str,  default="",    metavar="ARGS",
                   help="Extra arguments appended to every jar invocation.")
    p.add_argument("--only",        nargs="+", metavar="REPO",
                   help="Process only rows whose repo matches these names.")
    p.add_argument("--retry-failed", action="store_true",
                   help="Re-run entries in <output_dir>/_failed.txt, "
                        "ignoring existing output files for those entries.")
    p.add_argument("--skip-failed",  action="store_true",
                   help="Skip entries listed in <output_dir>/_failed.txt.")
    p.add_argument("--dry-run",     action="store_true",
                   help="Print commands without executing them.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

def load_csv(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        sys.exit(f"Error: CSV not found: {csv_path}")
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"Error: CSV is empty: {csv_path}")
    for col in ("repo", "subrepo", "source_path"):
        if col not in rows[0]:
            sys.exit(f"Error: CSV missing column '{col}'. "
                     f"Columns found: {list(rows[0].keys())}")
    return rows


# ---------------------------------------------------------------------------
# Key and path helpers
# ---------------------------------------------------------------------------

def entry_key(row: dict) -> str:
    """Unique key for a subrepo row, used in _failed.txt and as output filename."""
    return f"{row['repo']}__{row['subrepo']}"


def output_path(output_dir: Path, row: dict) -> Path:
    return output_dir / row["repo"] / f"{row['subrepo']}.json"


# ---------------------------------------------------------------------------
# Failure log helpers
# ---------------------------------------------------------------------------

def load_failed(output_dir: Path) -> set[str]:
    f = output_dir / "_failed.txt"
    if not f.exists():
        return set()
    return {l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip()}


def append_failed(output_dir: Path, key: str):
    with open(output_dir / "_failed.txt", "a", encoding="utf-8") as f:
        f.write(key + "\n")


def remove_from_failed(output_dir: Path, key: str):
    f = output_dir / "_failed.txt"
    if not f.exists():
        return
    lines = [l for l in f.read_text().splitlines()
             if l.strip() and l.strip() != key]
    f.write_text("\n".join(lines) + ("\n" if lines else ""))


# ---------------------------------------------------------------------------
# Single-row execution
# ---------------------------------------------------------------------------

def run_one(row: dict, args: argparse.Namespace, force: bool = False) -> tuple[str, bool, str]:
    key      = entry_key(row)
    out      = output_path(args.output_dir, row)
    repo_dir = args.output_dir / row["repo"]

    if not force and out.exists():
        return key, True, "skipped (already done)"

    repo_dir.mkdir(parents=True, exist_ok=True)

    extra = args.extra_args.split() if args.extra_args.strip() else []

    cmd = [
        args.java, "-jar", str(args.jar.resolve()),
        "-f", "json",
        "-n", row["subrepo"],
        "-i", row["source_path"],
        "-o", str(repo_dir.resolve()),
        *extra,
    ]

    if args.dry_run:
        return key, True, f"[dry-run] {' '.join(cmd)}"

    try:
        result = subprocess.run(
            cmd,
            cwd=args.dataset_dir,
            timeout=args.timeout,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return key, True, "exit 0"
        else:
            detail = (result.stderr or result.stdout or "").strip()
            short  = detail[:200] + ("…" if len(detail) > 200 else "")
            return key, False, f"exit {result.returncode}: {short}"

    except subprocess.TimeoutExpired:
        return key, False, f"timeout after {args.timeout}s"
    except FileNotFoundError as e:
        return key, False, f"executable not found: {e}"
    except Exception as e:
        return key, False, str(e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if args.retry_failed and args.skip_failed:
        sys.exit("Error: --retry-failed and --skip-failed are mutually exclusive.")
    if not args.jar.exists():
        sys.exit(f"Error: jar not found: {args.jar}")
    if not args.dataset_dir.is_dir():
        sys.exit(f"Error: --dataset-dir not found: {args.dataset_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    rows = load_csv(args.csv)

    if args.only:
        only_set = set(args.only)
        rows = [r for r in rows if r["repo"] in only_set]
        missing = only_set - {r["repo"] for r in rows}
        if missing:
            print(f"Warning: --only names not found in CSV: {sorted(missing)}")

    force_set: set[str] = set()
    if args.retry_failed:
        force_set = load_failed(args.output_dir)
        if force_set:
            print(f"Retrying {len(force_set)} previously failed entry/entries.")
        else:
            print("No failed entries recorded; running normally.")

    skip_set: set[str] = set()
    if args.skip_failed:
        skip_set = load_failed(args.output_dir)
        if skip_set:
            print(f"Skipping {len(skip_set)} known-failed entry/entries.")

    # Build work list
    work: list[tuple[dict, bool]] = []
    already_done   = 0
    skipped_failed = 0

    for row in rows:
        key   = entry_key(row)
        force = key in force_set

        if key in skip_set:
            skipped_failed += 1
            continue
        if not force and output_path(args.output_dir, row).exists():
            already_done += 1
            continue
        work.append((row, force))

    print(f"CSV rows     : {len(rows)}")
    print(f"Already done : {already_done}")
    if skipped_failed:
        print(f"Skipped (known failed) : {skipped_failed}")
    print(f"To run       : {len(work)}")
    print(f"Dataset dir  : {args.dataset_dir}")
    print(f"Output dir   : {args.output_dir}")
    print(f"Timeout      : {args.timeout}s per entry")
    print()

    if not work:
        print("Nothing to do.")
        return

    if args.dry_run:
        print("[dry-run mode — no commands will execute]\n")

    succeeded = 0
    failed    = 0

    for idx, (row, force) in enumerate(work, 1):
        key = entry_key(row)
        print(f"[{idx}/{len(work)}] {key}", end=" … ", flush=True)

        key, ok, msg = run_one(row, args, force=force)
        print(f"{'✓' if ok else '✗'} {msg}")

        if ok:
            succeeded += 1
            if key in force_set:
                remove_from_failed(args.output_dir, key)
        else:
            failed += 1
            if not args.dry_run:
                append_failed(args.output_dir, key)

        if args.delay > 0 and idx < len(work):
            time.sleep(args.delay * random.uniform(0.7, 1.3))

    print(f"\n{'='*50}")
    print(f"Done. {succeeded} succeeded, {failed} failed.")
    if failed and not args.dry_run:
        print(f"Failed entries logged to: {args.output_dir / '_failed.txt'}")
        print("Re-run failures with: --retry-failed")


if __name__ == "__main__":
    main()
