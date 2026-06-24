#!/usr/bin/env python3
"""
Knowledge graph engine for codebase indexing.

Inspired by codebase-memory-mcp's multi-pass pipeline, adapted to Python stdlib:
  Pass 1 — Structure:   File discovery, directory tree, language detection
  Pass 2 — Extract:     Symbol extraction (regex, 11+ languages)
  Pass 3 — Resolve:     Call resolution (6-strategy cascade) + import resolution
  Pass 4 — Enrich:      OOP edges (inherits/implements), test edges, config links
  Pass 5 — Build:       Flush to SQLite, build indexes
  Pass 6 — Analyze:     Louvain communities, dead code, hotspots

Storage: SQLite3 (stdlib) — zero external dependencies.
Key graph queries are O(1) with proper indexes.
"""

import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

# ── Language patterns (same as code_index.py) ──────────────────────

LANG_PATTERNS = {
    "python": {
        "extensions": [".py", ".pyx", ".pxd"],
        "symbols": [
            (r"^\s*class\s+(\w+)(?:\s*\([^)]*\))?\s*:", "class", 1),
            (r"^\s*async\s+def\s+(\w+)", "async_function", 1),
            (r"^\s*def\s+(\w+)", "function", 1),
        ],
        "imports": [
            (r"^\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", [1, 2]),
        ],
        # Patterns for detecting calls within function bodies
        "calls": [
            (r"(?:self|cls)\.(\w+)\s*\(", 1),     # self.method() / cls.method()
            (r"\b([a-z_]\w*)\s*\(", 1),             # function_name() (lowercase start)
            (r"\.(\w+)\s*\(", 1),                   # obj.method()
        ],
        # Inheritance detection
        "inherits": [
            (r"^\s*class\s+(\w+)\s*\(\s*(\w+)", [1, 2]),
        ],
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "symbols": [
            (r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", "class", 1),
            (r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function", 1),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", "function", 1),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*\([^)]*\)\s*=>", "function", 1),
            (r"^\s*(?:export\s+)?interface\s+(\w+)", "interface", 1),
            (r"^\s*(?:export\s+)?type\s+(\w+)\s*=", "type", 1),
            (r"^\s*(?:export\s+)?enum\s+(\w+)", "enum", 1),
        ],
        "imports": [
            (r"""(?:import\s+.*?\s+from\s+['\"]([^'\"]+)['\"])""", [1]),
            (r"""import\s+['\"]([^'\"]+)['\"]""", [1]),
            (r"""require\s*\(\s*['\"]([^'\"]+)['\"]""", [1]),
        ],
        "calls": [
            (r"(?<!\bfunction\s)(?<!\bclass\s)\b(\w+)\s*\(", 1),
        ],
        "inherits": [
            (r"^\s*(?:export\s+)?class\s+(\w+)\s+extends\s+(\w+)", [1, 2]),
            (r"^\s*(?:export\s+)?class\s+(\w+)\s+implements\s+(\w+)", [1, 2]),
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
        "calls": [
            (r"(?<!\bfunction\s)(?<!\bclass\s)\b(\w+)\s*\(", 1),
        ],
        "inherits": [
            (r"^\s*class\s+(\w+)\s+extends\s+(\w+)", [1, 2]),
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
        "calls": [
            (r"(?<!\bfunc\s)\b(\w+)\s*\(", 1),
        ],
        "inherits": [
            (r"^\s*type\s+(\w+)\s+struct\s*\{[^}]*\}.*\n", [1]),  # Simplified — Go uses embedding
        ],
    },
    "cpp": {
        "extensions": [".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hp", ".hh", ".hxx", ".h", ".h++", ".inl"],
        "symbols": [
            (r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+(?:__declspec\s*\([^)]*\)\s*)?(?:[a-zA-Z_]\w*(?:::))?\s*(\w+)\s*(?:\s*:\s*[^{;]*)?\s*[;{]", "class", 1),
            (r"^\s*enum\s+(?:class\s+|struct\s+)?(\w+)", "enum", 1),
            (r"^\s*namespace\s+(\w+)\s*\{", "namespace", 1),
            (r"^\s*(?:\w+(?:::))?\s*(\w+)\s*::\s*\1\s*\([^)]*\)\s*(?:const\s*)?\s*(?:noexcept\s*)?\s*(?::\s*[^{]*)?\s*\{", "constructor", 1),
            (r"^\s*(?:\w+(?:::))?\s*~(\w+)\s*\([^)]*\)\s*(?:noexcept\s*)?\s*(?:override\s*)?\s*(?:final\s*)?\s*\{", "destructor", 1),
            (r"^\s*(?:template\s*<[^>]*>\s*)?(?:virtual\s+|static\s+|inline\s+|explicit\s+|constexpr\s+|consteval\s+)*(?:[\w:]+(?:<[^>]*>)?\s+)+(\w+)\s*::\s*(\w+)\s*\([^)]*\)\s*(?:const\s*)?\s*(?:noexcept\s*)?\s*(?:override\s*)?\s*(?:final\s*)?\s*(?::\s*[^{]*)?\s*\{", "method", 2),
            (r"^\s*(?:virtual\s+|static\s+|inline\s+|explicit\s+|constexpr\s+|consteval\s+)*(?:[\w:]+(?:<[^>]*>)?\s+)+(\w+)\s*\([^)]*\)\s*(?:const\s*)?\s*(?:noexcept\s*)?\s*(?:override\s*)?\s*(?:final\s*)?\s*(?::\s*[^{]*)?\s*\{", "method", 1),
            (r"^\s*template\s*<[^>]*>\s*(?:[\w:]+(?:<[^>]*>)?\s+)+(\w+)\s*\([^)]*\)\s*(?:const\s*)?\s*(?:noexcept\s*)?\s*\{", "function", 1),
            (r'^\s*(?:[\w:]+(?:<[^>]*>)?\s+)*(?:operator\s*(?:[+\-*/%&|^~!=<>]+|\[\]|\(\)|new|delete|""_|->|<=>))\s*\([^)]*\)\s*(?:const\s*)?\s*\{', "operator", 1),
            (r"^\s*(?:virtual\s+|static\s+|inline\s+|explicit\s+|constexpr\s+|consteval\s+)*(?:[\w:]+(?:<[^>]*>)?\s+)+(\w+)\s*\([^)]*\)\s*(?:const\s*)?\s*(?:noexcept\s*)?\s*\{", "function", 1),
            (r"^\s*(?:[\w:]+(?:<[^>]*>)?\s+)*(?:[\w:]*task|[\w:]*lazy|[\w:]*generator|[\w:]*async_generator|[\w:]*eager_task|[\w:]*shared_task)\s*<\s*[^>]*>\s*(\w+)\s*\([^)]*\)", "coroutine", 1),
            (r"^\s*using\s+(\w+)\s*=", "type_alias", 1),
            (r"^\s*typedef\s+.+\s+(\w+)\s*;", "typedef", 1),
            (r"^\s*template\s*<[^>]*>\s*concept\s+(\w+)\s*=", "concept", 1),
        ],
        "imports": [
            (r'^\s*#include\s+[<"]([^>"]+)[>"]', [1]),
        ],
        "calls": [
            (r"\b(\w+)\s*\(", 1),
        ],
        "inherits": [
            (r"^\s*class\s+(\w+)\s*:\s*(?:public|private|protected)\s+(\w+)", [1, 2]),
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
        "calls": [
            (r"(?<!\bclass\s)\b(\w+)\s*\(", 1),
        ],
        "inherits": [
            (r"^\s*class\s+(\w+)\s+extends\s+(\w+)", [1, 2]),
            (r"^\s*class\s+(\w+)\s+implements\s+(\w+)", [1, 2]),
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
        ],
        "imports": [
            (r"^\s*use\s+([\w:]+)", [1]),
        ],
        "calls": [
            (r"(?<!\bfn\s)\b(\w+)\s*\(", 1),
        ],
        "inherits": [
            (r"^\s*(?:pub\s+)?trait\s+(\w+)\s*:\s*(\w+)", [1, 2]),
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
        "calls": [
            (r"(?<!\bdef\s)(?<!\bclass\s)\b(\w+)\s*\(", 1),
            (r"\.(\w+)\s*\(", 1),  # method calls
        ],
        "inherits": [
            (r"^\s*class\s+(\w+)\s*<\s*(\w+)", [1, 2]),
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
        "calls": [
            (r"\b(\w+)\s+(?:\||;|&&|$)", 1),
        ],
        "inherits": [],
    },
    "capnp": {
        "extensions": [".capnp"],
        "symbols": [
            (r"^\s*struct\s+(\w+)", "struct", 1),
            (r"^\s*interface\s+(\w+)", "interface", 1),
            (r"^\s*enum\s+(\w+)", "enum", 1),
            (r"^\s*const\s+\w+\s*:\w+\s+(\w+)\s*=", "constant", 1),
            (r"^\s*annotation\s+(\w+)", "annotation", 1),
            (r"^\s*using\s+(\w+)\s*=", "type_alias", 1),
        ],
        "imports": [
            (r'^\s*using\s+(?:import\s+)?(?:["\']([^"\']+)["\']|([\w.]+))', [1, 2]),
        ],
        "calls": [],
        "inherits": [],
    },
}

ALWAYS_IGNORE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    "target", ".next", ".nuxt", "vendor", "bower_components",
    ".idea", ".vscode", ".vs", "CMakeFiles", "cmake-build-*",
    ".repo", ".DS_Store", "Thumbs.db",
    "*.min.js", "*.min.css", "*.bundle.js", "*.generated.*",
    "*.pb.go", "*.pb.cc", "*.pb.h", "*.capnp.h", "*.capnp.c++",
    "*.o", "*.obj", "*.a", "*.so", "*.dylib", "*.dll", "*.exe",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "Gemfile.lock", "poetry.lock", "Pipfile.lock",
}


# ── SQLite Graph DB ─────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    type      TEXT NOT NULL,          -- 'file', 'class', 'function', 'method', etc.
    name      TEXT NOT NULL,          -- symbol name or file path
    file_path TEXT,                   -- containing file (for symbols)
    line      INTEGER,                -- line number (for symbols)
    lang      TEXT,                   -- language
    parent_id INTEGER,                -- parent node (class contains method)
    properties TEXT                   -- JSON extra properties
);

CREATE TABLE IF NOT EXISTS edges (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES nodes(id),
    target_id INTEGER NOT NULL REFERENCES nodes(id),
    type      TEXT NOT NULL,          -- 'CONTAINS', 'IMPORTS', 'CALLS', 'INHERITS', 'IMPLEMENTS', 'DEFINES'
    confidence REAL DEFAULT 1.0,      -- 0.0-1.0 confidence score
    properties TEXT                   -- JSON: {arg_mapping, line, etc.}
);

-- Indexes for fast graph traversal
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id, type);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);

-- Full-text search on symbol names
CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(name, type, file_path);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS community (
    node_id INTEGER PRIMARY KEY REFERENCES nodes(id),
    community_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_community_id ON community(community_id);
"""


class GraphDB:
    """SQLite-backed knowledge graph for code intelligence."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ── Node operations ──

    def add_node(self, type_: str, name: str, file_path: str | None = None,
                 line: int | None = None, lang: str | None = None,
                 parent_id: int | None = None, properties: dict | None = None) -> int:
        """Insert a node and return its id. Deduplicates by (type, name, file_path)."""
        props_json = json.dumps(properties) if properties else None
        # Check for existing node
        cursor = self.conn.execute(
            "SELECT id FROM nodes WHERE type=? AND name=? AND file_path IS ?",
            (type_, name, file_path)
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        cursor = self.conn.execute(
            "INSERT INTO nodes(type, name, file_path, line, lang, parent_id, properties) VALUES(?,?,?,?,?,?,?)",
            (type_, name, file_path, line, lang, parent_id, props_json)
        )
        self.conn.execute(
            "INSERT INTO symbols_fts(name, type, file_path) VALUES(?,?,?)",
            (name, type_, file_path or "")
        )
        return cursor.lastrowid

    def add_nodes_batch(self, nodes: list[tuple]) -> list[int]:
        """Bulk insert nodes. Each tuple: (type, name, file_path, line, lang, parent_id, properties_json)."""
        ids = []
        for n in nodes:
            ids.append(self.add_node(*n))
        return ids

    # ── Edge operations ──

    def add_edge(self, source_id: int, target_id: int, type_: str,
                 confidence: float = 1.0, properties: dict | None = None) -> int:
        """Insert an edge. Deduplicates by (source, target, type)."""
        cursor = self.conn.execute(
            "SELECT id FROM edges WHERE source_id=? AND target_id=? AND type=?",
            (source_id, target_id, type_)
        )
        row = cursor.fetchone()
        if row:
            return row[0]
        props_json = json.dumps(properties) if properties else None
        cursor = self.conn.execute(
            "INSERT INTO edges(source_id, target_id, type, confidence, properties) VALUES(?,?,?,?,?)",
            (source_id, target_id, type_, confidence, props_json)
        )
        return cursor.lastrowid

    def add_edges_batch(self, edges: list[tuple]) -> None:
        """Bulk insert edges. Each tuple: (source_id, target_id, type, confidence, properties_json)."""
        self.conn.executemany(
            "INSERT OR IGNORE INTO edges(source_id, target_id, type, confidence, properties) VALUES(?,?,?,?,?)",
            edges
        )

    # ── Query helpers ──

    def get_node(self, node_id: int) -> dict | None:
        cursor = self.conn.execute(
            "SELECT id, type, name, file_path, line, lang, properties FROM nodes WHERE id=?", (node_id,)
        )
        row = cursor.fetchone()
        if row:
            return {"id": row[0], "type": row[1], "name": row[2],
                    "file_path": row[3], "line": row[4], "lang": row[5],
                    "properties": json.loads(row[6]) if row[6] else None}
        return None

    def find_nodes(self, type_: str | None = None, name: str | None = None,
                   file_path: str | None = None) -> list[dict]:
        """Find nodes by type, name, and/or file."""
        conditions = []
        params = []
        if type_:
            conditions.append("type=?")
            params.append(type_)
        if name:
            conditions.append("name LIKE ?")
            params.append(f"%{name}%")
        if file_path:
            conditions.append("file_path=?")
            params.append(file_path)
        where = " AND ".join(conditions) if conditions else "1=1"
        cursor = self.conn.execute(
            f"SELECT id, type, name, file_path, line, lang FROM nodes WHERE {where} LIMIT 200",
            params
        )
        return [{"id": r[0], "type": r[1], "name": r[2], "file_path": r[3],
                 "line": r[4], "lang": r[5]} for r in cursor.fetchall()]

    def find_neighbors(self, node_id: int, edge_type: str | None = None,
                       direction: str = "outbound") -> list[dict]:
        """Find neighbors of a node. direction: 'outbound', 'inbound', or 'both'."""
        results = []
        if direction in ("outbound", "both"):
            query = "SELECT e.target_id, e.type, n.type, n.name, n.file_path, n.line FROM edges e JOIN nodes n ON e.target_id=n.id WHERE e.source_id=?"
            params = [node_id]
            if edge_type:
                query += " AND e.type=?"
                params.append(edge_type)
            cursor = self.conn.execute(query, params)
            for r in cursor.fetchall():
                results.append({"node_id": r[0], "edge_type": r[1], "node_type": r[2],
                                "name": r[3], "file_path": r[4], "line": r[5], "direction": "outbound"})

        if direction in ("inbound", "both"):
            query = "SELECT e.source_id, e.type, n.type, n.name, n.file_path, n.line FROM edges e JOIN nodes n ON e.source_id=n.id WHERE e.target_id=?"
            params = [node_id]
            if edge_type:
                query += " AND e.type=?"
                params.append(edge_type)
            cursor = self.conn.execute(query, params)
            for r in cursor.fetchall():
                results.append({"node_id": r[0], "edge_type": r[1], "node_type": r[2],
                                "name": r[3], "file_path": r[4], "line": r[5], "direction": "inbound"})

        return results

    def trace_call_path(self, start_id: int, max_depth: int = 3,
                        direction: str = "outbound") -> list[dict]:
        """BFS traversal of CALLS edges. Returns path with nodes and edges."""
        visited: set[int] = set()
        queue = deque([(start_id, 0, [])])
        path: list[dict] = []

        while queue:
            node_id, depth, trail = queue.popleft()
            if depth > max_depth or node_id in visited:
                continue
            visited.add(node_id)
            node = self.get_node(node_id)
            if node:
                path.append({"depth": depth, "node": node, "trail": trail})

            neighbors = self.find_neighbors(node_id, edge_type="CALLS", direction=direction)
            for n in neighbors:
                if n["node_id"] not in visited:
                    queue.append((n["node_id"], depth + 1, trail + [node_id]))
        return path

    def find_dead_code(self, exclude_types: list[str] | None = None) -> list[dict]:
        """Find functions/methods with no inbound CALLS edges (excluding entry points)."""
        if exclude_types is None:
            exclude_types = ["class", "interface", "struct", "enum", "namespace"]
        cursor = self.conn.execute("""
            SELECT n.id, n.name, n.file_path, n.line, n.type
            FROM nodes n
            WHERE n.type IN ('function', 'method', 'coroutine')
              AND n.id NOT IN (
                SELECT DISTINCT target_id FROM edges WHERE type='CALLS'
              )
              AND n.id NOT IN (
                SELECT DISTINCT source_id FROM edges WHERE type='CALLS'
              )
            ORDER BY n.name
            LIMIT 200
        """)
        return [{"id": r[0], "name": r[1], "file_path": r[2], "line": r[3], "type": r[4]}
                for r in cursor.fetchall()]

    def get_stats(self) -> dict:
        """Get graph statistics."""
        node_count = self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edge_count = self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        node_types = {}
        for r in self.conn.execute("SELECT type, COUNT(*) FROM nodes GROUP BY type"):
            node_types[r[0]] = r[1]
        edge_types = {}
        for r in self.conn.execute("SELECT type, COUNT(*) FROM edges GROUP BY type"):
            edge_types[r[0]] = r[1]
        return {
            "total_nodes": node_count, "total_edges": edge_count,
            "node_types": node_types, "edge_types": edge_types,
        }

    def set_meta(self, key: str, value: str):
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        cursor = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

    def commit(self):
        self.conn.commit()


# ── Multi-pass Pipeline ─────────────────────────────────────────────

class IndexPipeline:
    """6-pass code indexing pipeline into a GraphDB."""

    def __init__(self, root: str, db: GraphDB, max_file_size: int = 200_000):
        self.root = os.path.abspath(root)
        self.db = db
        self.max_file_size = max_file_size
        self.file_node_ids: dict[str, int] = {}      # file_path -> node_id
        self.symbol_registry: dict[str, list[int]] = {}  # symbol_name -> [node_ids]

    # ── Pass 1: Structure ──────────────────────────────────────────

    def pass_structure(self, follow_symlinks: bool = False) -> int:
        """Discover files and create file/directory nodes."""
        t0 = time.time()
        count = 0

        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=follow_symlinks):
            dirnames[:] = [d for d in dirnames if d not in ALWAYS_IGNORE and not d.startswith(".")]

            for fname in filenames:
                filepath = os.path.join(dirpath, fname)
                if self._should_ignore(filepath):
                    continue
                lang = self._detect_language(fname)
                if not lang:
                    continue

                relpath = os.path.relpath(filepath, self.root)
                try:
                    stat = os.stat(filepath)
                    size = stat.st_size
                except OSError:
                    continue

                node_id = self.db.add_node(
                    "file", relpath, file_path=relpath, line=0, lang=lang,
                    properties={"size": size, "mtime": int(stat.st_mtime)}
                )
                self.file_node_ids[relpath] = node_id
                count += 1

        self.db.set_meta("pass_structure_time_ms", str(int((time.time() - t0) * 1000)))
        self.db.set_meta("pass_structure_files", str(count))
        self.db.commit()
        return count

    # ── Pass 2: Extract ────────────────────────────────────────────

    def pass_extract(self, workers: int = 0):
        """Extract symbols from files (parallel)."""
        t0 = time.time()
        workers = workers or min(os.cpu_count() or 4, 8)

        tasks = [(filepath, lang, self.root, self.max_file_size)
                 for filepath, node_id in self.file_node_ids.items()
                 for lang in [self._detect_language(filepath)] if lang]

        with ProcessPoolExecutor(max_workers=workers) as ex:
            for filepath, symbols, calls in ex.map(_extract_symbols_from_file, tasks):
                if not symbols:
                    continue
                file_id = self.file_node_ids.get(filepath)
                if not file_id:
                    continue

                parent_stack = [file_id]  # for namespace/class nesting
                for sym in symbols:
                    node_id = self.db.add_node(
                        sym["type"], sym["name"], file_path=filepath,
                        line=sym.get("line"), lang=sym.get("lang"),
                        parent_id=parent_stack[-1] if len(parent_stack) > 1 else file_id,
                        properties=sym.get("properties")
                    )
                    # DEFINES edge from parent
                    self.db.add_edge(parent_stack[-1], node_id, "DEFINES")
                    # Register symbol
                    name = sym["name"]
                    if name not in self.symbol_registry:
                        self.symbol_registry[name] = []
                    self.symbol_registry[name].append(node_id)

                    # Track nesting for class/namespace
                    if sym["type"] in ("class", "namespace", "struct"):
                        parent_stack.append(node_id)
                    elif sym["type"] == "end_scope":
                        if len(parent_stack) > 1:
                            parent_stack.pop()

                # Store raw calls for Pass 3
                if calls:
                    self.db.set_meta(f"_calls_{filepath}", json.dumps(calls))

        self.db.set_meta("pass_extract_time_ms", str(int((time.time() - t0) * 1000)))
        self.db.set_meta("pass_extract_symbols", str(len(self.symbol_registry)))
        self.db.commit()

    # ── Pass 3: Resolve ────────────────────────────────────────────

    def pass_resolve(self):
        """Resolve imports and calls into edges."""
        t0 = time.time()
        call_count = 0
        import_count = 0

        for filepath in self.file_node_ids:
            # Resolve imports
            import_count += self._resolve_imports(filepath)
            # Resolve calls
            call_count += self._resolve_calls(filepath)

        self.db.set_meta("pass_resolve_time_ms", str(int((time.time() - t0) * 1000)))
        self.db.set_meta("pass_resolve_calls", str(call_count))
        self.db.set_meta("pass_resolve_imports", str(import_count))
        self.db.commit()

    def _resolve_imports(self, filepath: str) -> int:
        """Resolve file-level imports to edges. Returns count of edges created."""
        file_id = self.file_node_ids.get(filepath)
        if not file_id:
            return 0

        lang = self._detect_language(filepath)
        if not lang:
            return 0

        try:
            with open(os.path.join(self.root, filepath), "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(self.max_file_size)
                lines = content.split("\n")
        except Exception:
            return 0

        cfg = LANG_PATTERNS.get(lang, {})
        import_patterns = cfg.get("imports", [])
        count = 0

        for line in lines:
            for pattern, groups in import_patterns:
                m = re.search(pattern, line)
                if m:
                    for g in groups:
                        val = m.group(g)
                        if not val:
                            continue

                        # Find matching file node
                        target_id = self._resolve_import_target(val, filepath)
                        if target_id and target_id != file_id:
                            self.db.add_edge(file_id, target_id, "IMPORTS")
                            count += 1
        return count

    def _resolve_import_target(self, import_path: str, source_file: str) -> int | None:
        """Resolve an import string to a file node id. Multi-strategy."""
        # Strategy 1: Exact path match
        for fpath, nid in self.file_node_ids.items():
            if import_path in fpath:
                return nid

        # Strategy 2: Basename match
        base = os.path.basename(import_path)
        for fpath, nid in self.file_node_ids.items():
            if os.path.basename(fpath) == base or os.path.splitext(os.path.basename(fpath))[0] == base:
                return nid

        # Strategy 3: Last segment match
        segments = import_path.replace("/", ".").replace("\\", ".").split(".")
        if segments:
            last = segments[-1]
            for fpath, nid in self.file_node_ids.items():
                if last in fpath:
                    return nid

        return None

    def _resolve_calls(self, filepath: str) -> int:
        """Resolve function calls within a file. 6-strategy cascade."""
        calls_json = self.db.get_meta(f"_calls_{filepath}")
        if not calls_json:
            return 0

        try:
            raw_calls = json.loads(calls_json)
        except json.JSONDecodeError:
            return 0

        count = 0
        for call in raw_calls:
            callee_name = call["name"]
            caller_line = call.get("caller_line")
            caller_name = call.get("caller_name")

            # Find caller node
            caller_id = self._find_symbol_in_file(caller_name, filepath)
            if not caller_id:
                continue

            # 6-strategy cascade for callee resolution
            target_id = self._resolve_call_target(callee_name, filepath)
            if target_id and target_id != caller_id:
                self.db.add_edge(caller_id, target_id, "CALLS",
                                confidence=0.7, properties={"line": caller_line})
                count += 1

        # Clean up temp data
        self.db.conn.execute("DELETE FROM meta WHERE key=?", (f"_calls_{filepath}",))
        return count

    def _resolve_call_target(self, callee: str, source_file: str) -> int | None:
        """6-strategy cascade for resolving a function call to a symbol node."""
        if callee not in self.symbol_registry:
            # Try lowercase
            callee_lower = callee.lower()
            for name in self.symbol_registry:
                if name.lower() == callee_lower:
                    callee = name
                    break
            else:
                return None

        candidates = self.symbol_registry[callee]

        # Strategy 1: Import map — find in imported modules (confidence 0.95)
        source_id = self.file_node_ids.get(source_file)
        if source_id:
            imported = self.db.find_neighbors(source_id, edge_type="IMPORTS", direction="outbound")
            imported_file_ids = {n["node_id"] for n in imported}
            for cid in candidates:
                cnode = self.db.get_node(cid)
                if cnode and self.file_node_ids.get(cnode["file_path"]) in imported_file_ids:
                    return cid  # confidence 0.95

        # Strategy 2: Same file (confidence 0.90)
        for cid in candidates:
            cnode = self.db.get_node(cid)
            if cnode and cnode["file_path"] == source_file:
                return cid

        # Strategy 3: Same module/directory (confidence 0.85)
        source_dir = os.path.dirname(source_file)
        for cid in candidates:
            cnode = self.db.get_node(cid)
            if cnode and os.path.dirname(cnode["file_path"] or "") == source_dir:
                return cid

        # Strategy 4: Unique name (confidence 0.75)
        unique_symbols = [cid for cid in candidates if len(self.symbol_registry.get(
            self.db.get_node(cid)["name"] if self.db.get_node(cid) else "", []
        )) == 1]
        if len(unique_symbols) == 1:
            return unique_symbols[0]

        # Strategy 5: First candidate (confidence 0.55)
        if candidates:
            return candidates[0]

        return None

    def _find_symbol_in_file(self, name: str, filepath: str) -> int | None:
        """Find a symbol node by name within a specific file."""
        for node_id in self.symbol_registry.get(name, []):
            node = self.db.get_node(node_id)
            if node and node["file_path"] == filepath:
                return node_id
        return None

    # ── Pass 4: Enrich ─────────────────────────────────────────────

    def pass_enrich(self):
        """Add inheritance/implementation edges and test edges."""
        t0 = time.time()
        inherits_count = 0

        for filepath in self.file_node_ids:
            lang = self._detect_language(filepath)
            if not lang:
                continue
            cfg = LANG_PATTERNS.get(lang, {})
            inherits_patterns = cfg.get("inherits", [])
            if not inherits_patterns:
                continue

            try:
                with open(os.path.join(self.root, filepath), "r", encoding="utf-8", errors="ignore") as f:
                    lines = f.read(self.max_file_size).split("\n")
            except Exception:
                continue

            for line in lines:
                for pattern, groups in inherits_patterns:
                    m = re.search(pattern, line)
                    if not m:
                        continue
                    child_name = m.group(groups[0])
                    parent_name = m.group(groups[1])
                    child_id = self._find_symbol_in_file(child_name, filepath)
                    parent_ids = self.symbol_registry.get(parent_name, [])
                    if child_id and parent_ids:
                        # Edge type: INHERITS or IMPLEMENTS
                        edge_type = "IMPLEMENTS" if "implements" in line.lower() else "INHERITS"
                        for pid in parent_ids[:1]:  # Just the first match
                            self.db.add_edge(child_id, pid, edge_type)
                            inherits_count += 1

        # Test edges: link test files to source files by naming convention
        for fpath in self.file_node_ids:
            if "test" not in fpath.lower():
                continue
            base = os.path.basename(fpath)
            # test_foo.py → foo.py, foo_test.py → foo.py
            for variant in [base.replace("test_", ""), base.replace("_test", ""), base.replace("Test", "")]:
                variant = os.path.splitext(variant)[0]
                if not variant:
                    continue
                for src_path, src_id in self.file_node_ids.items():
                    if variant in src_path and "test" not in src_path.lower():
                        test_id = self.file_node_ids.get(fpath)
                        if test_id:
                            self.db.add_edge(test_id, src_id, "TESTS")

        self.db.set_meta("pass_enrich_time_ms", str(int((time.time() - t0) * 1000)))
        self.db.set_meta("pass_enrich_inherits", str(inherits_count))
        self.db.commit()

    # ── Pass 5: Build ──────────────────────────────────────────────

    def pass_build(self):
        """Create indexes and finalize storage."""
        t0 = time.time()
        self.db.conn.execute("ANALYZE")
        self.db.conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')")
        self.db.set_meta("pass_build_time_ms", str(int((time.time() - t0) * 1000)))
        self.db.commit()

    # ── Pass 6: Analyze ────────────────────────────────────────────

    def pass_analyze(self):
        """Louvain community detection on CALLS graph."""
        t0 = time.time()
        communities = self._louvain_communities()
        self.db.conn.execute("DELETE FROM community")
        for node_id, comm_id in communities.items():
            self.db.conn.execute(
                "INSERT OR REPLACE INTO community(node_id, community_id) VALUES(?,?)",
                (node_id, comm_id)
            )
        self.db.set_meta("pass_analyze_communities", str(len(set(communities.values()))))
        self.db.set_meta("pass_analyze_time_ms", str(int((time.time() - t0) * 1000)))
        self.db.commit()

    def _louvain_communities(self, max_iterations: int = 10) -> dict[int, int]:
        """Simplified Louvain community detection on CALLS edges."""
        # Build adjacency list from CALLS edges
        adj: dict[int, set[int]] = defaultdict(set)
        cursor = self.db.conn.execute("SELECT source_id, target_id FROM edges WHERE type='CALLS'")
        for src, tgt in cursor.fetchall():
            adj[src].add(tgt)
            adj[tgt].add(src)

        nodes = list(adj.keys())
        if not nodes:
            return {}

        # Initialize: each node in its own community
        community: dict[int, int] = {n: i for i, n in enumerate(nodes)}
        m = sum(len(v) for v in adj.values())  # total degree (×2 for undirected)

        for iteration in range(max_iterations):
            changed = False
            for node in nodes:
                best_comm = community[node]
                best_delta = 0.0

                # Compute neighbor communities
                neighbor_comms: dict[int, float] = defaultdict(float)
                for neighbor in adj[node]:
                    neighbor_comms[community[neighbor]] += 1.0

                current_comm = community[node]
                # Remove node from its community
                community[node] = -1  # temporarily unassigned

                for comm_id, neighbor_weight in neighbor_comms.items():
                    # Modularity gain approximation
                    delta = neighbor_weight - (len(adj[node]) * sum(
                        1 for n in nodes if community.get(n) == comm_id
                    ) / max(m, 1))
                    if delta > best_delta:
                        best_delta = delta
                        best_comm = comm_id

                community[node] = best_comm
                if best_comm != current_comm:
                    changed = True

            if not changed:
                break

        # Renumber communities consecutively
        unique_comms = {}
        next_id = 0
        result = {}
        for node, comm in community.items():
            if comm not in unique_comms:
                unique_comms[comm] = next_id
                next_id += 1
            result[node] = unique_comms[comm]

        return result

    # ── Helpers ─────────────────────────────────────────────────────

    def _should_ignore(self, filepath: str) -> bool:
        rel = os.path.relpath(filepath, self.root)
        parts = set(Path(rel).parts)
        if parts & ALWAYS_IGNORE:
            return True
        base = os.path.basename(filepath)
        for pat in ALWAYS_IGNORE:
            if pat.startswith("*.") and base.endswith(pat[1:]):
                return True
        # Cap'n Proto double-extension files are ignored (generated)
        if any(ext in filepath for ext in [".capnp.h", ".capnp.c++"]):
            return True
        return False

    def _detect_language(self, filepath: str) -> str | None:
        ext = os.path.splitext(filepath)[1].lower()
        if not ext:
            base = os.path.basename(filepath)
            if base in ("Dockerfile", "Makefile", "Justfile", "CMakeLists.txt"):
                return None  # Not indexable as source
            if base.endswith(".capnp"):
                return "capnp"
            return None
        if filepath.endswith(".capnp"):
            return "capnp"
        for lang, cfg in LANG_PATTERNS.items():
            if ext in cfg["extensions"]:
                return lang
        return None


# ── Symbol extraction worker (runs in subprocess) ──────────────────

def _extract_symbols_from_file(args: tuple) -> tuple[str, list[dict], list[dict]]:
    """Extract symbols and calls from a single file. Runs in ProcessPoolExecutor."""
    filepath, lang, root, max_size = args
    symbols: list[dict] = []
    calls: list[dict] = []

    # Resolve path: filepath may be relative or absolute
    full_path = filepath if os.path.isabs(filepath) else os.path.join(root, filepath)

    try:
        with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(max_size)
            lines = content.split("\n")
    except Exception:
        return (os.path.relpath(filepath, root) if os.path.isabs(filepath) else filepath, [], [])

    cfg = LANG_PATTERNS.get(lang, {})
    symbol_patterns = cfg.get("symbols", [])
    call_patterns = cfg.get("calls", [])

    # Track scoping — detect which function we're inside
    current_function: str | None = None
    current_indent: int = 0
    func_start_line: int = -1
    scope_depth = 0  # brace-based for C-like languages
    brace_langs = {"cpp", "c", "java", "javascript", "typescript", "rust", "go"}

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*", "'''", '"""')):
            continue

        # Detect indentation level (for Python-like languages)
        indent_level = len(line) - len(line.lstrip())

        # Extract symbols
        for pattern, sym_type, group in symbol_patterns:
            m = re.search(pattern, line)
            if m:
                name = m.group(group) if isinstance(group, int) else None
                if not name and isinstance(group, list):
                    for g in group:
                        name = m.group(g)
                        if name:
                            break
                if name:
                    symbols.append({"type": sym_type, "name": name, "line": i, "lang": lang})

                    # Track function scope
                    if sym_type in ("function", "method", "coroutine", "async_function", "constructor", "destructor"):
                        current_function = name
                        current_indent = indent_level
                        func_start_line = i  # Guard: don't exit on the def line itself
                        scope_depth = 0  # Reset brace depth for C-like
                    elif sym_type in ("class", "struct", "interface", "namespace"):
                        if indent_level <= current_indent and lang not in brace_langs:
                            current_function = None  # Exited function scope
                    break  # One symbol per line

        # Track brace scope for C-like languages
        if lang in brace_langs:
            scope_depth += stripped.count("{") - stripped.count("}")
            if scope_depth < 0:
                scope_depth = 0
                current_function = None

        # Detect exit from function scope (Python-like: dedent past function start)
        # Guard: never exit on the same line we entered the function
        if lang not in brace_langs and current_function and i != func_start_line:
            if indent_level <= current_indent and stripped and not stripped.startswith((" ", "\t")):
                if not stripped.startswith(("if ", "for ", "while ", "try:", "except", "elif ", "else:", "with ")):
                    current_function = None

        # Extract calls within function bodies
        if current_function and call_patterns and (scope_depth > 0 or lang not in brace_langs):
            for pattern, group in call_patterns:
                for m in re.finditer(pattern, stripped):
                    name = m.group(group)
                    if name and len(name) >= 2:  # Skip single-char and Python keywords
                        if name in ("if", "for", "and", "not", "or", "in", "is", "def", "class", "with",
                                     "elif", "else", "try", "except", "return", "raise", "import", "from",
                                     "while", "break", "pass", "True", "False", "None", "print", "len",
                                     "range", "int", "str", "list", "dict", "set", "tuple", "type",
                                     "super", "self", "cls"):
                            continue
                        calls.append({"name": name, "caller_line": i, "caller_name": current_function})

    rel_path = os.path.relpath(full_path, root)
    return (rel_path, symbols, calls)


# ── Architecture analysis ───────────────────────────────────────────

def analyze_architecture(db: GraphDB) -> dict:
    """Generate architecture summary from the graph."""
    stats = db.get_stats()

    # Find communities
    comm_sizes: dict[int, int] = defaultdict(int)
    comm_nodes: dict[int, list[str]] = defaultdict(list)
    for r in db.conn.execute("SELECT c.community_id, n.name, n.type FROM community c JOIN nodes n ON c.node_id=n.id"):
        comm_sizes[r[0]] += 1
        comm_nodes[r[0]].append(f"{r[2]}:{r[1]}")

    # Top communities
    top_communities = sorted(comm_sizes.items(), key=lambda x: -x[1])[:10]

    # Entry points (files with many outbound CALLS, few inbound)
    cursor = db.conn.execute("""
        SELECT n.name, n.file_path,
               (SELECT COUNT(*) FROM edges WHERE source_id=n.id AND type='CALLS') as out_calls,
               (SELECT COUNT(*) FROM edges WHERE target_id=n.id AND type='CALLS') as in_calls
        FROM nodes n
        WHERE n.type='file'
        ORDER BY out_calls DESC
        LIMIT 10
    """)
    entry_points = [{"file": r[1], "out_calls": r[2], "in_calls": r[3]} for r in cursor.fetchall()]

    return {
        "stats": stats,
        "communities": [{"id": cid, "size": size, "sample_nodes": comm_nodes[cid][:5]}
                       for cid, size in top_communities],
        "entry_points": entry_points,
    }


# ── Main (for testing / standalone use) ─────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Code knowledge graph builder")
    parser.add_argument("root", nargs="?", default=".", help="Project root")
    parser.add_argument("-o", "--output", default=".code-graph.db", help="SQLite DB path")
    parser.add_argument("--passes", default="123456", help="Which passes to run (default: all)")
    parser.add_argument("--workers", type=int, default=0)
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    db_path = os.path.join(root, args.output)
    db = GraphDB(db_path)
    pipeline = IndexPipeline(root, db)

    passes_to_run = set(args.passes)
    total_t0 = time.time()

    if "1" in passes_to_run:
        n = pipeline.pass_structure()
        print(f"  Pass 1 (Structure): {n} files", file=sys.stderr)

    if "2" in passes_to_run:
        pipeline.pass_extract(workers=args.workers)
        ns = db.get_meta("pass_extract_symbols")
        print(f"  Pass 2 (Extract): {ns} unique symbol names", file=sys.stderr)

    if "3" in passes_to_run:
        pipeline.pass_resolve()
        nc = db.get_meta("pass_resolve_calls")
        ni = db.get_meta("pass_resolve_imports")
        print(f"  Pass 3 (Resolve): {nc} call edges, {ni} import edges", file=sys.stderr)

    if "4" in passes_to_run:
        pipeline.pass_enrich()
        print(f"  Pass 4 (Enrich): inheritance + test edges", file=sys.stderr)

    if "5" in passes_to_run:
        pipeline.pass_build()
        print(f"  Pass 5 (Build): indexes created", file=sys.stderr)

    if "6" in passes_to_run:
        pipeline.pass_analyze()
        nc = db.get_meta("pass_analyze_communities")
        print(f"  Pass 6 (Analyze): {nc} communities detected", file=sys.stderr)

    stats = db.get_stats()
    print(f"\n  Graph: {stats['total_nodes']} nodes, {stats['total_edges']} edges", file=sys.stderr)
    print(f"  Node types: {stats['node_types']}", file=sys.stderr)
    print(f"  Edge types: {stats['edge_types']}", file=sys.stderr)
    print(f"  Total time: {time.time() - total_t0:.1f}s", file=sys.stderr)

    db.close()


if __name__ == "__main__":
    main()
