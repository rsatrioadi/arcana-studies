#!/usr/bin/env python3
"""
extract_graph_java_v2.py — Run a Java analysis jar over repos listed in a roots CSV.

Reads the CSV produced by find_java_roots.py and for each row executes:

    java -jar <jar> -f json -n <repo> -i <src1+src2+...> -o <output_dir>

with <dataset_dir> as the working directory.

Resume: a repo is skipped if <output_dir>/<repo>.json already exists.
Failed repos are logged to <output_dir>/_failed.txt for easy re-running.

Usage:
  python extract_graph_java_v2.py --csv java_roots.csv --jar saboroot.jar
  python extract_graph_java_v2.py --csv java_roots.csv --jar saboroot.jar \\
      --dataset-dir repos/src/java --output-dir results
  python extract_graph_java_v2.py --csv java_roots.csv --jar saboroot.jar \\
      --delay 0.5 --timeout 300 --workers 4
  python extract_graph_java_v2.py --csv java_roots.csv --jar saboroot.jar \\
      --only acme__myapp microsoft__vscode-java   # run specific repos
  python extract_graph_java_v2.py --csv java_roots.csv --jar saboroot.jar \\
      --retry-failed                              # re-run _failed.txt entries
"""

import argparse
import csv
import subprocess
import sys
import time
import random
import concurrent.futures
from pathlib import Path


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Run a Java analysis jar over repos from a roots CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--csv",         type=Path, required=True,  metavar="FILE",
                   help="Roots CSV from find_java_roots.py (repo, source_paths).")
    p.add_argument("--jar",         type=Path, required=True,  metavar="FILE",
                   help="Path to the analysis jar.")
    p.add_argument("--dataset-dir", type=Path, default=Path("repos/src/java"), metavar="DIR",
                   help="Working directory for java invocations (default: repos/src/java).")
    p.add_argument("--output-dir",  type=Path, default=Path("results"),        metavar="DIR",
                   help="Output directory passed as -o to the jar (default: results).")
    p.add_argument("--delay",       type=float, default=0.0, metavar="SECS",
                   help="Base delay between runs in seconds (default: 0). "
                        "Actual delay is jittered ±30%%.")
    p.add_argument("--timeout",     type=int,   default=600,  metavar="SECS",
                   help="Per-repo timeout in seconds (default: 600).")
    p.add_argument("--workers",     type=int,   default=1,    metavar="N",
                   help="Parallel workers (default: 1). Use >1 only if the jar "
                        "is stateless and output files don't collide.")
    p.add_argument("--java",        type=str,   default="java", metavar="CMD",
                   help="Java executable (default: java). Override if using a "
                        "specific JDK, e.g. /usr/lib/jvm/java-17/bin/java.")
    p.add_argument("--extra-args",  type=str,   default="",   metavar="ARGS",
                   help="Extra arguments appended to every jar invocation "
                        "(shell-split, e.g. '--verbose --strict').")
    p.add_argument("--only",        nargs="+",  metavar="REPO",
                   help="Process only these repo names (overrides CSV order).")
    p.add_argument("--skip-empty",  action="store_true",
                   help="Skip repos with empty source_paths (default: warn and skip).")
    p.add_argument("--retry-failed", action="store_true",
                   help="Re-run repos listed in <output_dir>/_failed.txt, "
                        "ignoring existing output files for those repos.")
    p.add_argument("--skip-failed", action="store_true",
                   help="Skip repos listed in <output_dir>/_failed.txt "
                        "without retrying them.")
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
    for col in ("repo", "source_paths"):
        if col not in rows[0]:
            sys.exit(f"Error: CSV missing column '{col}'. "
                     f"Columns found: {list(rows[0].keys())}")
    return rows


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------

def output_path(output_dir: Path, repo: str) -> Path:
    return output_dir / f"{repo}.json"


def load_failed(output_dir: Path) -> set[str]:
    failed_file = output_dir / "_failed.txt"
    if not failed_file.exists():
        return set()
    return {line.strip() for line in failed_file.read_text().splitlines() if line.strip()}


def append_failed(output_dir: Path, repo: str):
    failed_file = output_dir / "_failed.txt"
    with open(failed_file, "a", encoding="utf-8") as f:
        f.write(repo + "\n")


def remove_from_failed(output_dir: Path, repo: str):
    failed_file = output_dir / "_failed.txt"
    if not failed_file.exists():
        return
    lines = [l for l in failed_file.read_text().splitlines()
             if l.strip() and l.strip() != repo]
    failed_file.write_text("\n".join(lines) + ("\n" if lines else ""))


# ---------------------------------------------------------------------------
# Single-repo execution
# ---------------------------------------------------------------------------

def run_one(repo: str, source_paths: str, args: argparse.Namespace,
            force: bool = False) -> tuple[str, bool, str]:
    """
    Returns (repo, success, message).
    force=True skips the already-done check (used by --retry-failed).
    """
    out_path = output_path(args.output_dir, repo)

    if not force and out_path.exists():
        return repo, True, "skipped (already done)"

    if not source_paths.strip():
        return repo, False, "skipped (empty source_paths)"

    extra = args.extra_args.split() if args.extra_args.strip() else []

    cmd = [
        args.java, "-jar", str(args.jar.resolve()),
        "-f", "json",
        "-n", repo,
        "-i", source_paths,
        "-o", str(args.output_dir.resolve()),
        *extra,
    ]

    if args.dry_run:
        cmd_str = " ".join(cmd)
        return repo, True, f"[dry-run] {cmd_str}"

    try:
        result = subprocess.run(
            cmd,
            cwd=args.dataset_dir,
            timeout=args.timeout,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return repo, True, f"exit 0"
        else:
            detail = (result.stderr or result.stdout or "").strip()
            short = detail[:200] + ("…" if len(detail) > 200 else "")
            return repo, False, f"exit {result.returncode}: {short}"

    except subprocess.TimeoutExpired:
        return repo, False, f"timeout after {args.timeout}s"
    except FileNotFoundError as e:
        return repo, False, f"executable not found: {e}"
    except Exception as e:
        return repo, False, str(e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    # Validate paths
    if not args.jar.exists():
        sys.exit(f"Error: jar not found: {args.jar}")
    if not args.dataset_dir.is_dir():
        sys.exit(f"Error: --dataset-dir not found: {args.dataset_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    rows = load_csv(args.csv)

    # Filter to --only if given
    if args.only:
        only_set = set(args.only)
        rows = [r for r in rows if r["repo"] in only_set]
        missing = only_set - {r["repo"] for r in rows}
        if missing:
            print(f"Warning: --only names not found in CSV: {sorted(missing)}")

    # Determine which repos to force-retry
    force_set: set[str] = set()
    if args.retry_failed:
        force_set = load_failed(args.output_dir)
        if force_set:
            print(f"Retrying {len(force_set)} previously failed repo(s).")
        else:
            print("No failed repos recorded; running normally.")

    skip_set: set[str] = set()
    if args.skip_failed:
        if args.retry_failed:
            sys.exit("Error: --retry-failed and --skip-failed are mutually exclusive.")
        skip_set = load_failed(args.output_dir)
        if skip_set:
            print(f"Skipping {len(skip_set)} known-failed repo(s).")

    # Build work list with status annotation for the summary table
    work: list[tuple[dict, bool]] = []          # (row, force)
    already_done = 0
    empty_paths = 0
    skipped_failed = 0

    for row in rows:
        repo = row["repo"]
        src  = row["source_paths"].strip()
        force = repo in force_set

        if not src:
            empty_paths += 1
            continue
        if repo in skip_set:
            skipped_failed += 1
            continue
        if not force and output_path(args.output_dir, repo).exists():
            already_done += 1
            continue
        work.append((row, force))

    total = len(rows)
    print(f"CSV repos    : {total}")
    print(f"Already done : {already_done}")
    print(f"Empty paths  : {empty_paths}")
    if skipped_failed:
        print(f"Skipped (known failed) : {skipped_failed}")
    print(f"To run       : {len(work)}")
    print(f"Dataset dir  : {args.dataset_dir}")
    print(f"Output dir   : {args.output_dir}")
    print(f"Workers      : {args.workers}")
    if args.timeout:
        print(f"Timeout      : {args.timeout}s per repo")
    print()

    if not work:
        print("Nothing to do.")
        return

    if args.dry_run:
        print("[dry-run mode — no commands will execute]\n")

    succeeded = 0
    failed    = 0

    def process(item):
        row, force = item
        return run_one(row["repo"], row["source_paths"], args, force=force)

    if args.workers > 1:
        executor_cls = concurrent.futures.ThreadPoolExecutor
        with executor_cls(max_workers=args.workers) as ex:
            futures = {ex.submit(process, item): item for item in work}
            for idx, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                repo, ok, msg = fut.result()
                tag = "✓" if ok else "✗"
                print(f"[{idx}/{len(work)}] {tag} {repo} — {msg}")
                if ok:
                    succeeded += 1
                    if repo in force_set:
                        remove_from_failed(args.output_dir, repo)
                else:
                    failed += 1
                    if not args.dry_run:
                        append_failed(args.output_dir, repo)
    else:
        for idx, item in enumerate(work, 1):
            row, force = item
            repo = row["repo"]
            print(f"[{idx}/{len(work)}] {repo}", end=" … ", flush=True)

            repo, ok, msg = run_one(repo, row["source_paths"], args, force=force)
            tag = "✓" if ok else "✗"
            print(f"{tag} {msg}")

            if ok:
                succeeded += 1
                if repo in force_set:
                    remove_from_failed(args.output_dir, repo)
            else:
                failed += 1
                if not args.dry_run:
                    append_failed(args.output_dir, repo)

            if args.delay > 0 and idx < len(work):
                time.sleep(args.delay * random.uniform(0.7, 1.3))

    print(f"\n{'='*50}")
    print(f"Done. {succeeded} succeeded, {failed} failed.")
    if failed and not args.dry_run:
        print(f"Failed repos logged to: {args.output_dir / '_failed.txt'}")
        print("Re-run failures with: --retry-failed")


if __name__ == "__main__":
    main()
