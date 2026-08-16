import json
from typing import Any, Dict, Optional
from ..catalog import register_tool
from ..base import BaseTool
from ..file_state import FILE_STATE_MANAGER
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext
from ...skills.manager import SKILL_SOURCE_EXTERNAL, SKILL_SOURCE_WORKSPACE, SkillsManager


@register_tool(name="skill_manage", toolset="skills")
class SkillManageTool(BaseTool):

    @property
    def name(self) -> str:
        return "skill_manage"

    def description(self, params=None) -> str:
        return """Modify writable skills under data/skills (workspace/external skills are read-only).

When to use:
- Create/delete/edit skill files when the user asks to manage skills.

When NOT to use:
- Loading skill instructions for a task (use `skill_view`).
- Installing from hubs (use `skill_hub`).

Actions:
- create / delete / replace / write_file
- For replace: `skill_view` first to copy exact text.

Failure recovery:
- Permission/read-only source -> only edit data/skills, or install via hub then manage."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "delete", "replace", "write_file"],
                    "description": "create/delete for skill lifecycle; replace/write_file for file edits.",
                },
                "name": {
                    "type": "string",
                    "description": "Skill directory name (see skills_list).",
                },
                "description": {
                    "type": "string",
                    "description": "Short description for frontmatter (create only).",
                },
                "category": {
                    "type": "string",
                    "description": "Directory category for create (skills/<category>/<name>/, default general).",
                },
                "file_path": {
                    "type": "string",
                    "description": "Relative path within skill directory. Default SKILL.md for replace.",
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find (replace only).",
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text (replace only).",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences of old_text (replace only, default false).",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content (write_file or create with custom SKILL.md).",
                },
            },
            "required": ["action", "name"],
        }

    def is_readonly(self, params=None) -> bool:
        return False

    def is_parallel(self, params=None) -> bool:
        return False

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        action: str,
        name: str,
        description: Optional[str] = None,
        category: Optional[str] = None,
        file_path: Optional[str] = None,
        old_text: Optional[str] = None,
        new_text: Optional[str] = None,
        replace_all: Optional[bool] = None,
        content: Optional[str] = None,
    ) -> ToolResult:
        if run_ctx.is_aborted():
            return run_ctx.aborted_tool_result(self.name)

        skill_name = (name or "").strip()
        if not skill_name:
            return ToolErrorResult("Missing name parameter")

        mgr = agent_ctx.skills_manager
        if mgr is None:
            return ToolErrorResult("skill_manage is not available: skills_manager is not configured")
        if not mgr.allow_manage:
            return ToolErrorResult("skill_manage is disabled. Set skills.allow_manage=yes in agent config.")

        act = (action or "").strip().lower()
        try:
            if act == "create":
                if mgr.find_skill(skill_name) is not None:
                    return ToolErrorResult(f"Skill already exists: {skill_name}")
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                result = mgr.create_skill(
                    skill_name,
                    description=description or "",
                    category=category or "general",
                    content=content,
                )
                output = json.dumps(result, ensure_ascii=False, indent=2)
                return ToolSuccessResult(output)
            if act == "delete":
                if not mgr.is_skill_deletable(skill_name):
                    return ToolErrorResult(
                        f"Skill is not deletable: {skill_name}. "
                        "Only data/skills entries can be deleted; workspace and external_dirs are read-only."
                    )
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                result = mgr.delete_skill(skill_name)
                output = json.dumps(result, ensure_ascii=False, indent=2)
                return ToolSuccessResult(output)
        except ValueError as e:
            return ToolErrorResult(str(e))

        found = mgr.find_skill(skill_name)
        if found and found[1] == SKILL_SOURCE_WORKSPACE:
            return ToolErrorResult(
                f"Skill is read-only (project workspace): {skill_name}. "
                "Edit files under <workspace>/skills/ outside this app; use skill_view to load."
            )
        if found and found[1] == SKILL_SOURCE_EXTERNAL and act in ("replace", "write_file"):
            pass
        elif not found:
            return ToolErrorResult(f"Skill not found: {skill_name}")

        try:
            if act == "replace":
                if old_text is None or new_text is None:
                    return ToolErrorResult("replace requires old_text and new_text")
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                rel_path = file_path or "SKILL.md"
                target_file = str(mgr.resolve_target_file(skill_name, rel_path))
            elif act == "write_file":
                if content is None:
                    return ToolErrorResult("write_file requires content")
                if not file_path or not file_path.strip():
                    return ToolErrorResult("write_file requires file_path")
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                target_file = str(mgr.resolve_target_file(skill_name, file_path))
            else:
                return ToolErrorResult("Unsupported action. Use create, delete, replace, or write_file.")
        except (ValueError, RuntimeError) as e:
            return ToolErrorResult(str(e))

        warning = FILE_STATE_MANAGER.check_stale_and_get_warning(run_ctx.actor_id, target_file)
        try:
            async with FILE_STATE_MANAGER.get_path_lock(target_file):
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                if act == "replace":
                    result = mgr.replace_skill_file(
                        skill_name,
                        file_path or "SKILL.md",
                        old_text,
                        new_text,
                        replace_all=bool(replace_all),
                    )
                else:
                    result = mgr.write_skill_file(skill_name, file_path, content)
        except (ValueError, RuntimeError) as e:
            return ToolErrorResult(str(e))
        except OSError as e:
            return ToolErrorResult(f"Failed to write skill file: {e}")

        FILE_STATE_MANAGER.record_write(run_ctx.actor_id, target_file)
        output = json.dumps(result, ensure_ascii=False, indent=2)
        return ToolSuccessResult(FILE_STATE_MANAGER.append_warning(output, warning))
