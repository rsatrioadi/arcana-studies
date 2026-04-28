#!/usr/bin/env python3
"""
find_java_roots.py — Locate Java source roots in extracted repos and write a CSV.

For each repo under <src-dir>/<lang>/<owner__name>/ the script:
  1. Walks every .java file (excluding generated/compiled output dirs).
  2. Reads each file's `package` declaration and back-computes the candidate
     source root by stripping (package components + filename) from the path.
  3. Tallies votes per candidate root; roots with ≥ MIN_FILES votes survive.
  4. Falls back to counting no-package files clustered in the same directory.
  5. Removes ancestor roots when a more-specific descendant root also qualifies.
  6. Writes CSV:  repo_name,"root1+root2+..."

Usage:
  python find_java_roots.py --src-dir repos/src/java
  python find_java_roots.py --src-dir repos/src/java --include-tests --output roots.csv
  python find_java_roots.py --src-dir repos/src/java --verbose
  python find_java_roots.py --src-dir /tmp/test_repos  # works on the test repo too
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants / heuristics
# ---------------------------------------------------------------------------

# Directories whose subtrees are unconditionally ignored.
PRUNE_DIRS = {
    "target",
    "build",
    "out",
    "dist",
    "bin",
    "output",
    ".gradle",
    ".mvn",
    "generated",
    "generated-sources",
    "generated-test-sources",
    "apt-generated",
    "node_modules",
    "vendor",
    ".git",
    ".idea",
    ".vscode",
    "__pycache__",
    "test-output",
    "site",
    "reports",
}

# Path segment patterns that mark test source trees.
TEST_SEGMENT_PATTERNS = re.compile(
    r"^(test|tests|androidTest|integrationTest|functionalTest|it)$",
    re.IGNORECASE,
)

# Package declaration: tolerates annotations, comments on preceding lines.
# We only scan the first MAX_HEADER_LINES lines for speed.
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;")
MAX_HEADER_LINES = 40

# A root must be supported by at least this many .java files.
MIN_FILES = 2


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(
        description="Find Java source roots in extracted repos and write CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--src-dir",
        type=Path,
        required=True,
        metavar="DIR",
        help="Directory containing owner__repo subdirectories (e.g. repos/src/java).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=Path("java_roots.csv"),
        metavar="FILE",
        help="Output CSV path (default: java_roots.csv).",
    )
    p.add_argument(
        "--include-tests",
        action="store_true",
        help="Include test source roots in output alongside main roots.",
    )
    p.add_argument(
        "--min-files",
        type=int,
        default=MIN_FILES,
        metavar="N",
        help=f"Minimum .java files required to confirm a root (default: {MIN_FILES}).",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-repo root detection details.",
    )
    p.add_argument(
        "--repo",
        metavar="NAME",
        help="Process only this repo name (for debugging).",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# File walking
# ---------------------------------------------------------------------------


def is_pruned(path: Path, repo_root: Path) -> bool:
    """True if any segment of path (relative to repo_root) is a prune target."""
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False
    return any(part.lower() in PRUNE_DIRS for part in rel.parts)


def is_test_path(path: Path, repo_root: Path) -> bool:
    """True if any segment looks like a test directory."""
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False
    return any(TEST_SEGMENT_PATTERNS.match(part) for part in rel.parts)


def iter_java_files(repo_root: Path):
    """
    Yield (java_file_path, is_test) for every .java file under repo_root,
    skipping pruned subtrees efficiently via os.walk.
    """
    import os

    for dirpath, dirnames, filenames in os.walk(repo_root):
        dp = Path(dirpath)
        # Prune in-place so os.walk doesn't descend into excluded dirs
        dirnames[:] = [d for d in dirnames if d.lower() not in PRUNE_DIRS]
        if is_pruned(dp, repo_root):
            dirnames.clear()
            continue
        test = is_test_path(dp, repo_root)
        for fn in filenames:
            if fn.endswith(".java"):
                yield dp / fn, test


# ---------------------------------------------------------------------------
# Package parsing
# ---------------------------------------------------------------------------


def read_package(java_file: Path) -> str | None:
    """Return the package name declared in java_file, or None."""
    try:
        with open(java_file, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= MAX_HEADER_LINES:
                    break
                m = PACKAGE_RE.match(line)
                if m:
                    return m.group(1).strip()
                # Stop early on class/interface/enum/import lines if no package found yet
                if re.match(
                    r"^\s*(public|private|protected|import|class|interface|enum|@)",
                    line,
                ):
                    if i > 0:  # give the very first line benefit of the doubt
                        break
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Root computation
# ---------------------------------------------------------------------------


def compute_root(java_file: Path, package: str) -> Path | None:
    """
    Given a file and its declared package, walk up len(package.parts) + 1
    levels and validate the round-trip.
    Returns the source root Path, or None if the path/package mismatch.
    """
    parts = package.split(".")
    # File parent must end with the package path segments
    parent = java_file.parent
    expected_tail = Path(*parts) if parts else Path()

    # Walk up len(parts) levels
    root = parent
    for _ in parts:
        root = root.parent

    # Validate: root / pkg_path / filename should equal java_file
    reconstructed = root.joinpath(*parts) / java_file.name
    if reconstructed.resolve() != java_file.resolve():
        return None
    return root


# ---------------------------------------------------------------------------
# Root discovery for one repo
# ---------------------------------------------------------------------------


def find_roots(repo_root: Path, include_tests: bool, min_files: int, verbose: bool):
    """
    Returns (main_roots, test_roots) — each a sorted list of Path objects
    relative to repo_root.
    """
    # votes[root] = count of .java files that confirmed it
    main_votes: dict[Path, int] = defaultdict(int)
    test_votes: dict[Path, int] = defaultdict(int)

    # For no-package files, cluster by parent dir
    main_nopack: dict[Path, int] = defaultdict(int)
    test_nopack: dict[Path, int] = defaultdict(int)

    for java_file, is_test in iter_java_files(repo_root):
        votes = test_votes if is_test else main_votes
        nopack = test_nopack if is_test else main_nopack

        pkg = read_package(java_file)
        if pkg:
            root = compute_root(java_file, pkg)
            if root is not None:
                votes[root] += 1
            else:
                if verbose:
                    print(
                        f"    [mismatch] {java_file.relative_to(repo_root)} "
                        f"package={pkg}"
                    )
        else:
            # No package: candidate root is immediate parent
            nopack[java_file.parent] += 1

    # Merge no-package clusters that meet min_files into votes
    for nopack, votes in [(main_nopack, main_votes), (test_nopack, test_votes)]:
        for parent, count in nopack.items():
            if count >= min_files:
                votes[parent] += count

    def filter_roots(votes):
        # Keep only roots meeting the file threshold
        confirmed = {r for r, c in votes.items() if c >= min_files}
        # Remove ancestor roots when a descendant is also confirmed
        # (descendant is more specific — prefer it)
        pruned = set()
        for r in confirmed:
            for other in confirmed:
                if r != other and r in other.parents:
                    pruned.add(r)  # r is an ancestor of other → drop r
        return sorted(confirmed - pruned, key=lambda p: str(p))

    main_roots = filter_roots(main_votes)
    test_roots = filter_roots(test_votes)

    if verbose:
        print(
            f"  main votes: { {str(k.relative_to(repo_root)): v for k, v in sorted(main_votes.items(), key=lambda x: (
                        str(x[0])
                    ))} }"
        )
        print(
            f"  test votes: { {str(k.relative_to(repo_root)): v for k, v in sorted(test_votes.items(), key=lambda x: (
                        str(x[0])
                    ))} }"
        )

    return main_roots, test_roots


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    args = parse_args()

    if not args.src_dir.is_dir():
        sys.exit(f"Error: --src-dir not found: {args.src_dir}")

    # Collect repo dirs: either all subdirs of src_dir, or the single named one
    if args.repo:
        repo_dirs = [args.src_dir / args.repo]
        if not repo_dirs[0].is_dir():
            sys.exit(f"Error: repo not found: {repo_dirs[0]}")
    else:
        repo_dirs = sorted(
            d
            for d in args.src_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )

    # --- Resume: load already-processed repos from existing CSV ---
    already_done: set[str] = set()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        with open(args.output, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames and "repo" in reader.fieldnames:
                already_done = {row["repo"] for row in reader}
        if already_done:
            print(
                f"Resuming: {len(already_done)} repo(s) already in {args.output}, skipping."
            )

    pending = [d for d in repo_dirs if d.name not in already_done]

    print(
        f"Scanning {len(repo_dirs)} repo(s) under {args.src_dir} "
        f"({len(pending)} remaining)"
    )
    print(f"Output  : {args.output}")
    print(f"Min files per root: {args.min_files}")
    if args.include_tests:
        print("Test roots: included")
    print()

    # Open CSV in append mode; write header only if file is new/empty
    write_header = not args.output.exists() or args.output.stat().st_size == 0
    csv_file = open(args.output, "a", newline="", encoding="utf-8")
    writer = csv.writer(csv_file, quoting=csv.QUOTE_MINIMAL)
    if write_header:
        writer.writerow(["repo", "source_paths"])
        csv_file.flush()

    written = 0
    skipped = []

    try:
        for idx, repo_dir in enumerate(pending, 1):
            repo_name = repo_dir.name
            if args.verbose:
                print(f"[{idx}/{len(pending)}] {repo_name}")
            else:
                print(f"[{idx}/{len(pending)}] {repo_name}", end=" ... ", flush=True)

            main_roots, test_roots = find_roots(
                repo_dir,
                include_tests=args.include_tests,
                min_files=args.min_files,
                verbose=args.verbose,
            )

            all_roots = main_roots[:]
            if args.include_tests:
                all_roots += [r for r in test_roots if r not in main_roots]

            if not all_roots:
                skipped.append(repo_name)
                if args.verbose:
                    print(f"  → no roots found\n")
                else:
                    print("no roots")
                # Write a row with empty source_paths so this repo is marked
                # done and won't be re-scanned on resume.
                writer.writerow([repo_name, ""])
                csv_file.flush()
                continue

            rel_roots = [str(r.relative_to(args.src_dir)) for r in all_roots]
            joined = "+".join(rel_roots)

            writer.writerow([repo_name, joined])
            csv_file.flush()
            written += 1

            if args.verbose:
                for r in rel_roots:
                    tag = (
                        "(test)"
                        if r in [str(x.relative_to(args.src_dir)) for x in test_roots]
                        else ""
                    )
                    print(f"  → {r} {tag}")
                print()
            else:
                print(f"{len(rel_roots)} root(s)")

    finally:
        csv_file.close()

    print(f"\nDone. {written} repos written, {len(skipped)} with no roots found.")
    if skipped:
        for name in skipped:
            print(f"  (no roots) {name}")


if __name__ == "__main__":
    main()
