#!/usr/bin/env python3
"""
compute_graph_stats.py

Walks a directory of SABO JSON property graphs and produces two CSVs:

  <out_dir>/repo_stats.csv              — per-graph structural and metric statistics
  <out_dir>/repo_edge_label_matrix.csv  — per-graph edge-label counts (wide format)

Handles two directory layouts:
  Flat:   <graphs_dir>/<repo>.json           → subrepo column is empty
  Split:  <graphs_dir>/<repo>/<subrepo>.json → one row per subrepo

Usage:
  python compute_graph_stats.py \\
      --graphs-dir /data/graphs/java \\
      --lang java \\
      --out-dir /data/stats

  python compute_graph_stats.py \\
      --graphs-dir /data/graphs/csharp \\
      --lang csharp \\
      --out-dir /data/stats \\
      --limit 10          # smoke-test on first 10 files
"""

import argparse
import collections
import csv
import json
import math
import pathlib
import statistics
import sys
from typing import Optional

# ── Label / edge constants ──────────────────────────────────────────────────

STRUCTURAL_LABELS = frozenset({
    "Project", "Folder", "File", "Scope", "Type", "Operation", "Variable",
})

# Ordered list used to fix column order in edge matrix CSV.
ALL_STRUCTURAL_EDGE_LABELS = [
    "includes", "contains", "requires", "declares", "encloses",
    "specializes", "encapsulates", "returns", "instantiates",
    "invokes", "uses", "parameterizes", "typed",
]
STRUCTURAL_EDGE_SET = frozenset(ALL_STRUCTURAL_EDGE_LABELS)

# Edges whose *presence* (any count > 0) is tracked as an extractor
# reliability / coverage signal.
COVERAGE_EDGES = ("requires", "specializes", "instantiates", "typed")

# Halstead property keys stored on measures edges.
HALSTEAD_KEYS = ("vocabulary", "length", "volume", "difficulty", "effort", "estimatedBugs")

# Sentinel value the extractor writes for class-level Halstead aggregates
# when the metric cannot be computed (vocabulary/difficulty == -1).
HALSTEAD_SENTINEL = -1


# ── Statistical helpers ─────────────────────────────────────────────────────

def _safe(fn, values):
    return fn(values) if values else None

def smean(v):   return _safe(statistics.mean,   v)
def smedian(v): return _safe(statistics.median, v)
def smax(v):    return _safe(max,               v)
def sstd(v):    return _safe(statistics.pstdev, v)  # population stdev (all repos = population)

def sp90(values):
    if not values:
        return None
    s = sorted(values)
    idx = max(0, math.ceil(0.9 * len(s)) - 1)
    return s[idx]


# ── BFS helpers ─────────────────────────────────────────────────────────────

def bfs_depths(roots, children_fn):
    """
    BFS from a set of root nodes.
    Returns a list of (node_id, depth) for every reachable node
    INCLUDING the roots themselves (at depth 0).
    Cycles are handled by the visited set.
    """
    if not roots:
        return []
    visited = set()
    queue = collections.deque((r, 0) for r in roots)
    result = []
    while queue:
        nid, d = queue.popleft()
        if nid in visited:
            continue
        visited.add(nid)
        result.append((nid, d))
        for child in children_fn(nid):
            if child not in visited:
                queue.append((child, d + 1))
    return result

def bfs_max_depth(roots, children_fn):
    depths = bfs_depths(roots, children_fn)
    return max((d for _, d in depths), default=None)

def bfs_nonroot_depths(roots, children_fn):
    """Depths of all non-root reachable nodes (used for mean depth calculations)."""
    return [d for _, d in bfs_depths(roots, children_fn) if d > 0]


# ── Core: process one JSON graph ────────────────────────────────────────────

def process_graph(path: pathlib.Path, lang: str, repo: str, subrepo: Optional[str]):
    """
    Parse one SABO JSON graph and return:
      stats_row   : dict of all computed statistics
      edge_counts : Counter of edge label -> count (ALL labels, for the matrix)
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    els = data.get("elements", {})
    raw_nodes = els.get("nodes", [])
    raw_edges = els.get("edges", [])

    # ── Index nodes ────────────────────────────────────────────────────────
    # node_by_id: id -> full data dict
    node_by_id = {n["data"]["id"]: n["data"] for n in raw_nodes}

    # out_adj[src][label] = [tgt, ...]
    # in_adj[tgt][label]  = [src, ...]
    out_adj = collections.defaultdict(lambda: collections.defaultdict(list))
    in_adj  = collections.defaultdict(lambda: collections.defaultdict(list))

    # Count ALL edge labels (including measures, for the matrix).
    all_edge_counts = collections.Counter()
    # Count only structural edges for the stats columns.
    struct_edge_counts = collections.Counter()

    for e in raw_edges:
        d = e["data"]
        src, tgt, lbl = d["source"], d["target"], d["label"]
        out_adj[src][lbl].append(tgt)
        in_adj[tgt][lbl].append(src)
        all_edge_counts[lbl] += 1
        if lbl in STRUCTURAL_EDGE_SET:
            struct_edge_counts[lbl] += 1

    # ── Classify nodes ─────────────────────────────────────────────────────
    def has_label(nd, lbl): return lbl in nd.get("labels", [])
    def get_kind(nd):       return nd.get("properties", {}).get("kind", "")

    def node_set(label=None, kind=None):
        result = set()
        for nid, nd in node_by_id.items():
            if label and not has_label(nd, label):
                continue
            if kind and get_kind(nd) != kind:
                continue
            result.add(nid)
        return result

    project_nodes   = node_set(label="Project")
    folder_nodes    = node_set(label="Folder")
    file_nodes      = node_set(label="File")
    scope_nodes     = node_set(label="Scope")
    type_nodes      = node_set(label="Type")
    operation_nodes = node_set(label="Operation")
    variable_nodes  = node_set(label="Variable")
    method_nodes    = node_set(label="Operation", kind="method")
    ctor_nodes      = node_set(label="Operation", kind="constructor")
    field_nodes     = node_set(label="Variable",  kind="field")
    param_nodes     = node_set(label="Variable",  kind="parameter")

    # ── Node counts ────────────────────────────────────────────────────────
    n = {
        "n_project":              len(project_nodes),
        "n_folder":               len(folder_nodes),
        "n_file":                 len(file_nodes),
        "n_scope":                len(scope_nodes),
        "n_type":                 len(type_nodes),
        "n_operation":            len(operation_nodes),
        "n_operation_method":     len(method_nodes),
        "n_operation_constructor":len(ctor_nodes),
        "n_variable":             len(variable_nodes),
        "n_variable_field":       len(field_nodes),
        "n_variable_param":       len(param_nodes),
    }

    # ── Degree helpers ─────────────────────────────────────────────────────
    def out_deg(src_set, lbl):
        return [len(out_adj[nid][lbl]) for nid in src_set]

    def in_deg(tgt_set, lbl):
        return [len(in_adj[nid][lbl]) for nid in tgt_set]

    # ── encapsulates: methods and fields per class ─────────────────────────
    methods_per_class = []
    fields_per_class  = []
    for nid in type_nodes:
        m_count = sum(
            1 for t in out_adj[nid]["encapsulates"]
            if get_kind(node_by_id.get(t, {})) in ("method", "constructor")
        )
        f_count = sum(
            1 for t in out_adj[nid]["encapsulates"]
            if get_kind(node_by_id.get(t, {})) == "field"
        )
        methods_per_class.append(m_count)
        fields_per_class.append(f_count)

    # ── Call graph (invokes) ───────────────────────────────────────────────
    call_fanout = out_deg(operation_nodes, "invokes")
    call_fanin  = in_deg(operation_nodes, "invokes")

    # ── Arity (parameterizes → Operation) ─────────────────────────────────
    arity = in_deg(operation_nodes, "parameterizes")

    # ── Field access (uses → Variable.field) ──────────────────────────────
    field_uses_fanin = in_deg(field_nodes, "uses")

    # ── Inheritance (specializes) ──────────────────────────────────────────
    # Edge direction: child -[specializes]→ parent
    # out_adj[child]["specializes"] = [parent, ...]
    # in_adj[parent]["specializes"] = [child, ...]
    inh_parents  = out_deg(type_nodes, "specializes")  # parents per class
    inh_children = in_deg(type_nodes, "specializes")   # children per class

    # Depth: BFS from root types (those with no parent = no outgoing specializes)
    if struct_edge_counts["specializes"] > 0:
        inh_roots = {n for n in type_nodes if not out_adj[n]["specializes"]}
        # children of a node = those that specialize it = in_adj[n]["specializes"]
        inh_depth_max  = bfs_max_depth(inh_roots, lambda n: in_adj[n]["specializes"])
        inh_depth_vals = bfs_nonroot_depths(inh_roots, lambda n: in_adj[n]["specializes"])
        inh_depth_mean = smean(inh_depth_vals)
    else:
        inh_depth_max  = None
        inh_depth_mean = None

    # ── requires: file import coupling ────────────────────────────────────
    file_req_out = out_deg(file_nodes, "requires")
    file_req_in  = in_deg(file_nodes, "requires")

    # ── Hierarchy depths ───────────────────────────────────────────────────

    # File-system depth: Project -[includes]→ Folder -[contains]→ ... -[contains]→ File
    def fs_children(nid):
        return out_adj[nid]["includes"] + out_adj[nid]["contains"]
    fs_depth_max = bfs_max_depth(project_nodes, fs_children)

    # Scope/package depth: Scope -[encloses]→ Scope chains
    if scope_nodes:
        scope_roots = {n for n in scope_nodes if not in_adj[n]["encloses"]}
        scope_children = lambda n: [
            t for t in out_adj[n]["encloses"]
            if has_label(node_by_id.get(t, {}), "Scope")
        ]
        scope_depth_max = bfs_max_depth(scope_roots, scope_children)
    else:
        scope_depth_max = None

    # ── Metric values from 'measures' edges ───────────────────────────────
    num_methods_vals = []
    num_stmts_vals   = []
    halstead         = collections.defaultdict(list)  # key → list[float]

    for e in raw_edges:
        d = e["data"]
        if d["label"] != "measures":
            continue
        props    = d.get("properties", {})
        tgt_nd   = node_by_id.get(d["target"], {})
        src_nd   = node_by_id.get(d["source"], {})
        metric   = tgt_nd.get("properties", {}).get("simpleName", "")
        src_kind = get_kind(src_nd)

        if metric == "NumMethods":
            val = props.get("value")
            if val is not None:
                num_methods_vals.append(val)

        elif metric == "NumStatements":
            val = props.get("value")
            if val is not None:
                num_stmts_vals.append(val)

        elif metric in ("HalsteadMetrics", "Halstead Complexity Metrics"):
            # Skip class-level aggregates (extractor writes -1 sentinel for
            # vocabulary/difficulty when it can't aggregate properly).
            if props.get("vocabulary", 0) == HALSTEAD_SENTINEL:
                continue
            # Keep only method/constructor level entries.
            if src_kind in ("method", "constructor"):
                for key in HALSTEAD_KEYS:
                    val = props.get(key)
                    if val is not None:
                        halstead[key].append(val)

    # ── Coverage flags ─────────────────────────────────────────────────────
    coverage = {lbl: int(struct_edge_counts[lbl] > 0) for lbl in COVERAGE_EDGES}

    # ── Assemble stats row ─────────────────────────────────────────────────
    def F(v):
        """Format a value for CSV: None → empty string, float → 6 sig-figs."""
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.6g}"
        return str(v)

    row = {
        # Identity
        "repo":    repo,
        "subrepo": subrepo or "",
        "lang":    lang,

        # Node counts by label and kind
        **n,

        # Edge counts (structural only)
        **{f"e_{lbl}": struct_edge_counts[lbl] for lbl in ALL_STRUCTURAL_EDGE_LABELS},

        # --- encapsulates ---
        "methods_per_class_mean":   F(smean(methods_per_class)),
        "methods_per_class_median": F(smedian(methods_per_class)),
        "methods_per_class_max":    F(smax(methods_per_class)),
        "methods_per_class_std":    F(sstd(methods_per_class)),
        "fields_per_class_mean":    F(smean(fields_per_class)),
        "fields_per_class_median":  F(smedian(fields_per_class)),
        "fields_per_class_max":     F(smax(fields_per_class)),

        # --- call graph (invokes) ---
        "call_fanout_mean":   F(smean(call_fanout)),
        "call_fanout_median": F(smedian(call_fanout)),
        "call_fanout_max":    F(smax(call_fanout)),
        "call_fanout_std":    F(sstd(call_fanout)),
        "call_fanin_mean":    F(smean(call_fanin)),
        "call_fanin_median":  F(smedian(call_fanin)),
        "call_fanin_max":     F(smax(call_fanin)),
        "call_fanin_std":     F(sstd(call_fanin)),

        # --- arity ---
        "arity_mean":   F(smean(arity)),
        "arity_median": F(smedian(arity)),
        "arity_max":    F(smax(arity)),

        # --- field access (uses) ---
        "field_uses_fanin_mean": F(smean(field_uses_fanin)),
        "field_uses_fanin_max":  F(smax(field_uses_fanin)),

        # --- inheritance (specializes) ---
        "inh_parents_mean":  F(smean(inh_parents)),   # avg parents per class
        "inh_children_mean": F(smean(inh_children)),  # avg children per class
        "inh_children_max":  F(smax(inh_children)),   # widest point in hierarchy
        # None = edge type absent entirely (distinct from 0 = flat hierarchy)
        "inh_depth_max":     F(inh_depth_max),
        "inh_depth_mean":    F(inh_depth_mean),

        # --- file coupling (requires) ---
        "file_req_out_mean": F(smean(file_req_out)),
        "file_req_out_max":  F(smax(file_req_out)),
        "file_req_in_mean":  F(smean(file_req_in)),
        "file_req_in_max":   F(smax(file_req_in)),

        # --- hierarchy depths ---
        "fs_depth_max":    F(fs_depth_max),
        "scope_depth_max": F(scope_depth_max),  # empty if no Scope nodes

        # --- NumMethods (from measures) ---
        "num_methods_mean":   F(smean(num_methods_vals)),
        "num_methods_median": F(smedian(num_methods_vals)),
        "num_methods_max":    F(smax(num_methods_vals)),

        # --- NumStatements (from measures) ---
        "stmts_mean":   F(smean(num_stmts_vals)),
        "stmts_median": F(smedian(num_stmts_vals)),
        "stmts_p90":    F(sp90(num_stmts_vals)),
        "stmts_max":    F(smax(num_stmts_vals)),
        "stmts_std":    F(sstd(num_stmts_vals)),

        # --- Halstead (method-level, sentinel-filtered) ---
        **{f"halstead_{k}_mean": F(smean(halstead[k])) for k in HALSTEAD_KEYS},
        **{f"halstead_{k}_p90":  F(sp90(halstead[k]))  for k in ("volume", "effort")},

        # --- Coverage / reliability flags ---
        **{f"coverage_{lbl}": coverage[lbl] for lbl in COVERAGE_EDGES},
    }

    return row, all_edge_counts


# ── File discovery ───────────────────────────────────────────────────────────

def discover_graphs(graphs_dir: pathlib.Path):
    """
    Yields (path, repo, subrepo) for every .json file under graphs_dir.

    Layout rules:
      <graphs_dir>/<repo>.json             → subrepo = None
      <graphs_dir>/<repo>/<subrepo>.json   → subrepo = <subrepo>
      Deeper nesting is skipped with a warning.
    """
    for p in sorted(graphs_dir.rglob("*.json")):
        rel = p.relative_to(graphs_dir)
        parts = rel.parts
        if len(parts) == 1:
            yield p, parts[0].removesuffix(".json"), None
        elif len(parts) == 2:
            yield p, parts[0], parts[1].removesuffix(".json")
        else:
            print(f"[WARN] Skipping unexpected nesting: {p}", file=sys.stderr)


# ── Resume helpers ────────────────────────────────────────────────────────────

def load_done_keys(stats_path: pathlib.Path) -> set:
    """
    Return the set of (repo, subrepo) tuples already written to repo_stats.csv.
    subrepo is "" for flat-layout repos (matches how process_graph stores it).
    """
    if not stats_path.exists() or stats_path.stat().st_size == 0:
        return set()
    with stats_path.open(newline="", encoding="utf-8") as f:
        return {(row["repo"], row["subrepo"]) for row in csv.DictReader(f)}


def load_existing_matrix(matrix_path: pathlib.Path) -> list:
    """
    Load an existing repo_edge_label_matrix.csv back into the internal
    list-of-dicts format used during processing.
    Returns [] if the file doesn't exist or is empty.
    """
    if not matrix_path.exists() or matrix_path.stat().st_size == 0:
        return []
    meta = {"repo", "subrepo", "lang"}
    rows = []
    with matrix_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            counts = {k: int(v) for k, v in row.items() if k not in meta and v != ""}
            rows.append({
                "repo":    row["repo"],
                "subrepo": row["subrepo"],
                "lang":    row["lang"],
                "_counts": counts,
            })
    return rows


# ── Writers ───────────────────────────────────────────────────────────────────

def write_matrix(matrix_rows: list, out_path: pathlib.Path) -> None:
    """
    (Re)write repo × edge-label matrix from the full in-memory list.

    Column order: known structural edge labels first (in schema order),
    then any other labels seen in the corpus sorted alphabetically.
    Missing entries are filled with 0.

    Called after every new row and after crash-recovery re-extractions,
    so it always reflects the complete known state.
    """
    if not matrix_rows:
        return
    all_labels_seen = {k for row in matrix_rows for k in row["_counts"]}
    known_first = [l for l in ALL_STRUCTURAL_EDGE_LABELS if l in all_labels_seen]
    rest        = sorted(l for l in all_labels_seen if l not in STRUCTURAL_EDGE_SET)
    ordered_labels = known_first + rest

    fieldnames = ["repo", "subrepo", "lang"] + ordered_labels
    # Write to a .tmp file then rename — avoids a partially-written matrix on crash.
    tmp = out_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in matrix_rows:
            out = {"repo": row["repo"], "subrepo": row["subrepo"], "lang": row["lang"]}
            for lbl in ordered_labels:
                out[lbl] = row["_counts"].get(lbl, 0)
            w.writerow(out)
    tmp.replace(out_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compute SABO graph statistics → repo_stats.csv + repo_edge_label_matrix.csv"
    )
    parser.add_argument("--graphs-dir", required=True,
                        help="Directory containing SABO .json graph files")
    parser.add_argument("--lang", required=True,
                        choices=["java", "csharp", "cpp", "unknown"],
                        help="Language label written into every output row")
    parser.add_argument("--out-dir", required=True,
                        help="Directory where output CSVs are written")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N graphs (smoke-test mode)")
    args = parser.parse_args()

    graphs_dir = pathlib.Path(args.graphs_dir)
    out_dir    = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stats_path  = out_dir / "repo_stats.csv"
    matrix_path = out_dir / "repo_edge_label_matrix.csv"
    error_path  = out_dir / "_stats_errors.txt"

    # ── Load resume state ──────────────────────────────────────────────────
    done_keys   = load_done_keys(stats_path)
    matrix_rows = load_existing_matrix(matrix_path)

    if done_keys:
        print(f"Resuming: {len(done_keys)} repo(s) already in stats CSV", file=sys.stderr)

    # ── Crash-recovery gap detection ───────────────────────────────────────
    # A crash between "stats flushed" and "matrix written" leaves repos that
    # are in done_keys but absent from the matrix.  Re-extract their edge counts
    # (cheap — skips all the stats computation) so the matrix is complete before
    # we start the main loop.
    matrix_done_keys = {(r["repo"], r["subrepo"]) for r in matrix_rows}
    gap_keys = done_keys - matrix_done_keys
    if gap_keys:
        print(f"[RECOVERY] {len(gap_keys)} repo(s) in stats but missing from matrix "
              f"— re-extracting edge counts only...", file=sys.stderr)
        all_entries = list(discover_graphs(graphs_dir))
        for path, repo, subrepo in all_entries:
            key = (repo, subrepo or "")
            if key not in gap_keys:
                continue
            try:
                _, edge_counts = process_graph(path, args.lang, repo, subrepo)
                matrix_rows.append({
                    "repo":    repo,
                    "subrepo": subrepo or "",
                    "lang":    args.lang,
                    "_counts": dict(edge_counts),
                })
                gap_keys.discard(key)
            except Exception as exc:
                print(f"[RECOVERY WARN] Could not recover matrix row for "
                      f"{repo}/{subrepo or '-'}: {exc}", file=sys.stderr)
        if matrix_rows:
            write_matrix(matrix_rows, matrix_path)
        if gap_keys:
            print(f"[RECOVERY WARN] {len(gap_keys)} repo(s) could not be recovered "
                  f"into matrix (source files may be missing).", file=sys.stderr)

    # ── Discover and filter pending entries ────────────────────────────────
    entries = list(discover_graphs(graphs_dir))
    if args.limit:
        entries = entries[:args.limit]

    pending = [(p, r, s) for p, r, s in entries if (r, s or "") not in done_keys]

    print(f"Found {len(entries)} graph(s) total, {len(pending)} pending", file=sys.stderr)
    if not pending:
        print("Nothing to do.", file=sys.stderr)
        return

    # ── Open stats CSV in append mode ─────────────────────────────────────
    # Header is written only when the file is new (size == 0 or doesn't exist).
    stats_is_new = not stats_path.exists() or stats_path.stat().st_size == 0
    stats_fh     = stats_path.open("a", newline="", encoding="utf-8")
    stats_writer = None   # DictWriter initialised on first successful row

    new_count = 0
    error_count = 0

    try:
        for i, (path, repo, subrepo) in enumerate(pending, 1):
            try:
                stats_row, edge_counts = process_graph(path, args.lang, repo, subrepo)

                # Initialise DictWriter on first row (fieldnames come from the row dict).
                if stats_writer is None:
                    stats_writer = csv.DictWriter(
                        stats_fh, fieldnames=list(stats_row.keys())
                    )
                    if stats_is_new:
                        stats_writer.writeheader()
                        stats_fh.flush()

                # Write and flush immediately — this is the durable record.
                stats_writer.writerow(stats_row)
                stats_fh.flush()
                new_count += 1

                # Update matrix in memory, then rewrite the file atomically.
                matrix_rows.append({
                    "repo":    repo,
                    "subrepo": subrepo or "",
                    "lang":    args.lang,
                    "_counts": dict(edge_counts),
                })
                write_matrix(matrix_rows, matrix_path)

            except Exception as exc:
                msg = f"[ERROR] {path}: {exc}"
                print(msg, file=sys.stderr)
                # Append to error log immediately — no buffering.
                with error_path.open("a", encoding="utf-8") as ef:
                    ef.write(msg + "\n")
                error_count += 1

            if i % 50 == 0 or i == len(pending):
                print(f"  {i}/{len(pending)} processed "
                      f"(+{new_count} ok, {error_count} errors so far)", file=sys.stderr)

    finally:
        stats_fh.close()

    print(
        f"Done. {new_count} new row(s) added | {error_count} error(s)",
        file=sys.stderr,
    )
    print(f"  → {stats_path}", file=sys.stderr)
    print(f"  → {matrix_path}", file=sys.stderr)
    if error_count:
        print(f"  → {error_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
