import hashlib
import json
import logging
import re
from typing import Dict, List, Tuple
from app.infrastructure.llms import embedding_factory
from app.infrastructure.vector_store import MatchDenseExpr, SearchRequest, VECTOR_STORE_CONN
from ...constants.experience_space import ExperienceAnalysisType, mr_pattern_space_name
from ..codevector.code_vector import CodeVectorService
from .models import ExperiencePattern


class PatternVectorService:
    """经验模式向量写入与检索（仅 ready 记录）。"""

    @staticmethod
    def build_embed_text(pattern: ExperiencePattern) -> str:
        file_tokens = []
        for fp in list(pattern.relevant_files or []) + list(pattern.anchors or []):
            norm = str(fp or "").replace("\\", "/")
            if not norm:
                continue
            file_tokens.append(norm)
            stem = norm.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem:
                file_tokens.append(stem)
            for part in norm.split("/"):
                if part and part not in {"app", "src"} and len(part) >= 2:
                    file_tokens.append(part)
        parts = [
            pattern.title,
            pattern.scenario,
            f"模式：{'；'.join(pattern.patterns)}",
            f"步骤：{'；'.join(pattern.plan)}" if pattern.plan else "",
            f"锚点：{' '.join(pattern.anchors)}" if pattern.anchors else "",
            f"相关文件：{' '.join(pattern.relevant_files)}" if pattern.relevant_files else "",
            f"关键词：{' '.join(dict.fromkeys(file_tokens))}" if file_tokens else "",
        ]
        return "\n".join(p for p in parts if p).strip()

    @staticmethod
    def lexical_boost(query: str, item: Dict[str, object]) -> float:
        """短词/弱 query 的词面加分，提升 jwt/鉴权 等命中。"""
        q = (query or "").strip().casefold()
        if not q:
            return 0.0
        tokens = [t for t in re.split(r"[\s,/\\_\-]+", q) if len(t) >= 2]
        if len(q) >= 2 and q not in tokens:
            tokens.insert(0, q)
        blob = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("scenario") or ""),
                " ".join(str(x) for x in (item.get("patterns") or [])),
                " ".join(str(x) for x in (item.get("plan") or [])),
                " ".join(str(x) for x in (item.get("anchors") or [])),
                " ".join(str(x) for x in (item.get("relevant_files") or [])),
            ]
        ).casefold()
        if not blob:
            return 0.0
        hits = 0
        for t in tokens:
            if t in blob:
                hits += 1
        if hits <= 0:
            return 0.0
        # 短 query 词面命中权重大一些
        weight = 0.35 if len(q) <= 8 else 0.2
        return min(0.6, hits * weight)

    @staticmethod
    def rerank_by_query(query: str, items: List[Dict[str, object]]) -> List[Dict[str, object]]:
        ranked: List[Dict[str, object]] = []
        for it in items:
            row = dict(it)
            base = float(row.get("similarity") or 0.0)
            boost = PatternVectorService.lexical_boost(query, row)
            row["_lexical_boost"] = boost
            row["similarity"] = base + boost
            ranked.append(row)
        ranked.sort(key=lambda x: float(x.get("similarity") or 0.0), reverse=True)
        return ranked

    @staticmethod
    def _pattern_key(pattern: ExperiencePattern) -> str:
        scenario = re.sub(r"\s+", " ", (pattern.scenario or "").strip().lower())
        modes = " | ".join(pattern.patterns)
        return f"{scenario} || {modes}"

    @staticmethod
    async def upsert_patterns(repo_id: str, patterns: List[ExperiencePattern]) -> int:
        valid = [p for p in patterns if p.title and p.scenario and p.patterns]
        if not valid:
            return 0
        pairs: List[Tuple[ExperiencePattern, str]] = []
        for p in valid:
            text = (PatternVectorService.build_embed_text(p) or "").strip()
            if not text:
                logging.warning("跳过空经验 embedding 文本 title=%s", p.title)
                continue
            pairs.append((p, text))
        if not pairs:
            return 0
        valid = [p for p, _ in pairs]
        texts = [t for _, t in pairs]
        vectors = await CodeVectorService._embed_texts(texts)
        if not vectors:
            raise RuntimeError("经验向量化失败")
        dim = len(vectors[0])
        vector_field = f"q_{dim}_vec"
        space = mr_pattern_space_name(repo_id, dim)
        await VECTOR_STORE_CONN.create_space(space, dim)
        records: List[Dict[str, object]] = []
        for idx, pattern in enumerate(valid):
            sha = (pattern.source_commits or ["unknown"])[0]
            pkey = PatternVectorService._pattern_key(pattern)
            stable_id = hashlib.sha1(f"{repo_id}|mr_pattern|{sha}|{pkey}".encode("utf-8")).hexdigest()
            records.append(
                {
                    "id": stable_id,
                    "repo_id": repo_id,
                    "file_path": "",
                    "analysis_type": ExperienceAnalysisType.MR_PATTERN_VECTOR,
                    "symbol_kind": "mr_pattern",
                    "symbol_name": sha,
                    "content": pattern.title,
                    "summary": json.dumps(pattern.to_payload(), ensure_ascii=False),
                    "start_line": 0,
                    "end_line": 0,
                    vector_field: vectors[idx],
                }
            )
        failed = await VECTOR_STORE_CONN.insert_records(space, records)
        if failed:
            raise RuntimeError(f"经验向量写入失败: {failed}")
        return len(records)

    @staticmethod
    async def upsert_pattern(repo_id: str, pattern: ExperiencePattern) -> None:
        await PatternVectorService.upsert_patterns(repo_id, [pattern])

    @staticmethod
    async def search(repo_id: str, query: str, top_k: int = 10) -> List[Dict[str, object]]:
        q = (query or "").strip()
        if not q:
            return []
        rows = await CodeVectorService._embed_texts([q])
        if not rows:
            return []
        dim = len(rows[0])
        space = mr_pattern_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            return []
        request = SearchRequest(
            select_fields=[
                "repo_id",
                "analysis_type",
                "symbol_name",
                "content",
                "summary",
            ],
            condition={
                "repo_id": repo_id,
                "analysis_type": ExperienceAnalysisType.MR_PATTERN_VECTOR,
            },
            match_exprs=[
                MatchDenseExpr(
                    vector_column_name=f"q_{dim}_vec",
                    embedding_data=rows[0],
                    embedding_data_type="float",
                    distance_type="cosine",
                    topn=max(top_k * 3, 15),
                )
            ],
            limit=max(top_k * 3, 15),
        )
        result = await VECTOR_STORE_CONN.search([space], request)
        docs = VECTOR_STORE_CONN.get_source(result) if result else []
        items: List[Dict[str, object]] = []
        for doc in docs:
            payload = PatternVectorService._parse_summary(doc.get("summary"))
            items.append(PatternVectorService._item_from_payload(payload, doc))
        return PatternVectorService.rerank_by_query(q, items)[: max(1, top_k)]

    @staticmethod
    def _item_from_payload(payload: dict, doc: dict) -> Dict[str, object]:
        title = payload.get("title") or doc.get("content")
        return {
            "title": title,
            "similarity": doc.get("_score"),
            "scenario": payload.get("scenario") or "",
            "patterns": payload.get("patterns") or [],
            "quality_score": float(payload.get("quality_score") or 0.0),
            "source_commits": payload.get("source_commits")
            or ([doc.get("symbol_name")] if doc.get("symbol_name") else []),
            "anchors": [str(x).strip() for x in (payload.get("anchors") or []) if str(x).strip()],
            "relevant_files": [
                str(x).strip().replace("\\", "/")
                for x in (payload.get("relevant_files") or [])
                if str(x).strip()
            ],
            "plan": payload.get("plan") or [],
        }

    @staticmethod
    def merge_by_scenario(items: List[Dict[str, object]]) -> List[Dict[str, object]]:
        grouped: Dict[str, Dict[str, object]] = {}
        for item in items:
            scenario = str(item.get("scenario") or "").strip()
            key = re.sub(r"\s+", " ", scenario.lower()) if scenario else str(item.get("title") or "")
            if not key:
                continue
            cur = grouped.get(key)
            if cur is None:
                grouped[key] = dict(item)
                continue
            if float(item.get("similarity") or 0.0) > float(cur.get("similarity") or 0.0):
                keep = dict(item)
            else:
                keep = cur
            merged_patterns = list(dict.fromkeys([*(cur.get("patterns") or []), *(item.get("patterns") or [])]))
            merged_commits = list(
                dict.fromkeys([*(cur.get("source_commits") or []), *(item.get("source_commits") or [])])
            )
            keep["patterns"] = merged_patterns
            keep["source_commits"] = merged_commits
            keep["merged_count"] = int(cur.get("merged_count") or 1) + 1
            grouped[key] = keep
        merged = list(grouped.values())
        merged.sort(key=lambda x: float(x.get("similarity") or 0.0), reverse=True)
        return merged

    @staticmethod
    def _parse_summary(raw: object) -> dict:
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    async def delete_repo_patterns(repo_id: str) -> int:
        model = embedding_factory.create_model()
        if not model:
            return 0
        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            return 0
        dim = len(vectors[0])
        space = mr_pattern_space_name(repo_id, dim)
        if not await VECTOR_STORE_CONN.space_exists(space):
            return 0
        deleted = int(await VECTOR_STORE_CONN.delete_records(space, {"repo_id": repo_id}))
        logging.info("已删除经验向量 repo_id=%s count=%s", repo_id, deleted)
        return deleted

    @staticmethod
    async def space_exists(repo_id: str) -> bool:
        model = embedding_factory.create_model()
        if not model:
            return False
        vectors, _ = await model.encode(["x"])
        if vectors is None or len(vectors) == 0:
            return False
        dim = len(vectors[0])
        return await VECTOR_STORE_CONN.space_exists(mr_pattern_space_name(repo_id, dim))
