from __future__ import annotations
import os
from typing import Any, Dict, List, Mapping, Optional


class SearchSnippetAttacher:
    """为检索命中从本地源码附加 snippet，供 Agent 直接喂上下文。

    仅处理 payload['items']；also_consider 默认不挂 snippet，避免冲上下文。
    """

    DEFAULT_MAX_LINES = 40
    DEFAULT_FALLBACK_LINES = 24
    DEFAULT_MAX_CHARS = 6000

    @classmethod
    def attach_to_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        enabled: bool = True,
        max_lines: int = DEFAULT_MAX_LINES,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> Dict[str, Any]:
        out = dict(payload)
        if not enabled:
            out["with_content"] = False
            return out
        items = out.get("items") or []
        if not isinstance(items, list):
            out["with_content"] = True
            return out
        default_root = str(out.get("path") or "")
        attached: List[Dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            row = dict(raw)
            root = str(row.get("path") or default_root or "")
            cls.attach_item(
                row,
                repo_root=root,
                max_lines=max_lines,
                max_chars=max_chars,
            )
            attached.append(row)
        out["items"] = attached
        out["with_content"] = True
        return out

    @classmethod
    def attach_item(
        cls,
        item: Dict[str, Any],
        *,
        repo_root: str,
        max_lines: int = DEFAULT_MAX_LINES,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> Dict[str, Any]:
        if item.get("snippet"):
            return item
        existing = item.get("content")
        if isinstance(existing, str) and existing.strip():
            item["snippet"] = cls._clip(existing, max_chars)
            return item
        fp = str(item.get("file_path") or "").strip()
        if not fp or not repo_root:
            return item
        abs_path = cls._resolve_abs(repo_root, fp)
        if not abs_path or not os.path.isfile(abs_path):
            return item
        start = cls._as_line(item.get("start_line"))
        end = cls._as_line(item.get("end_line"))
        text = cls.read_range(
            abs_path,
            start_line=start,
            end_line=end,
            max_lines=max_lines,
            fallback_lines=cls.DEFAULT_FALLBACK_LINES,
        )
        if text:
            item["snippet"] = cls._clip(text, max_chars)
            if start and not item.get("start_line"):
                item["start_line"] = start
            if end and not item.get("end_line"):
                item["end_line"] = end
        return item

    @classmethod
    def read_range(
        cls,
        abs_path: str,
        *,
        start_line: Optional[int],
        end_line: Optional[int],
        max_lines: int,
        fallback_lines: int,
    ) -> str:
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except OSError:
            return ""
        if not lines:
            return ""
        if start_line and end_line and end_line >= start_line:
            s = max(1, start_line)
            e = min(len(lines), end_line)
            span = e - s + 1
            if span > max_lines:
                e = s + max_lines - 1
            return "\n".join(lines[s - 1 : e])
        if start_line:
            s = max(1, start_line)
            e = min(len(lines), s + max_lines - 1)
            return "\n".join(lines[s - 1 : e])
        n = min(len(lines), max(1, fallback_lines))
        return "\n".join(lines[:n])

    @staticmethod
    def _resolve_abs(repo_root: str, file_path: str) -> Optional[str]:
        root = os.path.abspath(os.path.normpath(repo_root))
        rel = file_path.replace("\\", "/").lstrip("/")
        candidate = os.path.abspath(os.path.normpath(os.path.join(root, rel)))
        try:
            common = os.path.commonpath([root, candidate])
        except ValueError:
            return None
        if os.path.normcase(common) != os.path.normcase(root):
            return None
        return candidate

    @staticmethod
    def _as_line(value: Any) -> Optional[int]:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    @staticmethod
    def _clip(text: str, max_chars: int) -> str:
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 1)] + "…"
