from __future__ import annotations
import asyncio
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from ...repo_mgmt.models.git_repo_mgmt import RepoKind


class MultiPathSearchService:
    """多仓检索：对各已登记 path 扇出查询，再按分数融合（跨仓不去重同名相对路径）。"""

    SOURCE_GROUP = {
        ("exact", "symbol"): 0,
        ("exact", "symbol_weak"): 1,
        ("exact", "path"): 2,
        ("exact", ""): 2,
        ("codegraph", ""): 3,
        ("mr_experience", ""): 4,
        ("api", ""): 4,
        ("line_chunk", ""): 5,
        ("symbol_summary", ""): 5,
    }

    @staticmethod
    def normalize_paths(paths: Sequence[str] | str | None) -> List[str]:
        if paths is None:
            return []
        if isinstance(paths, str):
            raw = [paths]
        else:
            raw = list(paths)
        out: List[str] = []
        seen: set[str] = set()
        for p in raw:
            s = str(p or "").strip()
            if not s:
                continue
            key = s.replace("\\", "/").rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    @classmethod
    def tag_item(
        cls,
        item: Dict[str, object],
        *,
        repo_id: str,
        path: str,
        kind: str,
    ) -> Dict[str, object]:
        row = dict(item)
        row["repo_id"] = repo_id
        row["path"] = path
        row["kind"] = kind
        return row

    @classmethod
    def _rank_key(cls, item: Dict[str, object]) -> Tuple[int, float, str, str]:
        source = str(item.get("match_source") or "")
        tier = str(item.get("exact_tier") or "")
        group = cls.SOURCE_GROUP.get((source, tier))
        if group is None:
            group = cls.SOURCE_GROUP.get((source, ""), 9)
        score = float(item.get("score") or 0)
        path = str(item.get("path") or "")
        fp = str(item.get("file_path") or "")
        return (group, -score, path, fp)

    @classmethod
    def _dedupe_key(cls, item: Dict[str, object]) -> Tuple[object, ...]:
        return (
            item.get("repo_id") or item.get("path"),
            item.get("file_path"),
            item.get("symbol_name"),
            item.get("start_line"),
            item.get("end_line"),
            item.get("title"),
        )

    @classmethod
    def merge_items(
        cls,
        item_batches: Sequence[Sequence[Dict[str, object]]],
        *,
        top_k: int,
    ) -> List[Dict[str, object]]:
        best: Dict[Tuple[object, ...], Dict[str, object]] = {}
        for batch in item_batches:
            for raw in batch:
                it = dict(raw)
                key = cls._dedupe_key(it)
                prev = best.get(key)
                if prev is None or cls._rank_key(it) < cls._rank_key(prev):
                    best[key] = it
        ranked = sorted(best.values(), key=cls._rank_key)
        return ranked[: max(1, top_k)] if ranked else []

    @classmethod
    def merge_search_payloads(
        cls,
        payloads: Sequence[Dict[str, object]],
        *,
        top_k: int,
        query_fields: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        """合并多仓 search_* / resolve 风格 payload（均含 items）。"""
        if not payloads:
            return {
                "paths": [],
                "repos": [],
                "total": 0,
                "items": [],
                **(query_fields or {}),
            }
        if len(payloads) == 1:
            one = dict(payloads[0])
            path = one.get("path")
            if path and "paths" not in one:
                one["paths"] = [path]
            return one

        batches: List[List[Dict[str, object]]] = []
        also_batches: List[List[Dict[str, object]]] = []
        repos: List[Dict[str, object]] = []
        paths: List[str] = []
        errors: Dict[str, str] = {}
        channels_used: List[str] = []
        intents: List[str] = []
        read_hint: Optional[str] = None
        for payload in payloads:
            path = str(payload.get("path") or "")
            if path:
                paths.append(path)
            repos.append(
                {
                    "path": path or None,
                    "repo_id": payload.get("repo_id"),
                    "kind": payload.get("kind"),
                    "total": payload.get("total"),
                    "error": payload.get("error"),
                }
            )
            if payload.get("error"):
                errors[path or str(payload.get("repo_id"))] = str(payload.get("error"))
            for ch in payload.get("channels_used") or []:
                if ch not in channels_used:
                    channels_used.append(str(ch))
            if payload.get("intent"):
                intents.append(str(payload.get("intent")))
            if payload.get("read_hint") and not read_hint:
                read_hint = str(payload.get("read_hint"))
            items = []
            for it in payload.get("items") or []:
                items.append(
                    cls.tag_item(
                        dict(it),
                        repo_id=str(payload.get("repo_id") or it.get("repo_id") or ""),
                        path=path or str(it.get("path") or ""),
                        kind=str(payload.get("kind") or it.get("kind") or RepoKind.CODE),
                    )
                )
            batches.append(items)
            also_items = []
            for it in payload.get("also_consider") or []:
                also_items.append(
                    cls.tag_item(
                        dict(it),
                        repo_id=str(payload.get("repo_id") or it.get("repo_id") or ""),
                        path=path or str(it.get("path") or ""),
                        kind=str(payload.get("kind") or it.get("kind") or RepoKind.CODE),
                    )
                )
            also_batches.append(also_items)

        merged = cls.merge_items(batches, top_k=top_k)
        primary_keys = {cls._dedupe_key(it) for it in merged}
        also_merged_raw = cls.merge_items(also_batches, top_k=max(top_k, 8))
        also_merged = [it for it in also_merged_raw if cls._dedupe_key(it) not in primary_keys]
        out: Dict[str, object] = {
            "paths": paths,
            "repos": repos,
            "total": len(merged),
            "items": merged,
            "also_consider": also_merged,
            "also_consider_total": len(also_merged),
            **(query_fields or {}),
        }
        if read_hint:
            out["read_hint"] = read_hint
        if channels_used:
            out["channels_used"] = channels_used
        if intents:
            uniq: List[str] = []
            for i in intents:
                if i not in uniq:
                    uniq.append(i)
            out["intent"] = uniq[0] if len(uniq) == 1 else "mixed"
            out["intents"] = uniq
        if errors:
            out["repo_errors"] = errors
        return out

    @classmethod
    async def fanout(
        cls,
        paths: Sequence[str],
        *,
        top_k: int,
        run_one: Callable[[str], Awaitable[Dict[str, object]]],
        query_fields: Optional[Dict[str, object]] = None,
    ) -> Dict[str, object]:
        normalized = cls.normalize_paths(paths)
        if not normalized:
            raise ValueError("至少指定一个 --path")
        if len(normalized) == 1:
            one = await run_one(normalized[0])
            return cls.merge_search_payloads([one], top_k=top_k, query_fields=query_fields)

        results = await asyncio.gather(
            *[run_one(p) for p in normalized],
            return_exceptions=True,
        )
        payloads: List[Dict[str, object]] = []
        for path, result in zip(normalized, results):
            if isinstance(result, Exception):
                payloads.append(
                    {
                        "path": path,
                        "repo_id": None,
                        "kind": None,
                        "total": 0,
                        "items": [],
                        "error": str(result),
                    }
                )
                continue
            payloads.append(result)
        return cls.merge_search_payloads(payloads, top_k=top_k, query_fields=query_fields)
