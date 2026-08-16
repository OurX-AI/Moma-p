from typing import Any, Dict
from app.agents.schemes import AgentContext, RuntimeContext
from app.agents.tools.catalog import register_tool
from app.agents.tools.schemes import ToolErrorResult, ToolResult
from app.codebase.integration.facade import CodebaseFacade
from app.agents.tools.codebase._common import CodebaseQueryToolBase


@register_tool(name="codebase_symbol_locate", toolset="codebase")
class CodebaseSymbolLocateTool(CodebaseQueryToolBase):
    @property
    def name(self) -> str:
        return "codebase_symbol_locate"

    def description(self, params=None) -> str:
        return """Locate code files/line ranges by symbol or keyword via CodeBase semantic index.

When to use (PRIORITY: HIGH — 优先使用此工具定位代码，不确定代码在哪时首选):
- When looking for function/class/variable definitions or usages
- When searching for code related to a specific concept or pattern (e.g., 搜"鉴权"能找到 auth 相关代码)
- When you need to understand code structure and relationships
- Exact regex/string search when you already know the text — use grep_search instead

Why this is useful:
- Semantic search understands code meaning, not just text matching
- Can find related code even with different naming conventions
- Complements grep_search: grep for exact text, symbol_locate for conceptual search

Failure recovery:
- Not ready / disabled / error -> fall back to grep_search + read_file
- Weak hits -> refine query, or use grep_search; follow with codebase_dependency_query if available"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "符号名、路径片段或关键词。建议使用完整标识符（如函数名、类名）或3个以上字符的关键词以获得更精确的匹配",
                    "minLength": 1,
                },
                "top_k": {
                    "type": "integer",
                    "description": "返回候选数量，默认10，范围1-50",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    def is_available(self) -> bool:
        return self._code_base_enabled()

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        query: str,
        top_k: int = 10,
    ) -> ToolResult:
        del run_ctx
        try:
            repo = await self._ensure_query_ready(agent_ctx)
            payload = await CodebaseFacade.locate_symbol(
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
            return ToolErrorResult(f"codebase_symbol_locate 执行失败: {exc}")
