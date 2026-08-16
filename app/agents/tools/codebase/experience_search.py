from typing import Any, Dict
from app.agents.schemes import AgentContext, RuntimeContext
from app.agents.tools.catalog import register_tool
from app.agents.tools.schemes import ToolErrorResult, ToolResult
from app.codebase.integration.facade import CodebaseFacade
from app.agents.tools.codebase._common import CodebaseQueryToolBase


@register_tool(name="codebase_experience_search", toolset="codebase")
class CodebaseExperienceSearchTool(CodebaseQueryToolBase):
    @property
    def name(self) -> str:
        return "codebase_experience_search"

    def description(self, params=None) -> str:
        return """Search historical MR experience patterns (architectural decisions, conventions, migration strategies) extracted from past commits via CodeBase index.

When to use (PRIORITY: HIGH — 排查问题或实现新功能前首选此工具，查看团队历史经验):
- Before implementing a feature or fix, check if similar patterns already exist
- When you need guidance on architectural conventions or directory structure decisions
- When onboarding to unfamiliar modules — past experience reveals team approaches
- When looking for migration strategies or protocol bridge patterns

Why this is useful:
- Leverages team's historical knowledge and best practices
- Surfaces solutions that have been validated in production
- Helps avoid repeating past mistakes

Failure recovery:
- Not ready / disabled / error -> skip (no grep equivalent for experience data)
- No experience data -> refine query with different keywords"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "自然语言描述要查询的经验场景，如 '认证模块接入新协议'、'目录迁移范式'",
                    "minLength": 1,
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回候选数量，默认10，范围1-30",
                    "minimum": 1,
                    "maximum": 30,
                },
            },
            "required": ["query"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    def is_available(self) -> bool:
        return self._code_base_enabled() and self._mr_experience_enabled()

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        query: str,
        top_k: int = 10,
    ) -> ToolResult:
        del run_ctx
        try:
            repo = await self._ensure_repo(agent_ctx)
            payload = await CodebaseFacade.search_patterns(
                repo_id=repo["repo_id"],
                query=query,
                top_k=top_k,
            )
            return self._ok(
                {
                    "ok": True,
                    "tool": self.name,
                    "repo_id": repo["repo_id"],
                    "scan_status": repo["status"],
                    "query": query,
                    "top_k": top_k,
                    "data": payload,
                }
            )
        except Exception as exc:
            return ToolErrorResult(f"codebase_experience_search 执行失败: {exc}")
