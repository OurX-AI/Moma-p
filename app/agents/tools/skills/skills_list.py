import json
from typing import Any, Dict, Optional
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolResult, ToolSuccessResult, ToolErrorResult
from ...schemes import AgentContext, RuntimeContext
from ...skills.paths import DEFAULT_SKILL_CATEGORY, normalize_category


@register_tool(name="skills_list", toolset="skills")
class SkillsListTool(BaseTool):

    @property
    def name(self) -> str:
        return "skills_list"

    def description(self, params=None) -> str:
        return """List available skills (name, description, category, available).

When to use:
- Discover skill directory names before `skill_view`.
- Optionally filter by `category` (`skills/<category>/<name>/`).

When NOT to use:
- Loading full skill instructions (use `skill_view`).

Failure recovery:
- Empty list / missing skill -> broaden category filter or install via `skill_hub` if appropriate."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional category filter (e.g. etf, trading, orchestration). Case-insensitive. Omit to list all skills.",
                },
            },
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        category: Optional[str] = None,
    ) -> ToolResult:
        if run_ctx.is_aborted():
            return run_ctx.aborted_tool_result(self.name)

        mgr = agent_ctx.skills_manager
        if mgr is None:
            return ToolErrorResult("skills_list is not available: skills_manager is not configured")

        raw_category = (category or "").strip()
        filter_category = normalize_category(raw_category) if raw_category else None
        items = []
        categories: set[str] = set()
        for s in mgr.list_skills(filter_unavailable=False, category=filter_category):
            if run_ctx.is_aborted():
                return run_ctx.aborted_tool_result(self.name)
            name = s["name"]
            if not mgr.is_skill_allowed(name):
                continue
            meta = s.get("meta") or {}
            available = mgr._check_requirements(meta)
            item = {
                "name": name,
                "description": s.get("description") or "",
                "category": s.get("category") or DEFAULT_SKILL_CATEGORY,
                "available": available,
                "source": s.get("source") or "builtin",
            }
            if not filter_category:
                categories.add(item["category"])
            if not available:
                setup = mgr.get_skill_setup_info(meta)
                if setup.get("requires"):
                    item["requires"] = setup["requires"]
                if setup.get("setup_needed"):
                    item["setup_needed"] = True
                if setup.get("missing_bins"):
                    item["missing_bins"] = setup["missing_bins"]
                if setup.get("missing_env"):
                    item["missing_env"] = setup["missing_env"]
                if setup.get("usage_hint"):
                    item["usage_hint"] = setup["usage_hint"]
            items.append(item)

        payload: dict[str, Any] = {
            "count": len(items),
            "skills": items,
        }
        if filter_category:
            payload["category"] = filter_category
        else:
            payload["categories"] = sorted(categories)

        output = json.dumps(payload, ensure_ascii=False, indent=2)
        return ToolSuccessResult(output)
