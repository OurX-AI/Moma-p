from pathlib import Path
from typing import Any, Dict, List, Union
from app.services.lsp import CodeLSPService


class LspFileDiagnostics:
    """写文件后采集并格式化 LSP 错误诊断。"""

    @staticmethod
    def pretty_item(item: Dict[str, Any]) -> str:
        severity_map = {1: "ERROR", 2: "WARN", 3: "INFO", 4: "HINT"}
        severity = severity_map.get(int(item.get("severity", 1)), "ERROR")
        msg = str(item.get("message") or "").strip()
        rng = item.get("range") or {}
        start = (rng.get("start") or {}) if isinstance(rng, dict) else {}
        line = int(start.get("line", 0)) + 1
        col = int(start.get("character", 0)) + 1
        return f"{severity} [{line}:{col}] {msg}"

    @staticmethod
    async def collect(
        file_paths: Union[str, List[str]],
        workspace_root: str,
    ) -> Dict[str, List[Dict[str, Any]]]:
        root = (workspace_root or "").strip()
        if not root:
            return {}
        if isinstance(file_paths, str):
            targets = [file_paths]
        else:
            targets = list(file_paths or [])
        unique_targets = list(dict.fromkeys(targets))
        for target in unique_targets:
            try:
                available = await CodeLSPService.has_clients(target, repo_id=root)
                if not available:
                    continue
                await CodeLSPService.touch_file(
                    target, wait_for_diagnostics=True, repo_id=root
                )
            except Exception:
                continue
        try:
            all_diagnostics = await CodeLSPService.diagnostics()
        except Exception:
            return {}
        normalized_targets = {str(Path(p).resolve()) for p in unique_targets}
        filtered: Dict[str, List[Dict[str, Any]]] = {}
        for file_path, items in (all_diagnostics or {}).items():
            norm = str(Path(file_path).resolve())
            if norm not in normalized_targets:
                continue
            only_errors = [x for x in (items or []) if int(x.get("severity", 1)) == 1]
            if only_errors:
                filtered[norm] = only_errors
        return filtered

    @staticmethod
    def format_suffix(diagnostics: Dict[str, List[Dict[str, Any]]]) -> str:
        if not diagnostics:
            return ""
        parts = ["\n\nLSP errors detected in changed files, please fix:"]
        for file_path, items in diagnostics.items():
            parts.append(f'\n\n<diagnostics file="{file_path}">')
            limited = items[:20]
            for item in limited:
                parts.append(f"\n{LspFileDiagnostics.pretty_item(item)}")
            if len(items) > 20:
                parts.append(f"\n... and {len(items) - 20} more")
            parts.append("\n</diagnostics>")
        return "".join(parts)
