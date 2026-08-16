from typing import Any, Dict
from app.agents.schemes import AgentContext, RuntimeContext
from app.agents.tools.catalog import register_tool
from app.agents.tools.schemes import ToolErrorResult, ToolResult
from app.codebase.integration.facade import CodebaseFacade
from app.agents.tools.codebase._common import CodebaseQueryToolBase


@register_tool(name="codebase_similar_code_search", toolset="codebase")
class CodebaseSimilarCodeSearchTool(CodebaseQueryToolBase):
    @property
    def name(self) -> str:
        return "codebase_similar_code_search"

    def description(self, params=None) -> str:
        return """Find similar implementations to a code snippet via CodeBase index.

When to use (PRIORITY: HIGH — 写新代码或重构前首选此工具):
- Reuse existing patterns before writing new code
- Find implementations that solve similar problems
- Discover existing utilities or helpers that match your needs
- Search by concept/pattern when exact identifiers are unknown

Why this is useful:
- Semantic similarity finds related code even with different naming conventions
- Useful when grep can't find matches because the code uses different terminology
- Supports multi-line code snippets (pass as JSON string with \\n)

Failure recovery:
- Not ready / disabled / error -> grep_search with key identifiers, or read_file
- Weak results -> refine query, or use grep_search as backup"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code_text": {
                    "type": "string",
                    "description": "用于相似检索的代码片段，支持多行代码",
                    "minLength": 1,
                },
                "top_k": {
                    "type": "integer",
                    "description": "期望返回候选数量，默认10，范围1-50。注意：系统会根据信号强度和质量过滤，实际返回数量可能少于此值",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["code_text"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    def is_available(self) -> bool:
        return self._code_base_enabled() and self._line_chunk_enabled()

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        code_text: str,
        top_k: int = 10,
    ) -> ToolResult:
        del run_ctx
        try:
            repo = await self._ensure_query_ready(agent_ctx)
            payload = await CodebaseFacade.search_similar_code(
                repo_id=repo["repo_id"],
                code_text=code_text,
                top_k=top_k,
            )
            return self._ok(
                {
                    "ok": True,
                    "tool": self.name,
                    "repo_id": repo["repo_id"],
                    "scan_status": repo["status"],
                    "top_k": top_k,
                    "data": payload,
                }
            )
        except Exception as exc:
            return ToolErrorResult(f"codebase_similar_code_search 执行失败: {exc}")
