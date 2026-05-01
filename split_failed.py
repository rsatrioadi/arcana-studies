#!/usr/bin/env python3
"""
split_failed.py — Expand failed repos from roots CSV into one row per source path.

Reads _failed.txt (one repo name per line) and java_roots.csv, filters to the
failed repos, then splits each multi-path row into individual (repo, subrepo,
source_path) rows.

Subrepo is extracted as the first path component after stripping the repo prefix:
  owner__repo/app/src/main/java   → subrepo = app
  owner__repo/lib/src/main/java   → subrepo = lib
  owner__repo/src/main/java       → subrepo = owner__repo  (single-module fallback)

Usage:
  python split_failed.py --failed results/_failed.txt --csv java_roots.csv
  python split_failed.py --failed results/_failed.txt --csv java_roots.csv \\
      --output failed_split.csv
"""

import argparse
import csv
import sys
from pathlib import Path


# Path components that indicate a source root with no meaningful module prefix.
# When the first component after stripping the repo prefix is one of these,
# the repo is treated as single-module and subrepo falls back to the repo name.
SOURCE_INDICATORS = {"src", "source", "sources", "java", "main", "kotlin"}


def parse_args():
    p = argparse.ArgumentParser(
        description="Expand failed-repo rows into one row per source path.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--failed", type=Path, required=True, metavar="FILE",
                   help="_failed.txt produced by run_analysis.py.")
    p.add_argument("--csv", type=Path, required=True, metavar="FILE",
                   help="java_roots.csv produced by find_java_roots.py.")
    p.add_argument("--output", type=Path, default=Path("failed_split.csv"),
                   metavar="FILE",
                   help="Output CSV path (default: failed_split.csv).")
    return p.parse_args()


def extract_subrepo(repo: str, source_path: str) -> str:
    """
    Derive the subrepo name from a single source_path string.

    source_path looks like:  owner__repo/module/src/main/java
    Strip the leading `repo/` prefix, then take the first component.
    If that component is a known source-dir indicator (src, java, …),
    the repo has no meaningful module level → fall back to repo name.
    """
    prefix = repo + "/"
    remainder = source_path[len(prefix):] if source_path.startswith(prefix) else source_path
    first = Path(remainder).parts[0] if remainder else ""
    if not first or first.lower() in SOURCE_INDICATORS:
        return repo
    return first


def main():
    args = parse_args()

    for path, label in [(args.failed, "--failed"), (args.csv, "--csv")]:
        if not path.exists():
            sys.exit(f"Error: {label} file not found: {path}")

    # Load failed repo names
    failed = {
        line.strip()
        for line in args.failed.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    if not failed:
        sys.exit(f"Error: {args.failed} is empty.")

    # Load roots CSV, filter to failed repos
    with open(args.csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for col in ("repo", "source_paths"):
        if col not in (rows[0] if rows else {}):
            sys.exit(f"Error: CSV missing column '{col}'.")

    matched = {r["repo"]: r["source_paths"] for r in rows if r["repo"] in failed}

    not_found = failed - matched.keys()
    if not_found:
        print(f"Warning: {len(not_found)} failed repo(s) not found in CSV:")
        for name in sorted(not_found):
            print(f"  {name}")

    # Build expanded rows
    out_rows = []
    for repo, source_paths_cell in sorted(matched.items()):
        paths = [p.strip() for p in source_paths_cell.split("+") if p.strip()]
        if not paths:
            print(f"Warning: {repo} has no source paths in CSV — skipping.")
            continue
        for sp in paths:
            subrepo = extract_subrepo(repo, sp)
            out_rows.append((repo, subrepo, sp))

    # Write output
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["repo", "subrepo", "source_path"])
        writer.writerows(out_rows)

    print(f"Expanded {len(matched)} repo(s) into {len(out_rows)} row(s) → {args.output}")


if __name__ == "__main__":
    main()
