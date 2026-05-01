#!/usr/bin/env python3
"""
run_index.py — Run an indexer executable over every repo in a source directory.

For each subdirectory <repo> under <sources>, executes:

    <exe> index <sources>/<repo> -o <output_dir>/<repo>.json

Resume: skips repos whose output file already exists.
Failures are logged to <output_dir>/_failed.txt.

Usage:
  python run_index.py --exe ./mytool --sources repos/src/java --output-dir results
  python run_index.py --exe ./mytool --sources repos/src/java --output-dir results \\
      --timeout 300 --delay 0.5
  python run_index.py --exe ./mytool --sources repos/src/java --output-dir results \\
      --retry-failed
  python run_index.py --exe ./mytool --sources repos/src/java --output-dir results \\
      --only owner__repoA owner__repoB
  python run_index.py --exe ./mytool --sources repos/src/java --output-dir results \\
      --dry-run
"""

import argparse
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
        description="Run an indexer executable over every repo in a source directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--exe",        type=Path, required=True, metavar="FILE",
                   help="Executable to run (e.g. ./saboroot or /usr/local/bin/mytool).")
    p.add_argument("--sources",    type=Path, required=True, metavar="DIR",
                   help="Directory containing extracted repo subdirectories.")
    p.add_argument("--output-dir", type=Path, required=True, metavar="DIR",
                   help="Directory where <repo>.json output files are written.")
    p.add_argument("--timeout",    type=int,  default=600,   metavar="SECS",
                   help="Per-repo timeout in seconds (default: 600).")
    p.add_argument("--delay",      type=float, default=0.0,  metavar="SECS",
                   help="Base delay between runs (default: 0). Jittered ±30%%.")
    p.add_argument("--only",       nargs="+", metavar="REPO",
                   help="Process only these repo directory names.")
    p.add_argument("--retry-failed", action="store_true",
                   help="Re-run repos in <output_dir>/_failed.txt, "
                        "ignoring existing output files for those repos.")
    p.add_argument("--dry-run",    action="store_true",
                   help="Print commands without executing them.")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Failure log helpers
# ---------------------------------------------------------------------------

def load_failed(output_dir: Path) -> set[str]:
    f = output_dir / "_failed.txt"
    if not f.exists():
        return set()
    return {l.strip() for l in f.read_text(encoding="utf-8").splitlines() if l.strip()}


def append_failed(output_dir: Path, repo: str):
    with open(output_dir / "_failed.txt", "a", encoding="utf-8") as f:
        f.write(repo + "\n")


def remove_from_failed(output_dir: Path, repo: str):
    f = output_dir / "_failed.txt"
    if not f.exists():
        return
    lines = [l for l in f.read_text().splitlines()
             if l.strip() and l.strip() != repo]
    f.write_text("\n".join(lines) + ("\n" if lines else ""))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if not args.exe.exists():
        sys.exit(f"Error: executable not found: {args.exe}")
    if not args.sources.is_dir():
        sys.exit(f"Error: --sources not found: {args.sources}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Collect repo dirs
    all_repos = sorted(d for d in args.sources.iterdir() if d.is_dir())

    if args.only:
        only_set = set(args.only)
        all_repos = [d for d in all_repos if d.name in only_set]
        missing = only_set - {d.name for d in all_repos}
        if missing:
            print(f"Warning: --only names not found in sources: {sorted(missing)}")

    force_set: set[str] = set()
    if args.retry_failed:
        force_set = load_failed(args.output_dir)
        if force_set:
            print(f"Retrying {len(force_set)} previously failed repo(s).")
        else:
            print("No failed repos recorded; running normally.")

    # Partition into to-run vs already-done
    to_run = []
    already_done = 0
    for repo_dir in all_repos:
        out = args.output_dir / f"{repo_dir.name}.json"
        if out.exists() and repo_dir.name not in force_set:
            already_done += 1
        else:
            to_run.append(repo_dir)

    print(f"Sources      : {args.sources}  ({len(all_repos)} repo dirs)")
    print(f"Output dir   : {args.output_dir}")
    print(f"Already done : {already_done}")
    print(f"To run       : {len(to_run)}")
    if args.timeout:
        print(f"Timeout      : {args.timeout}s per repo")
    print()

    if not to_run:
        print("Nothing to do.")
        return

    if args.dry_run:
        print("[dry-run mode — no commands will execute]\n")

    succeeded = 0
    failed    = 0

    for idx, repo_dir in enumerate(to_run, 1):
        repo = repo_dir.name
        out  = args.output_dir / f"{repo}.json"
        cmd  = [str(args.exe.resolve()), "index",
                str(repo_dir.resolve()),
                "-o", str(out.resolve())]

        print(f"[{idx}/{len(to_run)}] {repo}", end=" … ", flush=True)

        if args.dry_run:
            print(f"[dry-run] {' '.join(cmd)}")
            succeeded += 1
            continue

        try:
            result = subprocess.run(
                cmd,
                timeout=args.timeout,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print("✓")
                succeeded += 1
                if repo in force_set:
                    remove_from_failed(args.output_dir, repo)
            else:
                detail = (result.stderr or result.stdout or "").strip()
                short  = detail[:200] + ("…" if len(detail) > 200 else "")
                print(f"✗ exit {result.returncode}: {short}")
                failed += 1
                append_failed(args.output_dir, repo)

        except subprocess.TimeoutExpired:
            print(f"✗ timeout after {args.timeout}s")
            failed += 1
            append_failed(args.output_dir, repo)
        except FileNotFoundError as e:
            print(f"✗ executable not found: {e}")
            failed += 1
            append_failed(args.output_dir, repo)
            break  # no point continuing if the exe is missing
        except Exception as e:
            print(f"✗ {e}")
            failed += 1
            append_failed(args.output_dir, repo)

        if args.delay > 0 and idx < len(to_run):
            time.sleep(args.delay * random.uniform(0.7, 1.3))

    print(f"\n{'='*50}")
    print(f"Done. {succeeded} succeeded, {failed} failed.")
    if failed and not args.dry_run:
        print(f"Failed repos logged to: {args.output_dir / '_failed.txt'}")
        print("Re-run failures with:  --retry-failed")


if __name__ == "__main__":
    main()
