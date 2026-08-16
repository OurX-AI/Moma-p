"""记忆管理器：用户 / 工作空间 / Agent 类型 三层记忆

1) 用户记忆：`<runtime_data>/<user_id>/MEMORY.md`（跨 repo/workspace/agent，关于用户本人的稳定偏好）
2) 工作空间记忆：`<runtime_data>/<workspace_index>/MEMORY.md` 与 `HISTORY.md`（同工作区多会话共享）
3) Agent 类型记忆：`<runtime_data>/<agent_type>/MEMORY.md`（该类 Agent 可复用经验）

均使用 MEMORY.md / HISTORY.md 文件存储。
"""
import asyncio
import json
import logging
import hashlib
from pathlib import Path
from typing import Any, List, Optional, Tuple
from pydantic import BaseModel, Field
from app.config.settings import settings
from ..base import BaseMemoryManager
from ...contants import MEMORY_DIR_NAME
from ...schemes import AgentContext
from ...sessions.manager import SESSION_MANAGER
from ...sessions.message import Message
from app.infrastructure.llms.chat_models.factory import llm_factory
from app.utils.common import increase_md_heading_levels



class MemoryExtractPrompt(BaseModel):
    system_prompt: str = Field(..., description="系统提示，说明本类记忆的提取角色与目标")
    user_instruction: str = Field(
        ...,
        description="对本次待处理内容的说明，与「当前长时记忆」「待处理内容」一起拼成 user_question",
    )

    @classmethod
    def for_workspace(cls) -> "MemoryExtractPrompt":
        """工作空间级预设：提炼该工作空间下通用约定/偏好/关键结论，供后续会话复用；同时维护事件日志供检索。"""
        return cls(
            system_prompt="""You are the workspace memory consolidation agent. The workspace has two persistent stores:

1. **MEMORY.md** – Long-term facts, preferences, and key conclusions specific to THIS workspace (repo). Referenced as: "Remember important information in {{ workspace_path }}/.memory/MEMORY.md".
2. **HISTORY.md** – Recent event log only (grep-searchable). Keep only the last 14 days (or last 100 entries if no dates); drop older entries. Referenced as: "Past events are logged in {{ workspace_path }}/.memory/HISTORY.md" and "Recall past events: grep {{ workspace_path }}/.memory/HISTORY.md".

**What belongs in MEMORY.md (high value, stable):**
- **Workspace-specific conventions / preferences**: naming, branching, commit message style, test runner, build commands, lint config choices that the team settled on.
- **Stable key conclusions**: architectural decisions and their rationale, known constraints, external system endpoints/IDs that don't change often.
- **Recurring pitfalls in THIS repo**: gotchas that bit past sessions and are likely to recur (e.g., "tests in tests/slow/ must be run with --slow flag").
- **User's working style for this workspace**: how they like PRs structured, review cadence, what they consider in-scope vs out-of-scope.

**What NOT to store in MEMORY.md (put in HISTORY.md or drop entirely):**
- **Git state / commit investigations**: detached HEAD, specific commit hashes being investigated, who-changed-what. `git log` / `git blame` are authoritative — never duplicate them in memory.
- **Ephemeral debugging state**: temporary branch names, in-progress rebase/cherry-pick, "investigation ongoing" notes. These are events → HISTORY.md, not MEMORY.md.
- **Code structure / file paths / architecture overview**: module layout, "where X is defined", import graphs. These can be derived by reading the code and go stale fast.
- **In-progress / unresolved work**: "investigating X", "to be decided", anything without a conclusion. Only record once a conclusion is reached; otherwise it belongs in HISTORY.md (if at all).
- **Correction notes**: never keep "X -> corrected: Y" form. Write only the correct value.
- **Generic dev knowledge** that isn't specific to this workspace (e.g., "Python uses venv") — not useful here.
- **One-off events with no reuse value** (e.g., "found MEMORY.md empty and recovered from git").

**Full overwrite + prune:** Call the save_memory tool with both:
- **memory_update**: **Full overwrite**. Review "Current Workspace Memory" and "Content to Process". Keep only high-value, stable items; drop anything that falls into the exclusion list above (or that has clearly become outdated). Merge new stable facts. Output the complete, updated MEMORY.md text (not an incremental patch). If nothing meets the bar, output an empty or near-empty memory — do not pad with low-signal notes.
- **history_entry**: **Full overwrite**. From "Current Workspace History", keep only entries from the last 14 days (or the last 100 entries if dates are unclear), drop older ones, then append one new entry at the end (start with [YYYY-MM-DD HH:MM], then 2–5 sentences summarizing key events/decisions/topics for grep search). Output the complete HISTORY.md text (recent entries + new entry only).""",
            user_instruction="Based on the conversation below, review and fully update long-term memory (memory_update: keep only high-value stable workspace facts, prune excluded categories) and the history log (history_entry: keep only last 14 days or last 100 entries, add the new entry, output the full HISTORY text), then call save_memory with both.",
        )

    @classmethod
    def for_agent(cls) -> "MemoryExtractPrompt":
        """Agent 级别预设：跨 repo、跨任务，提炼该类 Agent 通用的工作经验（how to work effectively），供后续任意 repo/任务复用；只保留高价值、可复用内容，控制篇幅避免上下文过长。"""
        return cls(
            system_prompt="""You are the experience consolidation agent for this agent type. Distill **only truly important, reusable** experience into this agent type's long-term memory.

**Scope**: This is **agent-level public memory** - cross-repo, cross-task, injected into every future session of this agent type. Position it like a Skill: insights about **how to work effectively** as this agent type, not knowledge about any specific codebase or task. Keep it **concise and high-signal** to avoid context overflow.

**What to include (high value only):**
- **Workflow patterns**: Effective task-execution habits that recur across sessions (e.g., "verify file exists before editing", "run tests after non-trivial changes", "confirm destructive actions before executing").
- **Tool usage tips**: Which tools to prefer for which job, tool-specific gotchas that recur across sessions.
- **Environment & platform quirks**: Cross-session constraints (OS-specific paths, shell/encoding pitfalls, dependency/version traps).
- **Recurring failure patterns**: Mistakes that tend to repeat - wrong usage, common misjudgments, pitfalls independent of any specific repo.

**What to exclude or drop:**
- **Repo-specific knowledge**: codebase structure, module locations, repo-specific conventions, file paths - these belong to workspace memory, not here.
- **Task-specific implementation**: one-off solutions, specific bug fixes, feature details - these belong to session context, not here.
- **Generic capability descriptions** and **low-signal items** that won't clearly help future runs across different repos and tasks.

If there is no high-value reusable content, keep memory empty.

**Full overwrite + prune:** Review "Current Agent Memory" and "Content to Process". Decide what to keep, what is outdated or repo/task-specific and should be dropped, and what new experience is worth adding. Output the **complete** updated MEMORY.md: merge new high-value items, remove repo-specific/task-specific/low-value items, and enforce a hard total length limit of <=1500 tokens. This level has no event log; pass an empty string for history_entry.""",
            user_instruction="Based on the content below, review and fully update agent-level long-term memory. Keep only high-value, reusable experience that applies **across repos and tasks**; if none, keep memory empty. Enforce hard length limit <=1500 tokens. Leave history_entry empty, then call save_memory.",
        )

    @classmethod
    def for_user(cls) -> "MemoryExtractPrompt":
        """用户级预设：跨 repo、跨 workspace、跨 agent 类型，提炼关于该用户本人的稳定协作偏好与硬性约束。

        严格原则：宁缺毋滥。该记忆会被注入该用户未来所有会话，污染代价极高--
        错误或低信号的记忆会损害每一次未来对话，因此门槛极严，不确定时一律不记。
        """
        return cls(
            system_prompt="""You are the user profile consolidation agent. This memory stores **stable, cross-repo, cross-workspace** facts about THIS user that will be injected into every future session they have, regardless of repo, workspace, or agent type.

**Scope**: This is **user-level memory** - the highest level, injected most broadly. Because it pollutes context across ALL of the user's future sessions, the bar for inclusion is **extremely high**. When in doubt, do NOT record. Wrong or low-signal memory here damages every future conversation; an empty user memory is strictly better than a noisy one.

**What belongs here (only if stable across MULTIPLE sessions, not a one-off):**
- **Stable collaboration preferences**: communication style (terse vs. verbose), preferred reply language, whether to explain reasoning or just act, whether to ask clarifying questions or proceed with judgment.
- **Persistent technical background**: their seniority in key domains, primary tech stacks they work in (affects how much explanation they need), documented accessibility needs.
- **Hard constraints on process**: e.g., "never push to main without approval", "always run tests before reporting done", "prefer one bundled PR over many small ones". Only record if the user has **explicitly stated these as rules** - never infer from a single action.
- **Persistent dislikes / allergy triggers**: tools, approaches, or patterns the user has explicitly rejected more than once.

**What to exclude (drop or do not record):**
- **Repo-specific knowledge**: codebase structure, module locations, repo-specific conventions, file paths - these belong to workspace memory.
- **Agent-type-specific experience**: how to work as a particular agent type - belongs to agent memory.
- **Task / session-specific details**: specific bugs, features, investigations, in-progress work - these belong to session context, not user memory.
- **One-off instructions or corrections**: things said once in a single conversation that may not generalize. Only record if the pattern recurs across sessions.
- **Inferred personality judgments**: "the user seems impatient", "the user is detail-oriented", "the user prefers X" - too subjective, **never** record inferences; only record what the user has explicitly stated.
- **Generic dev knowledge** not specific to this user.
- **Ephemeral emotional states**: "user was frustrated today", "user was in a hurry" - never record these.
- **Anything you are not CONFIDENT will still be true in future sessions.**

**Strict inclusion rule (apply rigidly):** Before recording any item, it must pass ALL three tests:
1. **Stable** - observed or stated across multiple sessions, not a one-off.
2. **Useful** - clearly improves future collaboration across repos/workspaces.
3. **Not better stored elsewhere** - not repo-specific (->workspace memory), not agent-type-specific (->agent memory), not task-specific (->session context).
If an item fails ANY test, **drop it**.

**Full overwrite + prune:** Review "Current User Memory" and "Content to Process". Decide what to keep, what to drop (repo/task/agent-specific, one-off, inferred, or low-signal), and what new experience is worth adding. Output the **complete** updated MEMORY.md: merge new high-value items, remove anything that fails the strict inclusion rule, and enforce a hard total length limit of <=800 tokens. This level has no event log; pass an empty string for history_entry. If nothing meets the bar, output an empty or near-empty memory - do not pad with low-signal notes.""",
            user_instruction="Based on the content below, review and fully update user-level long-term memory. Apply the **strict inclusion rule** rigidly: when in doubt, drop. Keep only items that are stable across sessions, clearly useful for future collaboration, and not better stored in workspace/agent/session memory. Enforce hard length limit <=800 tokens. If nothing meets the bar, keep memory empty. Leave history_entry empty, then call save_memory.",
        )


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


class DefaultMemory(BaseMemoryManager):
    """默认文件型记忆：工作空间与 Agent 类型记忆（MEMORY.md / HISTORY.md）。"""
    def __init__(self, ctx: AgentContext) -> None:
        super().__init__(ctx)
        index = hashlib.sha256(ctx.workspace_path.encode()).hexdigest()  # 按照 Hash 创建索引
        memory_dir = Path(settings.runtime_data_dir) / MEMORY_DIR_NAME
        self._agent_memory = memory_dir / ctx.agent_type / "MEMORY.md"
        self._user_memory = memory_dir / ctx.user_id / "MEMORY.md"
        self._workspace_memory = memory_dir / index / "MEMORY.md"
        self._workspace_history = memory_dir / index / "HISTORY.md"

    async def _read_file(self, file: Path) -> str:
        if not file.exists():
            return ""
        return await asyncio.to_thread(file.read_text, encoding="utf-8")

    async def _write_file(self, file: Path, content: str) -> None:
        file.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(file.write_text, content, encoding="utf-8")

    @staticmethod
    def _messages_to_lines(messages: List[Message]) -> List[str]:
        """将 Message 列表转为可读文本行，使用 Message.to_user_message()。"""
        lines: List[str] = []
        for m in messages:
            d = m.to_user_message()
            content = (d.get("content") or "").strip()
            if not content:
                continue
            role = (d.get("role") or "?").upper()
            ts = d.get("create_time") or ""
            if isinstance(ts, str) and len(ts) > 16:
                ts = ts[:16]
            lines.append(f"[{ts}] {role}: {content[:500]}")
        return lines

    async def _extract(
        self,
        llm: Any,
        system_prompt: str,
        user_question: str,
    ) -> Tuple[Optional[str], Optional[str]]:
        """调用 LLM 提取记忆，返回 (memory_update, history_entry)，不写入 store。由调用方决定写入 session 或 store。"""
        try:
            if llm is None:
                logging.warning("Memory extract: llm is invalid")
                return None, None
            
            response, _ = await llm.ask_tools(
                system_prompt=system_prompt,
                user_prompt="",
                user_question=user_question,
                history=None,
                tools=_SAVE_MEMORY_TOOL,
                tool_choice="required",
            )

            if not response.success or not response.tool_calls:
                logging.warning("Memory extract: LLM did not call save_memory, skipping")
                return None, None
            for tool in response.tool_calls:
                if tool.name != "save_memory":
                    continue
                args = tool.args if isinstance(tool.args, dict) else {}
                update = args.get("memory_update")
                if update is not None and not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                entry = args.get("history_entry")
                if entry is not None and not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                return update, entry
            
            return None, None
        except Exception:
            logging.exception("Memory extract failed")
            return None, None
    
    async def _consolidate_workspace_memory(
        self,
        llm: Any,
        content: str,
    ) -> None:
        """工作空间记忆合并：提炼到 `<workspace_path>/.memory/MEMORY.md` 与 `HISTORY.md`（均为全量覆盖）。"""
        system_prompt = MemoryExtractPrompt.for_workspace().system_prompt
        current_memory = await self._read_file(self._workspace_memory)
        current_history = await self._read_file(self._workspace_history)
        user_content = (
            f"## Current Workspace Memory\n{current_memory or '(empty)'}\n\n"
            f"## Current Workspace History\n{current_history or '(empty)'}\n\n"
            f"## Content to Process\n{content}"
        )
        user_question = f"{MemoryExtractPrompt.for_workspace().user_instruction}\n\n{user_content}"
        memory_update, history_entry = await self._extract(
            llm=llm,
            system_prompt=system_prompt,
            user_question=user_question
        )
        if memory_update is not None and memory_update != current_memory:
            await self._write_file(self._workspace_memory, memory_update)
        if history_entry is not None and history_entry != current_history:
            await self._write_file(self._workspace_history, history_entry)

    async def _consolidate_agent_memory(self, llm: Any, content: str) -> None:
        """Agent 类型记忆合并：提炼到 `<workspace_path>/.memory/<agent_type>/MEMORY.md`。"""
        system_prompt = MemoryExtractPrompt.for_agent().system_prompt
        current_memory = await self._read_file(self._agent_memory)
        agent_info = (
            f"## Agent Info\n"
            f"- Type: {self._ctx.agent_type}\n"
            f"- Description: {self._ctx.agent_description or '(none)'}\n\n"
        )
        user_content = (
            f"{agent_info}"
            f"## Current Agent Memory\n{current_memory or '(empty)'}\n\n"
            f"## Content to Process\n{content}"
        )
        user_question = f"{MemoryExtractPrompt.for_agent().user_instruction}\n\n{user_content}"
        memory_update, _ = await self._extract(
            llm=llm,
            system_prompt=system_prompt,
            user_question=user_question,
        )
        if memory_update is not None and memory_update != current_memory:
            await self._write_file(self._agent_memory, memory_update)

    async def _consolidate_user_memory(self, llm: Any, content: str) -> None:
        """用户级记忆合并：提炼到 `<runtime_data>/<user_id>/MEMORY.md`（跨 repo/workspace/agent 共享）。"""
        system_prompt = MemoryExtractPrompt.for_user().system_prompt
        current_memory = await self._read_file(self._user_memory)
        user_info = (
            f"## User Info\n"
            f"- User ID: {self._ctx.user_id or '(none)'}\n\n"
        )
        user_content = (
            f"{user_info}"
            f"## Current User Memory\n{current_memory or '(empty)'}\n\n"
            f"## Content to Process\n{content}"
        )
        user_question = f"{MemoryExtractPrompt.for_user().user_instruction}\n\n{user_content}"
        memory_update, _ = await self._extract(
            llm=llm,
            system_prompt=system_prompt,
            user_question=user_question,
        )
        if memory_update is not None and memory_update != current_memory:
            await self._write_file(self._user_memory, memory_update)

    async def consolidate_memory(
        self,
        llm: Any,
        *,
        archive_all: bool = False,
        memory_window: int = 20
    ) -> bool:
        """记忆合并入口：基于 last_consolidated 取待处理消息，依次执行会话/工作空间/Agent 类型三层记忆提取，最后统一更新 last_consolidated 并持久化会话。"""
        session = await SESSION_MANAGER.get_session(self._ctx.session_id)
        if not session:
            return False
            
        if archive_all:
            old_messages = session.messages
            keep_count = 0
        else:
            keep_count = max(0, memory_window // 2)
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[
                session.last_consolidated : -keep_count if keep_count else len(session.messages)
            ]
        # 如果没有需要合并的消息，则直接返回
        if not old_messages:
            return True
            
        # 记录合并消息数量和保留消息数量
        logging.info(
            "Memory consolidation: %s to consolidate, keep=%s",
            len(old_messages),
            keep_count,
        )

        lines = self._messages_to_lines(old_messages)
        if not lines:
            return True
        content = "\n".join(lines)
        
        await asyncio.gather(
            self._consolidate_workspace_memory(llm, content),
            self._consolidate_agent_memory(llm, content),
            self._consolidate_user_memory(llm, content),
        )

        # 更新会话的 last_consolidated 并持久化会话
        session.last_consolidated = (
            len(session.messages) if archive_all else (len(session.messages) - keep_count)
        )
        await SESSION_MANAGER.save_session(session)

        logging.info("Memory consolidation done: last_consolidated=%s", session.last_consolidated)

        return True

    async def get_workspace_memory_context(self) -> str:
        """将工作空间记忆拼成可追加到 Prompt 的 Markdown 片段（来自 .workspace/<workspace_index>/memory.md）。"""
        content = await self._read_file(self._workspace_memory)
        if not (content or "").strip():
            return ""
        content = increase_md_heading_levels(content.strip(), levels=2)
        return f"## Long-term Memory\n{content}\n"

    async def get_agent_memory_context(self) -> str:
        """将 Agent 类型记忆拼成可追加到 Prompt 的 Markdown 片段（来自 memory/<agent_type>/MEMORY.md）。"""
        content = await self._read_file(self._agent_memory)
        if not (content or "").strip():
            return ""
        content = increase_md_heading_levels(content.strip(), levels=2)
        return f"## Agent Experience (success/failure)\n{content}\n"

    async def get_user_memory_context(self) -> str:
        """将用户级记忆拼成可追加到 Prompt 的 Markdown 片段（来自 memory/<user_id>/MEMORY.md）。"""
        content = await self._read_file(self._user_memory)
        if not (content or "").strip():
            return ""
        content = increase_md_heading_levels(content.strip(), levels=2)
        return f"## User Profile\n{content}\n"

    async def get_memory_context(
        self,
    ) -> str:
        """组合记忆上下文，供上层一次性拼接到 prompt。"""
        parts = [
            await self.get_user_memory_context(),
            await self.get_agent_memory_context(),
            await self.get_workspace_memory_context(),
        ]
        return "\n".join(p for p in parts if (p or "").strip())