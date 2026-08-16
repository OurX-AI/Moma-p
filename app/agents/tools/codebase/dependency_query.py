from typing import Any, Dict
from app.agents.schemes import AgentContext, RuntimeContext
from app.agents.tools.catalog import register_tool
from app.agents.tools.schemes import ToolErrorResult, ToolResult
from app.codebase.integration.facade import CodebaseFacade
from app.agents.tools.codebase._common import CodebaseQueryToolBase


@register_tool(name="codebase_dependency_query", toolset="codebase")
class CodebaseDependencyQueryTool(CodebaseQueryToolBase):
    @property
    def name(self) -> str:
        return "codebase_dependency_query"

    def description(self, params=None) -> str:
        return """Query dependency relationships for a file or symbol via CodeBase index.

When to use (PRIORITY: HIGH — 评估改动影响面时首选此工具):
- Assess blast radius before edits (dependents/depended/callers/callees)
- Understanding what calls a function or what a function calls
- Analyzing module relationships and import chains
- Initial unknown locate (prefer codebase_symbol_locate first to find the target)

Why this is useful:
- Traverses dependency graph to capture indirect relationships
- Faster than grep for callers/callees analysis
- Supports both file-level and symbol-level queries

Failure recovery:
- Not ready / disabled / error -> lsp findReferences/incomingCalls or grep_search
- Empty results -> refine query, or use grep_search as backup"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "target_type": {
                    "type": "string",
                    "description": "查询目标类型：file 或 symbol",
                    "enum": ["file", "symbol"],
                },
                "target": {
                    "type": "string",
                    "description": "当 target_type=file 时传相对路径（如 src/main.py）；symbol 时传函数/类名（如 ask_tools_stream，不要传完整签名）",
                    "minLength": 1,
                },
                "direction": {
                    "type": "string",
                    "description": "file: dependents/depended; symbol: callers/callees",
                    "enum": ["dependents", "depended", "callers", "callees"],
                },
                "limit": {
                    "type": "integer",
                    "description": "symbol 查询时返回上限，默认20，范围1-100",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["target_type", "target", "direction"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    def is_available(self) -> bool:
        return self._code_base_enabled() and self._code_graph_enabled()

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        target_type: str,
        target: str,
        direction: str,
        limit: int = 20,
    ) -> ToolResult:
        del run_ctx
        try:
            repo = await self._ensure_query_ready(agent_ctx)
            payload = await CodebaseFacade.query_dependencies(
                repo_id=repo["repo_id"],
                target_type=target_type,
                target=target,
                direction=direction,
                limit=limit,
            )
            return self._ok(
                {
                    "ok": True,
                    "tool": self.name,
                    "repo_id": repo["repo_id"],
                    "scan_status": repo["status"],
                    "data": payload,
                }
            )
        except Exception as exc:
            return ToolErrorResult(f"codebase_dependency_query 执行失败: {exc}")
