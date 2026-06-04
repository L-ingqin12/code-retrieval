#!/usr/bin/env python3
"""
Compare two codebase indices to analyze what changed between commits/branches.

Workflow:
  1. Build indices:   python code_index.py . --commit main~3 && python code_index.py . --commit main
  2. Compare:          python code_diff.py . main~3 main
  3. Or compare live:  python code_diff.py . abc1234 def5678 --live (builds if needed)

Output: structured diff report (files, symbols, imports, entry points, impact).
"""

import argparse
import json
import os
import subprocess
import sys

# ── Helpers ────────────────────────────────────────────────────────

def _git(args: list[str], cwd: str, timeout: int = 15) -> str:
    try:
        result = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout.strip()
    except FileNotFoundError:
        raise RuntimeError("git command not found")


def git_resolve_ref(root: str, ref: str) -> str:
    return _git(["rev-parse", ref], root)


def find_store(root: str, store_dir: str | None = None) -> str:
    if store_dir:
        return store_dir
    return os.path.join(root, ".code-index")


def load_index(store: str, ref: str) -> dict | None:
    """Load index from store by ref (commit hash, branch, or 'current')."""
    commits_dir = os.path.join(store, "commits")
    heads_dir = os.path.join(store, "heads")

    # Exact commit
    candidate = os.path.join(commits_dir, f"{ref}.json")
    if os.path.isfile(candidate):
        with open(candidate) as f:
            return json.load(f)

    # Partial match
    if os.path.isdir(commits_dir):
        for fname in os.listdir(commits_dir):
            if fname.startswith(ref) and fname.endswith(".json"):
                with open(os.path.join(commits_dir, fname)) as f:
                    return json.load(f)

    # Branch
    candidate = os.path.join(heads_dir, f"{ref}.json")
    if os.path.isfile(candidate):
        with open(candidate) as f:
            return json.load(f)

    # Current
    candidate = os.path.join(store, "current.json")
    if os.path.isfile(candidate):
        with open(candidate) as f:
            return json.load(f)

    return None


# ── Diff logic ─────────────────────────────────────────────────────

def diff_indices(base: dict, target: dict) -> dict:
    """Compute structured diff between two indices."""
    base_files = {e["path"]: e for e in base.get("files", [])}
    target_files = {e["path"]: e for e in target.get("files", [])}

    base_paths = set(base_files.keys())
    target_paths = set(target_files.keys())

    added_paths = target_paths - base_paths
    deleted_paths = base_paths - target_paths
    common_paths = base_paths & target_paths

    # ── Modified files: compare symbols and imports ──
    modified: list[dict] = []
    for path in sorted(common_paths):
        bf = base_files[path]
        tf = target_files[path]

        b_symbols = {(s["type"], s["name"]) for s in bf.get("symbols", [])}
        t_symbols = {(s["type"], s["name"]) for s in tf.get("symbols", [])}

        sym_added = [
            {"type": t, "name": n} for t, n in (t_symbols - b_symbols)
        ][:20]
        sym_removed = [
            {"type": t, "name": n} for t, n in (b_symbols - t_symbols)
        ][:20]

        b_imports = set(bf.get("imports", []))
        t_imports = set(tf.get("imports", []))
        imp_added = list(t_imports - b_imports)[:10]
        imp_removed = list(b_imports - t_imports)[:10]

        size_delta = tf.get("size", 0) - bf.get("size", 0)
        lines_delta = (tf.get("lines", 0) or 0) - (bf.get("lines", 0) or 0)

        if sym_added or sym_removed or imp_added or imp_removed or abs(size_delta) > 50:
            modified.append({
                "path": path,
                "size_delta": size_delta,
                "lines_delta": lines_delta,
                "symbols_added": sym_added,
                "symbols_removed": sym_removed,
                "imports_added": imp_added,
                "imports_removed": imp_removed,
            })

    # ── Entry point changes ──
    base_entries = set(base.get("entry_points", []))
    target_entries = set(target.get("entry_points", []))
    entry_changes = {
        "added": sorted(target_entries - base_entries),
        "removed": sorted(base_entries - target_entries),
    }

    # ── Impact analysis ──
    # Files with most dependents that were modified
    base_graph = base.get("import_graph", {})
    target_graph = target.get("import_graph", {})

    # Find files whose dependents changed
    impact: dict[str, dict] = {}
    for mod in modified:
        path = mod["path"]
        old_deps = len(base_graph.get(path, []))
        new_deps = len(target_graph.get(path, []))
        if abs(new_deps - old_deps) > 2:
            impact[path] = {"dependents_before": old_deps, "dependents_after": new_deps}
    # Also highlight new files with many dependents
    for path in added_paths:
        dep_count = len(target_graph.get(path, []))
        if dep_count >= 5:
            impact[path] = {"dependents_before": 0, "dependents_after": dep_count, "new_high_impact": True}

    return {
        "base": {
            "commit": base["meta"].get("git_short_commit", "?"),
            "branch": base["meta"].get("git_branch", "?"),
            "files": len(base_files),
            "symbols": base["meta"].get("total_symbols", 0),
        },
        "target": {
            "commit": target["meta"].get("git_short_commit", "?"),
            "branch": target["meta"].get("git_branch", "?"),
            "files": len(target_files),
            "symbols": target["meta"].get("total_symbols", 0),
        },
        "summary": {
            "files_added": len(added_paths),
            "files_deleted": len(deleted_paths),
            "files_modified": len(modified),
            "files_unchanged": len(common_paths) - len(modified),
            "symbols_added": sum(len(m["symbols_added"]) for m in modified),
            "symbols_removed": sum(len(m["symbols_removed"]) for m in modified),
        },
        "added": sorted(added_paths),
        "deleted": sorted(deleted_paths),
        "modified": modified,
        "entry_changes": entry_changes,
        "high_impact": impact,
    }


# ── Renderers ──────────────────────────────────────────────────────

def render_summary(diff: dict) -> str:
    """One-paragraph human-readable summary."""
    s = diff["summary"]
    b = diff["base"]
    t = diff["target"]
    lines = [
        f"# Diff: {b['commit']} ({b['branch']}) → {t['commit']} ({t['branch']})",
        f"Files: {b['files']} → {t['files']} "
        f"(+{s['files_added']} -{s['files_deleted']} ~{s['files_modified']})",
        f"Symbols: {b['symbols']} → {t['symbols']} "
        f"(+{s['symbols_added']} -{s['symbols_removed']})",
    ]
    if diff["entry_changes"]["added"]:
        lines.append(f"New entry points: {', '.join(diff['entry_changes']['added'])}")
    if diff["entry_changes"]["removed"]:
        lines.append(f"Removed entry points: {', '.join(diff['entry_changes']['removed'])}")
    if diff["high_impact"]:
        lines.append(f"High-impact changes: {len(diff['high_impact'])} files")
    return "\n".join(lines)


def render_full(diff: dict) -> str:
    """Full diff report."""
    out = [render_summary(diff), ""]

    if diff["added"]:
        out.append("## Added files")
        for f in diff["added"][:30]:
            out.append(f"  + {f}")
        if len(diff["added"]) > 30:
            out.append(f"  ... and {len(diff['added']) - 30} more")
        out.append("")

    if diff["deleted"]:
        out.append("## Deleted files")
        for f in diff["deleted"][:30]:
            out.append(f"  - {f}")
        if len(diff["deleted"]) > 30:
            out.append(f"  ... and {len(diff['deleted']) - 30} more")
        out.append("")

    if diff["modified"]:
        out.append(f"## Modified files ({len(diff['modified'])})")
        for mod in diff["modified"][:50]:
            out.append(f"  ~ {mod['path']} ({mod['size_delta']:+d} bytes, {mod['lines_delta']:+d} lines)")
            for s in mod.get("symbols_added", [])[:5]:
                out.append(f"      + [{s['type']}] {s['name']}")
            for s in mod.get("symbols_removed", [])[:5]:
                out.append(f"      - [{s['type']}] {s['name']}")
            if mod.get("imports_added"):
                out.append(f"      imports+: {', '.join(mod['imports_added'][:5])}")
            if mod.get("imports_removed"):
                out.append(f"      imports-: {', '.join(mod['imports_removed'][:5])}")
        if len(diff["modified"]) > 50:
            out.append(f"  ... and {len(diff['modified']) - 50} more modified files")
        out.append("")

    if diff["high_impact"]:
        out.append("## High-impact changes")
        for path, info in sorted(diff["high_impact"].items(), key=lambda x: -abs(x[1].get("dependents_after", 0) - x[1].get("dependents_before", 0))):
            before = info["dependents_before"]
            after = info["dependents_after"]
            tag = " (NEW)" if info.get("new_high_impact") else ""
            out.append(f"  {path}: dependents {before} → {after}{tag}")

    return "\n".join(out)


def render_compact(diff: dict) -> str:
    """Minimal diff for agent context windows."""
    s = diff["summary"]
    b = diff["base"]
    t = diff["target"]
    lines = [
        f"Diff {b['commit']}→{t['commit']}: "
        f"+{s['files_added']} -{s['files_deleted']} ~{s['files_modified']} files, "
        f"+{s['symbols_added']} -{s['symbols_removed']} symbols",
    ]
    # Only show files with the most symbol changes
    top_mods = sorted(
        diff["modified"],
        key=lambda m: len(m.get("symbols_added", [])) + len(m.get("symbols_removed", [])),
        reverse=True,
    )[:10]
    if top_mods:
        lines.append("Top changes:")
        for m in top_mods:
            s_add = len(m.get("symbols_added", []))
            s_rem = len(m.get("symbols_removed", []))
            added_names = [s["name"] for s in m.get("symbols_added", [])[:3]]
            removed_names = [s["name"] for s in m.get("symbols_removed", [])[:3]]
            details = []
            if added_names:
                details.append(f"+{','.join(added_names)}")
            if removed_names:
                details.append(f"-{','.join(removed_names)}")
            lines.append(f"  {m['path']} (+{s_add}/-{s_rem}) {' '.join(details)}")
    return "\n".join(lines)


# ── Live diff (build indices on the fly) ───────────────────────────

def live_diff(root: str, base_ref: str, target_ref: str, store_dir: str) -> dict:
    """Build indices for both refs if needed, then diff."""
    # Resolve refs
    base_commit = git_resolve_ref(root, base_ref)
    target_commit = git_resolve_ref(root, target_ref)
    print(f"Base:   {base_ref} → {base_commit[:8]}", file=sys.stderr)
    print(f"Target: {target_ref} → {target_commit[:8]}", file=sys.stderr)

    # Load or build base
    base_index = load_index(store_dir, base_commit)
    if not base_index:
        print(f"Building index for {base_ref}...", file=sys.stderr)
        import subprocess as sp
        sp.run(
            [sys.executable, "-m", "code_index", root, "--commit", base_ref, "--store-dir", store_dir],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            check=True,
        )
        base_index = load_index(store_dir, base_commit)
    if not base_index:
        raise RuntimeError(f"Could not build or load index for {base_ref}")

    # Load or build target
    target_index = load_index(store_dir, target_commit)
    if not target_index:
        print(f"Building index for {target_ref}...", file=sys.stderr)
        import subprocess as sp
        sp.run(
            [sys.executable, "-m", "code_index", root, "--commit", target_ref, "--store-dir", store_dir],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            check=True,
        )
        target_index = load_index(store_dir, target_commit)
    if not target_index:
        raise RuntimeError(f"Could not build or load index for {target_ref}")

    return diff_indices(base_index, target_index)


# ── Main ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compare two codebase indices (commits/branches/tags)."
    )
    parser.add_argument("root", help="Project root directory")
    parser.add_argument("base", nargs="?", help="Base ref (commit, branch, tag)")
    parser.add_argument("target", nargs="?", help="Target ref (commit, branch, tag)")
    parser.add_argument("--store-dir", default=None, help="Index store directory")
    parser.add_argument("--live", action="store_true", help="Auto-build indices if missing")
    parser.add_argument("--format", choices=["full", "summary", "compact"], default="full",
                        help="Output format: full, summary, or compact (default: full)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON diff")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    store = find_store(root, args.store_dir)

    def resolve_load(ref: str) -> dict | None:
        """Resolve ref via git, then load from store by commit hash."""
        try:
            commit = git_resolve_ref(root, ref)
        except Exception:
            return load_index(store, ref)  # fallback: try as literal key
        idx = load_index(store, commit)
        if idx:
            return idx
        return load_index(store, ref)  # fallback: branch name or 'current'

    # ── Convenience: diff working tree vs HEAD ──
    if not args.base and not args.target:
        current = load_index(store, "current")
        if not current:
            print("No stored index found. Build one first with: python code_index.py .", file=sys.stderr)
            sys.exit(1)
        print(f"Working tree vs {current['meta'].get('git_short_commit', 'unknown')}", file=sys.stderr)
        import subprocess as sp
        sp.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "code_index.py"), root, "--store-dir", store],
            check=True,
        )
        working = load_index(store, "current")
        if not working:
            print("Failed to build working tree index.", file=sys.stderr)
            sys.exit(1)
        diff = diff_indices(current, working)
    elif args.base and not args.target:
        # Diff base vs current working tree
        if args.live:
            diff = live_diff(root, args.base, "HEAD", store)
        else:
            base_idx = resolve_load(args.base)
            target_idx = load_index(store, "current")
            if not base_idx:
                print(f"Index for '{args.base}' not found. Use --live to auto-build.", file=sys.stderr)
                sys.exit(1)
            if not target_idx:
                print("No current index found. Run: python code_index.py .", file=sys.stderr)
                sys.exit(1)
            diff = diff_indices(base_idx, target_idx)
    else:
        # Diff base vs target
        if args.live:
            diff = live_diff(root, args.base, args.target, store)
        else:
            base_idx = resolve_load(args.base)
            target_idx = resolve_load(args.target)
            if not base_idx:
                print(f"Index for '{args.base}' not found. Use --live to auto-build.", file=sys.stderr)
                sys.exit(1)
            if not target_idx:
                print(f"Index for '{args.target}' not found. Use --live to auto-build.", file=sys.stderr)
                sys.exit(1)
            diff = diff_indices(base_idx, target_idx)

    if args.json:
        print(json.dumps(diff, ensure_ascii=False, indent=2))
    elif args.format == "summary":
        print(render_summary(diff))
    elif args.format == "compact":
        print(render_compact(diff))
    else:
        print(render_full(diff))


if __name__ == "__main__":
    main()
