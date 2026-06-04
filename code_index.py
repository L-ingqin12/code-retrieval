#!/usr/bin/env python3
"""
Git-aware codebase indexer for AI agent consumption.

Design goals:
- Process 100k files in under 30s (parallel, regex-only, no AST)
- Compact JSON output suitable for agent context windows
- Incremental: re-index only changed files (filesystem mode)
- Git-aware: index at any commit without checkout; store indices per commit
- Language-agnostic with pluggable extractors

Storage layout (when --store-dir is used):
  .code-index/
    commits/<commit_hash>.json    # per-commit indices
    heads/<branch_name>.json       # per-branch symlinks/copies
    current.json                   # latest index

Output index structure (~1-3% of codebase size):
  {meta{root,commit,branch,...}, files{symbols,imports,exports}, import_graph, entry_points, dir_map}
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# ── Language patterns ──────────────────────────────────────────────

LANG_PATTERNS = {
    "python": {
        "extensions": [".py", ".pyx", ".pxd"],
        "symbols": [
            (r"^\s*class\s+(\w+)", "class", 1),
            (r"^\s*async\s+def\s+(\w+)", "async_function", 1),
            (r"^\s*def\s+(\w+)", "function", 1),
        ],
        "imports": [
            (r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", [1, 2]),
        ],
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "symbols": [
            (r"^\s*(?:export\s+)?class\s+(\w+)", "class", 1),
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function", 1),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", "function", 1),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*\([^)]*\)\s*=>", "function", 1),
            (r"^\s*(?:export\s+)?interface\s+(\w+)", "interface", 1),
            (r"^\s*(?:export\s+)?type\s+(\w+)", "type", 1),
            (r"^\s*(?:export\s+)?enum\s+(\w+)", "enum", 1),
        ],
        "imports": [
            (r"""(?:import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]|import\s+['\"]([^'\"]+)['\"])""", [1, 2]),
            (r"require\s*\(\s*['\"]([^'\"]+)['\"]", [1]),
        ],
    },
    "javascript": {
        "extensions": [".js", ".jsx", ".mjs", ".cjs"],
        "symbols": [
            (r"^\s*class\s+(\w+)", "class", 1),
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function", 1),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", "function", 1),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*\([^)]*\)\s*=>", "function", 1),
        ],
        "imports": [
            (r"""(?:import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]|import\s+['\"]([^'\"]+)['\"]|require\s*\(\s*['\"]([^'\"]+)['\"])""", [1, 2, 3]),
        ],
    },
    "go": {
        "extensions": [".go"],
        "symbols": [
            (r"^\s*type\s+(\w+)\s+struct", "struct", 1),
            (r"^\s*type\s+(\w+)\s+interface", "interface", 1),
            (r"^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)", "function", 1),
            (r"^\s*func\s+\(\w+\s+\*?(\w+)\)\s+(\w+)", "method", 2),
        ],
        "imports": [
            (r"""import\s+(?:\(\s*)?(?:[_\w]*\s+)?["\"]([^"\"]+)["\"]""", [1]),
        ],
    },
    "rust": {
        "extensions": [".rs"],
        "symbols": [
            (r"^\s*(?:pub\s+)?fn\s+(\w+)", "function", 1),
            (r"^\s*(?:pub\s+)?struct\s+(\w+)", "struct", 1),
            (r"^\s*(?:pub\s+)?enum\s+(\w+)", "enum", 1),
            (r"^\s*(?:pub\s+)?trait\s+(\w+)", "trait", 1),
            (r"^\s*(?:pub\s+)?impl\s+(?:[\w<>,: ]+\s+)?(?:for\s+)?(\w+)", "impl", 1),
            (r"^\s*(?:pub\s+)?mod\s+(\w+)", "module", 1),
            (r"^\s*macro_rules!\s*(\w+)", "macro", 1),
        ],
        "imports": [
            (r"^\s*use\s+([\w:]+)", [1]),
        ],
    },
    "java": {
        "extensions": [".java"],
        "symbols": [
            (r"^\s*(?:public\s+|private\s+|protected\s+)?class\s+(\w+)", "class", 1),
            (r"^\s*(?:public\s+|private\s+|protected\s+)?interface\s+(\w+)", "interface", 1),
            (r"^\s*(?:public\s+|private\s+|protected\s+)?enum\s+(\w+)", "enum", 1),
            (r"^\s*(?:public\s+|private\s+|protected\s+)?(?:static\s+)?[\w<>[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:\{|throws)", "method", 1),
        ],
        "imports": [
            (r"^\s*import\s+([\w.]+)", [1]),
        ],
    },
    "cpp": {
        "extensions": [".cpp", ".cc", ".cxx", ".c++", ".hpp", ".h", ".hh", ".hxx"],
        "symbols": [
            (r"^\s*(?:template\s*<[^>]*>\s*)?class\s+(?:\w+\s+)?(\w+)", "class", 1),
            (r"^\s*(?:template\s*<[^>]*>\s*)?struct\s+(\w+)", "struct", 1),
            (r"^\s*(?:virtual\s+|static\s+|inline\s+|const\s+)*[\w:]+\s+(\w+)\s*\([^)]*\)\s*(?:const\s*)?(?:\{|override)", "function", 1),
        ],
        "imports": [
            (r'^\s*#include\s+[<"]([^>"]+)[>"]', [1]),
        ],
    },
    "ruby": {
        "extensions": [".rb"],
        "symbols": [
            (r"^\s*class\s+(\w+)", "class", 1),
            (r"^\s*module\s+(\w+)", "module", 1),
            (r"^\s*def\s+(\w+)", "method", 1),
        ],
        "imports": [
            (r"""^\s*(?:require|require_relative|load)\s+['\"]([^'\"]+)['\"]""", [1]),
        ],
    },
    "shell": {
        "extensions": [".sh", ".bash", ".zsh"],
        "symbols": [
            (r"^\s*(?:function\s+)?(\w+)\s*\(\s*\)\s*\{", "function", 1),
        ],
        "imports": [
            (r"""^\s*(?:source|\.)\s+['\"]?([^'\"\s]+)['\"]?""", [1]),
        ],
    },
}

ALWAYS_IGNORE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    "target", ".next", ".nuxt", "vendor", "bower_components",
    ".idea", ".vscode", ".vs", "*.min.js", "*.min.css", "*.bundle.js",
    "*.generated.*", "*.pb.go", "*.pb.cc", "*.pb.h",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "Gemfile.lock", "poetry.lock", "Pipfile.lock",
    ".DS_Store", "Thumbs.db",
}

ENTRY_INDICATORS = [
    "main.py", "main.ts", "main.go", "main.rs", "main.cpp", "main.c",
    "app.py", "app.ts", "server.py", "server.ts", "server.go",
    "index.ts", "index.js", "index.tsx", "index.jsx",
    "cmd/", "cmd/main.go", "src/main/",
    "__main__.py", "__init__.py",
    "Program.cs", "Application.java",
]

CONFIG_INDICATORS = [
    "package.json", "tsconfig.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "go.sum", "Makefile", "CMakeLists.txt",
    "Dockerfile", "docker-compose.yml", ".dockerignore",
    ".env", ".env.example", ".env.local", ".env.production",
    ".gitignore", ".eslintrc", ".prettierrc", "biome.json",
    "next.config.js", "vite.config.ts", "webpack.config.js",
    ".github/workflows/", ".gitlab-ci.yml", "Jenkinsfile",
    "README.md", "CONTRIBUTING.md", "CHANGELOG.md",
]

# ── Git helpers ────────────────────────────────────────────────────

def _git(args: list[str], cwd: str, timeout: int = 15) -> str:
    """Run a git command, return stdout. Raises on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return result.stdout.strip()
    except FileNotFoundError:
        raise RuntimeError("git command not found")


def is_git_repo(root: str) -> bool:
    """Check if root is inside a git repository."""
    try:
        _git(["rev-parse", "--git-dir"], root)
        return True
    except Exception:
        return False


def get_git_info(root: str) -> dict:
    """Collect git metadata for the current working tree."""
    info: dict = {}
    try:
        info["commit"] = _git(["rev-parse", "HEAD"], root)
    except Exception:
        info["commit"] = None

    try:
        info["short_commit"] = _git(["rev-parse", "--short", "HEAD"], root)
    except Exception:
        info["short_commit"] = None

    try:
        info["branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    except Exception:
        info["branch"] = None

    try:
        info["tag"] = _git(["describe", "--tags", "--exact-match"], root)
    except Exception:
        info["tag"] = None

    # Describe: nearest tag + distance
    try:
        info["describe"] = _git(["describe", "--tags", "--always"], root)
    except Exception:
        info["describe"] = None

    # Dirty check
    try:
        status = _git(["status", "--porcelain"], root)
        info["dirty"] = bool(status)
    except Exception:
        info["dirty"] = None

    return info


def git_list_files(root: str, commit: str) -> list[str]:
    """List all files tracked by git at a given commit."""
    output = _git(["ls-tree", "-r", "--name-only", commit], root)
    if not output:
        return []
    return [f for f in output.split("\n") if f]


def git_read_file(root: str, commit: str, path: str) -> str | None:
    """Read a file's content at a given commit. Returns None on binary or error."""
    try:
        return _git(["show", f"{commit}:{path}"], root, timeout=10)
    except Exception:
        return None


def git_changed_files(root: str, base: str, target: str) -> list[str]:
    """List files changed between two commits."""
    output = _git(["diff", "--name-only", base, target], root)
    if not output:
        return []
    return output.split("\n")


def git_changed_files_detailed(root: str, base: str, target: str) -> list[dict]:
    """List files changed with status (A/M/D) between two commits."""
    output = _git(["diff", "--name-status", base, target], root)
    if not output:
        return []
    results: list[dict] = []
    for line in output.split("\n"):
        parts = line.split("\t", 1)
        if len(parts) == 2:
            results.append({"status": parts[0], "path": parts[1]})
    return results


def git_resolve_ref(root: str, ref: str) -> str:
    """Resolve a ref (branch, tag, HEAD~N) to a full commit hash."""
    return _git(["rev-parse", ref], root)

# ── Detection & filtering ──────────────────────────────────────────

def detect_language(filepath: str) -> str | None:
    """Match file extension to language."""
    ext = os.path.splitext(filepath)[1].lower()
    if not ext:
        base = os.path.basename(filepath)
        if base in ("Dockerfile", "Makefile", "Justfile"):
            return "makefile"
        return None
    for lang, cfg in LANG_PATTERNS.items():
        if ext in cfg["extensions"]:
            return lang
    return None


def should_ignore(filepath: str, extra_ignore: set, root: str) -> bool:
    """Check if file should be ignored."""
    rel = os.path.relpath(filepath, root)
    parts = set(Path(rel).parts)
    if parts & ALWAYS_IGNORE:
        return True
    base = os.path.basename(filepath)
    for pat in ALWAYS_IGNORE:
        if pat.startswith("*.") and base.endswith(pat[1:]):
            return True
    if extra_ignore:
        for pat in extra_ignore:
            if pat in parts or (pat.startswith("*.") and base.endswith(pat[1:])):
                return True
    return False


def parse_gitignore(root: str) -> set[str]:
    """Read .gitignore and return a set of patterns."""
    patterns: set[str] = set()
    gitignore = os.path.join(root, ".gitignore")
    if not os.path.isfile(gitignore):
        return patterns
    try:
        with open(gitignore, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.add(line.strip("/"))
    except Exception:
        pass
    return patterns

# ── Symbol & import extraction ─────────────────────────────────────

def extract_symbols(lines: list[str], lang: str) -> list[dict]:
    """Regex-extract symbols from file lines."""
    cfg = LANG_PATTERNS.get(lang)
    if not cfg:
        return []
    symbols: list[dict] = []
    for i, line in enumerate(lines, 1):
        for pattern, sym_type, group in cfg["symbols"]:
            m = re.search(pattern, line)
            if m:
                name = m.group(group)
                if name:
                    symbols.append({"type": sym_type, "name": name, "line": i})
                    break
    return symbols


def extract_imports(lines: list[str], lang: str) -> list[str]:
    """Regex-extract imports from file lines."""
    cfg = LANG_PATTERNS.get(lang)
    if not cfg:
        return []
    imports: list[str] = []
    for line in lines:
        for pattern, groups in cfg["imports"]:
            m = re.search(pattern, line)
            if m:
                for g in groups:
                    val = m.group(g)
                    if val:
                        imports.append(val.strip("'\""))
    return list(set(imports))

# ── File indexing (filesystem & git modes) ─────────────────────────

def index_file_fs(args: tuple[str, str, str]) -> dict | None:
    """Index a file from the filesystem."""
    filepath, lang, root = args
    try:
        stat = os.stat(filepath)
        size = stat.st_size
        mtime = int(stat.st_mtime)
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(200_000)
    except Exception:
        return None
    return _index_content(content, lang, root, filepath, size, mtime)


def index_file_git(args: tuple[str, str, str, str]) -> dict | None:
    """Index a file from git at a specific commit."""
    root, commit, path, lang = args
    content = git_read_file(root, commit, path)
    if content is None:
        return None
    return _index_content(content, lang, root, path, len(content.encode()), 0)


def _index_content(
    content: str, lang: str, root: str, path: str, size: int, mtime: int,
) -> dict | None:
    """Index file content into entry dict. Shared by FS and git modes."""
    lines = content.split("\n")
    symbols = extract_symbols(lines, lang)
    imports = extract_imports(lines, lang)
    exports = [s["name"] for s in symbols if s["type"] in (
        "class", "function", "async_function", "struct", "enum", "trait",
        "interface", "type", "module", "method",
    )]
    line_count = len(lines)
    if len(content) >= 200_000:
        line_count = -1

    relpath = os.path.relpath(path, root) if os.path.isabs(path) else path
    return {
        "path": relpath,
        "size": size,
        "lines": line_count,
        "mtime": mtime,
        "lang": lang,
        "symbols": symbols,
        "imports": imports,
        "exports": exports,
    }

# ── Filesystem mode collection ─────────────────────────────────────

def collect_files_fs(root: str, ignore_patterns: set, follow_symlinks: bool = False) -> list[tuple[str, str]]:
    """Walk directory tree and return [(filepath, language), ...]."""
    results: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        dirnames[:] = [
            d for d in dirnames
            if d not in ALWAYS_IGNORE and d not in ignore_patterns and not d.startswith(".")
        ]
        for fname in filenames:
            filepath = os.path.join(dirpath, fname)
            if should_ignore(filepath, ignore_patterns, root):
                continue
            lang = detect_language(fname)
            if lang:
                results.append((filepath, lang))
    return results


def collect_files_git(root: str, commit: str) -> list[tuple[str, str]]:
    """Get all source files from git at a given commit."""
    all_files = git_list_files(root, commit)
    results: list[tuple[str, str]] = []
    for fpath in all_files:
        lang = detect_language(fpath)
        if lang and not should_ignore(fpath, set(), root):
            results.append((fpath, lang))
    return results

# ── Graph & metadata builders ──────────────────────────────────────

def build_import_graph(file_entries: list[dict]) -> dict[str, list[str]]:
    """Build import dependency graph."""
    export_map: dict[str, set[str]] = defaultdict(set)
    for entry in file_entries:
        for exp in entry.get("exports", []):
            export_map[exp].add(entry["path"])
        base = os.path.splitext(os.path.basename(entry["path"]))[0]
        export_map[base].add(entry["path"])

    graph: dict[str, list[str]] = defaultdict(list)
    for entry in file_entries:
        path = entry["path"]
        for imp in entry.get("imports", []):
            targets: set[str] = set()
            if imp in export_map:
                targets = export_map[imp]
            else:
                base = imp.split(".")[-1]
                if base in export_map:
                    targets = export_map[base]
            for t in targets:
                if t != path:
                    graph[path].append(t)
    return {k: list(set(v)) for k, v in graph.items()}


def find_entry_points(file_entries: list[dict]) -> list[str]:
    """Heuristically identify entry-point files."""
    entries: list[str] = []
    paths = [e["path"] for e in file_entries]
    for path in paths:
        base = os.path.basename(path)
        if base in ENTRY_INDICATORS:
            entries.append(path)
        for indicator in ENTRY_INDICATORS:
            if indicator.endswith("/") and ("/" + indicator) in ("/" + path):
                entries.append(path)
                break
    return sorted(set(entries))


def find_config_files(root: str) -> list[str]:
    """Find configuration files."""
    configs: list[str] = []
    for indicator in CONFIG_INDICATORS:
        if indicator.endswith("/"):
            full = os.path.join(root, indicator)
            if os.path.isdir(full):
                for f in os.listdir(full):
                    configs.append(os.path.join(indicator, f))
        else:
            if os.path.isfile(os.path.join(root, indicator)):
                configs.append(indicator)
    return sorted(configs)


def build_dir_map(file_entries: list[dict]) -> dict[str, dict]:
    """Build directory-level summary."""
    dirmap: dict[str, dict] = defaultdict(
        lambda: {"file_count": 0, "total_size": 0, "languages": defaultdict(int)}
    )
    for entry in file_entries:
        dirname = os.path.dirname(entry["path"]) or "."
        dirmap[dirname]["file_count"] += 1
        dirmap[dirname]["total_size"] += entry["size"]
        dirmap[dirname]["languages"][entry["lang"]] += 1
    result = {}
    for d, info in sorted(dirmap.items()):
        result[d] = {
            "file_count": info["file_count"],
            "total_size": info["total_size"],
            "languages": dict(info["languages"]),
        }
    return result


def smart_truncate_symbols(entries: list[dict], max_per_file: int) -> list[dict]:
    """Truncate symbol lists per file to keep index compact."""
    priority_types = {"class", "struct", "interface", "trait", "enum", "module"}
    for entry in entries:
        if len(entry.get("symbols", [])) > max_per_file:
            kept = [s for s in entry["symbols"] if s["type"] in priority_types]
            others = [s for s in entry["symbols"] if s["type"] not in priority_types]
            entry["symbols"] = (kept + others)[:max_per_file]
    return entries


def incremental_load(existing_path: str) -> dict | None:
    """Load existing index for incremental update."""
    if not os.path.isfile(existing_path):
        return None
    try:
        with open(existing_path, "r") as f:
            return json.load(f)
    except Exception:
        return None

# ── Index assembly ─────────────────────────────────────────────────

def assemble_index(
    root: str,
    indexed: list[dict],
    git_info: dict,
    build_ms: int,
    no_graph: bool = False,
) -> dict:
    """Assemble the full index dict from indexed entries."""
    indexed = smart_truncate_symbols(indexed, 100)
    indexed.sort(key=lambda e: e["path"])

    lang_stats: dict[str, int] = defaultdict(int)
    total_symbols = 0
    for e in indexed:
        lang_stats[e["lang"]] += 1
        total_symbols += len(e.get("symbols", []))

    import_graph: dict[str, list[str]] = {}
    if not no_graph:
        import_graph = build_import_graph(indexed)

    entry_points = find_entry_points(indexed)
    config_files = find_config_files(root)
    dir_map = build_dir_map(indexed)

    return {
        "meta": {
            "root": os.path.abspath(root),
            "indexed_at": int(time.time()),
            "build_time_ms": build_ms,
            "total_files": len(indexed),
            "total_symbols": total_symbols,
            "languages": dict(lang_stats),
            "git_commit": git_info.get("commit"),
            "git_short_commit": git_info.get("short_commit"),
            "git_branch": git_info.get("branch"),
            "git_tag": git_info.get("tag"),
            "git_describe": git_info.get("describe"),
            "git_dirty": git_info.get("dirty"),
        },
        "files": indexed,
        "import_graph": import_graph,
        "entry_points": entry_points,
        "config_files": config_files,
        "dir_map": dir_map,
    }

# ── Store management ───────────────────────────────────────────────

def store_index(index: dict, store_dir: str, commit: str | None, branch: str | None, *, is_current: bool = True):
    """Save index into the store directory, organized by commit and branch.

    Args:
        is_current: If True, update current.json. Set False for --commit historical indexing.
    """
    os.makedirs(store_dir, exist_ok=True)
    commits_dir = os.path.join(store_dir, "commits")
    heads_dir = os.path.join(store_dir, "heads")
    os.makedirs(commits_dir, exist_ok=True)
    os.makedirs(heads_dir, exist_ok=True)

    # Write by commit
    if commit:
        commit_path = os.path.join(commits_dir, f"{commit}.json")
        with open(commit_path, "w") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        # Update branch pointer
        if branch and branch != "HEAD":
            branch_path = os.path.join(heads_dir, f"{branch}.json")
            with open(branch_path, "w") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)

    # Only overwrite current for working-tree indexing, not for historical commits
    if is_current:
        current_path = os.path.join(store_dir, "current.json")
        with open(current_path, "w") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    return store_dir


def load_index_from_store(store_dir: str, ref: str) -> dict | None:
    """Load an index from the store by commit hash or branch name.

    Attempts: exact commit hash -> short hash -> branch name -> 'current'.
    """
    commits_dir = os.path.join(store_dir, "commits")
    heads_dir = os.path.join(store_dir, "heads")

    # Try exact commit hash
    candidate = os.path.join(commits_dir, f"{ref}.json")
    if os.path.isfile(candidate):
        with open(candidate) as f:
            return json.load(f)

    # Try partial match (short hash)
    if os.path.isdir(commits_dir):
        for fname in os.listdir(commits_dir):
            if fname.startswith(ref) and fname.endswith(".json"):
                with open(os.path.join(commits_dir, fname)) as f:
                    return json.load(f)

    # Try branch name
    candidate = os.path.join(heads_dir, f"{ref}.json")
    if os.path.isfile(candidate):
        with open(candidate) as f:
            return json.load(f)

    # Try "current"
    candidate = os.path.join(store_dir, "current.json")
    if os.path.isfile(candidate):
        with open(candidate) as f:
            return json.load(f)

    return None

# ── Main ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Build a compact, AI-friendly index of a codebase."
    )
    parser.add_argument("root", nargs="?", default=".", help="Project root directory")
    parser.add_argument("-o", "--output", default=".code-index.json", help="Output file path")
    parser.add_argument("--store-dir", default=None, help="Store directory for multi-commit indices")
    parser.add_argument("--commit", default=None, help="Index at a specific git commit ref")
    parser.add_argument("-i", "--incremental", action="store_true", help="Incremental update (FS mode only)")
    parser.add_argument("--max-symbols", type=int, default=100, help="Max symbols per file")
    parser.add_argument("--workers", type=int, default=0, help="Parallel workers (0=auto)")
    parser.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks")
    parser.add_argument("--no-graph", action="store_true", help="Skip import graph")
    parser.add_argument("--jsonl", action="store_true", help="Output JSONL instead of JSON")
    parser.add_argument("--list-stored", action="store_true", help="List stored indices and exit")
    args = parser.parse_args()

    root = os.path.abspath(args.root)

    # ── List stored indices ──
    if args.list_stored:
        store = args.store_dir or os.path.join(root, ".code-index")
        if not os.path.isdir(store):
            print("No index store found.", file=sys.stderr)
            sys.exit(0)
        commits_dir = os.path.join(store, "commits")
        heads_dir = os.path.join(store, "heads")
        print("=== Commits ===")
        if os.path.isdir(commits_dir):
            for f in sorted(os.listdir(commits_dir)):
                fpath = os.path.join(commits_dir, f)
                size_kb = os.path.getsize(fpath) / 1024
                print(f"  {f.replace('.json', '')}  ({size_kb:.0f} KB)")
        print("\n=== Branches ===")
        if os.path.isdir(heads_dir):
            for f in sorted(os.listdir(heads_dir)):
                fpath = os.path.join(heads_dir, f)
                with open(fpath) as fh:
                    data = json.load(fh)
                    commit = data["meta"].get("git_short_commit", "?")
                    files = data["meta"].get("total_files", 0)
                print(f"  {f.replace('.json', '')}  -> {commit}  ({files} files)")
        print("\n=== Current ===")
        cur = os.path.join(store, "current.json")
        if os.path.isfile(cur):
            with open(cur) as fh:
                data = json.load(fh)
                commit = data["meta"].get("git_short_commit", "?")
                files = data["meta"].get("total_files", 0)
                branch = data["meta"].get("git_branch", "?")
            print(f"  branch={branch}  commit={commit}  files={files}")
        sys.exit(0)

    t0 = time.time()

    # ── Git info ──
    git_info: dict = {}
    if is_git_repo(root):
        git_info = get_git_info(root)
        print(f"  Git: branch={git_info.get('branch')} commit={git_info.get('short_commit')}", file=sys.stderr)
    else:
        if args.commit:
            print("Error: --commit requires a git repository", file=sys.stderr)
            sys.exit(1)
        git_info = {"commit": None, "branch": None, "short_commit": None, "tag": None, "describe": None, "dirty": None}

    # ── Resolve commit if needed ──
    target_commit: str | None = None
    if args.commit:
        try:
            target_commit = git_resolve_ref(root, args.commit)
            print(f"  Resolved {args.commit} -> {target_commit[:8]}", file=sys.stderr)
        except Exception as e:
            print(f"Error resolving ref '{args.commit}': {e}", file=sys.stderr)
            sys.exit(1)

    # ── Collect & index files ──
    workers = args.workers or min(os.cpu_count() or 4, 8)
    indexed: list[dict] = []

    if target_commit:
        # ── Git mode: read files from a specific commit ──
        file_pairs = collect_files_git(root, target_commit)
        print(f"  Found {len(file_pairs)} source files at commit {target_commit[:8]}", file=sys.stderr)

        git_args = [(root, target_commit, path, lang) for path, lang in file_pairs]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for result in ex.map(index_file_git, git_args):
                if result:
                    indexed.append(result)

    else:
        # ── Filesystem mode ──
        ignore_patterns = parse_gitignore(root)
        file_pairs = collect_files_fs(root, ignore_patterns, args.follow_symlinks)
        print(f"  Found {len(file_pairs)} source files on disk", file=sys.stderr)

        # Incremental check
        existing_files: dict[str, dict] = {}
        if args.incremental:
            existing_index = incremental_load(args.output)
            if existing_index:
                existing_files = {e["path"]: e for e in existing_index.get("files", [])}
                print(f"  Loaded existing index with {len(existing_files)} files", file=sys.stderr)

        to_index: list[tuple[str, str, str]] = []
        for filepath, lang in file_pairs:
            rel = os.path.relpath(filepath, root)
            if args.incremental and rel in existing_files:
                try:
                    cur_mtime = int(os.stat(filepath).st_mtime)
                    if cur_mtime == existing_files[rel].get("mtime"):
                        continue
                except Exception:
                    pass
            to_index.append((filepath, lang, root))

        print(f"  Indexing {len(to_index)} changed/new files with {workers} workers", file=sys.stderr)

        with ProcessPoolExecutor(max_workers=workers) as ex:
            for result in ex.map(index_file_fs, to_index):
                if result:
                    indexed.append(result)

        # Merge unchanged files in incremental mode
        if args.incremental and existing_files:
            changed_paths = {e["path"] for e in indexed}
            for path, entry in existing_files.items():
                if path not in changed_paths:
                    indexed.append(entry)

    # ── Assemble index ──
    build_ms = int((time.time() - t0) * 1000)
    index = assemble_index(root, indexed, git_info, build_ms, args.no_graph)
    # Override git commit if we're indexing a specific one
    if target_commit:
        index["meta"]["git_commit"] = target_commit
        index["meta"]["git_short_commit"] = target_commit[:8]
        index["meta"]["git_dirty"] = False

    # ── Output ──
    store_dir = args.store_dir
    if store_dir is None and is_git_repo(root):
        store_dir = os.path.join(root, ".code-index")

    if store_dir:
        store_index(index, store_dir, index["meta"]["git_commit"], git_info.get("branch"),
                    is_current=not bool(target_commit))
        print(f"  Stored in {store_dir}/", file=sys.stderr)

    output_path = args.output if not os.path.isabs(args.output) else args.output
    if not os.path.isabs(output_path):
        output_path = os.path.join(root, output_path)
    with open(output_path, "w") as f:
        if args.jsonl:
            for entry in index["files"]:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        else:
            json.dump(index, f, ensure_ascii=False, indent=2)

    total = len(indexed)
    syms = index["meta"]["total_symbols"]
    edges = len(index["import_graph"])
    eps = len(index["entry_points"])
    print(f"  Done: {total} files, {syms} symbols, {edges} import edges, {eps} entry points", file=sys.stderr)
    print(f"  Index: {output_path} ({os.path.getsize(output_path) / 1024:.0f} KB)", file=sys.stderr)
    print(f"  Total time: {time.time() - t0:.1f}s", file=sys.stderr)

    for lang, count in sorted(index["meta"]["languages"].items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        print(f"  {lang:<15} {count:>8} {pct:>5.1f}%", file=sys.stderr)


if __name__ == "__main__":
    main()
