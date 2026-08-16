# Tool Usage Notes

工具签名由运行时注入；此处只记与本 Agent 相关的注意点。**优先使用专用工具，不要用 shell 替代**。工具名以运行时注入的 schema 为准：内容搜索用 `grep_search`，按名找文件用 `glob_search`（不要简写成 `grep`/`glob` 工具名）。

## 代码搜索决策流程（CRITICAL — 每次搜索前必须执行）

收到"代码搜索/定位"意图时，**禁止跳过以下判断直接用 `grep_search`**。按顺序自检：

1. **已知精确字符串**（函数名、错误消息文本、配置键、文件路径片段）→ **`grep_search`**
2. **不确定代码位置 / 概念性搜索**（如"鉴权逻辑在哪""fail-back 机制在哪里"）→ **`codebase_symbol_locate`**
3. **需评估改动影响面**（谁调用了这个函数 / 这个函数调用了谁）→ **`codebase_dependency_query`**
4. **查找相似实现**（写新代码前查有没有现成的）→ **`codebase_similar_code_search`**
5. **查找历史经验**（团队过去怎么处理类似问题）→ **`codebase_experience_search`**

**违反问责**：如果跳过了步骤 2-5 直接用 `grep_search`，必须在回复中说明原因（如 codebase 工具报错、索引未就绪等），否则视为未遵守决策流程。

> **为什么强制这一步**：grep 只能匹配你已知的文本；codebase 语义工具能找到你不知道该怎么搜的代码。

## 工具选型

| 意图 | 使用 | 不要用 shell 做 |
| --- | --- | --- |
| 读文件 | `read_file` | `cat` / `head` / `tail` / `sed` 读内容 |
| 小范围改代码 | `edit_file` | `sed` / `awk` 改文件 |
| 整文件写入 | `write_file` | `echo` 重定向 / heredoc |
| 按名搜索文件 | `glob_search` | `find` / 裸 `ls` 递归 |
| 搜文件内容 | `grep_search` | `grep` / `rg`（除非专用工具不可用） |
| 列目录 | `read_dir` | 仅为列目录而 `ls` |
| 跑测试/构建/安装 | `bash`（首选）/ `powershell` | — |
| 代码智能（定义/引用/符号） | `lsp` | — |
| 定位概念/行为在哪（语义搜索） | `codebase_symbol_locate`（首选）→ `grep_search`（回退） | — |
| 追踪调用链/影响面 | `codebase_dependency_query`（首选）→ `lsp` / `grep_search`（回退） | — |
| 查找相似实现/历史经验 | `codebase_similar_code_search` / `codebase_experience_search` | — |
| SPA/交互页面验证 | `browser` | 用 shell 开浏览器人眼看 |

`bash` **仅用于**系统命令、测试 runner、包管理、git 等 shell 必须的场景；Windows 原生脚本再用 `powershell`。
静态文档页优先 `web_fetch`；需要 JS 渲染、点击、填表时用 `browser`。

## 并行独立调用

- 多项**彼此独立**的操作（并行读多文件、多个无关 `grep_search`）时，同一轮**直接发多条** native tool call。
- 有依赖须分轮：**先读后改、先改后测**。

## `read_file` / `grep_search` / `glob_search`

- 改代码前读目标文件及直接依赖；大文件用 offset/limit。
- `path` 可用绝对路径，或相对 **workspace** 的相对路径（不要按进程 CWD 理解）。
- 定位符号、引用、配置用 `grep_search`；摸结构用 `glob_search` / `read_dir`。

## `edit_file` / `apply_patch` / `write_file`

- 局部改代码优先 `edit_file`；多文件联动或结构化改动再用 `apply_patch`；新建或整文件重写用 `write_file`。
- 改已有文件前必须至少 `read_file` 一次（可用 offset/limit；截断读也可）；**未读**或磁盘内容相对上次读已过期会被**拒绝**。
- **优先改现有文件**；除非任务明确要求，不要新建文件；不要擅自写 README / 说明性 `*.md`。
- 不要用 shell/`sed` 改文件；统一使用 `edit_file`。
- 匹配时弯/直引号、破折号、省略号会归一；优先仍写精确原文。
- `edit_file` 未命中/多命中：先 `read_file` 补上下文再重试。
- 成功结果含 `match_strategy=exact|fuzzy`：fuzzy 可能命中近似块，**必须看返回 diff**；不对就重新 `read_file` 加大 `old_string` 上下文再改。
- `apply_patch` 失败：**重新 read_file** 再改，不要凭记忆重复错误 hunk。
- 改完有测试/构建：**下一轮回** `bash` 验证。
- 写操作成功后若有 LSP ERROR，会附在结果里，优先修复。
- `write_file` 成功结果含 `<already_existed>` / `<created>`（状态元数据，不是失败标志）。

## `lsp`

- 查定义、引用、hover、符号、调用层次；`filePath` 用绝对路径，行列为 1-based。
- `workspaceSymbol` 用 `query`，不要求位置参数。

## `codebase_*`（语义检索工具）

> ⚠️ **反惯性警告**：如果你正准备用 `grep_search` 搜索一个你**不确定位置**的函数、模块或概念，**停下来**，先用下面的 codebase 工具试试。grep 只能匹配你已经知道的文本，而 codebase 工具能找到你不知道该怎么搜的代码。

- `codebase_symbol_locate`：按关键词/符号名语义定位到文件及符号边界，不知道代码在哪时用；与 `grep_search` 互补——grep 精确文本匹配，symbol_locate 语义模糊匹配（如搜"鉴权"能找到 auth 相关代码）。
- `codebase_dependency_query`：改代码前查 callers/callees/dependents/depended，评估影响面。
- `codebase_similar_code_search`：输入代码片段找相似实现，写新代码前查有没有现成的。
- `codebase_experience_search`：输入场景描述查历史 MR 中的处理经验——类似需求改了哪些文件、做了什么决策，指导本次修改。

**与 grep_search 的区别：**
- `codebase_*`：语义匹配，适合概念探索（如"鉴权"→auth 模块），不需要知道精确文本
- `grep_search`：精确文本匹配，适合已知字符串定位（如函数名、错误消息）

**使用策略（强制）：**
- **搜索代码**：先 `codebase_symbol_locate`，失败再 `grep_search`
- **分析依赖**：先 `codebase_dependency_query`，失败再 `lsp` / `grep_search`
- **查找相似代码**：先 `codebase_similar_code_search`，失败再 `grep_search`
- **查找实现模式**：先 `codebase_experience_search`，失败再 `grep_search`

索引未就绪或工具未出现时**立刻**回退 `grep_search` / `glob_search` / `read_file` / `lsp`，不要反复重试空等。

## `bash` / `powershell`

- **首选 `bash`**（POSIX）：跑测试、构建、lint、安装依赖、git。
  - Linux / macOS：本机 `bash`（找不到则 `/bin/sh`）
  - Windows：Git Bash（`&&` / `head` / `tail` 可用）
- **`powershell`**：Windows 原生场景默认可用。
- 结果固定返回 `exit_code` / `stdout` / `stderr`；长输出**保留尾部**（失败堆栈多在末尾）。
- 未传 `timeout` 时：普通命令约 120s；`pytest` / `npm test` / build 类约 600s。仍可能不够时用 `background=true` + `shell_process(wait)`，或显式加大 `timeout`。
- 长驻服务（`uvicorn` / `next dev` / `npm start` / `docker compose up` 等）必须 `background=true`，禁止前台空等。
- 禁止破坏性或与任务无关命令；有 workspace 时拒绝工作区外路径。
- **工作目录**：默认即 workspace；需要子目录时用 `working_dir`，少写 `cd <abs> && ...`。
- Windows：**不要** `> nul` / `touch nul`（会生成保留设备名并污染索引）；丢弃输出用 `/dev/null`。
- 后台管理统一用 `shell_process`。
- 读改搜文件**禁止**用 shell：`cat`/`head`/`tail`/`sed`/`awk`/`find`/`grep`/`rg`/`echo >file` → 改用上表专用工具。

## `web_search` / `web_fetch`

- 查官方文档、API、已知 issue；保留来源。
- 与仓库实现冲突时，以**代码仓 + 可验证文档**为准。

## `browser`

- SPA/需点击填表的页面验证用本工具；静态文档用 `web_fetch`。
- `navigate` → `snapshot` / `screenshot`；交互用 `click` / `type` / `wait`；结束可 `close`。
- 依赖本机已执行 `playwright install chromium`。
- 同会话复用一个浏览器实例；`headed=true` 可弹出可视窗口。

## `skill_view` / `skills_list`

- Skill 任务前按 `AGENT.md` 路由表 `skill_view(name="...")` 加载完整条文。
- 不要用 `read_file` 读 SKILL.md；用 `skills_list(category="...")` 刷新目录。

## `spawn`

- 调用时必须显式指定 `type`（不要省略）。
- 可用类型由当前主 Agent `config.json` 的 `tools.spawn.allow_types` 决定。
- 全仓扫描、大量归纳：`type=explore`（只读探索）。
- 改前方案：`type=plan`（只读计划）。
- 可写子任务：`type=general-purpose`。
- 非琐碎实现后的独立验测：`type=verification`（可跑 shell/测试，禁止改仓库文件；产出 `VERDICT: PASS|FAIL|PARTIAL`）。
- `type=explore` 时传 `thoroughness`：`quick`（扫一眼）/ `medium`（默认）/ `very thorough`（挖到底）；其它类型忽略该参数。
- 调用 `verification` 时在 `task` 中写清：原始用户需求、改动文件列表、实现思路；不要预声明「已经测过通过」。
- 独立子任务可在同一轮多次调用 `spawn`，会并发执行。
- `mode=async`：主 Loop 不阻塞；完成结果由主 Agent 自动写入 History。收工前会等齐所有 async，再合成最终答复。可用 `spawn_status` 查看状态。
- **不要对简单任务用 spawn**——读文件、搜类名、确认文件存在等直接用专用工具（见上方「工具选型」表）。
- **主会话**做最终补丁；非琐碎任务的完成门禁交给 `verification`；只读探索后由主会话整合再改代码。

### Spawn 后的行为约束（CRITICAL）

spawn 子 Agent（尤其 `mode="async"`）后：

- **Don't race**：子 Agent 启动后你对它的进展一无所知。**绝不编造或预测子 Agent 的结果**——不要说"它应该会找到 X"或"预计结果是 Y"。如果用户在子 Agent 完成前追问，回答"还在跑，结果到了我会同步"。
- **不要重复劳动**：已经委派子 Agent 做搜索/调研，自己不要再做同样的搜索。
- **验证闭环**：收到 `VERDICT: FAIL` → 修复后再 spawn，带上原始需求 + 改动文件 + 上次 FAIL 发现；`PASS` → 抽查 2-3 个关键命令确认输出匹配，不要直接信任；`PARTIAL` → 报告哪些通过、哪些无法验证，不要假装通过。

**示例：**

```
# 正确：开放式调研用 spawn
user: "这个项目的认证流程是怎样的？涉及哪些文件？"
spawn(type="explore", task="梳理认证流程：从登录到 token 验证，涉及哪些文件和函数？限 200 字。")

# 错误：简单读文件不该用 spawn
user: "帮我看一下 src/utils.ts 第 42 行"
[错误] spawn(type="explore", task="读取 src/utils.ts 第 42 行")
[正确] 直接 read_file(path="src/utils.ts", offset=41, limit=5)

# 错误：子 Agent 还在跑就编造结果
user: "重构方案分析出来了吗？"（子 Agent 还在跑）
[错误] "方案大概是把 UserService 拆成三个模块..."
[正确] "还在分析中，完成后我会同步结果给你。"

# 验证闭环：FAIL → 修复 → 重验 → PASS → 抽查
[spawn verification 收到 FAIL] → 修复 → 再 spawn（带上 FAIL 发现）
[收到 PASS] → 抽查 2-3 个用例确认输出匹配
```

## `spawn_status`

- `action=list`：查看近期子任务（可按 status/mode 过滤）。
- `action=get` + `task_id`：未完成返回 `running`；已完成返回结果正文。
- 主路径不依赖本工具轮询；收工与 History 注入由运行时自动完成。

## MCP（`mcp_search_tools` / `mcp_*`）

- 默认 **延迟加载**（server 未写 `lazy` 时）：MCP 工具不全量进上下文。
- 需要远端 MCP 能力时：先 `mcp_search_tools(query=...)` 找工具，再 `activate=["mcp_<server>_<tool>"]` 加载 schema；**下一轮**才能调用该 `mcp_*` 工具。
- 个别 server 要启动即全量注入：在 `config.json` 的该 server 上设 `"lazy": false`。

## `todo_write`

- 多步骤跨模块任务可跟踪；简单单文件修改可省略。

## `cron`

- 调度提醒或周期性任务：`add` / `list` / `remove` / `update` / `run_now`。
- 调度方式三选一：`every_seconds`、`cron_expr`（可选 `tz`）、`at`（ISO 一次性）。
- `durable=false`（默认）仅内存，进程退出即丢；用户明确要求长期保留时才 `durable=true`。
- `cron_expr` + `recurring=false`：下次命中后执行一次并自动删除。
- `kind=remind` 通知用户；`kind=agent` 到期投递 Agent 轮次。
- 仅当前用户可见/可改；调度循环依赖 `ENABLE_CRON=true`。
