#!/usr/bin/env python3
"""
MCP Server for Code Retrieval Tools.

Implements the Model Context Protocol (MCP) over stdio JSON-RPC 2.0.
Zero external dependencies — Python stdlib only.

Exposes 11 tools:
  code_summary, code_search, code_deps, code_dependents,
  code_diff, code_log_recent, code_log_search, code_log_trace,
  code_log_impact, code_hotspots, code_index_build

Usage:
  python mcp_server.py                          # run as MCP stdio server
  python mcp_server.py --project /path/to/proj  # specify project root

Configure in Claude Code (.mcp.json in project root):
  {
    "mcpServers": {
      "code-retrieval": {
        "command": "python",
        "args": ["path/to/mcp_server.py", "--project", "${projectDir}"],
        "description": "Code search, diff, log analysis tools"
      }
    }
  }
"""

import json
import os
import subprocess
import sys
import traceback

# ── Protocol constants ─────────────────────────────────────────────

JSONRPC = "2.0"
SERVER_NAME = "code-retrieval-mcp"
SERVER_VERSION = "1.0.0"

# ── Tool definitions ────────────────────────────────────────────────

TOOLS = [
    {
        "name": "code_summary",
        "description": "Get a concise overview of the project: file counts, languages, entry points, directory structure, and config files. Use this FIRST when entering an unfamiliar project.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "code_search",
        "description": "Search for symbol definitions (functions, classes, interfaces, etc.) across the codebase. Returns file path, line number, symbol type, and symbol name. Much faster than grep for finding definitions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Symbol name to search for (substring match)"},
                "type": {"type": "string", "description": "Filter by symbol type: function, class, method, struct, interface, enum, trait"},
                "lang": {"type": "string", "description": "Filter by language: python, typescript, javascript, go, rust, java, cpp"},
                "limit": {"type": "integer", "description": "Max results (default: 20)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "code_deps",
        "description": "Trace forward dependencies of a file — what modules does this file import and depend on? Shows the dependency tree up to the specified depth.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to project root"},
                "depth": {"type": "integer", "description": "Traversal depth (default: 2)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "code_dependents",
        "description": "Find reverse dependencies — what files import/depend on the given file? Use this to assess the impact of changing a file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to project root"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "code_diff",
        "description": "Compare two commits/branches at the symbol level. Shows files added/deleted/modified, symbols (functions/classes) added/removed, import changes, and high-impact files. Use for PR review, bug tracing, or understanding what changed between versions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "base": {"type": "string", "description": "Base ref (commit hash, branch, tag, or HEAD~N)"},
                "target": {"type": "string", "description": "Target ref (commit hash, branch, tag, or HEAD~N). Default: HEAD"},
                "format": {"type": "string", "description": "Output format: compact (agent-optimized), summary (one paragraph), full (detailed)"},
            },
            "required": ["base"],
        },
    },
    {
        "name": "code_log_recent",
        "description": "Show recent commits with code change impact summary. Each commit shows: changed files, affected areas, dependency impact count, and high-impact files. Use to understand recent development activity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of commits (default: 20)"},
                "since": {"type": "string", "description": "Start date filter, e.g. '2026-05-01'"},
                "author": {"type": "string", "description": "Filter by author name"},
                "area": {"type": "string", "description": "Filter by directory path"},
            },
        },
    },
    {
        "name": "code_log_search",
        "description": "Search commits by message keyword or by code changes (symbols added/removed). Returns matching commits with impact scores. Use to find when and why a feature/bug was introduced.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query (matches commit messages and/or code changes)"},
                "search_in": {"type": "string", "description": "Where to search: message, code, or all (default)"},
                "n": {"type": "integer", "description": "Max results (default: 30)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "code_log_trace",
        "description": "Trace a symbol's evolution through git history. Shows every commit that modified the symbol, the diff context, and the impact of each change. Use to understand how and why a function/class evolved.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Symbol name to trace (function, class, etc.)"},
                "n": {"type": "integer", "description": "Max commits to show (default: 20)"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "code_log_impact",
        "description": "Deep analysis of a single commit: what files changed, what symbols were affected, who depends on each changed file, and an overall impact score. Use before merging/reverting a commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "commit": {"type": "string", "description": "Commit hash or ref to analyze"},
            },
            "required": ["commit"],
        },
    },
    {
        "name": "code_hotspots",
        "description": "Identify high-risk files — those that change frequently AND have many dependents. Score = change_frequency + dependent_count. Higher scores mean more risk. Use to prioritize code review and testing effort.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "n": {"type": "integer", "description": "Number of recent commits to analyze (default: 100)"},
                "since": {"type": "string", "description": "Start date filter, e.g. '2026-05-01'"},
            },
        },
    },
    {
        "name": "code_index_build",
        "description": "Build or update the codebase index. The index is required for all other tools. Call this when first entering a project, or after pulling new changes. Supports incremental updates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "incremental": {"type": "boolean", "description": "Incremental update — only re-index changed files (default: true)"},
                "commit": {"type": "string", "description": "Index a specific git commit instead of working tree"},
            },
        },
    },
]

# ── Tool dispatcher ─────────────────────────────────────────────────

def get_scripts_dir() -> str:
    """Get the directory containing the tool scripts."""
    return os.path.dirname(os.path.abspath(__file__))


def run_tool(project_root: str, tool_name: str, args: dict) -> str:
    """Execute a tool by calling its underlying Python script and return the output."""
    scripts = get_scripts_dir()

    # Ensure index exists (unless we're building it)
    index_path = os.path.join(project_root, ".code-index", "current.json")
    legacy_index = os.path.join(project_root, ".code-index.json")
    has_index = os.path.isfile(index_path) or os.path.isfile(legacy_index)

    if tool_name != "code_index_build" and not has_index:
        # Auto-build index
        run_tool(project_root, "code_index_build", {"incremental": False})

    if tool_name == "code_index_build":
        cmd = [sys.executable, os.path.join(scripts, "code_index.py"), project_root]
        if args.get("incremental", True):
            cmd.append("--incremental")
        commit = args.get("commit")
        if commit:
            cmd.extend(["--commit", commit])
        return _run_subprocess(cmd)

    elif tool_name == "code_summary":
        cmd = [sys.executable, os.path.join(scripts, "code_query.py"),
               "-i", _find_index(project_root), "summary"]
        return _run_subprocess(cmd)

    elif tool_name == "code_search":
        cmd = [sys.executable, os.path.join(scripts, "code_query.py"),
               "-i", _find_index(project_root), "search", args["query"]]
        if args.get("type"):
            cmd.extend(["--type", args["type"]])
        if args.get("lang"):
            cmd.extend(["--lang", args["lang"]])
        if args.get("limit"):
            cmd.extend(["--limit", str(args["limit"])])
        return _run_subprocess(cmd)

    elif tool_name == "code_deps":
        cmd = [sys.executable, os.path.join(scripts, "code_query.py"),
               "-i", _find_index(project_root), "deps", args["path"]]
        if args.get("depth"):
            cmd.extend(["--depth", str(args["depth"])])
        return _run_subprocess(cmd)

    elif tool_name == "code_dependents":
        cmd = [sys.executable, os.path.join(scripts, "code_query.py"),
               "-i", _find_index(project_root), "dependents", args["path"]]
        return _run_subprocess(cmd)

    elif tool_name == "code_diff":
        base = args["base"]
        target = args.get("target", "HEAD")
        fmt = args.get("format", "compact")
        cmd = [sys.executable, os.path.join(scripts, "code_diff.py"),
               project_root, base, target, "--live", "--format", fmt]
        return _run_subprocess(cmd)

    elif tool_name == "code_log_recent":
        cmd = [sys.executable, os.path.join(scripts, "code_log.py"),
               project_root, "recent", "-n", str(args.get("n", 20))]
        if args.get("since"):
            cmd.extend(["--since", args["since"]])
        if args.get("author"):
            cmd.extend(["--author", args["author"]])
        if args.get("area"):
            cmd.extend(["--area", args["area"]])
        return _run_subprocess(cmd)

    elif tool_name == "code_log_search":
        cmd = [sys.executable, os.path.join(scripts, "code_log.py"),
               project_root, "search", args["query"],
               "-n", str(args.get("n", 30)),
               "--in", args.get("search_in", "all")]
        return _run_subprocess(cmd)

    elif tool_name == "code_log_trace":
        cmd = [sys.executable, os.path.join(scripts, "code_log.py"),
               project_root, "trace", args["symbol"],
               "-n", str(args.get("n", 20))]
        return _run_subprocess(cmd)

    elif tool_name == "code_log_impact":
        cmd = [sys.executable, os.path.join(scripts, "code_log.py"),
               project_root, "impact", args["commit"]]
        return _run_subprocess(cmd)

    elif tool_name == "code_hotspots":
        cmd = [sys.executable, os.path.join(scripts, "code_log.py"),
               project_root, "hotspots", "-n", str(args.get("n", 100))]
        if args.get("since"):
            cmd.extend(["--since", args["since"]])
        return _run_subprocess(cmd)

    else:
        return f"Unknown tool: {tool_name}"


def _find_index(project_root: str) -> str:
    """Find the index file path."""
    store_index = os.path.join(project_root, ".code-index", "current.json")
    if os.path.isfile(store_index):
        return store_index
    legacy = os.path.join(project_root, ".code-index.json")
    if os.path.isfile(legacy):
        return legacy
    return os.path.join(project_root, ".code-index.json")


def _run_subprocess(cmd: list[str], timeout: int = 120) -> str:
    """Run a subprocess and return its stdout. Stderr is included in result if failure."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        if result.returncode != 0:
            return f"Error (exit {result.returncode}):\n{result.stderr}\n{result.stdout}"
        return result.stdout
    except subprocess.TimeoutExpired:
        return "Error: Tool execution timed out (limit: {timeout}s)"
    except Exception as e:
        return f"Error executing tool: {e}"


# ── MCP Protocol handler ────────────────────────────────────────────

class MCPServer:
    """Minimal MCP JSON-RPC 2.0 server over stdio."""

    def __init__(self, project_root: str):
        self.project_root = project_root
        self.initialized = False
        self.client_capabilities: dict = {}

    def handle_message(self, message: dict) -> dict | None:
        """Process a single JSON-RPC message. Returns response or None for notifications."""
        msg_id = message.get("id")
        method = message.get("method", "")
        params = message.get("params", {})

        if method == "initialize":
            return self._respond(msg_id, self._handle_initialize(params))
        elif method == "notifications/initialized":
            self.initialized = True
            return None  # No response for notifications
        elif method == "tools/list":
            return self._respond(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            return self._respond(msg_id, self._handle_tool_call(params))
        elif method == "ping":
            return self._respond(msg_id, {})
        else:
            return self._error(msg_id, -32601, f"Method not found: {method}")

    def _handle_initialize(self, params: dict) -> dict:
        self.client_capabilities = params.get("capabilities", {})
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        }

    def _handle_tool_call(self, params: dict) -> dict:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in {t["name"] for t in TOOLS}:
            return {"content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}], "isError": True}

        try:
            result = run_tool(self.project_root, tool_name, arguments)
            return {
                "content": [{"type": "text", "text": result}],
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Tool execution error: {e}\n{traceback.format_exc()}"}],
                "isError": True,
            }

    def _respond(self, msg_id, result) -> dict:
        return {"jsonrpc": JSONRPC, "id": msg_id, "result": result}

    def _error(self, msg_id, code: int, message: str) -> dict:
        return {"jsonrpc": JSONRPC, "id": msg_id, "error": {"code": code, "message": message}}

    def run(self):
        """Main loop: read JSON-RPC from stdin, write responses to stdout."""
        # Log to stderr so it doesn't interfere with stdout protocol
        print(f"MCP Server '{SERVER_NAME}' v{SERVER_VERSION} starting", file=sys.stderr)
        print(f"Project root: {self.project_root}", file=sys.stderr)

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"JSON parse error: {e}", file=sys.stderr)
                continue

            response = self.handle_message(message)

            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()


# ── Entry point ─────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="MCP Server for Code Retrieval")
    parser.add_argument("--project", default=".", help="Project root directory")
    parser.add_argument("--describe", action="store_true", help="Print tool descriptions and exit")
    args = parser.parse_args()

    if args.describe:
        print(json.dumps({"tools": TOOLS}, indent=2, ensure_ascii=False))
        return

    project_root = os.path.abspath(args.project)
    if not os.path.isdir(project_root):
        print(f"Error: project directory not found: {project_root}", file=sys.stderr)
        sys.exit(1)

    server = MCPServer(project_root)
    server.run()


if __name__ == "__main__":
    main()
