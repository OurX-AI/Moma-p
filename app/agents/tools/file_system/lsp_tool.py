import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from app.services.lsp import CodeLSPService


_OPERATIONS = {
    "goToDefinition",
    "findReferences",
    "hover",
    "documentSymbol",
    "workspaceSymbol",
    "goToImplementation",
    "prepareCallHierarchy",
    "incomingCalls",
    "outgoingCalls",
}


@register_tool(name="lsp", toolset="filesystem")
class LspTool(BaseTool):
    """Language Server Protocol 查询工具（定义/引用/符号等）。"""

    @property
    def name(self) -> str:
        return "lsp"

    def description(self, params=None) -> str:
        return """Query Language Server Protocol (LSP) code intelligence.

When to use:
- Resolve definitions, references, hover/types, document/workspace symbols, call hierarchy.
- After edits, investigate LSP ERROR diagnostics attached to write results.

When NOT to use:
- Text/regex search across unknown files (use `grep_search`).
- Broad file discovery (use `glob_search`).

Operations:
- goToDefinition, findReferences, hover, documentSymbol, workspaceSymbol
- goToImplementation, prepareCallHierarchy, incomingCalls, outgoingCalls

Execution rules:
- `filePath` absolute; `line`/`character` are 1-based.
- `workspaceSymbol` uses `query` and does not require position params.

Failure recovery:
- Empty/unavailable result -> fall back to `grep_search` + `read_file`.
- Wrong position -> re-read file and recalculate 1-based coordinates."""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": sorted(_OPERATIONS),
                    "description": "LSP operation to perform",
                },
                "filePath": {
                    "type": "string",
                    "description": "Absolute path to the source file (required except workspaceSymbol)",
                },
                "line": {
                    "type": "integer",
                    "description": "1-based line number",
                },
                "character": {
                    "type": "integer",
                    "description": "1-based character/column number",
                },
                "query": {
                    "type": "string",
                    "description": "Symbol query for workspaceSymbol",
                },
            },
            "required": ["operation"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return True

    @staticmethod
    def _resolve_file_path(file_path: str) -> Path:
        return Path(file_path).expanduser().resolve()

    @staticmethod
    def _require_position(
        line: Optional[int],
        character: Optional[int],
    ) -> Optional[Tuple[int, int]]:
        if line is None and character is None:
            return None
        if line is None or character is None:
            raise ValueError("line and character must both be provided")
        return int(line), int(character)

    async def _call_operation(
        self,
        *,
        operation: str,
        file_path: str,
        line: Optional[int],
        character: Optional[int],
        query: str,
        workspace_root: str,
    ) -> List[Any]:
        if operation == "goToDefinition":
            if line is None or character is None:
                raise ValueError("line and character are required for goToDefinition")
            return await CodeLSPService.definition(
                file_path, line, character, repo_id=workspace_root
            )
        if operation == "findReferences":
            if line is None or character is None:
                raise ValueError("line and character are required for findReferences")
            return await CodeLSPService.references(
                file_path, line, character, repo_id=workspace_root
            )
        if operation == "hover":
            if line is None or character is None:
                raise ValueError("line and character are required for hover")
            return await CodeLSPService.hover(
                file_path, line, character, repo_id=workspace_root
            )
        if operation == "documentSymbol":
            return await CodeLSPService.document_symbol(
                file_path, repo_id=workspace_root
            )
        if operation == "workspaceSymbol":
            return await CodeLSPService.workspace_symbol(
                query or "", repo_id=workspace_root
            )
        if operation == "goToImplementation":
            if line is None or character is None:
                raise ValueError("line and character are required for goToImplementation")
            return await CodeLSPService.implementation(
                file_path, line, character, repo_id=workspace_root
            )
        if operation == "prepareCallHierarchy":
            if line is None or character is None:
                raise ValueError("line and character are required for prepareCallHierarchy")
            return await CodeLSPService.prepare_call_hierarchy(
                file_path, line, character, repo_id=workspace_root
            )
        if operation == "incomingCalls":
            if line is None or character is None:
                raise ValueError("line and character are required for incomingCalls")
            return await CodeLSPService.incoming_calls(
                file_path, line, character, repo_id=workspace_root
            )
        if operation == "outgoingCalls":
            if line is None or character is None:
                raise ValueError("line and character are required for outgoingCalls")
            return await CodeLSPService.outgoing_calls(
                file_path, line, character, repo_id=workspace_root
            )
        raise ValueError(f"unsupported operation: {operation}")

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        operation: str,
        filePath: str = "",
        line: Optional[int] = None,
        character: Optional[int] = None,
        query: str = "",
        **kwargs: Any,
    ) -> ToolResult:
        try:
            op = (operation or "").strip()
            if op not in _OPERATIONS:
                return ToolErrorResult(
                    f"operation must be one of: {', '.join(sorted(_OPERATIONS))}"
                )
            workspace_root = str(agent_ctx.workspace_path or "").strip()
            if not workspace_root:
                return ToolErrorResult("workspace_path is required for lsp tool")
            if op != "workspaceSymbol":
                if not (filePath or "").strip():
                    return ToolErrorResult("filePath is required for this operation")
                target = self._resolve_file_path(filePath)
                if not target.is_file():
                    return ToolErrorResult(f"File not found: {target}")
            else:
                target = Path(workspace_root)
            available = await CodeLSPService.has_clients(
                str(target if target.is_file() else Path(workspace_root)),
                repo_id=workspace_root,
            )
            if op != "workspaceSymbol" and not available:
                return ToolErrorResult("No LSP server available for this file type.")
            pos = self._require_position(line, character)
            line_num = pos[0] if pos else None
            char_num = pos[1] if pos else None
            if target.is_file():
                await CodeLSPService.touch_file(
                    str(target), wait_for_diagnostics=True, repo_id=workspace_root
                )
            result = await self._call_operation(
                operation=op,
                file_path=str(target) if target.is_file() else "",
                line=line_num,
                character=char_num,
                query=(query or "").strip(),
                workspace_root=workspace_root,
            )
            output = (
                "No results found for " + op
                if not result
                else json.dumps(result, ensure_ascii=False, indent=2)
            )
            return ToolSuccessResult(output)
        except ValueError as e:
            return ToolErrorResult(str(e))
        except Exception as e:
            return ToolErrorResult(f"lsp failed: {str(e)}")
