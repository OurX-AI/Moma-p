from typing import Any, TYPE_CHECKING
from ..base import BaseTool
from ..catalog import register_tool
from ..policy import DELEGATION_TOOL_NAME, SUBAGENT_TOOLSET
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from ...contants import load_subagent_config
from ...core.subagent import ExploreThoroughness
if TYPE_CHECKING:
    from ...core.subagent import SubAgentManager


@register_tool(name=DELEGATION_TOOL_NAME, toolset=SUBAGENT_TOOLSET)
class SpawnTool(BaseTool):
    """Tool to spawn a subagent for background task execution."""

    def _spawn_cfg(self) -> dict[str, Any]:
        cfg = self._agent_ctx.agent_config if self._agent_ctx else {}
        tools = cfg.get("tools") if isinstance(cfg, dict) else None
        tools = tools if isinstance(tools, dict) else {}
        spawn = tools.get("spawn")
        return spawn if isinstance(spawn, dict) else {}

    def _allow_types(self) -> list[str]:
        raw = self._spawn_cfg().get("allow_types")
        if not isinstance(raw, list):
            return []
        return [str(x).strip() for x in raw if str(x).strip()]

    def _default_type(self) -> str:
        return str(self._spawn_cfg().get("default_type") or "").strip()

    def _type_catalog_lines(self) -> list[str]:
        """列出可用 type 及 when_to_use（来自各 subagents/{type}/config.json）。"""
        parent = (self._agent_ctx.agent_type or "").strip() if self._agent_ctx else ""
        lines: list[str] = []
        for t in self._allow_types():
            hint = ""
            if parent:
                try:
                    raw = load_subagent_config(parent, t)
                    hint = str(raw.get("when_to_use") or raw.get("description_en") or "").strip()
                except ValueError:
                    hint = ""
            lines.append(f"- {t}: {hint}" if hint else f"- {t}")
        return lines

    @property
    def name(self) -> str:
        return DELEGATION_TOOL_NAME

    def description(self, params=None) -> str:
        """UI 短描述。"""
        return "Spawn a subagent for a focused subtask"

    def prompt(self) -> str:
        """给 Agent 的完整说明（含当前 allow_types / default_type）。"""
        allow = self._allow_types()
        default_type = self._default_type()
        catalog = self._type_catalog_lines()
        types_block = "\n".join(catalog) if catalog else "(none configured)"
        default_hint = default_type or (allow[0] if allow else "")
        levels = " | ".join(ExploreThoroughness.LEVELS)
        return (
            "Spawn a subagent for a focused subtask and return a concise result.\n\n"
            "When to use:\n"
            "- Long-running, tool-intensive, or data-heavy work that should stay out of the main context.\n"
            "- Parallel independent subtasks in one turn (they run concurrently).\n"
            "- explore / plan / general-purpose / verification according to allow_types.\n\n"
            "When NOT to use:\n"
            "- Tiny one-step actions.\n"
            "- Tasks that need frequent back-and-forth with the main agent.\n\n"
            "Execution rules:\n"
            "- Write `task` as clear scope + expected output, not raw pasted data.\n"
            f"- Available subagent types (pass as `type`; default `{default_hint}` when omitted):\n"
            f"{types_block}\n"
            f"- For type=`explore`, set `thoroughness` ({levels}; default `{ExploreThoroughness.DEFAULT}`): "
            "quick for targeted lookups, medium for balanced exploration, "
            "very thorough for comprehensive multi-location analysis.\n"
            "- For type=`verification` (after non-trivial writes): pass original user task + "
            "changed files + approach; do not pre-declare PASS.\n"
            "- mode='sync' (default) waits for the result; mode='async' returns a task id immediately.\n"
            "- After mode='async', the main agent continues and ingests results automatically; "
            "it waits for outstanding async subagents before the final answer.\n"
            "- Optional: spawn_status(action='get'|'list') to inspect task state.\n\n"
            "Failure recovery:\n"
            "- Vague subagent result -> re-spawn with sharper scope/output contract.\n"
            "- verification FAIL -> fix in main session, then re-spawn with prior findings."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        allow = self._allow_types()
        default_type = self._default_type()
        type_prop: dict[str, Any] = {
            "type": "string",
            "description": (
                "Subagent type to spawn. "
                + (f"Default: {default_type}." if default_type else "Required if no default_type is configured.")
            ),
        }
        if allow:
            type_prop["enum"] = allow
        if default_type:
            type_prop["default"] = default_type
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "A single, clear task for the subagent to complete "
                        "(goal, boundaries, and desired result format). "
                        "Avoid pasting raw file contents or very long text."
                    ),
                },
                "type": type_prop,
                "thoroughness": {
                    "type": "string",
                    "enum": list(ExploreThoroughness.LEVELS),
                    "description": (
                        "Explore search depth only (ignored for other types). "
                        f"Default for explore: {ExploreThoroughness.DEFAULT}. "
                        "quick=targeted; medium=balanced; very thorough=comprehensive."
                    ),
                    "default": ExploreThoroughness.DEFAULT,
                },
                "mode": {
                    "type": "string",
                    "enum": ["sync", "async"],
                    "description": (
                        "Optional. 'sync' waits and returns result in this call. "
                        "'async' returns task id immediately and notifies on completion. Default: sync."
                    ),
                    "default": "sync",
                },
                "label": {
                    "type": "string",
                    "description": "Optional short label for logs/UI (directory, feature, or topic name).",
                },
            },
            "required": ["task"],
        }

    def is_readonly(self, params=None) -> bool:
        return False

    def is_parallel(self, params=None) -> bool:
        # 同轮多个 spawn 可并发（各子 Agent 独立上下文）；写冲突由 sibling-write 提醒兜底
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        task: str,
        *,
        type: str | None = None,
        mode: str = "sync",
        label: str | None = None,
        thoroughness: str | None = None,
    ) -> ToolResult:
        """Spawn a subagent to execute the given task."""
        subagent_manager = agent_ctx.subagent_manager
        if subagent_manager is None:
            return ToolErrorResult("spawn is not available: subagent_manager is not configured")
        chosen = (type or "").strip() or self._default_type()
        try:
            text = await subagent_manager.start_task(
                task,
                run_ctx,
                mode=mode,
                label=label,
                subagent_type=chosen,
                thoroughness=thoroughness,
            )
        except ValueError as exc:
            return ToolErrorResult(str(exc))
        return ToolSuccessResult(text)
