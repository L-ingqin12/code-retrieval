# MCP 集成指南

## 设计原则

**零侵入 — 不修改目标项目的任何代码。** 所有工具文件独立存放，索引数据存储在项目的 `.code-index/` 目录中。

---

## 快速开始（3 步）

```bash
# 1. clone 工具到固定位置
git clone https://github.com/L-ingqin12/code-retrieval.git ~/tools/code-retrieval

# 2. 配置 MCP 客户端（见下方各客户端说明）

# 3. 在 Agent 中直接使用 — 首次调用时自动构建索引
```

---

## 客户端配置

### Claude Code

**项目级** (`.mcp.json` 在项目根目录):
```json
{
  "mcpServers": {
    "code-retrieval": {
      "command": "python",
      "args": [
        "~/tools/code-retrieval/mcp_server.py",
        "--project",
        "${projectDir}"
      ]
    }
  }
}
```

**全局级** (`~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "code-retrieval": {
      "command": "python",
      "args": [
        "/home/user/tools/code-retrieval/mcp_server.py",
        "--project",
        "${projectDir}"
      ]
    }
  }
}
```

### OpenCode

**项目级** (`.opencode/mcp.json` 在项目根目录):
```json
{
  "mcpServers": {
    "code-retrieval": {
      "command": "python",
      "args": [
        "~/tools/code-retrieval/mcp_server.py",
        "--project",
        "${projectDir}"
      ]
    }
  }
}
```

**全局级** (`~/.config/opencode/mcp.json`):
```json
{
  "mcpServers": {
    "code-retrieval": {
      "command": "python",
      "args": [
        "/home/user/tools/code-retrieval/mcp_server.py",
        "--project",
        "${projectDir}"
      ]
    }
  }
}
```

或使用 OpenCode 的 `/mcp` 命令：
```
/mcp add code-retrieval -- python ~/tools/code-retrieval/mcp_server.py --project ${projectDir}
```

### Continue (VS Code / JetBrains)

`~/.continue/config.json`:
```json
{
  "experimental": {
    "mcpServers": {
      "code-retrieval": {
        "command": "python",
        "args": [
          "/home/user/tools/code-retrieval/mcp_server.py",
          "--project",
          "${projectDir}"
        ]
      }
    }
  }
}
```

### Cursor

`~/.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "code-retrieval": {
      "command": "python",
      "args": [
        "/home/user/tools/code-retrieval/mcp_server.py",
        "--project",
        "${projectDir}"
      ]
    }
  }
}
```

### 通用格式（适用于任何 MCP 客户端）

所有客户端配置格式一致，核心就是 `command` + `args`：

```json
{
  "mcpServers": {
    "code-retrieval": {
      "command": "python",
      "args": ["/path/to/mcp_server.py", "--project", "${projectDir}"]
    }
  }
}
```

> `command` 也可用 `python3`，取决于系统。建议使用绝对路径避免 PATH 问题。

---

## 作为 Git Submodule（推荐团队使用）

```bash
# 在目标项目中
git submodule add https://github.com/L-ingqin12/code-retrieval.git .claude/tools/code-retrieval
```

然后在项目 `.mcp.json` 中指向 submodule 路径：

```json
{
  "mcpServers": {
    "code-retrieval": {
      "command": "python",
      "args": [
        "${projectDir}/.claude/tools/code-retrieval/mcp_server.py",
        "--project",
        "${projectDir}"
      ]
    }
  }
}
```

项目 `.gitignore` 中添加：

```
.code-index/
.code-index.json
```

---

## 工具清单

Agent 连接后自动获得 11 个工具：

| 工具 | 说明 | 典型耗时 |
|------|------|---------|
| `code_summary` | 项目全景（文件数/语言/入口点/目录结构） | <50ms |
| `code_search` | 按符号名搜索函数/类/接口 | <50ms |
| `code_deps` | 正向依赖链（文件依赖了谁） | <50ms |
| `code_dependents` | 反向依赖（谁依赖了这个文件） | <50ms |
| `code_diff` | 两个 commit/branch 的符号级对比 | ~2s |
| `code_log_recent` | 最近提交 + 代码影响摘要 | ~500ms |
| `code_log_search` | 按 message 或代码变更搜索 commit | ~500ms |
| `code_log_trace` | 符号的完整演化历史 | ~500ms |
| `code_log_impact` | 特定 commit 的影响分析 | ~500ms |
| `code_hotspots` | 高风险文件（频繁修改 + 高依赖） | ~1s |
| `code_index_build` | 构建/增量更新索引 | ~0.3-30s |

---

## 依赖

**零外部依赖。** MCP Server 仅使用 Python 3.10+ 标准库 (`json`, `subprocess`, `os`, `sys`)。

唯一前提：项目需在 git 仓库中（git log / diff 等功能依赖 git）。

---

## MCP 方式 vs 直接脚本调用

| 维度 | 直接调用脚本 | MCP Server |
|------|------------|-----------|
| 集成 | 手动拼 shell 命令 | Agent 自动发现 |
| 入参 | 字符串拼接 | 结构化 JSON Schema（类型校验） |
| 索引 | 需手动 `code_index.py` | 首次调用自动构建 |
| 上下文 | Agent 需记忆命令格式 | Agent 看 tool description 即可 |
| 跨项目 | 需 `cd` 切换 | `${projectDir}` 自动注入 |
| 团队 | 每人自己配 alias | `.mcp.json` 提交到仓库 |
| 多客户端 | 每个客户端不同配置 | MCP 协议统一，一次编写到处运行 |
