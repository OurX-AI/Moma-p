---
name: memory
description: Two-layer memory system with grep-based recall.
always: true
metadata: {}
---

# Memory

## Structure

Memory files live under the workspace. Use **workspace path** from runtime context as `<WORKSPACE_PATH>` and **agent type** as `<AGENT_TYPE>`:

| 文件 | 用途 | 加载到上下文 |
|------|------|--------------|
| `<WORKSPACE_PATH>/.memory/<AGENT_TYPE>/MEMORY.md` | **Agent 长期记忆**（偏好、业务事实、可复用经验） | 是（Agent Experience） |
| `<WORKSPACE_PATH>/.memory/MEMORY.md` | 工作空间级记忆（自动合并写入） | 是（Long-term Memory） |
| `<WORKSPACE_PATH>/.memory/HISTORY.md` | 事件日志（仅 grep，不注入上下文） | 否 |

## 手动更新（write_file）

需要 Agent **主动记住**的信息（用户偏好、持仓、项目约定等），写入：

```
<WORKSPACE_PATH>/.memory/<AGENT_TYPE>/MEMORY.md
```

**不要**写到 `<WORKSPACE_PATH>/.memory/MEMORY.md`（该文件由系统自动合并维护，Agent 勿手动覆盖）。

## Search Past Events

```bash
grep -i "keyword" <WORKSPACE_PATH>/.memory/HISTORY.md
```

Use the `exec` tool to run grep when available.

## Auto-consolidation

会话过长时，系统会自动合并：

- 工作空间事实 → `<WORKSPACE_PATH>/.memory/MEMORY.md` + `HISTORY.md`
- Agent 经验 → `<WORKSPACE_PATH>/.memory/<AGENT_TYPE>/MEMORY.md`

无需手动管理自动合并；只需在需要时更新 Agent 的 `MEMORY.md`。
