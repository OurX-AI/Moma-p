import json
from typing import Any, Dict, Optional
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext


@register_tool(name="skill_view", toolset="skills")
class SkillViewTool(BaseTool):

    @property
    def name(self) -> str:
        return "skill_view"

    def description(self, params=None) -> str:
        return """Load a skill by name (SKILL.md body + linked files).

When to use:
- Task matches a Skill in the system `# Skills` section — load full instructions before acting.
- Read skill-linked files via optional `file_path` (e.g. references/api.md).

When NOT to use:
- Discovering which skills exist (use `skills_list`).
- Reading skill files via `read_file` instead of this tool.

Failure recovery:
- Unknown name -> `skills_list` then retry with exact directory name."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill directory name (see skills_list or the # Skills section in system prompt).",
                },
                "file_path": {
                    "type": "string",
                    "description": "Optional relative path within the skill directory (e.g. references/api.md, scripts/setup.sh). Omit to read SKILL.md.",
                },
            },
            "required": ["name"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        name: str,
        file_path: Optional[str] = None,
    ) -> ToolResult:
        if run_ctx.is_aborted():
            return run_ctx.aborted_tool_result(self.name)

        skill_name = (name or "").strip()
        if not skill_name:
            return ToolErrorResult("Missing name parameter")

        mgr = agent_ctx.skills_manager
        if mgr is None:
            return ToolErrorResult("skill_view is not available: skills_manager is not configured")
        if not mgr.is_skill_allowed(skill_name):
            return ToolErrorResult(f"Skill not allowed for this agent: {skill_name}")

        rel_file = (file_path or "").strip() or None
        found = mgr.find_skill(skill_name)
        if not found:
            if rel_file:
                return ToolErrorResult(f"Skill file not found: {skill_name}/{rel_file}")
            return ToolErrorResult(f"Skill not found: {skill_name}")
        entry, _source = found

        content = mgr.get_skill_content(skill_name, rel_file, entry=entry)
        if content is None:
            if rel_file:
                return ToolErrorResult(f"Skill file not found: {skill_name}/{rel_file}")
            return ToolErrorResult(f"Skill not found: {skill_name}")

        available = mgr._check_requirements(entry.meta)
        payload: dict[str, Any] = {
            "name": skill_name,
            "available": available,
            "file": rel_file or "SKILL.md",
            "content": content,
        }
        setup = mgr.get_skill_setup_info(entry.meta)
        payload["setup_needed"] = setup.get("setup_needed", False)
        if not available:
            if setup.get("requires"):
                payload["requires"] = setup["requires"]
            if setup.get("missing_bins"):
                payload["missing_bins"] = setup["missing_bins"]
            if setup.get("missing_env"):
                payload["missing_env"] = setup["missing_env"]
        if not rel_file:
            linked = mgr.list_linked_files(entry)
            if linked:
                payload["linked_files"] = linked
            hints = []
            if setup.get("usage_hint"):
                hints.append(setup["usage_hint"])
            if linked:
                hints.append(
                    "To view linked files, call skill_view(name, file_path) "
                    "where file_path is e.g. references/api.md or scripts/setup.sh"
                )
            if hints:
                payload["usage_hint"] = " ".join(hints)
        elif setup.get("usage_hint") and not available:
            payload["usage_hint"] = setup["usage_hint"]

        return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
