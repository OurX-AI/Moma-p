from __future__ import annotations
from typing import Dict, Iterable, List, Sequence, Set


class DirSiblingExpander:
    """定位命中后，把同目录其他已索引文件补进 also_consider，防漏改兄弟模块。"""

    MAX_PER_DIR = 4
    MAX_TOTAL_ADD = 8
    SIBLING_SCORE = 1.35

    @staticmethod
    def parent_dir(file_path: object) -> str:
        fp = str(file_path or "").replace("\\", "/").strip()
        if "/" not in fp:
            return ""
        return fp.rsplit("/", 1)[0]

    @classmethod
    def expand(
        cls,
        *,
        primary: Sequence[Dict[str, object]],
        also: Sequence[Dict[str, object]],
        candidate_paths: Iterable[str],
    ) -> List[Dict[str, object]]:
        """在 also 基础上追加同目录兄弟路径（不进入 primary）。"""
        out = [dict(it) for it in also]
        seen: Set[str] = set()
        for it in list(primary) + out:
            fp = str(it.get("file_path") or "").replace("\\", "/")
            if fp:
                seen.add(fp)

        seed_dirs: List[str] = []
        for it in primary:
            parent = cls.parent_dir(it.get("file_path"))
            if parent and parent not in seed_dirs:
                seed_dirs.append(parent)
        if not seed_dirs:
            return out

        by_dir: Dict[str, List[str]] = {d: [] for d in seed_dirs}
        for raw in candidate_paths:
            fp = str(raw or "").replace("\\", "/").strip()
            if not fp or fp in seen:
                continue
            parent = cls.parent_dir(fp)
            if parent not in by_dir:
                continue
            by_dir[parent].append(fp)

        added = 0
        for parent in seed_dirs:
            siblings = sorted(by_dir.get(parent) or [])
            per_dir = 0
            for fp in siblings:
                if added >= cls.MAX_TOTAL_ADD:
                    return out
                if per_dir >= cls.MAX_PER_DIR:
                    break
                if fp in seen:
                    continue
                seen.add(fp)
                out.append(
                    {
                        "file_path": fp,
                        "score": cls.SIBLING_SCORE,
                        "match_source": "dir_sibling",
                        "exact_tier": None,
                        "why": f"[related] 同目录兄弟：{fp}",
                    }
                )
                per_dir += 1
                added += 1
        return out
