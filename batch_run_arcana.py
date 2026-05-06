#!/usr/bin/env python3
"""
batch_run_arcana.py — Run arcana on a folder of .json graph files.

Usage:
    python batch_run_arcana.py <graphs_dir> <config_template> <output_dir> [--command PIPELINE] [--log LOG]

Arguments:
    graphs_dir       Directory containing input .json graph files
    config_template  Path to the config.ini template
    output_dir       Directory where enriched output graphs will be saved
    --command        Arcana command pipeline, dash-separated (default: metrics-llm)
    --log            Path to the progress log file (default: batch_run_arcana_progress.log)
    --skip-existing  Skip files whose output already exists (independent of log)

Resume behaviour:
    Files logged as DONE are skipped automatically on re-runs.
    Files logged as FAILED are re-attempted unless --skip-existing is set.

Example:
    python batch_run_arcana.py ./graphs config.ini ./output --command metrics-llm
"""

import argparse
import configparser
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def make_logger(log_path: Path) -> logging.Logger:
    """Create a logger that writes to both a file (always-flushed) and stdout."""
    logger = logging.getLogger("batch_run_arcana")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()  # idempotent if called twice

    fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")

    # File handler — flush after each emit so the log is always current
    class FlushingFileHandler(logging.FileHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()

    fh = FlushingFileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Progress file helpers
# ---------------------------------------------------------------------------

STATUS_DONE   = "DONE"
STATUS_FAILED = "FAILED"
STATUS_SKIP   = "SKIPPED"


def load_progress(log_path: Path) -> dict[str, str]:
    """
    Parse the progress log and return a mapping of
    ``filename → STATUS_DONE | STATUS_FAILED | STATUS_SKIP``.

    Only the *last* recorded status for each file is kept, so a re-run that
    recovers from FAILED will be reflected correctly.
    """
    progress: dict[str, str] = {}
    if not log_path.exists():
        return progress

    with open(log_path, "r", encoding="utf-8") as fh:
        for line in fh:
            for tag in (STATUS_DONE, STATUS_FAILED, STATUS_SKIP):
                marker = f"[{tag}]"
                if marker in line:
                    # Lines look like:  2026-... [DONE]    foo.json
                    after = line.split(marker, 1)[1].strip()
                    # `after` may contain extra text after the filename for FAILED
                    filename = after.split(" — ")[0].strip()
                    progress[filename] = tag
                    break

    return progress


def log_status(logger: logging.Logger, tag: str, filename: str, detail: str = "") -> None:
    """Emit a consistently formatted status line that load_progress can parse."""
    suffix = f" — {detail}" if detail else ""
    msg = f"[{tag}] {filename}{suffix}"
    if tag == STATUS_DONE:
        logger.info(msg)
    elif tag == STATUS_FAILED:
        logger.error(msg)
    else:
        logger.info(msg)


# ---------------------------------------------------------------------------
# Config generation
# ---------------------------------------------------------------------------

def make_file_config(template_path: Path,
                     input_json: Path,
                     output_json: Path,
                     tmp_config_path: Path) -> None:
    """
    Copy the template config, overriding only:
      [project] name   → stem of the input file (no extension)
      [project] input  → absolute path to input_json
      [project] output → absolute path to output_json
    All other keys / sections are preserved unchanged.
    """
    cfg = configparser.ConfigParser()
    cfg.read(template_path, encoding="utf-8")

    if not cfg.has_section("project"):
        cfg.add_section("project")

    cfg.set("project", "name",   input_json.stem)
    cfg.set("project", "input",  str(input_json.resolve()))
    cfg.set("project", "output", str(output_json.resolve()))

    with open(tmp_config_path, "w", encoding="utf-8") as fh:
        cfg.write(fh)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_arcana(config_path: Path, command: str, cwd: Path,
               logger: logging.Logger) -> tuple[bool, str]:
    """
    Invoke ``python -m arcana --config <config> <command>`` and return
    (success, error_message).  stderr is captured and included in the error
    message on failure; on success it is logged at DEBUG level.
    """
    cmd = [sys.executable, "-m", "arcana", "--config", str(config_path), command]
    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return False, f"subprocess error: {exc}"

    if result.stderr:
        logger.debug("arcana stderr:\n%s", result.stderr.rstrip())

    if result.returncode != 0:
        # Trim stderr to the last 800 chars to avoid enormous log entries
        tail = result.stderr[-800:].strip() if result.stderr else "<no stderr>"
        return False, f"exit code {result.returncode} — {tail}"

    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch-run arcana over a folder of .json graph files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("graphs_dir",      type=Path, help="Folder with input .json files")
    parser.add_argument("config_template", type=Path, help="Template config.ini")
    parser.add_argument("output_dir",      type=Path, help="Folder for enriched output graphs")
    parser.add_argument(
        "--command", default="metrics-llm",
        help="Arcana command pipeline, dash-separated (default: metrics-llm)",
    )
    parser.add_argument(
        "--log", type=Path, default=Path("batch_run_arcana_progress.log"),
        help="Progress log file (default: batch_run_arcana_progress.log)",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="Skip processing if the output file already exists on disk",
    )
    args = parser.parse_args()

    # ---- Validate inputs ---------------------------------------------------
    if not args.graphs_dir.is_dir():
        print(f"ERROR: graphs_dir '{args.graphs_dir}' is not a directory.", file=sys.stderr)
        return 1
    if not args.config_template.is_file():
        print(f"ERROR: config_template '{args.config_template}' not found.", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Use the current working directory as the base for temp files and execution
    cwd = Path.cwd()

    logger = make_logger(args.log)
    logger.info("=" * 72)
    logger.info("batch_run_arcana starting")
    logger.info("  graphs_dir      : %s", args.graphs_dir.resolve())
    logger.info("  config_template : %s", args.config_template.resolve())
    logger.info("  output_dir      : %s", args.output_dir.resolve())
    logger.info("  command         : %s", args.command)
    logger.info("  log             : %s", args.log.resolve())
    logger.info("=" * 72)

    # ---- Collect input files -----------------------------------------------
    input_files = sorted(args.graphs_dir.glob("*.json"))
    if not input_files:
        logger.warning("No .json files found in '%s'. Nothing to do.", args.graphs_dir)
        return 0

    logger.info("Found %d .json file(s).", len(input_files))

    # ---- Load resume state --------------------------------------------------
    progress = load_progress(args.log)
    logger.info("Resume state loaded: %d file(s) already logged.",
                sum(1 for v in progress.values() if v == STATUS_DONE))

    # ---- Process each file -------------------------------------------------
    counts = {STATUS_DONE: 0, STATUS_FAILED: 0, STATUS_SKIP: 0}

    for input_json in input_files:
        filename = input_json.name
        output_json = args.output_dir / filename

        # --- Resume / skip logic ---
        if progress.get(filename) == STATUS_DONE:
            logger.info("Skipping (already DONE): %s", filename)
            counts[STATUS_SKIP] += 1
            continue

        if args.skip_existing and output_json.exists():
            logger.info("Skipping (output exists): %s", filename)
            log_status(logger, STATUS_SKIP, filename, "output file already exists")
            counts[STATUS_SKIP] += 1
            continue

        # --- Generate per-file config ---
        tmp_config = cwd / f"_batch_tmp_{input_json.stem}.ini"
        try:
            make_file_config(args.config_template, input_json, output_json, tmp_config)
        except Exception as exc:
            log_status(logger, STATUS_FAILED, filename, f"config generation error: {exc}")
            counts[STATUS_FAILED] += 1
            continue

        # --- Run arcana ---
        logger.info("Processing: %s → %s", filename, output_json.name)
        t0 = time.monotonic()
        success, error_msg = run_arcana(tmp_config, args.command, cwd, logger)
        elapsed = time.monotonic() - t0

        # --- Cleanup temp config (always, even on failure) ---
        try:
            tmp_config.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not delete temp config '%s': %s", tmp_config, exc)

        # --- Record result ---
        if success:
            log_status(logger, STATUS_DONE, filename,
                       f"elapsed {elapsed:.1f}s")
            counts[STATUS_DONE] += 1
        else:
            log_status(logger, STATUS_FAILED, filename, error_msg)
            counts[STATUS_FAILED] += 1

    # ---- Summary ------------------------------------------------------------
    logger.info("=" * 72)
    logger.info("Batch complete. done=%d  failed=%d  skipped=%d",
                counts[STATUS_DONE], counts[STATUS_FAILED], counts[STATUS_SKIP])
    logger.info("=" * 72)

    return 0 if counts[STATUS_FAILED] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
