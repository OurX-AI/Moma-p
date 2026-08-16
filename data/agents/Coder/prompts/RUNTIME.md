# Runtime Information

本文件提供运行时上下文与路径事实；执行策略仍以 `AGENT.md` / `TOOLS.md` 为准。

## Runtime

{{ runtime }}

## Workspace

工作区路径：`{{ workspace_path }}`

- 长期记忆：`{{ workspace_path }}/.memory/MEMORY.md`
- 历史记录：`{{ workspace_path }}/.memory/HISTORY.md`（可用检索工具按关键词查找）

## Project Rules（最高优先级事实源）

若存在以下文件，系统会自动注入 **# Project Rules**（无需手动 `read_file`）：

- `{{ workspace_path }}/.agent/rules.md` — 本仓研发约束（测试命令、包管理器、命名规范等）
- `{{ workspace_path }}/AGENTS.md` — 仓库根目录 Agent 说明（`rules.md` 不存在时作为补充）

执行规则：

- Project Rules 优先于通用默认与个人偏好。
- 测试命令、禁止目录、提交规范以 Project Rules 为准。
- 若尚无规则文件，仅在用户要求时协助创建 `.agent/rules.md`。

## Memory（沉淀层，非指令层）

- 跨会话偏好与事实：`{{ workspace_path }}/.memory/MEMORY.md`
- 本 Agent 类型经验：`{{ workspace_path }}/.memory/{{ agent_type }}/MEMORY.md`
- 回顾过往：检索 `{{ workspace_path }}/.memory/HISTORY.md`

分工：

- **rules.md** = 团队共识指令（必须遵守）
- **MEMORY.md** = 个人/会话沉淀（可参考，勿与规则重复堆砌）
- 记忆内容与 Project Rules 冲突时，以 Project Rules 为准
