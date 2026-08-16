"""图谱查询结果规范化：降噪、去重、边界提示。"""
from __future__ import annotations
from typing import Iterable, List, Optional, Set


class GraphResultNormalizer:
    """清洗 CodeGraph 文件/符号命中，并给出不支持场景说明。"""

    NOISE_PATH_PARTS = (
        "/node_modules/",
        "/.venv/",
        "/venv/",
        "/__pycache__/",
        "/.git/",
        "/dist/",
        "/build/",
        "/target/",
        "/.pytest_cache/",
        "/site-packages/",
    )
    SUPPORTED_FILE_EXTS = {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".java",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".cxx",
        ".hpp",
        ".hh",
        ".hxx",
    }

    @classmethod
    def normalize_path(cls, path: str) -> str:
        return (path or "").replace("\\", "/").lstrip("./")

    @classmethod
    def is_noise_path(cls, path: str) -> bool:
        p = f"/{cls.normalize_path(path).lower()}/"
        return any(part in p for part in cls.NOISE_PATH_PARTS)

    @classmethod
    def is_supported_source_path(cls, path: str) -> bool:
        norm = cls.normalize_path(path)
        if not norm or cls.is_noise_path(norm):
            return False
        lower = norm.lower()
        return any(lower.endswith(ext) for ext in cls.SUPPORTED_FILE_EXTS)

    @classmethod
    def unsupported_file_message(cls, file_path: str) -> Optional[str]:
        norm = cls.normalize_path(file_path)
        if not norm:
            return "文件路径为空，无法查询图谱依赖"
        if cls.is_noise_path(norm):
            return f"路径属于忽略目录，图谱通道不保证结果: {norm}"
        lower = norm.lower()
        if "." not in lower.rsplit("/", 1)[-1]:
            return None
        if not any(lower.endswith(ext) for ext in cls.SUPPORTED_FILE_EXTS):
            return (
                f"当前 CodeGraph 适配层未将扩展名视为源码文件: {norm}；"
                "结果可能为空，请改用 related/similar 或确认索引语言覆盖"
            )
        return None

    @classmethod
    def missing_symbol_message(cls, symbol: str, relation: str) -> str:
        sym = (symbol or "").strip() or "?"
        kind = (relation or "").strip().lower()
        if kind == "callees":
            return f"未找到符号 {sym!r} 的 callees（开源 CodeGraph 可能未索引该符号；可降级 related）"
        return (
            f"未找到符号 {sym!r} 的 callers（开源 CodeGraph 可能未索引该符号；"
            "可改用 related/graph 文件依赖）"
        )

    @classmethod
    def is_symbol_not_found_error(cls, exc: object) -> bool:
        text = str(exc or "").lower()
        markers = (
            "not found",
            "symbol not found",
            "未找到符号",
            "no such symbol",
        )
        return any(m in text for m in markers)

    @classmethod
    def clean_paths(cls, paths: Iterable[str], *, exclude: Optional[str] = None) -> List[str]:
        exclude_norm = cls.normalize_path(exclude or "")
        out: List[str] = []
        seen: Set[str] = set()
        for raw in paths:
            norm = cls.normalize_path(str(raw or ""))
            if not norm or not cls.is_supported_source_path(norm):
                continue
            if exclude_norm and norm == exclude_norm:
                continue
            if norm in seen:
                continue
            seen.add(norm)
            out.append(norm)
        return out

    @classmethod
    def clean_symbol_hits(cls, hits: Iterable[dict], *, limit: int = 20) -> List[dict]:
        out: List[dict] = []
        seen: Set[tuple] = set()
        for hit in hits or []:
            if not isinstance(hit, dict):
                continue
            fp = cls.normalize_path(str(hit.get("file_path") or ""))
            name = str(hit.get("name") or "").strip()
            if not name:
                continue
            if fp and not cls.is_supported_source_path(fp):
                continue
            key = (fp, name, hit.get("start_line"))
            if key in seen:
                continue
            seen.add(key)
            row = dict(hit)
            row["file_path"] = fp or None
            out.append(row)
            if len(out) >= max(1, limit):
                break
        return out
