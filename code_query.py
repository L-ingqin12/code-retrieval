#!/usr/bin/env python3
"""
Query a codebase index built by code_index.py.

Usage examples:
  # Find all files defining a symbol
  python code_query.py symbol AuthService

  # Find all files importing a module
  python code_query.py imports react

  # Show entry points
  python code_query.py entries

  # Show directory overview
  python code_query.py dirs --depth 2

  # Find files by name pattern
  python code_query.py files "auth*.py"

  # Trace dependencies of a file
  python code_query.py deps src/auth.py --depth 2

  # Find files that depend on this file
  python code_query.py dependents src/utils.py

  # Full-text search in symbol names
  python code_query.py search "authenticate" --type function --lang python
"""

import argparse
import json
import os
import sys
from collections import defaultdict


def load_index(index_path: str) -> dict:
    with open(index_path, "r") as f:
        return json.load(f)


def cmd_symbol(args, index: dict):
    """Find files that define a symbol."""
    name = args.name.lower()
    results = []
    for f in index["files"]:
        for s in f.get("symbols", []):
            if args.exact:
                if s["name"] == args.name:
                    results.append((f["path"], s))
            else:
                if name in s["name"].lower():
                    results.append((f["path"], s))

    for path, sym in results:
        print(f"{path}:{sym['line']}  [{sym['type']}] {sym['name']}")


def cmd_imports(args, index: dict):
    """Find files that import a given module."""
    target = args.module
    results = []
    for f in index["files"]:
        for imp in f.get("imports", []):
            if target in imp or imp.endswith(target):
                results.append((f["path"], imp))
                break

    for path, imp in results:
        print(f"{path}  <-  {imp}")


def cmd_entries(args, index: dict):
    """List entry points."""
    for ep in index.get("entry_points", []):
        print(ep)
    if not index.get("entry_points"):
        print("No entry points identified.")


def cmd_dirs(args, index: dict):
    """Show directory overview."""
    dirmap = index.get("dir_map", {})
    depth = args.depth

    # Aggregate at requested depth
    aggregated: dict[str, dict] = defaultdict(lambda: {"file_count": 0, "total_size": 0, "languages": defaultdict(int)})

    for dirpath, info in dirmap.items():
        parts = dirpath.split(os.sep)
        key = os.sep.join(parts[:depth]) if depth > 0 else dirpath
        aggregated[key]["file_count"] += info["file_count"]
        aggregated[key]["total_size"] += info["total_size"]
        for lang, cnt in info.get("languages", {}).items():
            aggregated[key]["languages"][lang] += cnt

    print(f"{'Directory':<40} {'Files':>6} {'Size':>10} {'Languages'}")
    print("-" * 80)
    for d in sorted(aggregated.keys()):
        info = aggregated[d]
        langs = ", ".join(f"{l}:{c}" for l, c in sorted(info["languages"].items()))
        size_kb = info["total_size"] / 1024
        size_str = f"{size_kb:.0f}KB" if size_kb < 1024 else f"{size_kb/1024:.1f}MB"
        print(f"{d:<40} {info['file_count']:>6} {size_str:>10} {langs}")


def cmd_files(args, index: dict):
    """Find files by glob pattern."""
    import fnmatch
    pattern = args.pattern
    results = [f["path"] for f in index["files"] if fnmatch.fnmatch(f["path"], pattern)]
    # Also try basename match
    if not results:
        results = [f["path"] for f in index["files"] if fnmatch.fnmatch(os.path.basename(f["path"]), pattern)]

    for r in sorted(results):
        # Find matching file entry for size/symbol info
        entry = next((f for f in index["files"] if f["path"] == r), None)
        if entry:
            syms = ", ".join(f"{s['type']}:{s['name']}" for s in entry.get("symbols", [])[:5])
            sym_suffix = f"  [{entry['lang']}] {syms}"
            if len(entry.get("symbols", [])) > 5:
                sym_suffix += f" (+{len(entry['symbols']) - 5} more)"
        else:
            sym_suffix = ""
        print(f"{r}{sym_suffix}")


def cmd_search(args, index: dict):
    """Full-text search across symbol names and file paths."""
    query = args.query.lower()
    results: list[tuple[str, dict]] = []

    for f in index["files"]:
        # Filter by language
        if args.lang and f["lang"] != args.lang:
            continue

        matched_symbols = []
        for s in f.get("symbols", []):
            if args.type and s["type"] != args.type:
                continue
            if query in s["name"].lower():
                matched_symbols.append(s)

        # Also match file path
        path_match = query in f["path"].lower()

        if matched_symbols or path_match:
            results.append((f["path"], {
                "entry": f,
                "matched_symbols": matched_symbols,
                "path_match": path_match,
            }))

    # Sort: more symbol matches first, then path matches
    results.sort(key=lambda x: -(len(x[1]["matched_symbols"]) * 2 + (1 if x[1]["path_match"] else 0)))

    for path, info in results[:args.limit]:
        f = info["entry"]
        if info["matched_symbols"]:
            for s in info["matched_symbols"][:5]:
                print(f"{path}:{s['line']}  [{s['type']}] {s['name']}")
        elif info["path_match"]:
            sym_count = len(f.get("symbols", []))
            print(f"{path}  [{f['lang']}] {sym_count} symbols")


def cmd_deps(args, index: dict):
    """Show dependencies of a file (what it imports)."""
    target = args.path
    graph = index.get("import_graph", {})

    visited: set[str] = set()
    current = [target]

    for level in range(args.depth):
        if not current:
            break
        next_level: list[str] = []
        for f in current:
            if f in visited:
                continue
            visited.add(f)
            deps = graph.get(f, [])
            indent = "  " * level
            print(f"{indent}{f}")
            for dep in sorted(deps):
                if dep not in visited:
                    next_level.append(dep)
        current = next_level


def cmd_dependents(args, index: dict):
    """Show files that depend on the given file."""
    target = args.path
    graph = index.get("import_graph", {})

    dependents = []
    for f, deps in graph.items():
        if target in deps:
            dependents.append(f)

    for d in sorted(dependents):
        print(d)

    if not dependents:
        print(f"No files depend on '{target}' (in import graph).")


def cmd_stats(args, index: dict):
    """Print index statistics."""
    meta = index["meta"]
    print(f"Project root:     {meta['root']}")
    print(f"Indexed at:       {meta['indexed_at']}")
    print(f"Total files:      {meta['total_files']}")
    print(f"Total symbols:    {meta['total_symbols']}")
    print(f"Build time:       {meta['build_time_ms']}ms")
    print(f"Entry points:     {len(index.get('entry_points', []))}")
    print(f"Config files:     {len(index.get('config_files', []))}")
    print(f"Import edges:     {len(index.get('import_graph', {}))}")
    print(f"\nLanguage breakdown:")
    for lang, cnt in sorted(meta.get("languages", {}).items(), key=lambda x: -x[1]):
        print(f"  {lang:<15} {cnt:>6} files")


def cmd_summary(args, index: dict):
    """Print a concise project overview (fits in ~200 tokens, good for agent context)."""
    meta = index["meta"]
    print(f"# {meta['root']}")
    print(f"Files: {meta['total_files']} | Symbols: {meta['total_symbols']} | Langs: {', '.join(meta['languages'])}")
    print()

    entries = index.get("entry_points", [])
    if entries:
        print(f"## Entry points ({len(entries)})")
        for ep in entries[:10]:
            print(f"  - {ep}")
        print()

    # Top-level directories
    dirmap = index.get("dir_map", {})
    top_dirs = defaultdict(lambda: {"files": 0, "size": 0, "langs": defaultdict(int)})
    for d, info in dirmap.items():
        top = d.split(os.sep)[0] if d != "." else "."
        top_dirs[top]["files"] += info["file_count"]
        top_dirs[top]["size"] += info["total_size"]
        for l, c in info["languages"].items():
            top_dirs[top]["langs"][l] += c

    print(f"## Top-level structure")
    for d in sorted(top_dirs.keys()):
        info = top_dirs[d]
        size_kb = info["size"] / 1024
        size_str = f"{size_kb:.0f}KB" if size_kb < 1024 else f"{size_kb / 1024:.1f}MB"
        langs = ", ".join(f"{l}:{c}" for l, c in sorted(info["langs"].items()))
        print(f"  {d:<30} {info['files']:>4} files  {size_str:>8}  [{langs}]")

    configs = index.get("config_files", [])
    if configs:
        print(f"\n## Config files: {', '.join(configs[:8])}")


def main():
    parser = argparse.ArgumentParser(description="Query a codebase index")
    parser.add_argument("-i", "--index", default=".code-index.json", help="Index file path")

    sub = parser.add_subparsers(dest="command", help="Query command")

    # symbol
    p = sub.add_parser("symbol", help="Find symbol definition")
    p.add_argument("name", help="Symbol name (substring match)")
    p.add_argument("--exact", action="store_true", help="Exact name match")
    p.set_defaults(func=cmd_symbol)

    # imports
    p = sub.add_parser("imports", help="Find files importing a module")
    p.add_argument("module", help="Module name")
    p.set_defaults(func=cmd_imports)

    # entries
    p = sub.add_parser("entries", help="List entry points")
    p.set_defaults(func=cmd_entries)

    # dirs
    p = sub.add_parser("dirs", help="Directory overview")
    p.add_argument("--depth", type=int, default=2, help="Directory depth")
    p.set_defaults(func=cmd_dirs)

    # files
    p = sub.add_parser("files", help="Find files by pattern")
    p.add_argument("pattern", help="Glob pattern (e.g. 'auth*.py')")
    p.set_defaults(func=cmd_files)

    # search
    p = sub.add_parser("search", help="Search symbols and files")
    p.add_argument("query", help="Search query")
    p.add_argument("--type", help="Symbol type filter")
    p.add_argument("--lang", help="Language filter")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.set_defaults(func=cmd_search)

    # deps
    p = sub.add_parser("deps", help="Show file dependencies")
    p.add_argument("path", help="File path relative to root")
    p.add_argument("--depth", type=int, default=1, help="Traversal depth")
    p.set_defaults(func=cmd_deps)

    # dependents
    p = sub.add_parser("dependents", help="Show files depending on this file")
    p.add_argument("path", help="File path relative to root")
    p.set_defaults(func=cmd_dependents)

    # stats
    p = sub.add_parser("stats", help="Index statistics")
    p.set_defaults(func=cmd_stats)

    # summary
    p = sub.add_parser("summary", help="Concise project overview")
    p.set_defaults(func=cmd_summary)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    index_path = os.path.join(os.path.abspath("."), args.index)
    if not os.path.isfile(index_path):
        # Try to find it
        for root_dir in [".", ".."]:
            candidate = os.path.join(os.path.abspath(root_dir), args.index)
            if os.path.isfile(candidate):
                index_path = candidate
                break
        else:
            print(f"Index not found. Run code_index.py first to build {args.index}", file=sys.stderr)
            sys.exit(1)

    index = load_index(index_path)
    args.func(args, index)


if __name__ == "__main__":
    main()
