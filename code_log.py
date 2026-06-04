#!/usr/bin/env python3
"""
Git log analysis integrated with codebase index.

Answers questions like:
  - "Which recent commits touched the auth system?"
  - "What's the impact of commit abc1234?"
  - "How has authenticate() evolved over the last 2 weeks?"
  - "Which areas of the codebase are changing fastest?"

Core flow: git log → changed files → cross-reference index → impact report.
"""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict

# ── Git helpers ────────────────────────────────────────────────────

def _git(args: list[str], cwd: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            ["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout.strip()
    except FileNotFoundError:
        raise RuntimeError("git command not found")


def git_log_recent(
    root: str, n: int = 20, since: str | None = None, until: str | None = None,
    author: str | None = None, grep: str | None = None, paths: list[str] | None = None,
) -> list[dict]:
    """Get recent commits with metadata and changed files."""
    args = ["log", f"-{n}", "--format=%H%x00%h%x00%aI%x00%an%x00%s"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    if author:
        args.append(f"--author={author}")
    if grep:
        args.append(f"--grep={grep}")
        args.append("-i")  # case-insensitive grep
    if paths:
        args.extend(["--"] + paths)

    output = _git(args, root)
    if not output:
        return []

    commits: list[dict] = []
    for line in output.split("\n"):
        parts = line.split("\x00", 4)
        if len(parts) < 5:
            continue
        full_hash, short_hash, date_str, author_name, message = parts

        # Get changed files for this commit
        try:
            changed = _git(["diff-tree", "--no-commit-id", "--name-only", "-r", full_hash], root)
            files = changed.split("\n") if changed else []
        except Exception:
            files = []

        commits.append({
            "hash": full_hash,
            "short": short_hash,
            "date": date_str,
            "author": author_name,
            "message": message,
            "files": files,
        })

    return commits


def git_log_search_symbol(root: str, symbol: str, n: int = 20) -> list[dict]:
    """Find commits that added or removed references to a symbol."""
    output = _git(["log", f"-{n}", "-p", "-S", symbol, "--format=%H%x00%h%x00%aI%x00%an%x00%s"], root)
    if not output:
        return []

    commits: list[dict] = []
    current: dict | None = None
    for line in output.split("\n"):
        if "\x00" in line:
            if current:
                commits.append(current)
            parts = line.split("\x00", 4)
            if len(parts) >= 5:
                current = {
                    "hash": parts[0], "short": parts[1], "date": parts[2],
                    "author": parts[3], "message": parts[4], "files": [], "diff_lines": [],
                }
        elif current and line.startswith(("---", "+++", "@@", "+", "-")):
            if line.startswith("--- a/") or line.startswith("+++ b/"):
                fpath = line[6:] if line.startswith("--- a/") else line[6:]
                if fpath not in current["files"]:
                    current["files"].append(fpath)
            elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                if symbol in line:
                    current["diff_lines"].append(line[:120])
    if current:
        commits.append(current)

    return commits


def git_log_file_history(root: str, filepath: str, n: int = 20) -> list[dict]:
    """Get commit history for a specific file."""
    output = _git(["log", f"-{n}", "--format=%H%x00%h%x00%aI%x00%an%x00%s", "--", filepath], root)
    if not output:
        return []
    commits: list[dict] = []
    for line in output.split("\n"):
        parts = line.split("\x00", 4)
        if len(parts) >= 5:
            commits.append({
                "hash": parts[0], "short": parts[1], "date": parts[2],
                "author": parts[3], "message": parts[4], "files": [filepath],
            })
    return commits


def git_commit_detail(root: str, commit: str) -> dict | None:
    """Get detailed info for a single commit."""
    try:
        info = _git(["log", "-1", "--format=%H%x00%h%x00%aI%x00%an%x00%s%x00%b", commit], root)
        parts = info.split("\x00", 5)
        changed = _git(["diff-tree", "--no-commit-id", "--name-status", "-r", commit], root)
        files = []
        for line in changed.split("\n"):
            if line:
                sp = line.split("\t", 1)
                files.append({"status": sp[0], "path": sp[1]}) if len(sp) == 2 else None

        diff_stat = _git(["diff-tree", "--no-commit-id", "--stat", "-r", commit], root)

        return {
            "hash": parts[0], "short": parts[1], "date": parts[2],
            "author": parts[3], "message": parts[4], "body": parts[5] if len(parts) > 5 else "",
            "files": files, "diff_stat": diff_stat,
        }
    except Exception as e:
        print(f"Error getting commit detail: {e}", file=sys.stderr)
        return None

# ── Index integration ──────────────────────────────────────────────

def find_store(root: str, store_dir: str | None = None) -> str:
    if store_dir:
        return store_dir
    return os.path.join(root, ".code-index")


def load_current_index(store: str) -> dict | None:
    """Load the current index."""
    candidate = os.path.join(store, "current.json")
    if not os.path.isfile(candidate):
        # Try legacy flat file
        candidate = os.path.join(os.path.dirname(store), ".code-index.json")
        if not os.path.isfile(candidate):
            return None
    with open(candidate) as f:
        return json.load(f)


def load_index_for_commit(store: str, commit: str) -> dict | None:
    """Try to load the exact index for a commit, or fall back to current."""
    commits_dir = os.path.join(store, "commits")
    candidate = os.path.join(commits_dir, f"{commit}.json")
    if os.path.isfile(candidate):
        with open(candidate) as f:
            return json.load(f)
    # Try partial match
    if os.path.isdir(commits_dir):
        for fname in os.listdir(commits_dir):
            if fname.startswith(commit) and fname.endswith(".json"):
                with open(os.path.join(commits_dir, fname)) as f:
                    return json.load(f)
    # Fall back to current
    return load_current_index(store)


def get_dependents(index: dict, paths: list[str]) -> dict[str, list[str]]:
    """For each path, find all files that depend on it."""
    graph = index.get("import_graph", {})
    result: dict[str, list[str]] = {}
    for path in paths:
        deps = []
        for f, f_deps in graph.items():
            if path in f_deps:
                deps.append(f)
        result[path] = deps
    return result


def get_file_symbols(index: dict, path: str) -> list[dict]:
    """Get symbols defined in a file according to the index."""
    for entry in index.get("files", []):
        if entry["path"] == path:
            return entry.get("symbols", [])
    return []


def get_symbol_file(index: dict, symbol: str) -> str | None:
    """Find which file defines a symbol."""
    for entry in index.get("files", []):
        for s in entry.get("symbols", []):
            if s["name"] == symbol:
                return entry["path"]
    return None

# ── Analysis functions ─────────────────────────────────────────────

def analyze_recent(root: str, index: dict | None, n: int, since: str | None,
                   until: str | None, author: str | None, area: str | None) -> list[dict]:
    """Get recent commits with impact analysis."""
    paths = [area] if area else None
    commits = git_log_recent(root, n=n, since=since, until=until, author=author, paths=paths)

    if not index:
        return commits

    files_index = {e["path"]: e for e in index.get("files", [])}

    for commit in commits:
        # Categorize changed files
        areas: dict[str, int] = defaultdict(int)
        total_dependents = 0
        high_impact_files: list[str] = []

        for fpath in commit.get("files", []):
            # Area categorization (top-level dir)
            area = fpath.split("/")[0] if "/" in fpath else "root"
            areas[area] += 1

            # Check dependency impact
            deps = get_dependents(index, [fpath])
            dep_count = len(deps.get(fpath, []))
            total_dependents += dep_count
            if dep_count >= 5:
                high_impact_files.append(fpath)

            # Check if file exists in index
            if fpath in files_index:
                pass  # known file

        commit["areas"] = dict(areas)
        commit["dependent_count"] = total_dependents
        commit["high_impact_files"] = high_impact_files
        commit["symbol_count"] = sum(
            len(get_file_symbols(index, f)) for f in commit.get("files", [])
        )

    return commits


def analyze_symbol_trace(root: str, index: dict | None, symbol: str, n: int) -> list[dict]:
    """Trace a symbol's evolution through commits."""
    commits = git_log_search_symbol(root, symbol, n=n)

    if not index:
        return commits

    for commit in commits:
        deps = get_dependents(index, commit.get("files", []))
        commit["impact"] = {f: len(d) for f, d in deps.items() if d}

    return commits


def analyze_commit_impact(root: str, index: dict | None, commit_ref: str) -> dict | None:
    """Deep analysis of a single commit's impact."""
    detail = git_commit_detail(root, commit_ref)
    if not detail:
        return None

    if not index:
        return detail

    changed_paths = [f["path"] for f in detail.get("files", [])]

    # For each changed file, get symbols and dependents
    file_analysis: list[dict] = []
    total_dependents = 0
    symbols_affected: list[str] = []

    for fpath in changed_paths:
        symbols = get_file_symbols(index, fpath)
        deps = get_dependents(index, [fpath])
        dep_list = deps.get(fpath, [])

        total_dependents += len(dep_list)
        symbols_affected.extend(s["name"] for s in symbols)

        file_analysis.append({
            "path": fpath,
            "symbols": symbols,
            "dependents": dep_list[:10],  # cap at 10
            "dependent_count": len(dep_list),
        })

    # Sort files by impact (dependent count descending)
    file_analysis.sort(key=lambda f: -f["dependent_count"])

    detail["file_analysis"] = file_analysis
    detail["total_dependents"] = total_dependents
    detail["symbols_affected"] = list(set(symbols_affected))
    detail["impact_score"] = total_dependents + len(changed_paths)

    return detail


def analyze_hotspots(root: str, index: dict | None, n: int, since: str | None) -> list[dict]:
    """Identify code hotspots (files that change frequently)."""
    commits = git_log_recent(root, n=n, since=since)

    # Aggregate file change frequency
    file_freq: dict[str, list[dict]] = defaultdict(list)
    for c in commits:
        for f in c.get("files", []):
            file_freq[f].append({"commit": c["short"], "date": c["date"], "message": c["message"]})

    # Score: frequency + dependents (if index available)
    hotspots: list[dict] = []
    for fpath, changes in file_freq.items():
        score = len(changes)
        dep_count = 0
        if index:
            deps = get_dependents(index, [fpath])
            dep_count = len(deps.get(fpath, []))
            score += dep_count  # more dependents = hotter
        hotspots.append({
            "path": fpath,
            "changes": len(changes),
            "dependent_count": dep_count,
            "score": score,
            "recent_commits": [c["commit"] for c in changes[:3]],
            "recent_messages": [c["message"] for c in changes[:3]],
        })

    hotspots.sort(key=lambda h: -h["score"])
    return hotspots[:20]


def search_commits(root: str, index: dict | None, query: str,
                   search_in: str = "all", n: int = 30) -> list[dict]:
    """Search commits by message, code changes, or both."""
    results: list[dict] = []

    if search_in in ("message", "all"):
        # Search commit messages
        commits = git_log_recent(root, n=n, grep=query)
        for c in commits:
            c["match_type"] = "message"
        results.extend(commits)

    if search_in in ("code", "all"):
        # Search for symbol changes
        commits = git_log_search_symbol(root, query, n=n)
        existing_hashes = {c["hash"] for c in results}
        for c in commits:
            if c["hash"] not in existing_hashes:
                c["match_type"] = "code"
                results.append(c)

    # Deduplicate and sort by date
    seen: set[str] = set()
    unique: list[dict] = []
    for c in sorted(results, key=lambda x: x.get("date", ""), reverse=True):
        if c["hash"] not in seen:
            seen.add(c["hash"])
            unique.append(c)

    # Add impact scoring if index available
    if index and unique:
        for c in unique[:20]:
            deps = get_dependents(index, c.get("files", []))
            c["impact_score"] = sum(len(d) for d in deps.values())

    return unique[:n]

# ── Renderers ──────────────────────────────────────────────────────

def render_commits_timeline(commits: list[dict], max_width: int = 100) -> str:
    """Render commits as a readable timeline."""
    lines: list[str] = []
    for i, c in enumerate(commits):
        date_short = c.get("date", "")[:10]
        msg = c.get("message", "")
        short = c.get("short", "")[:8]
        author = c.get("author", "")[:15]

        # Impact indicators
        extras: list[str] = []
        if c.get("high_impact_files"):
            extras.append(f"⚠{len(c['high_impact_files'])}")
        if c.get("dependent_count"):
            extras.append(f"→{c['dependent_count']}deps")
        if c.get("symbol_count"):
            extras.append(f"Σ{c['symbol_count']}")
        if c.get("match_type"):
            extras.append(f"[{c['match_type']}]")
        extra_str = " " + " ".join(extras) if extras else ""

        lines.append(f"{date_short} {short} {author:<15} {msg[:max_width]}{extra_str}")

        # Show areas
        areas = c.get("areas", {})
        if areas:
            area_str = ", ".join(f"{a}:{n}" for a, n in sorted(areas.items(), key=lambda x: -x[1])[:5])
            lines.append(f"         {' ' * 8} files: {len(c.get('files',[]))} [{area_str}]")

        # Show high-impact files inline
        hi_files = c.get("high_impact_files", [])[:3]
        if hi_files:
            lines.append(f"         {' ' * 8} ⚡ {'  '.join(hi_files)}")

        if i < len(commits) - 1:
            lines.append("")

    return "\n".join(lines)


def render_hotspots(hotspots: list[dict]) -> str:
    """Render hotspot analysis."""
    lines = [f"{'Score':<6} {'Chg':>4} {'Deps':>5}  {'File':<40}  {'Recent commits'}"]
    lines.append("-" * 85)
    for h in hotspots:
        recent = ", ".join(h.get("recent_commits", [])[:3])
        lines.append(
            f"{h['score']:<6} {h['changes']:>4} {h['dependent_count']:>5}  "
            f"{h['path']:<40}  {recent}"
        )
    return "\n".join(lines)


def render_trace(commits: list[dict], symbol: str) -> str:
    """Render symbol trace output."""
    lines = [f"# Evolution of '{symbol}'", ""]
    for c in commits:
        date = c.get("date", "")[:10]
        short = c.get("short", "")[:8]
        msg = c.get("message", "")
        impact = c.get("impact", {})

        lines.append(f"{date} {short}  {msg}")
        for line in c.get("diff_lines", [])[:3]:
            lines.append(f"         {line.strip()}")
        if impact:
            imp_str = ", ".join(f"{f}(→{n})" for f, n in sorted(impact.items(), key=lambda x: -x[1])[:3])
            lines.append(f"         impact: {imp_str}")
        lines.append("")
    return "\n".join(lines)


def render_impact(detail: dict) -> str:
    """Render commit impact analysis."""
    lines = [
        f"# Impact analysis for {detail['short']}",
        f"Author:  {detail['author']}",
        f"Date:    {detail['date']}",
        f"Message: {detail['message']}",
    ]

    if detail.get("body"):
        lines.append(f"\n{detail['body'].strip()}")

    lines.extend([
        "",
        f"## Files changed: {len(detail.get('files', []))}",
        f"Total dependents affected: {detail.get('total_dependents', 0)}",
        f"Symbols affected: {len(detail.get('symbols_affected', []))}",
        f"Impact score: {detail.get('impact_score', 0)}",
        "",
    ])

    if detail.get("diff_stat"):
        lines.append("```")
        lines.append(detail["diff_stat"].strip())
        lines.append("```")
        lines.append("")

    # Per-file breakdown
    for fa in detail.get("file_analysis", [])[:15]:
        status = next((f["status"] for f in detail["files"] if f["path"] == fa["path"]), "M")
        sym_str = ", ".join(f"{s['name']}" for s in fa["symbols"][:6])
        dep_str = ", ".join(fa.get("dependents", [])[:5])
        lines.append(f"  {status} {fa['path']}  (→{fa['dependent_count']} deps)")
        if sym_str:
            lines.append(f"      symbols: {sym_str}")
        if dep_str:
            lines.append(f"      dependents: {dep_str}")

    return "\n".join(lines)


def render_area_summary(commits: list[dict]) -> str:
    """Aggregate recent activity by code area."""
    area_stats: dict[str, dict] = defaultdict(lambda: {
        "commits": 0, "files_changed": set(), "authors": set(),
    })
    for c in commits:
        for area, _count in c.get("areas", {}).items():
            area_stats[area]["commits"] += 1
            area_stats[area]["authors"].add(c.get("author", ""))

    lines = [f"{'Area':<20} {'Commits':>8} {'Authors':>6}"]
    lines.append("-" * 40)
    for area, stats in sorted(area_stats.items(), key=lambda x: -x[1]["commits"]):
        lines.append(f"{area:<20} {stats['commits']:>8} {len(stats['authors']):>6}")
    return "\n".join(lines)

# ── Main ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Git log analysis integrated with codebase index."
    )
    parser.add_argument("root", nargs="?", default=".", help="Project root")
    parser.add_argument("--store-dir", default=None, help="Index store directory")
    parser.add_argument("-i", "--index", action="store_true", default=True, help="Use index (default)")
    parser.add_argument("--no-index", action="store_true", help="Skip index, pure git log")

    sub = parser.add_subparsers(dest="command", help="Analysis command")

    # recent
    p = sub.add_parser("recent", help="Recent commits with impact summary")
    p.add_argument("-n", type=int, default=20, help="Number of commits")
    p.add_argument("--since", help="Start date (e.g. '2026-05-01')")
    p.add_argument("--until", help="End date")
    p.add_argument("--author", help="Filter by author")
    p.add_argument("--area", help="Filter by directory")
    p.add_argument("--area-summary", action="store_true", help="Aggregate by area only")

    # search
    p = sub.add_parser("search", help="Search commits by message or code")
    p.add_argument("query", help="Search query")
    p.add_argument("-n", type=int, default=30, help="Max results")
    p.add_argument("--in", dest="search_in", choices=["message", "code", "all"], default="all",
                   help="Where to search")

    # trace
    p = sub.add_parser("trace", help="Trace a symbol's evolution through commits")
    p.add_argument("symbol", help="Symbol name to trace")
    p.add_argument("-n", type=int, default=20, help="Max commits")

    # impact
    p = sub.add_parser("impact", help="Analyze a specific commit's impact")
    p.add_argument("commit", help="Commit ref to analyze")

    # hotspots
    p = sub.add_parser("hotspots", help="Identify frequently-changing code areas")
    p.add_argument("-n", type=int, default=100, help="Commits to analyze")
    p.add_argument("--since", help="Start date")

    # area-activity (new)
    p = sub.add_parser("area", help="Show activity by code area/bug frequency")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    root = os.path.abspath(args.root)
    store = find_store(root, args.store_dir)

    # Load index
    index: dict | None = None
    if not args.no_index:
        index = load_current_index(store)
        if index:
            print(f"  Using index: {index['meta'].get('git_short_commit', '?')} "
                  f"({index['meta'].get('total_files', 0)} files)", file=sys.stderr)
        else:
            print("  No index found. Run: python code_index.py .", file=sys.stderr)

    # ── Dispatch ──
    if args.command == "recent":
        commits = analyze_recent(root, index, args.n, args.since, args.until, args.author, args.area)
        if args.area_summary:
            print(render_area_summary(commits))
        else:
            print(render_commits_timeline(commits))

    elif args.command == "search":
        commits = search_commits(root, index, args.query, args.search_in, args.n)
        if not commits:
            print(f"No commits found matching '{args.query}'")
        else:
            print(render_commits_timeline(commits))

    elif args.command == "trace":
        commits = analyze_symbol_trace(root, index, args.symbol, args.n)
        if not commits:
            print(f"No commit history found for symbol '{args.symbol}'")
        else:
            print(render_trace(commits, args.symbol))

    elif args.command == "impact":
        detail = analyze_commit_impact(root, index, args.commit)
        if not detail:
            print(f"Could not analyze commit '{args.commit}'")
            sys.exit(1)
        print(render_impact(detail))

    elif args.command == "hotspots":
        hotspots = analyze_hotspots(root, index, args.n, args.since)
        print(render_hotspots(hotspots))

    elif args.command == "area":
        # Show area activity over recent commits
        commits = git_log_recent(root, n=50, since=getattr(args, 'since', None))
        # Enrich with index data
        if index:
            for c in commits:
                areas: dict[str, int] = defaultdict(int)
                for f in c.get("files", []):
                    area = f.split("/")[0] if "/" in f else "root"
                    areas[area] += 1
                c["areas"] = dict(areas)
        print("## Recent activity by area (last 50 commits)")
        print(render_area_summary(commits))


if __name__ == "__main__":
    main()
