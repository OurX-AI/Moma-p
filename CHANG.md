# 初始发布版本优化（2026.07~08）

## 背景
业界AI Agent发布不停，我们自己没做一个原创编码Agent。
刚好年初用AI开发了一个通用Agent框架，主要模型+网页操作的搜索。
因此，业务继续用AI尝试开发一个编码Agent，看看关键部分难度在哪里？

关键感觉：
1. 框架好些，工程细节调优是关键。(从跑通框架但实际编码不可以用，到现在我自己PC用自己Agent开发Agnet，历时4个月（大部分是周末和中午），6W行代码。)
2. 很多调优是有思路后要实验，比如意图识别怎么转换为约束，上下文压缩阈值什么合适等。


## 1. 性能与准确性优化

### 1.1 优化效果

#### 1.1.1 评测效果对比（SWE-bench Lite）

SWE-bench 是业界标准的代码修复评测基准，包含 2294 个真实 GitHub issue 任务；SWE-bench Lite 是从中筛选的 300 个任务子集，降低了评测成本但保持了代表性。由于完整跑 300 个任务仍然耗时且开销较大，在迭代优化阶段我们进一步精选了 5 个任务（requests-1963、flask-4045、seaborn-2848、pylint-5859、pytest-11143）作为快速验证子集——覆盖了不同难度级别和 bug 类型，能在单次评测中快速暴露问题。

对比 MOMA、Claude Code、Cursor 三款 Agent 工具的修复能力。

**评测基准数据（2026-08）：**

##### A. Gold 文本覆盖度评测（gold-line scoring）

**指标说明：** 将 gold patch（官方参考修复）中的每行新增代码（`+` 行）与 agent 的 patch 做文本匹配，统计命中行数。分母 13 = 5 个用例的 gold 新增行总数（requests 2 + flask 6 + seaborn 2 + pylint 2 + pytest 1）。**此指标衡量的是"agent 改得像不像参考答案"，不涉及测试执行，措辞不同即算 miss。仅作为辅助参考，不代表真实修复能力。**

**执行条件：**

| 条件 | MOMA | Claude Code | Cursor |
|------|------|-------------|--------|
| 评测脚本 | `compare_gold_coverage.py` | 同左 | 同左 |
| 匹配方式 | 归一化文本子串匹配 | 同左 | 同左 |
| 数据来源 | 各 agent 在 smoke5 上独立跑出的 patch | 同左 | 同左 |

MOMA smoke5 评测的演进路径（gold-line scoring）：2/13 → 6/13 → 7/13 → 10/13。下表"期初"列为未经优化的基线，"优化后"列为 verification tightening 后的最佳版本。

| 任务实例 | Cursor 命中 | Cursor 耗时 | Claude Code 命中 | Claude Code 耗时 | MOMA（期初）命中 | MOMA（期初）耗时 | MOMA（优化后）命中 | MOMA（优化后）耗时 |
|----------|------------|------------|-----------------|-----------------|----------------|----------------|-------------------|-------------------|
| requests-1963 | 2/2 | 116s | 2/2 | 1053s | 0/2 | 1800s | 1/2 | 683s |
| flask-4045 | 6/6 | 103s | 6/6 | 349s | 0/6 | 549s | 6/6 | 441s |
| seaborn-2848 | 2/2 | 164s | 1/2 | 1800s | 0/2 | 538s | 2/2 | 553s |
| pylint-5859 | 0/2 | 144s | 2/2 | 416s | 1/2 | 1509s | 0/2 | 371s |
| pytest-11143 | 1/1 | 244s | 0/1 | 0.1s | 1/1 | 978s | 1/1 | 467s |
| **总计** | **11/13** | **771s** | **11/13** | **3618s** | **2/13** | **5373s** | **12/13** | **2517s** |

##### B. 真实测试执行评测（test execution / SWE-bench resolved）

**指标说明：** 按 SWE-bench 标准流程执行——checkout base_commit → apply agent patch → apply gold test_patch（补充 FAIL_TO_PASS 测试）→ 运行 pytest。`resolved` 要求 FAIL_TO_PASS 全过且 PASS_TO_PASS 全过。FTP = FAIL_TO_PASS 通过数/总数，PTP = PASS_TO_PASS 通过数/总数。**此指标衡量的是"agent 的修复是否真正通过了测试"，是评测的核心指标。**

**执行条件（2026-08-15）：**

| 条件 | MOMA（DeepSeek） | MOMA（Mimo） | Claude Code | Cursor |
|------|-----------------|-------------|-------------|--------|
| Agent 版本 | Moma-Coder dev 分支 | 同左 | Claude Code（ccb CLI） | Cursor Agent CLI（`agent`） |
| 模型 | DeepSeek v4-pro（api.deepseek.com） | mimo-2.5 | mimo-2.5 | Cursor 默认模型 |
| 超时 | 1800s/实例 | 同左 | 同左 | 同左 |
| 评测脚本 | `score_predictions.py` | 同左 | 同左 | 同左 |
| Python | hermes venv（Python 3.11.9） | 同左 | 同左 | 同左 |

评分公式：`score = 0.15×apply + 0.05×test_patch + 0.6×(FTP通过/FTP总数) + 0.2×(PTP通过/PTP总数)`

| 实例 | 指标 | MOMA（DeepSeek） | MOMA（Mimo） | Claude Code | Cursor |
|------|------|-----------------|-------------|-------------|--------|
| requests-1963 | apply | ✅ | ✅ | ✅ | ✅ |
| | FTP | 1/7 | 1/7 | 0/7 | 1/7 |
| | PTP | 82/112 | 82/112 | 83/112 | 83/112 |
| | **score** | **0.432** | **0.432** | **0.348** | **0.434** |
| flask-4045 | apply | ✅ | ✅ | ✅ | ✅ |
| | FTP | 2/2 | 1/2 | 2/2 | 2/2 |
| | PTP | 50/50 | 50/50 | 50/50 | 50/50 |
| | **score** | **1.000** | **0.700** | **1.000** | **1.000** |
| seaborn-2848 | apply | ✅ | ✅ | ✅ | ✅ |
| | FTP | 1/1 | 0/1 | 0/1 | 1/1 |
| | PTP | 50/50 | 50/50 | 50/50 | 50/50 |
| | **score** | **1.000** | **0.400** | **0.400** | **1.000** |
| pylint-5859 | apply | ✅ | ✅ | ✅ | ✅ |
| | FTP | 1/1 | 1/1 | 1/1 | 1/1 |
| | PTP | 10/10 | 10/10 | 10/10 | 10/10 |
| | **score** | **1.000** | **1.000** | **1.000** | **1.000** |
| pytest-11143 | apply | ✅ | ✅ | ✅ | ✅ |
| | FTP | 0/1 | 0/1 | 0/1 | 0/1 |
| | PTP | 0/114 | 0/114 | 0/114 | 0/114 |
| | **score** | **0.200** | **0.200** | **0.200** | **0.200** |
| **平均** | | **0.726** | **0.546** | **0.590** | **0.727** |
| **resolved** | | **3/5** | **1/5** | **2/5** | **3/5** |

> **A/B 两表对比说明：** A 表中 Cursor 和 Claude Code 都达到 11/13 gold 覆盖度，看似接近满分；但 B 表的真实测试执行显示 Cursor 和 MOMA（DeepSeek）各 3/5 resolved，说明文本匹配与实际修复能力之间存在显著差距——"改得像"不等于"改对了"。

**关键发现：**
- MOMA（DeepSeek）3/5 resolved，与 Cursor 持平；MOMA（Mimo）仅 1/5，模型选择影响显著
- flask-4045：DeepSeek 修全两处（FTP=2/2），Mimo 只修了 `__init__`（FTP=1/2），未遵循"修前搜全"策略
- seaborn-2848：DeepSeek 修对了 `_oldcore.py` 的 KeyError 兜底（FTP=1/1），Mimo 修错方向（FTP=0/1）
- Claude Code 2/5 resolved，seaborn 修错方向（数据过滤 vs color lookup 兜底）
- requests-1963 和 pytest-11143 三家都没完全解决，为共同难点
- MOMA（DeepSeek）prompt 优化（implement.md 加"修前搜全"规则）后 flask 从 1/2 → 2/2，验证了 prompt 改进的有效性
- Claude Code 的 seaborn 实例因 ccb 命令行长度限制导致首次运行失败，修复 runner 后重跑成功但超时；pylint 和 pytest 均正常完成

#### 1.1.2 任务执行效率对比

基于历史 session 记录，对比优化前后 MOMA 处理同一任务（failback 机制诊断）的执行效率。

**执行效率对比（同一任务：failback 机制诊断——分析 MOMA 项目模型配额耗尽后为何没有 fallback 备用模型）：**

| 指标 | Claude Code | MOMA 优化前 | MOMA 优化后 | MOMA改善幅度 |
|------|------------|------------|------------|------------|
| Assistant 轮次 | 5轮 | 39 轮 | 7轮 | ↓82% |
| 工具调用总数 | 11次 | ~90 次 | 18 次 | ↓80% |
| grep_search 调用 | 4次 | 30 次 | 5次 | ↓83% |
| read_file 调用 | 7次 | 54 次 | 11次 | ↓80% |
| 执行耗时 | 2.5 min | ~17 min | 3 min | ↓82% |
| 任务完成状态 | 完成 | 完成 | 完成 | - |

#### 1.1.3 CodeBase 开通前后对比

CodeBase 是 MOMA 的代码语义索引层，提供基于 embedding 的代码理解和依赖分析能力。支持以下四种能力：

1. **symbol_locate（符号定位）**：通过符号名或关键词在语义索引中定位代码文件和行范围，支持函数/类/变量的定义和引用查找
2. **dependency_query（依赖查询）**：查询文件或符号的依赖关系，支持 dependents（谁依赖我）、depended（我依赖谁）、callers（谁调用我）、callees（我调用谁）四个方向
3. **similar_code_search（相似代码搜索）**：给定一段代码片段，查找代码库中相似的实现，用于复用已有模式
4. **experience_search（经验搜索）**：搜索从历史 MR/commit 中提取的经验模式（架构决策、约定、迁移策略等）

**CodeBase 工具 A/B 测试（2026-08-09，同一调试任务，四次对比）：**

| 配置 | 耗时 | 工具调用 | 任务完成 | 分析深度 |
|------|------|----------|----------|----------|
| CodeBase ON | ~8.5 min | 47 次 | 完成 | 诊断 + 1 修复建议 |
| CodeBase OFF（grep-only） | ~4.5 min | 20 次 | 完成 | 诊断 + **2 修复建议** |
| CodeBase ON（再次） | ~8.9 min | 32 次 | 完成 | 诊断 + **3 修复建议** |

A/B 测试进一步验证：对于 debugging/tracing 类任务，grep-only 路径（4.5 min / 20 次调用）比 CodeBase ON（8.5 min / 47 次调用）更快且分析更深。`codebase_symbol_locate` 返回低相关度结果，`codebase_dependency_query` 对下划线方法返回空结果。

**结论：** 对于中小规模代码仓库，CodeBase 的 symbol 定位并未带来准确率提升——原因是 symbol 定位是模糊语义匹配（embedding 相似度），而 grep 是精准字符串匹配。在小仓库中 grep 直接搜索关键词更快更准。CodeBase 的价值主要体现在大仓库的代码导航和依赖分析场景。

---

### 1.2 用户消息预分析优化

#### 概述

在用户消息进入 Agent 主循环前进行意图分类，识别 implement / consult / analyze / debug 四种任务类型，根据意图注入不同的行为指引和收工输出格式模板。独立 preprocess 模块支持多 Agent 类型派发。

涉及提交：7a1ca0b、22e0c00

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | 意图分类 | ✅ | 将用户消息分类为 implement / consult / analyze / debug 四种任务类型。核心文件：`app/agents/preprocess/`（新增模块） |
| 2 | 任务类型指引注入 | ✅ | 根据意图注入不同的行为指引和收工输出格式模板 |
| 3 | 独立 preprocess 模块 | ✅ | 支持多 Agent 类型派发，问题改写对用户可见 |

**Before：** 用户消息直接传入 Agent 主循环，无意图分类。Agent 无法区分"请帮我修改这个 bug"（需要 edit_file）和"请解释这个函数的逻辑"（不需要 edit_file），导致两类任务使用相同的行为模式。咨询类任务中 Agent 经常不必要地调用 edit_file，实现类任务中 Agent 又经常过度探索而不动手修改。

**After：** `CoderPreprocess` 在用户消息进入 Agent 主循环前进行意图分类（使用轻量级 LLM 调用），将消息分类为四种任务类型并注入对应的行为指引。四种任务类型的具体规则和输出要求如下：

**implement（代码修改）—— 行为规则：**
- 动手优先：定位到 bug 所在文件+函数+具体行后，立刻 `edit_file`。不确定改法时，先写一个最小修复，跑测试看结果，比继续读更有效。
- 避免完美主义探索：连续多次 `read_file` / `grep_search` / `glob_search` 而不出 `edit_file` 是探索陷阱，只消耗 token 不产生 patch。
- 读完即改：读完相关文件后，下一个动作应是 `edit_file` 或 `bash`（跑测试/复现），而非继续读更多文件加深理解。
- 搜索预算：单文件修复 1-2 次搜索足矣；跨文件 3-5 次；超过 5 次仍定位不到，换 pattern 或换工具（`lsp` / `codebase_*`）。

**implement 收工输出格式：**
- **完成情况**：一句话结论（完成 / 部分完成 / 未完成 + 核心交付物）
- **改动文件**：每个文件一行（`path/to/file` - 改动要点，说 what 不说 why）；无改动写"无（仅查询/排查/答疑）"
- **验证**：跑了什么命令 / `spawn verification` 裁决 / 结果（PASS / FAIL / skipped + 原因）；未验证须说明原因
- **未覆盖/后续**：风险、待办、建议下一步；无则写"无"

**consult（咨询/解释）—— 行为规则：**
- 以解释为主：用户希望理解代码/机制，不要修改代码。
- 读优先：用 `read_file` / `grep_search` 充分理解相关代码，然后给出结构化解释。
- 不要 edit：除非用户明确要求修改，否则不要调用 `edit_file` / `apply_patch` / `write_file`。
- 解释结构：先给一句话总结，再展开细节；引用具体文件:行号。

**consult 收工输出格式：**
- **结论**：直接回答用户问题，不绕弯
- **关键依据**：`path/to/file:line` - 支撑结论的代码位置（2-4 处）；纯概念问题无代码依据时写"无（纯概念解释）"
- **延伸说明**：相关边界情况、注意事项、相关 API/配置；无则写"无"

**analyze（分析/对比）—— 行为规则：**
- 结构化对比：用户希望对比方案/分析 trade-off，不要急于改代码。
- 列选项：列出 2-3 个可行方案，每个给 trade-off（优点/缺点/适用场景）。
- 引用证据：用 `read_file` / `grep_search` 拿到具体代码作为分析依据，不要凭空推断。
- 给建议：分析后给一个推荐方向，但不要直接 edit——等用户决定。

**analyze 收工输出格式：**
- **结论/建议**：推荐哪个方案 + 一句话理由
- **方案对比**：方案 A/B/C 各自的优点 / 缺点
- **取舍依据**：按什么维度判（性能 / 可维护 / 工期 / 风险...）
- **后续**：需要用户拍板的点 / 建议下一步；无则写"无"

**debug（调查/复现）—— 行为规则：**
- 先复现：用 `bash` 跑复现脚本/测试，拿到实际错误信息。
- traceback 优先：错误堆栈中的 `File "..."` 行号是定位起点。
- 最小假设：基于复现结果提出根因假设，用 `read_file` 验证，而非盲目读多个文件。
- 暂不 edit：调查阶段以理解为主，根因确认后再考虑修复。

**debug 收工输出格式：**
- **根因**：一句话定位问题
- **证据链**：现象 -> 假设 -> 验证 -> 结论（每步贴关键输出 / `文件:行`）
- **已做修复**：改了什么 / `path/to/file`；未改写"仅排查，未改代码"
- **仍需验证**：复测命令 / 待用户确认的点；无则写"无"

**分类规则（LLM 分类器使用）：**
- 根据用户的主要意图分类，而非表面关键词
- `fix/modify/implement/refactor/add feature` 动词 -> implement
- `explain/how does/why/what is` 动词 -> consult
- `compare/analyze/trade-off/pros and cons` -> analyze
- `investigate/why is this failing/reproduce` -> debug
- 用户提到 bug 但只问"为什么" -> consult（不是 implement）
- 用户说"fix this bug" -> implement
- SWE-bench 风格任务描述（fix issue X, modify code to address Y）-> implement
- 意图不明确时默认为 implement（这是 coding agent，代码修改是主要职责）

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | 意图分类准确率提升 | P2 | 基于历史 session 数据微调分类模型，提高边界案例的分类准确率 |
| 2 | 多轮意图跟踪 | P3 | 在多轮对话中跟踪意图变化，动态调整行为指引 |

---

### 1.3 上下文压缩优化

#### 概述

通过两阶段压缩策略管理上下文窗口，避免长会话中 token 超限导致 API 失败。MicroCompact 每轮按 token 阈值精准裁剪旧工具结果，Autocompact 在仍超限时进行全量摘要压缩。同时引入异常驱动的 Reactive Compact，在 API 返回上下文溢出错误时自动压缩重试。

涉及提交：2c137f3、7a1ca0b、22e0c00、3f2cda1

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | 两阶段压缩策略 | ✅ | MicroCompact（每轮 token 阈值触发，精准裁剪旧工具结果）+ Autocompact（仍超限时全量摘要压缩）。核心文件：`app/agents/sessions/compaction.py`、`session.py` |
| 2 | Reactive Compact | ✅ | API 返回上下文溢出错误时，捕获 `ContextOverflowError` 异常并触发紧急压缩后重试，无需字符串匹配 |
| 3 | 压缩摘要提示词增强 | ✅ | compaction summary prompt 加入结构化指引，保留关键文件路径和决策上下文 |
| 4 | 上下文大小动态计算 | ✅ | 上下文窗口大小从固定配置改为根据模型 `max_context_tokens` 动态计算，不同模型自动适配不同压缩阈值 |
| 5 | 压缩保留工具条数调优 | ✅ | `compaction_keep_last_n` 默认值从20调整为10，参考 Claude Code 的 keepRecent:5 取折中值。`compaction_prune_keep_recent=8` 确保最近8轮工具结果不被 MicroCompact 清理 |

**1. 两阶段压缩策略**

**Before：** 仅有单一压缩机制，在上下文完全溢出时才触发全量摘要压缩。压缩时机过晚，Agent 在接近溢出时仍在积累无效 token（如重复的文件读取结果），压缩本身也因输入过长而耗时增加。

**After：** 引入两阶段压缩——第一阶段 MicroCompact 在每轮 Agent 循环开始时执行，通过 `prune()` 方法按 token 阈值从最旧的工具结果开始清理，直到当前 token 用量降至 `context_limit - max_output_tokens - compaction_reserved` 以下。`prune()` 接受 `target_tokens` 参数，仅在 `target_tokens > 0` 时执行清理，否则直接返回（避免无谓开销）。第二阶段 Autocompact 在 MicroCompact 仍无法控制 token 量时触发，调用 LLM 对整段对话历史生成结构化摘要，替换原始消息。摘要包含：主要请求和意图、关键技术概念、文件和代码片段、错误和修复、当前工作、可选下一步。

**2. Reactive Compact**

**Before：** 当 API 返回上下文溢出错误时，Agent 直接报错终止，用户需要手动重启会话。没有自动重试机制。

**After：** `handle_reactive_compact()` 捕获 `ContextOverflowError` 异常，立即触发紧急压缩。压缩后渐进式收紧 `keep_last_n` 参数（每次减半，最小值为 2），然后重试 API 调用。最多重试 `compaction_reactive_max_attempts`（默认 3）次。相比字符串匹配错误消息的方式，异常驱动更可靠且不依赖特定错误格式。

**3. 压缩摘要提示词增强**

**Before：** 压缩摘要使用简单的 LLM 指令（如"请总结以下对话"），LLM 在压缩时倾向于丢失具体的文件路径、代码片段和决策上下文，导致压缩后 Agent 因信息缺失而重新读取已读过的文件。

**After：** 重构 `compaction_prompt.py`，使用结构化模板引导 LLM 在压缩时保留关键信息。模板分为六个区块：Primary Request and Intent、Key Technical Concepts、Files and Code Sections（强调保留完整代码片段）、Errors and fixes、Current Work、Optional Next Step。结构化模板显著减少了压缩后的信息丢失，降低了 Agent 因上下文缺失而重新读取文件的概率。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | Token-based 清理替代 count-based | P1 | 将 `compaction_prune_keep_recent` 从固定条数改为 token 阈值，根据实际内容大小动态调整保留数量 |
| 2 | 时间维度过期清理 | P2 | 当工具结果距今超过阈值时自动标记为可清理，防止陈旧信息累积 |

---

### 1.4 命令行优化

#### 概述

将单体 `shell_exec.py` 模块化为 bash / powershell / runner / runtime / common / output 六个子模块，新增 `ExecRuntime` 跨平台 shell 探测。构建多层级权限安全防线（硬拒绝 -> 配置 deny -> allow -> 模式默认）和跨平台进程沙箱（macOS Seatbelt / Linux bubblewrap / Windows 透传）。

涉及提交：545d481、db15454、30ed4d5

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | 多层级权限安全防线 | ✅ | 硬拒绝层（`_HARD_SHELL_DENY` 拦截 `rm -rf /` 等）-> 配置 deny -> allow -> 模式默认。核心文件：`app/agents/permissions/` |
| 2 | 跨平台进程沙箱 | ✅ | macOS Seatbelt（生成 `.sb` profile，默认拒绝写）、Linux bubblewrap、Windows 透传。核心文件：`app/agents/sandbox/` |
| 3 | 命令包装剥离 | ✅ | `CommandWrapperStripper` 剥离 timeout / env / nohup 等前缀，使权限规则正确匹配 |
| 4 | 模块化 Shell 执行器 | ✅ | `shell_exec.py`（245 行单体）拆分为 bash / powershell / runner / runtime / common / output，新增 `ExecRuntime` 跨平台 shell 探测。核心文件：`app/agents/tools/exec/` |
| 5 | 智能 cd 剥离 | ✅ | `peel_cd_prefix` 识别 `cd "path" && cmd` 模式，workspace 内剥掉 cd 前缀减少误拦 |

**1. 多层级权限安全防线**

采用四级纵深防御架构，从硬编码黑名单到配置化策略逐层过滤。硬拒绝层通过 `_HARD_SHELL_DENY` 正则列表拦截 `rm -rf /`、`mkfs` 等高危命令，该层不可被配置覆盖。配置层从 `permissions.json` 加载用户自定义的 deny/allow 规则列表。模式层根据当前 Agent 模式（plan/auto/default）应用不同的默认权限集。每层独立判定，任一层拦截即终止执行。

**2. 跨平台进程沙箱**

macOS 使用 Seatbelt 框架生成 `.sb` 安全 profile，默认拒绝所有写操作，仅允许 workspace 目录和系统临时目录写入。Linux 使用 bubblewrap（bwrap）构建轻量级沙箱，通过 `--ro-bind` 挂载只读目录、`--bind` 挂载可写目录、`--unshare-net` 隔离网络。Windows 当前采用透传模式，依赖权限层控制。`SandboxAdapter` 统一抽象三平台差异，对上层提供一致的 `sandbox_exec()` 接口。

**3. 命令包装剥离**

Agent 在执行用户命令时，LLM 经常会在命令外包装 `timeout`、`env`、`nohup`、`nice` 等前缀。`CommandWrapperStripper` 通过词法分析逐 token 剥离这些包装，使底层权限匹配规则能正确识别实际执行的命令。例如 `timeout 30 env VAR=x rm -rf /tmp` 会被剥离为 `rm -rf /tmp`，从而被硬拒绝层拦截。

**4. 模块化 Shell 执行器**

原 `shell_exec.py`（245 行单体）拆分为六个职责单一的子模块：`bash.py`（Bash 命令构建）、`powershell.py`（PowerShell 命令构建）、`runner.py`（进程启动与 I/O 管理）、`runtime.py`（跨平台 shell 探测）、`common.py`（共享工具函数）、`output.py`（输出截断与格式化）。新增 `ExecRuntime` 类自动探测系统可用 shell（bash/zsh/powershell/cmd），根据平台选择最优执行路径。

**5. 智能 cd 剥离**

`peel_cd_prefix` 函数识别 `cd "path" && command` 模式，在 workspace 内部执行时自动剥离 `cd` 前缀，直接在目标目录执行命令。这减少了权限层对 `cd` 命令的误拦截，同时避免了 `cd` 失败导致后续命令不执行的问题。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | Windows 沙箱实现 | P2 | 基于 Windows Job Object 或 AppContainer 实现进程隔离，替代当前透传模式 |
| 2 | 权限规则热更新 | P3 | 支持运行时修改 `permissions.json` 而无需重启 Agent |

---

### 1.5 文件操作优化

#### 概述

聚焦 Agent 运行时的核心性能瓶颈：上下文压缩清除历史工具内容后，Agent 在后续轮次中因缺乏文件内容而反复重新读取同一文件，导致大量无效 token 消耗和响应延迟。通过 FileContentCache 读文件缓存、edit_file 分层模糊匹配、写前硬闸等机制解决。

涉及提交：2c137f3、7a1ca0b、22e0c00、3f2cda1、f8f184b、5891698、0052a7c、afe17e9、90635ea、f33be01、0ccd70c

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | FileContentCache 读文件缓存 | ✅ | LRU 缓存（max_size=50, ttl=300s）缓存 `read_file` 结果。上下文压缩清除消息后，Agent 重新读取同一文件时命中缓存直接返回，避免重复磁盘 IO 和 token 消耗。输出标记 `cache_hit` |
| 2 | 工具结果截断策略 | ✅ | 按工具粒度配置 max_lines / max_bytes / direction，压缩时保留恢复指引占位符。避免单次工具调用结果过长撑爆上下文 |
| 3 | 截断落盘移出工作区 | ✅ | 截断内容写入系统临时目录而非 workspace，避免污染代码索引 |
| 4 | edit_file 分层模糊匹配 | ✅ | 精确优先 -> 引号归一 / 行 trim / 缩进弹性模糊回退，`replace_all` 仅允许精确匹配，返回 `match_strategy` 元数据。提高首次编辑成功率，减少重试 |
| 5 | 文本归一化匹配 | ✅ | `normalize_for_match` 归一化弯引号 / 破折号 / 省略号，`preserve_quote_style` 保持文件引号风格。配合 edit_file 提高模糊匹配命中率 |
| 6 | 写前硬闸 | ✅ | `FileStateManager` 检查未读全文件、磁盘已变更等条件；片段读（offset/limit）后允许编辑。避免盲目写入导致的数据丢失 |

**1. FileContentCache 读文件缓存**

**Before：** 上下文压缩（MicroCompact / Autocompact）裁剪旧工具结果后，Agent 在后续轮次中因上下文中缺少文件内容而重新调用 `read_file`。同一文件在一次会话中可能被读取 30+ 次（历史数据：33 次文件读取仅覆盖 6 个唯一文件），每次读取消耗磁盘 IO 和 token。

**After：** 实现 `FileContentCache` 类（LRU 缓存，max_size=50, ttl=300s），缓存 `read_file` 的完整读取结果。缓存命中时直接返回已缓存内容，避免重复磁盘 IO 和 token 消耗。缓存 key 为文件绝对路径，隐式隔离不同工作空间。仅缓存完整读取（无 offset/limit 或 limit >= DEFAULT_READ_LIMIT），片段读不缓存。输出标记 `cache_hit` 便于统计命中率。

**2. 工具结果截断策略**

**Before：** 工具结果无截断机制，`read_file` 返回完整文件内容（可能数千行），`grep_search` 返回所有匹配结果（大代码库中可能数百行），`bash` 输出可能包含大量调试信息。单次工具结果过长直接撑爆上下文，触发紧急压缩。

**After：** 按工具粒度配置截断参数：`read_file` 默认 max_lines=500、max_bytes=50000、direction=head（保留头部）；`grep_search` 默认 max_lines=200、direction=head；`bash` 默认 max_lines=300、direction=tail（保留尾部，方便查看错误输出）。截断时在末尾插入恢复指引占位符 `[Truncated: use offset/limit to read more]`，提示 Agent 可通过分段读取获取完整内容。

**3. 截断落盘移出工作区**

**Before：** 截断的工具结果写入 workspace 目录下的 `.moma/truncated/`，导致截断文件被 git 追踪、被代码索引工具（如 ripgrep、LSP）扫描，污染代码搜索结果。

**After：** 截断内容写入系统临时目录（`tempfile.gettempdir()`），彻底避免污染代码索引。

**4. edit_file 分层模糊匹配**

**Before：** `edit_file` 仅支持精确字符串匹配。当 LLM 生成的 `old_string` 与文件内容存在微小差异（如弯引号 vs 直引号、tab vs space 缩进、行首尾空格）时，匹配失败，Agent 需要重新读取文件并重试编辑，浪费一轮 LLM 调用。SWE-bench Lite 评测中，初始版本有多个实例因 edit_file 匹配失败导致空补丁（354 chars）。

**After：** 实现三级匹配策略——第一级精确匹配（原始字符串直接匹配）；第二级归一化匹配（引号归一 `"'` -> `"`、行首尾 trim、缩进弹性 `tab` <-> `space`）；第三级模糊回退（允许少量字符差异）。`replace_all` 模式仅允许精确匹配，避免误替换。每次匹配返回 `match_strategy` 元数据（`exact` / `normalized` / `fuzzy`），便于统计匹配质量。

**5. 文本归一化匹配**

**Before：** LLM 在生成代码时经常使用弯引号（`""''`）、各种破折号（`—–`）和省略号（`…`），而源代码文件通常使用直引号和 ASCII 连字符。这些 Unicode 字符差异导致 edit_file 精确匹配失败。

**After：** `normalize_for_match` 函数将弯引号归一化为直引号、将各种破折号归一化为 ASCII 连字符、将省略号归一化为三个句点。`preserve_quote_style` 在输出时保持文件原始引号风格，避免引入不必要的格式变更。

**6. 写前硬闸**

**Before：** Agent 在未读取文件的情况下直接调用 `write_file` 或 `edit_file`，可能导致覆盖文件内容；或者在读取文件后文件被外部修改（如另一个进程写入），Agent 基于过期内容进行编辑，导致数据丢失。

**After：** `FileStateManager` 在执行前进行安全检查——(1) 如果 Agent 从未读取过该文件（无 `read_file` 记录），阻止写入并提示先读取；(2) 如果文件在读取后被外部修改（磁盘 mtime 变化），阻止写入并提示重新读取；(3) 片段读（offset/limit）后允许编辑，因为 Agent 可能只需要修改特定区域。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | 缓存预热与预取 | P2 | 根据当前任务模式预测可能读取的文件，提前加载到 FileContentCache |
| 2 | 缓存命中率监控 | P2 | 统计 FileContentCache 命中率，识别热文件和冷文件，动态调整 max_size |

---

### 1.6 流式工具调度

#### 概述

实现 LLM 流式响应中的工具即时调度——当流式解析到完整的 tool_call（参数闭合）后立即提交执行，不等待同一轮所有 tool_call 解析完毕。通过 `ToolScheduleSession` 调度器实现分组串行 + 组内并行的工具执行策略，最大化工具并行度，减少等待时间。

涉及提交：相关提交见 react.py、scheduler.py

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | 流式即时调度 | ✅ | `StreamToolCallReady` 事件触发 `tool_scheduler.submit()`，参数闭合即执行，不等全部 tool_call 解析完毕 |
| 2 | 分组串行 + 组内并行 | ✅ | `ToolScheduleSession` 通过 `partition_parallel_groups()` 将工具分为串行组，组内使用 `asyncio.gather` 并行执行 |
| 3 | 工具结果回调 | ✅ | `ToolRunNotifier` 提供 `on_tool_run_result` 回调，工具完成后立即通知 UI 刷新状态 |
| 4 | 取消与异常处理 | ✅ | Agent 中止时 `discard_tasks()` 取消在途任务；单条工具异常不影响其他工具执行 |

**1. 流式即时调度**

**Before：** LLM 流式返回多个 tool_call 时，需要等待所有 tool_call 解析完毕后才统一执行。当 LLM 返回 3-5 个工具调用时，最后一个工具的参数可能需要数秒才能解析完成，前几个已解析完毕的工具只能空等。

**After：** `ToolScheduleSession` 在 `_think_with_act_impl` 中被创建（react.py:516-518）。当 event_stream 产出 `StreamToolCallReady` 事件时（react.py:553-568），立即将完整的 tool_call 提交到调度器执行。调度器通过 `_advance()` 方法检查前一组是否完成，若完成则启动当前组内尚未执行的工具。这使得第一个解析完毕的工具在最后一个工具还在解析时就已经开始执行。

**2. 分组串行 + 组内并行**

`ToolsFactory.partition_parallel_groups()` 根据工具的并发安全性（`is_parallel()`）将工具分为串行组和并行组。例如：`read_file` 和 `grep_search` 可以并行（只读操作），但 `edit_file` 必须与同文件的其他写操作串行。组间严格串行（前一组全部完成后才启动下一组），组内使用 `asyncio.Semaphore` + `asyncio.gather` 并行执行。

**3. 工具结果回调**

`ToolRunNotifier` 提供两个回调：`on_tool_run_started`（工具开始执行时触发）和 `on_tool_run_result`（工具完成时触发）。react.py 中通过 `on_tool_run_result` 回调将工具结果写入 `results_by_id` 字典，并调用 `notify_user()` 通知 UI 刷新工具状态（从"执行中 ▶"变为"完成 ✓/失败 ✗"）。

**4. 取消与异常处理**

Agent 中止时调用 `discard_tasks()` 取消所有在途任务，不触发 notifier（避免中止后的无效回调）。单条工具执行异常时，`_run_item()` 捕获异常并生成 `ToolErrorResult`，不影响其他工具的继续执行。`wait_complete()` 在流结束后轮询等待所有工具完成，确保结果完整收集。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | 工具执行超时 | P2 | 为单条工具设置执行超时，防止工具卡死阻塞整个调度流程 |
| 2 | 工具依赖声明 | P3 | 支持工具间显式依赖声明（如 B 依赖 A 的输出），实现 DAG 式调度 |

---

### 1.7 模型缓存命中

#### 概述

通过 Anthropic Prompt Caching 和 OpenAI 自动缓存机制，减少重复输入 token 的处理成本。核心思路：将 System Prompt 拆分为静态/动态两部分，静态部分加 `cache_control` 断点实现跨轮复用；消息和工具层面也添加断点最大化缓存命中率。

涉及提交：1d47e8f、2783927、2c137f3、f1cafb5、ddcdd59、a0e90e2

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | System Prompt 静态/动态拆分 | ✅ | `build_system_prompt` 拆分为静态（agent prompt、项目规则、常驻技能）与动态（memory、task_prompt_section）两部分。Anthropic 发两个 `system` block，静态加 `cache_control` 断点；OpenAI 拼接为单一字符串 |
| 2 | System Prompt 跨轮缓存 | ✅ | `ReActAgent` 将静态 prompt 缓存在实例属性，每轮调用 `build_system_prompt(sys_prompt_cache=...)` 跳过重建，Agent 实例生命周期内有效 |
| 3 | System Prompt 缓存断点 | ✅ | `AnthropicModels._build_system_param()` 将静态 block 加 `cache_control: {"type": "ephemeral"}`（Anthropic 默认 5 分钟 TTL），动态 block 不加 |
| 4 | 消息级缓存断点 | ✅ | `AnthropicModels._add_message_cache_breakpoints()` 在最后一条 user 和 assistant 消息上添加 `cache_control` 断点 |
| 5 | 工具级缓存断点 | ✅ | `ask_tools` / `ask_tools_stream` 中，tools 数组最后一个工具添加 `cache_control` 断点 |
| 6 | cache TTL 配置 | ✅ | 移除 `ttl: "1h"` 配置和 `_cache_ttl` 属性。`_cache_control()` 统一返回 `{"type": "ephemeral"}`（Anthropic 默认 5 分钟 TTL）。原因：1h TTL 需 beta header，商业项目不宜依赖 beta 特性 |
| 7 | OpenAI 自动缓存日志 | ✅ | 基类 `LLM._log_cache_usage()` 在每次 API 调用后记录 cache_read / cache_write 和命中率。OpenAI 自动缓存（>1024 tokens）无需显式断点 |
| 8 | Anthropic 缓存命中日志 | ✅ | 同上，复用基类方法。cache_read 来自 `cache_read_input_tokens`，cache_write 来自 `cache_creation_input_tokens` |
| 9 | 累计缓存统计 | ✅ | `LLM.log_cache_summary()` 输出会话级汇总：总调用次数、累计 cache_read/cache_write 及占比、估算节省 token 数 |
| 10 | 模型工厂 api_type 路由 | ✅ | `chat_models.json` 每个 Provider 配置 `api_type: "openai" | "anthropic"`。`LLMFactory._get_model_class()` 按 api_type 动态路由模型类 |
| 11 | 命名规范化 | ✅ | `ClaudeModels` -> `AnthropicModels`，`claude_llm.py` -> `anthropic_llm.py`，对齐厂商命名规范 |

**1. System Prompt 静态/动态拆分**

`build_system_prompt` 将 System Prompt 拆分为两部分：静态部分包含 agent prompt、项目规则（`rules.md`）、常驻技能描述，这些内容在整个 Agent 实例生命周期内不变；动态部分包含 memory 摘要、当前任务提示（`task_prompt_section`），每轮可能变化。Anthropic API 发送两个 `system` block，静态 block 加 `cache_control` 断点实现跨轮复用；OpenAI API 拼接为单一字符串。

**2. System Prompt 跨轮缓存**

`ReActAgent` 将静态 prompt 缓存在实例属性 `_sys_prompt_cache` 中，每轮调用 `build_system_prompt(sys_prompt_cache=...)` 时跳过静态部分的重建。缓存在 Agent 实例生命周期内有效，避免每轮重复拼接大量静态文本。

**3. 消息级缓存断点**

`AnthropicModels._add_message_cache_breakpoints()` 在最后一条 user 消息和最后一条 assistant 消息上添加 `cache_control: {"type": "ephemeral"}` 断点。这使得 Anthropic 服务端可以缓存断点之前的所有消息内容，后续轮次仅需处理新增消息。

**4. 工具级缓存断点**

在 `ask_tools` / `ask_tools_stream` 中，tools 数组的最后一个工具添加 `cache_control` 断点。这使得工具定义（通常包含大量 JSON Schema）可以被缓存，避免每轮重复传输。

**5. 缓存 TTL 配置**

Anthropic Prompt Caching 支持两种 TTL：`ephemeral`（默认 5 分钟）和 `1h`（需 beta header）。出于商业项目稳定性考虑，移除了 `1h` TTL 配置，统一使用 `ephemeral`。5 分钟 TTL 对于大多数对话场景足够，且不需要依赖 beta 特性。

**6. 累计缓存统计**

`LLM.log_cache_summary()` 在会话结束时输出汇总信息：总 API 调用次数、累计 cache_read_input_tokens / cache_creation_input_tokens 及占比、估算节省的 token 数（基于 cache_read 便宜 90% 的定价）。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 12 | 缓存失效检测与原因分析 | P1 | 参考 Claude Code 的 `promptCacheBreakDetection.ts`。两阶段检测：调用前记录 system/tools/model 的 hash，调用后对比 cache_read 下降幅度。检测维度：system prompt 变化、tool schema 变化、model 切换、TTL 过期、服务端驱逐。失效时输出 WARNING 并附带原因 |
| 13 | 缓存命中率趋势监控 | P2 | 滑动窗口统计（最近 N 次调用），检测命中率是否持续下降。可选接入 Grafana/Prometheus 指标导出 |
| 14 | 缓存成本估算 | P2 | 基于 Anthropic/OpenAI 的缓存价格差（cache_read 便宜 90%），计算每次调用和会话累计节省的费用。在 `log_cache_summary` 中输出金额 |
| 15 | cache_control scope 支持 | P3 | Anthropic 支持 `scope: "global" | "organization"` 控制缓存共享范围。当前固定为 ephemeral（会话级），后续可配置为全局共享以跨用户命中 |
| 16 | 缓存断点动态调整 | P3 | 根据实际命中率反馈，动态调整断点位置。例如：如果 system prompt 缓存命中率低，考虑合并静态/动态 block |
| 17 | Compaction 后缓存基线重置 | P4 | 对话 compact 后消息数量减少导致 cache_read 自然下降。需通知检测模块重置基线，避免误报为缓存失效 |

---

### 1.8 Agent 步数控制优化

#### 概述

解决 Agent 在未配置步数上限时被默认值强制截断的问题。原实现中 `max_steps` 为必填整数，Agent 在达到上限后强制终止，导致复杂任务（如多文件重构、长链路调试）在未完成时被截断。修改后 `max_steps` 支持 `None` 表示无上限，未配置时 Agent 可持续执行直到任务完成或用户手动终止。

涉及提交：afe17e9

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | max_steps 可空 | ✅ | `max_steps` 支持设为 None 表示无上限，`_parse_max_steps` 解析 modes 配置（空值/null -> 不限制），`_reached_max_steps` 未配置时恒为 False。核心文件：`app/agents/core/base.py` |

**Before：Agent 被默认步数截断**

原实现中 `_max_steps` 为 `int` 类型（非 Optional），在 modes 配置中未设置 `max_steps` 时会使用一个默认上限值。`_reached_max_steps()` 比较 `self._current_step >= self._max_steps`，一旦达到上限就强制终止 Agent 循环。这导致以下问题：

- 复杂任务（如跨 5+ 文件的重构、需要多轮调试的 bug 修复）在未完成时被截断，Agent 无法产出最终结果
- 用户无法通过配置控制是否启用步数限制，必须显式设置一个足够大的数字
- 部分 modes 配置中 `max_steps` 为空字符串或 `null`，解析时抛出异常或被忽略，行为不一致

**After：max_steps 支持可空，未配置时无上限**

修改后的实现：

1. `_max_steps` 类型从 `int` 改为 `Optional[int]`，支持 `None` 值表示无上限
2. `_parse_max_steps(raw)` 方法统一处理各种输入：`None` -> `None`、空字符串 `""` -> `None`、`"null"` / `"None"` 字符串 -> `None`、正整数字符串 -> 对应整数、负数或零 -> `None`
3. `_reached_max_steps()` 方法在 `self._max_steps is None or self._max_steps <= 0` 时恒返回 `False`，Agent 持续执行
4. 用户仍可通过配置 `run_limit.max_steps` 设置明确的步数上限（如 `"max_steps": 50`）

**效果：** Agent 在未显式配置步数上限时可持续执行，直到任务完成或用户手动终止。复杂任务不再被默认截断。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | 动态步数限制 | P3 | 根据任务复杂度和已用 token 动态调整 max_steps，防止单次会话无限运行 |

---

### 1.9 SubAgent 能力优化

#### 概述

支持 explore / plan / general-purpose / verification 四种 SubAgent 类型的多类型编排。`SubAgentTaskRegistry` 提供 sync/async 任务管理，async 子任务结果自动 drain 到主 History。验证 Agent 强制输出 `VERDICT: PASS / FAIL / PARTIAL`。集成 LSP 代码智能服务支持五种语言服务器。

涉及提交：039fcd6、d0ee3ba、afe17e9

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | 多类型 SubAgent 编排 | ✅ | 支持 explore / plan / general-purpose / verification 四种子类型，`type` 参数指定。核心文件：`app/agents/core/subagent.py` |
| 2 | 异步任务管理 | ✅ | `SubAgentTaskRegistry`（`app/agents/core/subagent_task.py`，新增）支持 sync/async 模式，async 子任务结果自动 drain 到主 History |
| 3 | 验证 Agent | ✅ | `VerificationGate` 为只读验证场景提供专门 Agent 类型，强制输出 `VERDICT: PASS / FAIL / PARTIAL` |
| 4 | spawn 工具增强 | ✅ | 新增 `thoroughness` 参数（explore 专用 quick / medium / very thorough），`mode=async` 支持同轮并发 |
| 5 | LSP 代码智能服务 | ✅ | `CodeLSPService` 管理 pyright / typescript / gopls / java / clangd 五种语言服务器 |

**1. 多类型 SubAgent 编排**

四种 SubAgent 类型各有专注领域：`explore`（只读探索，适合代码搜索和架构分析）、`plan`（只读规划，适合实现方案设计）、`general-purpose`（全能力，适合需要编辑文件的子任务）、`verification`（只读验证，适合代码审查和测试验证）。通过 `type` 参数在 spawn 时指定，不同类型自动加载对应的工具集和行为约束。

**2. 异步任务管理**

`SubAgentTaskRegistry` 管理 sync/async 两种子任务模式。sync 模式下主 Agent 阻塞等待子任务完成；async 模式下子任务在后台执行，结果通过 `drain_to_history()` 自动写入主 Agent 的 History。支持同轮并发多个 async 子任务，主 Agent 可在子任务执行期间继续处理其他工作。

**3. 验证 Agent**

`VerificationGate` 为代码审查和测试验证场景提供专门的 Agent 类型。强制输出结构化判定：`VERDICT: PASS`（验证通过）、`VERDICT: FAIL`（验证失败，附带失败原因）、`VERDICT: PARTIAL`（部分通过，附带未通过项）。验证 Agent 为只读模式，不允许编辑文件，确保验证过程不引入新变更。

**4. spawn 工具增强**

新增 `thoroughness` 参数（仅 explore 类型生效）：`quick`（快速搜索，限制搜索深度）、`medium`（中等深度，平衡速度和覆盖率）、`very thorough`（深度搜索，最大化覆盖率）。`mode=async` 支持同轮并发多个子任务，主 Agent 可同时启动多个 explore 子任务搜索不同方向。

**5. LSP 代码智能服务**

`CodeLSPService` 管理五种语言服务器的生命周期：pyright（Python）、typescript（TypeScript/JavaScript）、gopls（Go）、java（Java）、clangd（C/C++）。提供符号定位、类型信息、引用查找等能力，作为 grep_search 的补充，用于精确的代码导航。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | SubAgent 结果缓存 | P2 | 对相同输入的 explore 子任务结果进行缓存，避免重复搜索 |
| 2 | 子任务依赖编排 | P3 | 支持子任务间的依赖声明，实现 DAG 式任务编排 |

---

## 2. 其他优化

### 2.1 工具权限控制优化

#### 概述

构建多层级权限安全防线，从硬编码黑名单到配置化策略逐层过滤命令执行请求。支持 JSON 配置文件驱动的 deny/allow 规则，以及基于 Agent 模式的默认权限集。

涉及提交：545d481、db15454、30ed4d5

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | 硬拒绝层 | ✅ | `_HARD_SHELL_DENY` 正则列表拦截 `rm -rf /`、`mkfs` 等高危命令，不可被配置覆盖 |
| 2 | 配置 deny 层 | ✅ | 从 `permissions.json` 加载用户自定义 deny 规则列表 |
| 3 | 配置 allow 层 | ✅ | 从 `permissions.json` 加载用户自定义 allow 规则列表 |
| 4 | 模式默认层 | ✅ | 根据当前 Agent 模式（plan/auto/default）应用不同的默认权限集 |

**1. 硬拒绝层**

`_HARD_SHELL_DENY` 是一个正则表达式列表，包含 `rm -rf /`、`mkfs`、`dd if=/dev/zero` 等高危命令模式。该层在所有其他层之前执行，拦截结果不可被任何配置覆盖。设计原则：即使用户配置了 allow 规则，硬拒绝层仍然生效，防止误配置导致的安全风险。

**2. 配置 deny/allow 层**

`permissions.json` 文件支持 `deny` 和 `allow` 两个规则列表。每条规则是一个正则表达式，匹配命令字符串。处理顺序：硬拒绝 -> 配置 deny -> 配置 allow -> 模式默认。如果命令匹配 deny 规则，直接拒绝；如果匹配 allow 规则，直接放行；否则进入模式默认层。

**3. 模式默认层**

不同 Agent 模式有不同的默认权限集：`plan` 模式最严格，仅允许只读命令（`ls`、`cat`、`grep` 等）；`auto` 模式中等，允许常见开发命令但拦截危险操作；`default` 模式最宽松，仅被硬拒绝和配置 deny 层拦截。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | 权限规则审计日志 | P2 | 记录所有权限判定结果（允许/拒绝），便于事后审计和规则调优 |
| 2 | 规则冲突检测 | P3 | 检测 deny/allow 规则间的冲突（如同一命令同时匹配 deny 和 allow） |

---

### 2.2 沙箱支持

#### 概述

实现跨平台进程沙箱，macOS 使用 Seatbelt、Linux 使用 bubblewrap、Windows 当前透传。`SandboxAdapter` 统一抽象三平台差异，对上层提供一致的 `sandbox_exec()` 接口。

涉及提交：545d481、db15454

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | macOS Seatbelt 沙箱 | ✅ | 生成 `.sb` profile，默认拒绝所有写操作，仅允许 workspace 和系统临时目录写入 |
| 2 | Linux bubblewrap 沙箱 | ✅ | 使用 bwrap 构建轻量级沙箱，`--ro-bind` 只读挂载、`--bind` 可写挂载、`--unshare-net` 网络隔离 |
| 3 | Windows 透传模式 | ✅ | 当前依赖权限层控制，未实现进程级隔离 |
| 4 | SandboxAdapter 统一接口 | ✅ | 抽象三平台差异，提供一致的 `sandbox_exec()` 接口 |

**1. macOS Seatbelt 沙箱**

Seatbelt 是 macOS 内置的强制访问控制框架。`SeatbeltSandbox` 类动态生成 `.sb` 安全 profile，默认拒绝所有文件系统写操作，然后逐条添加允许规则：workspace 目录可写、系统临时目录可写、`/dev/null` 可写。Profile 通过 `sandbox-exec` 命令应用到子进程。

**2. Linux bubblewrap 沙箱**

bubblewrap（bwrap）是 Linux 上的轻量级沙箱工具。`BubblewrapSandbox` 类构建 bwrap 命令行：`--ro-bind / /`（只读挂载根目录）、`--bind workspace /workspace`（可写挂载 workspace）、`--unshare-net`（隔离网络）、`--die-with-parent`（父进程退出时自动终止）。沙箱内进程无法访问 workspace 外的文件系统。

**3. SandboxAdapter 统一接口**

`SandboxAdapter` 类根据 `platform.system()` 自动选择对应的沙箱实现。对上层提供统一的 `sandbox_exec(command, workspace, timeout)` 接口，隐藏平台差异。Windows 当前返回透传模式（直接执行命令），未来可替换为 Windows Job Object 实现。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | Windows 沙箱实现 | P2 | 基于 Windows Job Object 或 AppContainer 实现进程隔离 |
| 2 | 沙箱资源限制 | P3 | 添加 CPU 时间、内存、进程数等资源限制 |

---

### 2.3 其它工具增强

#### 概述

新增 Cron 定时调度、Browser 浏览器工具、web_fetch LLM 抽取等多项工具能力。对现有工具进行增强：grep_search ripgrep 分页搜索、路径解析与安全防护等。

涉及提交：f8f184b、5891698、0052a7c、afe17e9、90635ea、f33be01、0ccd70c

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | Cron 定时调度 | ✅ | 支持 add / list / remove / update / run_now，调度类型含 `every_seconds` / `cron_expr`（带时区）/ `at`（一次性）。核心文件：`app/agents/tools/cron/cron.py` |
| 2 | Browser 浏览器工具 | ✅ | 基于 Playwright，支持 navigate / snapshot / click / type / wait / screenshot / close。核心文件：`app/agents/tools/web/browser.py` |
| 3 | web_fetch LLM 抽取 | ✅ | `WebFetchLlmExtractor` 用会话模型按 prompt 从网页内容抽取信息 |
| 4 | 结构化 ask_question | ✅ | 从简单字符串列表改为含 question + options + header + multiSelect 的结构 |
| 5 | ripgrep 分页搜索 | ✅ | grep_search 引入 ripgrep 子进程后端，支持 offset / head_limit / multiline / context |
| 6 | 路径解析与安全防护 | ✅ | `ToolPathResolver` 将工具路径解析为绝对路径（相对 workspace 而非进程 CWD）；`WindowsReservedNameGuard` 拦截 CON / PRN / AUX / NUL / COM1-9 / LPT1-9 保留设备名 |

**1. Cron 定时调度**

`CronManager` 支持四种操作：`add`（添加定时任务）、`list`（列出所有任务）、`remove`（移除任务）、`update`（更新任务）、`run_now`（立即执行）。调度类型：`every_seconds`（固定间隔）、`cron_expr`（标准 cron 表达式，支持时区）、`at`（一次性定时执行）。任务持久化到 `cron_tasks.json`，Agent 重启后自动恢复。

**2. Browser 浏览器工具**

基于 Playwright 实现浏览器自动化，支持六种操作：`navigate`（导航到 URL）、`snapshot`（获取页面快照，返回 DOM 结构和可交互元素列表）、`click`（点击元素）、`type`（输入文本）、`wait`（等待元素出现或页面加载完成）、`screenshot`（截取页面截图）、`close`（关闭浏览器）。浏览器实例在 Agent 会话内复用，避免重复启动。

**3. web_fetch LLM 抽取**

`WebFetchLlmExtractor` 在获取网页内容后，使用会话模型按用户指定的 prompt 从 HTML 中抽取结构化信息。适用于从文档页面提取 API 规范、从博客提取技术要点等场景。

**4. 结构化 ask_question**

`ask_question` 工具从简单的字符串列表改为结构化格式：`question`（问题文本）、`options`（选项列表，每项含 label 和 description）、`header`（选项分类标题）、`multiSelect`（是否允许多选）。支持 `preview` 字段展示选项的预览内容（代码片段、ASCII 图等）。

**5. ripgrep 分页搜索**

`grep_search` 引入 ripgrep 子进程后端，替代原有的 Python 正则搜索。支持 `offset`（分页偏移）、`head_limit`（结果数量限制）、`multiline`（跨行匹配）、`context`（上下文行数）等参数。ripgrep 的搜索速度比 Python 正则快 10-100 倍，尤其在大代码库中效果显著。

**6. 路径解析与安全防护**

`ToolPathResolver` 将工具传入的相对路径解析为绝对路径，基准目录为 workspace 而非进程 CWD，避免因 Agent 切换目录导致路径解析错误。`WindowsReservedNameGuard` 拦截 Windows 保留设备名（CON、PRN、AUX、NUL、COM1-9、LPT1-9），防止 Agent 尝试创建这些特殊文件导致意外行为。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | Browser 工具增强 | P2 | 支持文件上传、下拉选择、iframe 操作等高级交互 |
| 2 | Cron 任务持久化增强 | P3 | 支持任务执行历史记录和失败重试策略 |

---

### 2.4 模型异常降级

#### 概述

将脆弱的字符串匹配错误检测重构为类型化异常体系。新增 `LLMError` 基类及 `ContextOverflowError`、`InvalidResponseError` 子类，LLM 和 CV 模型的错误从返回错误流改为抛出异常，上层通过 try/except 精确捕获并驱动 Reactive Compact。同时实现额度耗尽自动 fallback 机制。

涉及提交：911a2c6、84a482b

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | 类型化异常体系 | ✅ | 新增 `LLMError`（基类）-> `ContextOverflowError`、`InvalidResponseError`，替代脆弱的字符串匹配错误检测。核心文件：`anthropic_llm.py`、`base.py` |
| 2 | 统一异常抛出 | ✅ | LLM 和 CV 模型的错误从返回错误流改为抛出异常，上层可通过 try/except 精确捕获 |
| 3 | 异常驱动 Reactive Compact | ✅ | `ContextOverflowError` 触发自动压缩重试，无需字符串匹配错误消息 |
| 4 | 额度耗尽 fallback | ✅ | `call_with_llm_fallback` 遍历 primary/fallback 模型对，429 等额度耗尽异常先经 `_is_retryable_error` 同模型重试 3 次（指数退避），耗尽后抛出 `RuntimeError`，被 `call_with_llm_fallback` 捕获并切换备用模型。核心文件：`utils.py`、`chat_models/base.py` |

**1. 类型化异常体系**

原实现通过字符串匹配 API 错误消息（如 `"context_length_exceeded"`）来检测错误类型，脆弱且易受 API 版本变化影响。新增异常体系：`LLMError`（基类）-> `ContextOverflowError`（上下文溢出）、`InvalidResponseError`（无效响应）。异常由 LLM 适配层在解析 API 响应时抛出，包含结构化的错误信息（错误码、错误类型、原始消息）。

**2. 异常驱动 Reactive Compact**

`ContextOverflowError` 在 Agent 主循环中被捕获，触发 `handle_reactive_compact()` 执行紧急压缩。压缩后渐进式收紧 `keep_last_n` 参数（每次减半，最小值为 2），然后重试 API 调用。相比字符串匹配方式，异常驱动更可靠且不依赖特定错误格式。

**3. 额度耗尽 fallback**

`call_with_llm_fallback` 函数遍历 primary/fallback 模型对列表。当 API 返回 429（Rate Limit）等额度耗尽异常时，`_is_retryable_error` 判断是否可重试，可重试则同模型重试 3 次（指数退避：1s、2s、4s）。重试耗尽后抛出 `RuntimeError`，被外层捕获并切换到下一个 fallback 模型。支持多级 fallback（如 primary -> fallback_1 -> fallback_2）。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | 异常分类细化 | P2 | 新增 `RateLimitError`、`AuthenticationError`、`ModelNotFoundError` 等子类，支持更精确的错误处理 |
| 2 | Fallback 策略配置化 | P2 | 支持通过配置文件定义 fallback 模型链和重试策略 |

---

### 2.5 三层记忆机制

#### 概述

构建用户级、Workspace 级、Agent 级三层记忆架构，分别存储跨 repo 偏好、repo 特定经验和跨 repo 工作经验。严格准入规则确保用户级记忆仅收录跨会话偏好与事实，与 `rules.md` 团队指令明确区分。

涉及提交：3f2cda1

#### 已完成

| 编号 | 功能 | 状态 | 实现思路 |
|------|------|------|----------|
| 1 | 三层记忆架构 | ✅ | 用户级（跨 repo / workspace / agent）、Workspace 级（repo 特定）、Agent 级（跨 repo 工作经验）。核心文件：`app/agents/memory/`（重构） |
| 2 | 严格准入规则 | ✅ | 用户级记忆仅收录跨会话偏好与事实，避免与 `rules.md` 重复 |
| 3 | 并行化记忆合并 | ✅ | 记忆检索与上下文构建并行执行 |
| 4 | 记忆边界界定 | ✅ | `rules.md` = 团队指令（必须遵守），`MEMORY.md` = 个人沉淀（可参考） |

**1. 三层记忆架构**

- **用户级记忆**（跨 repo / workspace / agent）：存储用户的个人偏好、常用工具配置、编码风格偏好等。例如"用户偏好使用 pytest 而非 unittest"、"用户习惯使用中文注释"。存储在用户 home 目录下，所有 repo 共享。
- **Workspace 级记忆**（repo 特定）：存储当前 repo 的特定经验，如"该项目使用 poetry 管理依赖"、"CI 使用 GitHub Actions"。存储在 workspace 的 `.claude/memory/` 目录下。
- **Agent 级记忆**（跨 repo 工作经验）：存储 Agent 在多个 repo 中积累的工作经验，如"Python 项目通常需要在修改后运行 pytest"、"TypeScript 项目需要先 npm install"。

**2. 严格准入规则**

用户级记忆仅收录跨会话的偏好与事实，有严格的准入检查：排除临时任务状态（"当前正在修复 bug #123"）、排除代码实现细节（这些应由代码本身表达）、排除与 `rules.md` 重复的内容（`rules.md` 是团队指令，优先级更高）。准入规则通过 LLM 分类 + 规则过滤双重保障。

**3. 并行化记忆合并**

记忆检索（从三层记忆中读取相关条目）与上下文构建（组装 System Prompt）并行执行，减少 Agent 启动时的延迟。使用 `asyncio.gather()` 同时执行记忆检索和上下文构建，两个任务完成后合并结果。

**4. 记忆边界界定**

`rules.md` = 团队指令（必须遵守，如编码规范、提交格式、分支策略）。`MEMORY.md` = 个人沉淀（可参考，如个人偏好、历史经验）。两者明确区分，避免混淆。`rules.md` 的内容在 System Prompt 中以高优先级注入，`MEMORY.md` 的内容在记忆检索时作为参考。

#### 下一步

| 编号 | 功能 | 优先级 | 实现思路 |
|------|------|--------|----------|
| 1 | 记忆自动提取 | P2 | 在每次会话结束时自动提取有价值的信息存入记忆，减少手动维护负担 |
| 2 | 记忆冲突检测 | P3 | 检测新记忆与已有记忆间的冲突，提示用户确认 |
