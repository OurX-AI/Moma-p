import json
from typing import Any, Dict, Optional
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext
from ...skills.hub import HUB_SERVICE


@register_tool(name="skill_hub", toolset="skills")
class SkillHubTool(BaseTool):

    @property
    def name(self) -> str:
        return "skill_hub"

    def description(self, params=None) -> str:
        return """Browse and install skills from Skills Hub (builtin, GitHub, ClawHub, skills.sh, LobeHub).

When to use:
- Discover/install/update/uninstall hub skills instead of manual filesystem copies.

When NOT to use:
- Loading an already-available skill for a task (use `skill_view`).
- Editing local skill content (use `skill_manage`).

Actions:
- search / inspect / install / list_installed / uninstall / check / update / audit
- list_taps / add_tap / remove_tap for custom GitHub taps

Failure recovery:
- Ambiguous install target -> `search` then `inspect` before `install`.
- Update conflicts -> `check` then `update` with force only when requested."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "search",
                        "inspect",
                        "install",
                        "list_installed",
                        "uninstall",
                        "check",
                        "update",
                        "audit",
                        "list_taps",
                        "add_tap",
                        "remove_tap",
                    ],
                },
                "query": {
                    "type": "string",
                    "description": "Search keyword or github owner/repo for search action.",
                },
                "identifier": {
                    "type": "string",
                    "description": "Skill identifier for inspect/install, e.g. builtin:weather.",
                },
                "source": {
                    "type": "string",
                    "enum": [
                        "all",
                        "builtin",
                        "bundled",
                        "github",
                        "clawhub",
                        "skills-sh",
                        "skills.sh",
                        "well-known",
                        "wellknown",
                        "lobehub",
                    ],
                    "description": "Source filter for search (default all; bundled is alias of builtin).",
                },
                "name": {
                    "type": "string",
                    "description": "Skill name for uninstall/update/check/audit; or owner/repo for add_tap/remove_tap.",
                },
                "force": {
                    "type": "boolean",
                    "description": "Force install when policy would block non-dangerous findings.",
                },
                "enable_for_agents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For install: agent_type list to auto-enable skill in config after install.",
                },
                "include_content": {
                    "type": "boolean",
                    "description": "For inspect: include SKILL.md body preview (max 4000 chars).",
                },
                "limit": {
                    "type": "integer",
                    "description": "For search: page size (default 20, max 100).",
                },
                "offset": {
                    "type": "integer",
                    "description": "For search: pagination offset (default 0).",
                },
            },
            "required": ["action"],
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
        query: Optional[str] = None,
        identifier: Optional[str] = None,
        source: Optional[str] = None,
        name: Optional[str] = None,
        force: Optional[bool] = None,
        include_content: Optional[bool] = None,
        enable_for_agents: Optional[list[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> ToolResult:
        if run_ctx.is_aborted():
            return run_ctx.aborted_tool_result(self.name)

        act = (action or "").strip().lower()
        try:
            if act == "search":
                page_limit = 20 if limit is None else int(limit)
                page_offset = 0 if offset is None else int(offset)
                payload = await HUB_SERVICE.search(
                    query or "",
                    source=source or "all",
                    limit=page_limit,
                    offset=page_offset,
                )
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "inspect":
                ident = (identifier or "").strip()
                if not ident:
                    return ToolErrorResult("inspect requires identifier")
                payload = await HUB_SERVICE.inspect(ident, include_content=bool(include_content))
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "install":
                ident = (identifier or "").strip()
                if not ident:
                    return ToolErrorResult("install requires identifier")
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                agents = [str(x).strip() for x in (enable_for_agents or []) if str(x).strip()]
                payload = await HUB_SERVICE.install(ident, force=bool(force), enable_for_agents=agents)
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "list_installed":
                items = HUB_SERVICE.list_installed()
                payload = {"count": len(items), "skills": items}
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "uninstall":
                skill_name = (name or "").strip()
                if not skill_name:
                    return ToolErrorResult("uninstall requires name")
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                payload = HUB_SERVICE.uninstall(skill_name)
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "check":
                payload = {
                    "skills": await HUB_SERVICE.check_updates((name or "").strip() or None),
                }
                payload["updates_available"] = sum(1 for x in payload["skills"] if x.get("update_available"))
                payload["count"] = len(payload["skills"])
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "update":
                skill_name = (name or "").strip()
                if not skill_name:
                    return ToolErrorResult("update requires name")
                if run_ctx.is_aborted():
                    return run_ctx.aborted_tool_result(self.name)
                payload = await HUB_SERVICE.update(skill_name, force=bool(force))
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "audit":
                items = HUB_SERVICE.audit((name or "").strip() or None)
                payload = {"count": len(items), "skills": items}
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "list_taps":
                payload = HUB_SERVICE.list_github_taps()
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "add_tap":
                repo = (name or query or "").strip()
                if not repo:
                    return ToolErrorResult("add_tap requires repo (owner/repo) in name or query")
                payload = HUB_SERVICE.add_github_tap(repo)
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            if act == "remove_tap":
                repo = (name or query or "").strip()
                if not repo:
                    return ToolErrorResult("remove_tap requires repo (owner/repo) in name or query")
                payload = HUB_SERVICE.remove_github_tap(repo)
                return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))
            return ToolErrorResult("Unsupported action")
        except ValueError as e:
            return ToolErrorResult(str(e))
        except Exception as e:
            return ToolErrorResult(f"skill_hub failed: {e}")
