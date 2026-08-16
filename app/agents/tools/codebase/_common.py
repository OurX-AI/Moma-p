import json
from typing import Any, Dict
from app.config.settings import settings
from app.codebase.integration.facade import CodebaseFacade
from app.agents.schemes import AgentContext
from app.agents.tools.base import BaseTool
from app.agents.tools.schemes import ToolResult, ToolSuccessResult


class CodebaseQueryToolBase(BaseTool):
    @staticmethod
    def _ok(payload: Dict[str, Any]) -> ToolResult:
        return ToolSuccessResult(json.dumps(payload, ensure_ascii=False, indent=2))

    async def _ensure_repo(self, agent_ctx: AgentContext) -> Dict[str, str]:
        workspace_path = str(agent_ctx.workspace_path or "").strip()
        if not workspace_path:
            raise ValueError("workspace_path 不能为空")
        user_id = str(agent_ctx.user_id or "").strip() or "cli"
        ready = await CodebaseFacade.ensure_workspace_registered(
            workspace_path=workspace_path,
            user_id=user_id,
        )
        if not ready.ok:
            raise ValueError(ready.message or "CodeBase 仓库初始化失败")
        if not ready.repo_id:
            raise ValueError("未获取到 repo_id")
        return {
            "repo_id": ready.repo_id,
            "workspace_path": workspace_path,
            "status": ready.status,
        }

    async def _ensure_query_ready(self, agent_ctx: AgentContext) -> Dict[str, Any]:
        repo = await self._ensure_repo(agent_ctx)
        ok = await CodebaseFacade.can_query(repo_id=repo["repo_id"])
        if not ok:
            raise ValueError("embedding 未完备，暂不允许查询")
        return repo

    @staticmethod
    def _code_base_enabled() -> bool:
        return bool(settings.codebase_enabled)

    @staticmethod
    def _code_graph_enabled() -> bool:
        return bool(settings.code_graph_enabled)

    @staticmethod
    def _line_chunk_enabled() -> bool:
        return bool(settings.code_analysis_line_chunk_enabled)

    @staticmethod
    def _mr_experience_enabled() -> bool:
        return bool(settings.mr_experience_enabled)
