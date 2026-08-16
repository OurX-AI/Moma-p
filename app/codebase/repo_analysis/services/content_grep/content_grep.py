from __future__ import annotations
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Pattern, Sequence, Set
from ..repo_path_ignore import RepoPathIgnore
from app.utils.common import normalize_path


@dataclass(frozen=True)
class _PreparedTerm:
    """预编译检索词，避免逐行重复编译正则。"""

    raw: str
    lower: str
    is_identifier: bool
    ident_pattern: Optional[Pattern[str]]


class ContentGrepService:
    """仓内全文/标识符 grep：对标 Instant Grep，供 resolve 并联召回。"""

    MAX_FILES_SCANNED = 8000
    MAX_HITS = 40
    MAX_FILE_BYTES = 1_500_000
    MAX_TERM_LEN = 80
    MIN_TERM_LEN = 2
    # 已有足够强命中时可提前结束全仓扫描
    EARLY_STOP_STRONG_MIN = 8
    EARLY_STOP_STRONG_SCORE = 2.5
    EARLY_STOP_MIN_SCANNED = 80

    @classmethod
    def search(
        cls,
        repo_root: str,
        terms: Sequence[str],
        *,
        extensions: Optional[Set[str]] = None,
        top_k: int = 10,
        builtin_dir_names: Optional[Set[str]] = None,
    ) -> List[Dict[str, object]]:
        root = os.path.abspath(os.path.normpath(repo_root or ""))
        if not root or not os.path.isdir(root):
            return []
        prepared = cls._prepare_terms(terms)
        if not prepared:
            return []
        ext_set = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in (extensions or set())}
        ignorer = RepoPathIgnore.load(root, builtin_dir_names=builtin_dir_names)
        best: Dict[str, Dict[str, object]] = {}
        scanned = 0

        for parent, dirs, files in os.walk(root):
            ignorer.filter_walk_dirs(parent, dirs)
            for name in files:
                if scanned >= cls.MAX_FILES_SCANNED:
                    break
                abs_path = os.path.join(parent, name)
                rel = normalize_path(os.path.relpath(abs_path, root))
                if ignorer.should_ignore_file(rel):
                    continue
                if ext_set:
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in ext_set:
                        continue
                scanned += 1
                hit = cls._scan_file(abs_path, rel, prepared)
                if not hit:
                    continue
                prev = best.get(rel)
                if prev is None or float(hit["score"]) > float(prev["score"]):
                    best[rel] = hit
                if cls._should_early_stop(best, scanned):
                    ranked = sorted(
                        best.values(),
                        key=lambda it: (-float(it["score"]), str(it["file_path"])),
                    )
                    return ranked[: max(1, min(top_k, cls.MAX_HITS))]
            if scanned >= cls.MAX_FILES_SCANNED:
                break

        ranked = sorted(best.values(), key=lambda it: (-float(it["score"]), str(it["file_path"])))
        return ranked[: max(1, min(top_k, cls.MAX_HITS))]

    @classmethod
    def _should_early_stop(cls, best: Dict[str, Dict[str, object]], scanned: int) -> bool:
        if scanned < cls.EARLY_STOP_MIN_SCANNED:
            return False
        strong = sum(
            1 for it in best.values() if float(it.get("score") or 0) >= cls.EARLY_STOP_STRONG_SCORE
        )
        return strong >= cls.EARLY_STOP_STRONG_MIN

    @classmethod
    def _normalize_terms(cls, terms: Sequence[str]) -> List[str]:
        return [p.raw for p in cls._prepare_terms(terms)]

    @classmethod
    def _prepare_terms(cls, terms: Sequence[str]) -> List[_PreparedTerm]:
        out: List[_PreparedTerm] = []
        seen: Set[str] = set()
        for raw in terms:
            t = " ".join(str(raw or "").split()).strip()
            if not t:
                continue
            if len(t) < cls.MIN_TERM_LEN or len(t) > cls.MAX_TERM_LEN:
                continue
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            is_ident = bool(t.isascii() and re.search(r"^[A-Za-z_][A-Za-z0-9_]*$", t))
            pattern = None
            if is_ident:
                pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(t)}(?![A-Za-z0-9_])")
            out.append(
                _PreparedTerm(
                    raw=t,
                    lower=key,
                    is_identifier=is_ident,
                    ident_pattern=pattern,
                )
            )
            if len(out) >= 12:
                break
        return out

    @classmethod
    def _scan_file(
        cls,
        abs_path: str,
        rel_path: str,
        terms: Sequence[_PreparedTerm],
    ) -> Optional[Dict[str, object]]:
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            return None
        if size <= 0 or size > cls.MAX_FILE_BYTES:
            return None
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            return None
        if not text:
            return None

        best_score = 0.0
        best_line = 0
        best_term = ""
        best_snippet = ""
        lines = text.splitlines()
        for idx, line in enumerate(lines, start=1):
            for term in terms:
                score = cls._line_score(line, term)
                if score <= best_score:
                    continue
                best_score = score
                best_line = idx
                best_term = term.raw
                best_snippet = line.strip()[:240]

        if best_score <= 0:
            stem = os.path.splitext(os.path.basename(rel_path))[0].lower()
            for term in terms:
                tl = term.lower
                if len(tl) >= 3 and (tl == stem or tl in stem or stem in tl):
                    return {
                        "file_path": rel_path.replace("\\", "/"),
                        "score": 1.2,
                        "match_source": "grep",
                        "channel": "grep",
                        "start_line": 1,
                        "end_line": 1,
                        "grep_term": term.raw,
                        "snippet": "",
                    }
            return None

        return {
            "file_path": rel_path.replace("\\", "/"),
            "score": best_score,
            "match_source": "grep",
            "channel": "grep",
            "start_line": best_line,
            "end_line": best_line,
            "grep_term": best_term,
            "snippet": best_snippet,
        }

    @classmethod
    def _line_score(cls, line: str, term: _PreparedTerm) -> float:
        if not line or not term.raw:
            return 0.0
        if term.is_identifier and term.ident_pattern is not None:
            if term.ident_pattern.search(line):
                return 2.8
            if term.lower in line.lower():
                return 1.6
            return 0.0
        if term.raw in line:
            return 2.4
        if term.lower in line.lower():
            return 1.8
        return 0.0
