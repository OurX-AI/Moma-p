from __future__ import annotations
import re
from typing import Dict, List, Optional, Tuple
from app.infrastructure.llms import embedding_factory
from app.infrastructure.vector_store import VECTOR_STORE_CONN
from ...constants import line_chunk_space_name, symbol_summary_space_name
from ...models.analysis_status import RepoAnalysisType as AnalysisType


class ExactMatchService:
    """基于已落库向量元数据的精确/边界匹配（不扫磁盘、不触发 analyze）。"""

    SCAN_CAP = 50000
    PATH_MIN_LEN = 5
    WEAK_PATH_TERMS = {
        "app",
        "src",
        "lib",
        "core",
        "base",
        "util",
        "utils",
        "common",
        "test",
        "tests",
        "agent",
        "agents",
        "api",
        "main",
        "init",
        "config",
        "service",
        "services",
    }

    @staticmethod
    async def _embedding_dim() -> Optional[int]:
        model = embedding_factory.create_model()
        if not model:
            return None
        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            return None
        return len(vectors[0])

    @staticmethod
    def split_tokens(text: str) -> List[str]:
        raw = (text or "").strip()
        if not raw:
            return []
        parts = re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", raw)
        out: List[str] = []
        for p in parts:
            out.append(p)
            for piece in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", p):
                if piece and piece not in out:
                    out.append(piece)
        return out

    @classmethod
    def score_symbol_keyword(cls, keyword: str, symbol_name: str, file_path: str = "") -> float:
        """仅按符号名分层打分；路径命中交给 match_chunk_paths，避免引用文件靠路径蹭 exact。"""
        kw = (keyword or "").strip()
        if not kw:
            return 0.0
        kw_l = kw.lower()
        name = symbol_name or ""
        name_l = name.lower()
        _ = file_path  # 保留参数兼容调用方；符号通道不再用路径加分

        # 1) 符号全名 / 后缀（Class.method）
        if name_l == kw_l:
            return 3.0
        if name_l.endswith("." + kw_l) or name_l.endswith("/" + kw_l):
            return 2.8
        # 短词/弱词：禁止靠 CamelCase 拆词蹭命中（避免 agent→BaseAgent）
        if len(kw_l) < cls.PATH_MIN_LEN or kw_l in cls.WEAK_PATH_TERMS:
            return 0.0
        # 2) CamelCase / 下划线边界整词
        name_tokens = {t.lower() for t in cls.split_tokens(name)}
        kw_tokens = [t.lower() for t in cls.split_tokens(kw)]
        if kw_l in name_tokens:
            return 2.5
        if kw_tokens and all(t in name_tokens for t in kw_tokens if len(t) >= 2):
            if len(kw_tokens) >= 2:
                return 2.2
        # 3) 符号名强子串（仅较长关键词）
        if len(kw_l) >= 6 and kw_l in name_l:
            return 1.8
        return 0.0

    @classmethod
    def score_path_keyword(cls, keyword: str, file_path: str) -> float:
        kw = (keyword or "").strip().lower()
        path = (file_path or "").replace("\\", "/").lower()
        if not kw or not path:
            return 0.0
        if len(kw) < cls.PATH_MIN_LEN or kw in cls.WEAK_PATH_TERMS:
            return 0.0
        # 完整相对路径前缀/包含（用于 agents/core/base）
        if "/" in kw and kw in path:
            return 1.4
        segs = re.split(r"[/_.-]+", path)
        if kw in segs:
            return 1.2
        stem = path.rsplit("/", 1)[-1]
        if stem.endswith(".py"):
            stem = stem[:-3]
        if kw == stem:
            return 1.3
        if len(kw) >= 6 and kw in path:
            return 1.05
        return 0.0

    @classmethod
    async def match_symbols(
        cls,
        repo_id: str,
        keywords: List[str],
        *,
        top_k: int = 10,
    ) -> List[Dict[str, object]]:
        keys = [str(k).strip() for k in (keywords or []) if k and str(k).strip()]
        if not keys:
            return []
        dim = await cls._embedding_dim()
        if not dim:
            return []
        space = symbol_summary_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            return []
        rows = await VECTOR_STORE_CONN.list_records(
            space,
            condition={
                "repo_id": repo_id,
                "analysis_type": AnalysisType.SYMBOL_SUMMARY_VECTOR.value,
            },
            select_fields=[
                "repo_id",
                "file_path",
                "analysis_type",
                "symbol_kind",
                "symbol_name",
                "start_line",
                "end_line",
                "summary",
            ],
            limit=cls.SCAN_CAP,
        )
        scored: List[Tuple[float, Dict[str, object]]] = []
        for row in rows:
            name = str(row.get("symbol_name") or "")
            path = str(row.get("file_path") or "")
            hit = 0.0
            for kw in keys:
                hit = max(hit, cls.score_symbol_keyword(kw, name, path))
            if hit <= 0:
                continue
            # 仅名称通道：>=2.8 为强定义；1.8~2.8 为弱符号；不再产出 path tier
            if hit >= 2.8:
                tier = "symbol"
            elif hit >= 1.8:
                tier = "symbol_weak"
            else:
                continue
            scored.append(
                (
                    hit,
                    {
                        "file_path": path,
                        "symbol_kind": row.get("symbol_kind"),
                        "symbol_name": name,
                        "start_line": row.get("start_line"),
                        "end_line": row.get("end_line"),
                        "summary": row.get("summary"),
                        "_score": hit,
                        "match_source": "exact",
                        "exact_tier": tier,
                    },
                )
            )
        scored.sort(key=lambda x: (-x[0], str(x[1].get("symbol_name") or "")))
        return [it for _, it in scored[: max(1, top_k)]]

    @classmethod
    async def list_indexed_file_paths(cls, repo_id: str) -> List[str]:
        """已索引文件路径列表（供同目录兄弟扩展），去重保序。"""
        dim = await cls._embedding_dim()
        if not dim:
            return []
        rows = await cls._list_path_rows(repo_id, dim)
        out: List[str] = []
        seen: set[str] = set()
        for row in rows:
            fp = str(row.get("file_path") or "").replace("\\", "/").strip()
            if not fp or fp in seen:
                continue
            seen.add(fp)
            out.append(fp)
        return out

    @classmethod
    async def list_indexed_symbol_names(cls, repo_id: str) -> List[str]:
        """已索引符号名列表（供仓内 identifier lexicon），去重保序。"""
        dim = await cls._embedding_dim()
        if not dim:
            return []
        space = symbol_summary_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            return []
        rows = await VECTOR_STORE_CONN.list_records(
            space,
            condition={
                "repo_id": repo_id,
                "analysis_type": AnalysisType.SYMBOL_SUMMARY_VECTOR.value,
            },
            select_fields=["symbol_name"],
            limit=cls.SCAN_CAP,
        )
        out: List[str] = []
        seen: set[str] = set()
        for row in rows:
            name = str(row.get("symbol_name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
        return out

    @classmethod
    async def match_paths(
        cls,
        repo_id: str,
        keywords: List[str],
        *,
        top_k: int = 10,
    ) -> List[Dict[str, object]]:
        """按路径段/文件名命中（弱于符号 exact）；优先扫符号空间，回退行块空间。"""
        keys = [str(k).strip() for k in (keywords or []) if k and str(k).strip()]
        if not keys:
            return []
        dim = await cls._embedding_dim()
        if not dim:
            return []
        rows = await cls._list_path_rows(repo_id, dim)
        if not rows:
            return []
        best_by_file: Dict[str, Tuple[float, Dict[str, object]]] = {}
        for row in rows:
            path = str(row.get("file_path") or "")
            hit = 0.0
            for kw in keys:
                hit = max(hit, cls.score_path_keyword(kw, path))
            if hit <= 0:
                continue
            item = {
                "file_path": path,
                "symbol_kind": None,
                "symbol_name": None,
                "start_line": row.get("start_line"),
                "end_line": row.get("end_line"),
                "_score": hit,
                "match_source": "exact",
                "exact_tier": "path",
            }
            prev = best_by_file.get(path)
            if prev is None or hit > prev[0]:
                best_by_file[path] = (hit, item)
        ranked = sorted(best_by_file.values(), key=lambda x: (-x[0], str(x[1].get("file_path") or "")))
        return [it for _, it in ranked[: max(1, top_k)]]

    @classmethod
    async def match_chunk_paths(
        cls,
        repo_id: str,
        keywords: List[str],
        *,
        top_k: int = 10,
    ) -> List[Dict[str, object]]:
        """兼容旧名：等同 match_paths。"""
        return await cls.match_paths(repo_id, keywords, top_k=top_k)

    @classmethod
    async def _list_path_rows(cls, repo_id: str, dim: int) -> List[Dict[str, object]]:
        symbol_space = symbol_summary_space_name(repo_id, dim)
        if await VECTOR_STORE_CONN.space_exists(symbol_space):
            rows = await VECTOR_STORE_CONN.list_records(
                symbol_space,
                condition={
                    "repo_id": repo_id,
                    "analysis_type": AnalysisType.SYMBOL_SUMMARY_VECTOR.value,
                },
                select_fields=[
                    "repo_id",
                    "file_path",
                    "analysis_type",
                    "start_line",
                    "end_line",
                ],
                limit=cls.SCAN_CAP,
            )
            if rows:
                return rows
        chunk_space = line_chunk_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(chunk_space):
            return []
        return await VECTOR_STORE_CONN.list_records(
            chunk_space,
            condition={
                "repo_id": repo_id,
                "analysis_type": AnalysisType.LINE_CHUNK_VECTOR.value,
            },
            select_fields=[
                "repo_id",
                "file_path",
                "analysis_type",
                "start_line",
                "end_line",
                "chunk_index",
            ],
            limit=cls.SCAN_CAP,
        )
