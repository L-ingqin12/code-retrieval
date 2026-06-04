# Agent Code Retrieval Strategy

面向 AI Agent 的代码分析策略体系。核心目标：用最少的上下文窗口消耗，获得最全面的代码理解。

---

## 工具矩阵速览

| 工具 | 功能 | 典型耗时 | 输出量级 |
|------|------|---------|---------|
| `code_index.py` | 构建索引 | 0.3-30s | ~codebase 1-3% |
| `code_query.py` | 索引查询 | <10ms | ~200B-5KB |
| `code_diff.py` | Commit 对比 | 0.1-3s | ~500B-10KB |
| `code_log.py` | 日志+影响分析 | 0.2-2s | ~500B-8KB |
| `grep` | 精确文本搜索 | 100-500ms | 按匹配量 |
| `LSP` | 定义/引用跳转 | ~50ms | 精确行号 |
| `git show/log` | 原生 git 查询 | ~100ms | 按需 |

---

## Agent 分析决策树

当你接到一个代码任务时，按以下路线选择工具组合：

```
任务是什么？
├── "理解这个项目" 
│   └── code_query summary → code_log area → 读入口文件
│
├── "找 XXX 函数/类的定义"
│   └── code_query search → LSP goToDefinition → Read 关键区域
│
├── "理解 XXX 功能的实现流程"
│   └── code_query search → code_query deps --depth 3 → 沿链 Read
│
├── "这个 Bug 什么时候引入的 / 为什么这么改"
│   └── code_log trace <symbol> → code_diff base target → code_log impact <commit>
│
├── "评估这个 PR / commit 的影响"
│   └── code_diff base target --live → code_log impact <commit> → code_query dependents
│
├── "最近哪些代码在频繁变更 / 哪里容易出问题"
│   └── code_log hotspots → code_log recent → 聚焦高热点区域分析
│
├── "某个历史版本中 XXX 是怎么实现的"
│   └── code_index --commit <ref> → code_query search → git show <ref>:<file>
│
└── "全面分析一个 commit（改了什么 + 为什么 + 影响谁）"
    └── code_log impact <commit>  (一站式输出)
```

---

## 策略 1-7: 基础分层检索

<details>
<summary>展开基础策略（索引查询、符号搜索、依赖链、grep、目录聚焦、窗口管理、多语言）</summary>

### 策略 1: 先看地图
```bash
python code_query.py summary    # 200 token 项目全景
```

### 策略 2: 符号优先
```bash
python code_query.py search "authenticate" --type function --lang python
```

### 策略 3: 依赖链追踪
```bash
python code_query.py deps src/auth/handler.py --depth 2   # 正向
python code_query.py dependents src/utils/token.py         # 反向
```

### 策略 4: grep 分层
```bash
grep -rl "keyword" --include="*.py" src/          # L1: 粗定位
grep -rn -C 3 "def authenticate" src/              # L2: 上下文
grep -rn "^class\s+\w*Auth" src/                   # L3: 语义模式
```

### 策略 5: 目录聚焦
```bash
python code_query.py dirs --depth 2
# → 识别领域目录后聚焦搜索
grep -rn "pattern" --include="*.py" src/auth/
```

### 策略 6: 上下文窗口管理
大文件先看结构锚点，再按需分段 Read。

### 策略 7: 多语言过滤
```bash
python code_query.py search "UserModel" --lang python
```

</details>

---

## 策略 8: Commit 日志驱动的追溯分析

### 场景：发现了问题，想知道是哪个 commit 引入的、为什么改的

**Step 1 — 搜索相关 commit**
```bash
# 按提交信息搜索
python code_log.py search "auth refactor" -n 20

# 按代码变更搜索（找改过特定函数名的 commit）
python code_log.py search "authenticate" --in code -n 20

# 全范围搜索（message + code）
python code_log.py search "timeout bug" --in all -n 30
```

**Step 2 — 追踪符号的演化历史**
```bash
# 看一个函数/类从诞生到现在的所有变更
python code_log.py trace "getUserPermissions" -n 30
```
输出包含：每个 commit 的 diff 行（含上下文）+ 每个 commit 的影响文件 + 这些文件的依赖数。

**Step 3 — 如果确定了一个可疑 commit，深入分析它的影响**
```bash
python code_log.py impact abc1234
```
输出包含：完整 commit message、变更文件数、影响的符号列表、每个变更文件的依赖者（被谁 import）、总影响评分。

### 为什么有效
不用 `git log` 一个 commit 一个 commit 地看。一步拿到"什么时候改的 + 改了哪些符号 + 改了影响谁"的完整链条。

---

## 策略 9: 影响面链式分析

### 场景：评估一个改动可能造成的影响

**链式分析流程：**
```bash
# 1. 看一个 commit 的直接影响
python code_log.py impact abc1234

# 2. 对有高依赖的文件，追踪其反向依赖链
python code_query.py dependents src/critical/file.py

# 3. 对比改动前后两个版本的完整差异
python code_diff.py abc1234 def5678 --live --format full
```

**多级影响评估：**
```
Level 0: 直接改了哪些文件  ← code_log impact
Level 1: 改了哪些符号      ← code_diff --format full
Level 2: 这些文件被谁依赖   ← code_query dependents
Level 3: 依赖链上哪些文件也会受影响 ← 递归 deps
```

### 输出解读
- `high_impact_files`: 改了后影响大量依赖者的文件 → **必须仔细 review**
- `symbols_affected`: 被删除或签名的符号 → **可能 Breaking Change**
- `dependent_count`: 依赖这个文件的文件数 → **越大越危险**

---

## 策略 10: 代码热点分析

### 场景：想知道代码库中哪些文件/目录风险最高，应该重点关注

```bash
# 找出最近频繁变更的高依赖文件（= 高热点）
python code_log.py hotspots -n 100 --since "2026-05-01"
```

输出格式：
```
Score   Chg  Deps  File                   Recent commits
  13      1    12  constants.py            abc1234
   9      1     8  game.py                 def5678
   7      4     3  ai.py                   abc, def, ghi, jkl
```

- **Score**: 综合热度 = 变更频率 + 依赖数
- **Chg**: 在分析的 commit 范围内被改了几次
- **Deps**: 有多少文件依赖它
- **含义**: 高 Score 文件 = 改得频繁 + 影响面大 = 高风险

### Agent 应该如何利用
1. 审阅代码时：优先看 hotspot 排名前 10 的文件
2. 评估 PR 时：如果 PR 碰到 top-5 hotspot 文件，需要更仔细 review
3. 定位 Bug 时：如果问题域和某个 hotspot 文件相关，优先怀疑它最近的变更

---

## 策略 11: 区域活跃度分析

### 场景：想知道项目的开发重心在哪

```bash
# 按目录聚合最近的提交活跃度
python code_log.py area
```

输出：
```
Area                 Commits  Authors
src/auth/                 15        3
src/api/                  12        4  
src/models/                3        1
tests/                    20        5
```

### Agent 解读
- 高活跃区域 = 正在积极开发的功能 → 可能不稳定
- 高活跃 + 少 author = 单点知识风险 → 只有一两人熟悉
- 零活跃 + 高依赖 = 稳定但关键的基础设施

### 结合其他工具
```bash
# 对活跃区域做深入的依赖分析
python code_query.py dirs --depth 2               # 看该区域结构
python code_log.py recent --area src/auth/ -n 20   # 只看该区域的 commit
```

---

## 策略 12: 跨版本的符号演变追踪

### 场景：理解一个模块的架构决策历史

```bash
# 完整链条：一个符号从创建到现在的每一次修改
python code_log.py trace "PaymentProcessor" -n 50
```

Agent 应该关注：
1. **符号的创建 commit** → 看最初的意图和设计
2. **参数变更** → 接口兼容性变化
3. **被删除后又恢复** → 可能的设计摇摆
4. **关联文件的同步变更** → 影响范围

### 结合版本对比
```bash
# 在两个重要时间点之间对比
python code_diff.py v1.0.0 v2.0.0 --live --format full
# 看一个模块在两次发布间的整体演进
```

---

## 策略 13: Agent 综合分析工作流

以下是一个 Agent 收到"分析这个 PR / 评估这个变更"任务时的完整工作流：

```bash
# 阶段 1: 快速概览（<2s，~1KB 上下文）
python code_log.py impact <commit>               # 变更的文件、符号、影响分
python code_log.py search "<keyword>" -n 5       # 是否有相关历史变更

# 阶段 2: 影响评估（<3s，~3KB 上下文）
python code_diff.py base target --live --format compact  # 符号级变更摘要
python code_log.py hotspots -n 30                # 变更文件是否在高热点区

# 阶段 3: 深度追溯（按需，~5KB 上下文）
python code_query.py dependents <changed-file>   # 每个变更文件的反向依赖
python code_log.py trace <key-symbol> -n 10      # 关键符号的历史演变

# 阶段 4: 上下文读取（按需）
Read <file> offset=<line> limit=60               # 只读变更相关片段
git show <commit>:<file>                         # 读变更前的版本作为对照
```

### 上下文预算指南
- 简单任务（找定义/查用法）：<1KB 上下文
- 中等任务（理解流程/评估小变更）：~3KB 上下文
- 复杂任务（PR review/架构分析）：~10KB 上下文
- 大型任务（跨版本审计/重构规划）：分散到多个子查询

---

## 问题 → 工具路由表

| 你遇到的问题 | 第一步 | 第二步 | 第三步 |
|------------|--------|--------|--------|
| "项目做什么的" | `code_query summary` | `code_log area` | Read 入口文件 |
| "XXX 在哪定义" | `code_query search XXX` | LSP goToDefinition | Read 关键行 |
| "XXX 怎么实现的" | `code_query deps --depth 3` | 沿依赖链 Read | LSP findReferences |
| "Bug 哪个 commit 引入" | `code_log search bug_desc --in all` | `code_log impact <commit>` | `code_diff --live` |
| "这个 PR 能合吗" | `code_log impact <pr-commit>` | `code_diff main pr --live` | `code_log hotspots` |
| "改这个函数会崩吗" | `code_query dependents file` | `code_log trace symbol` | `code_log hotspots` |
| "代码库哪最乱" | `code_log hotspots --since "3 months ago"` | `code_log area` | 对 top-5 做深度分析 |
| "为什么当时这么设计" | `code_log trace symbol -n 30` | Read 创建 commit 的 diff | `git log --grep` |
| "上次发布后改了什么" | `code_diff v1.0 v2.0 --live --format full` | `code_log recent -n 50` | `code_log area` |
| "迁移/重构的范围" | `code_query dependents target` | `code_log hotspots` | `code_diff --live` |

---

## 多工具组合速查

```bash
# ═══ 项目理解 ═══
python code_query.py summary                              # 宏观全貌
python code_log.py area                                   # 开发重心
python code_log.py hotspots -n 50                         # 风险文件

# ═══ 符号定位 ═══
python code_query.py search "symbol" --type function      # 秒级定位
python code_query.py deps file.py --depth 3                # 调用链
python code_query.py dependents file.py                    # 反向依赖

# ═══ 变更分析 ═══
python code_log.py impact <commit>                        # 单一 commit 影响
python code_log.py trace "symbol" -n 30                    # 符号演化史
python code_log.py search "keyword" --in all               # 搜索相关 commit
python code_diff.py base target --live --format full       # 两版本完整对比
python code_diff.py base target --live --format compact    # 两版本紧凑对比

# ═══ 近期动态 ═══
python code_log.py recent -n 20                            # 最近提交 + 影响摘要
python code_log.py recent --area src/auth/ -n 10           # 聚焦特定区域
python code_log.py recent --since "2026-05-01"             # 指定时间范围
python code_log.py recent --author "张三" -n 10            # 特定作者的提交

# ═══ 索引管理 ═══
python code_index.py .                                     # 构建/更新当前索引
python code_index.py --commit <ref>                        # 索引历史 commit
python code_index.py --list-stored                         # 查看已有索引
python code_index.py --incremental                         # 增量更新
```

---

## Agent 记忆要点

1. **永远先查索引**，不要直接全库 grep。索引在 <10ms 内给你答案。
2. **评估影响用日志**：`code_log impact` 一次性拿到 commit 的全部影响链。
3. **追溯历史用 trace**：`code_log trace <symbol>` 比 `git log -p` 快 10 倍且附带影响分析。
4. **热点 = 风险**：高 Score 的 hotspot 文件出问题时影响最大。
5. **组合使用，而非单一工具**：复杂问题需要 impact → dependents → trace 的链条。
6. **上下文窗口是稀缺资源**：用 compact/summary 格式，只在需要时展开 full。
