import json
import logging
import re
from typing import Any
import aiohttp
from ..models import SkillBundle, SkillMeta
from ..utils import content_hash, skill_description_from_bundle, skill_name_from_bundle
from .base import SkillSource
from .github import GitHubSkillSource

BASE_URL = "https://skills.sh"
SEARCH_URL = f"{BASE_URL}/api/search"
MIN_QUERY_LEN = 2
SKILL_LINK_RE = re.compile(
    r'href="/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)"'
)
INSTALL_CMD_RE = re.compile(
    r"npx\s+skills\s+add\s+(?P<repo>https?://github\.com/[^\s<`]+|[^\s<`]+)"
    r"(?:\s+--skill\s+(?P<skill>[^\s<`]+))?",
    re.IGNORECASE,
)
PROSE_SUMMARY_RE = re.compile(
    r'<div[^>]*class=["\'][^"\']*prose[^"\']*["\'][^>]*>.*?<p[^>]*>(?P<body>.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)


def normalize_skills_sh_path(raw: str) -> str:
    text = (raw or "").strip().strip("/")
    if not text:
        raise ValueError("skills.sh identifier path is required")
    for prefix in ("skills-sh:", "skills.sh:", "skills_sh:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):].strip().strip("/")
            break
    if text.startswith("http"):
        marker = "skills.sh/"
        idx = text.find(marker)
        if idx >= 0:
            text = text[idx + len(marker):].strip("/")
    parts = [p for p in text.split("/") if p]
    if len(parts) < 3:
        raise ValueError("skills.sh path must be owner/repo/skill")
    return "/".join(parts)


def wrap_identifier(path: str) -> str:
    return f"skills-sh:{normalize_skills_sh_path(path)}"


class SkillsShSkillSource(SkillSource):
    source_id = "skills-sh"

    def __init__(self) -> None:
        self._github = GitHubSkillSource()

    async def search(self, query: str, *, limit: int = 50, offset: int = 0) -> list[SkillMeta]:
        q = (query or "").strip()
        cap = max(1, min(limit, 500))
        start = max(0, offset)
        if not q:
            items = await self._browse_catalog(cap=cap, offset=start)
            return items
        if len(q) < MIN_QUERY_LEN:
            raise ValueError(f"skills.sh 搜索关键词至少需要 {MIN_QUERY_LEN} 个字符")
        try:
            async with aiohttp.ClientSession() as session:
                api_limit = min(start + cap, 50)
                async with session.get(
                    SEARCH_URL,
                    params={"q": q, "limit": api_limit},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        message = self._api_error_message(body) or f"skills.sh API HTTP {resp.status}"
                        raise ValueError(message)
                    data = json.loads(body) if body else {}
        except ValueError:
            raise
        except Exception as e:
            logging.warning("skills.sh search failed: %s", e)
            raise
        items = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        results: list[SkillMeta] = []
        for item in items:
            meta = self._meta_from_search_item(item)
            if meta:
                results.append(meta)
        return results[start:start + cap]

    async def _browse_catalog(self, *, cap: int, offset: int) -> list[SkillMeta]:
        merged: list[SkillMeta] = []
        seen: set[str] = set()
        for meta in await self._leaderboard_from_api(limit=50):
            if meta.identifier in seen:
                continue
            seen.add(meta.identifier)
            merged.append(meta)
        for meta in await self._browse_listing("/", limit=0):
            if meta.identifier in seen:
                continue
            seen.add(meta.identifier)
            merged.append(meta)
        end = offset + cap
        return merged[offset:end]

    async def _leaderboard_from_api(self, *, limit: int = 50) -> list[SkillMeta]:
        api_cap = max(1, min(limit, 50))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    SEARCH_URL,
                    params={"q": "skill", "limit": api_cap},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        return []
                    data = json.loads(body) if body else {}
        except Exception as e:
            logging.warning("skills.sh leaderboard fetch failed: %s", e)
            return []
        items = data.get("skills") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        results: list[SkillMeta] = []
        for item in items:
            meta = self._meta_from_search_item(item)
            if meta:
                results.append(meta)
        return results

    async def _browse_listing(self, page_path: str, *, limit: int) -> list[SkillMeta]:
        path = (page_path or "/").strip() or "/"
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{BASE_URL}{path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        raise ValueError(f"skills.sh 目录页不可用 (HTTP {resp.status})")
                    html = await resp.text()
        except ValueError:
            raise
        except Exception as e:
            logging.warning("skills.sh listing fetch failed for %s: %s", url, e)
            raise ValueError(f"skills.sh 目录页获取失败: {e}") from e
        seen: set[str] = set()
        results: list[SkillMeta] = []
        browse_rank = 0
        for match in SKILL_LINK_RE.finditer(html):
            canonical = match.group(1).strip("/")
            if not canonical or canonical in seen:
                continue
            seen.add(canonical)
            try:
                canonical = normalize_skills_sh_path(canonical)
            except ValueError:
                continue
            browse_rank += 1
            meta = self._meta_from_canonical(canonical, browse_rank=browse_rank)
            if meta:
                results.append(meta)
            if limit > 0 and len(results) >= limit:
                break
        return results

    @staticmethod
    def _api_error_message(body: str) -> str | None:
        text = (body or "").strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text[:200]
        if not isinstance(data, dict):
            return None
        err = data.get("error") or data.get("message")
        if isinstance(err, str) and err.strip():
            return err.strip()
        return None

    def _meta_from_canonical(
        self,
        canonical: str,
        *,
        installs: int | None = None,
        browse_rank: int | None = None,
        name: str | None = None,
    ) -> SkillMeta | None:
        try:
            canonical = normalize_skills_sh_path(canonical)
        except ValueError:
            return None
        parts = canonical.split("/", 2)
        repo_slug = f"{parts[0]}/{parts[1]}"
        skill_path = parts[2]
        skill_name = (name or skill_path.split("/")[-1]).strip()
        installs_hint = ""
        if isinstance(installs, int):
            installs_hint = f" · {installs:,} installs"
        description = f"Indexed by skills.sh from {repo_slug}{installs_hint}"
        return SkillMeta(
            name=skill_name,
            description=description,
            source=self.source_id,
            identifier=wrap_identifier(canonical),
            trust_level=self._github.trust_level_for(repo_slug),
            repo=repo_slug,
            path=skill_path,
            extra={
                "installs": installs,
                "browse_rank": browse_rank,
                "detail_url": f"{BASE_URL}/{canonical}",
                "repo_url": f"https://github.com/{repo_slug}",
            },
        )

    def _meta_from_search_item(self, item: dict[str, Any]) -> SkillMeta | None:
        if not isinstance(item, dict):
            return None
        canonical = str(item.get("id") or "").strip()
        repo = str(item.get("source") or "").strip()
        skill_id = str(item.get("skillId") or "").strip()
        if not canonical or canonical.count("/") < 2:
            if repo and skill_id:
                canonical = f"{repo}/{skill_id}"
            else:
                return None
        try:
            canonical = normalize_skills_sh_path(canonical)
        except ValueError:
            return None
        parts = canonical.split("/", 2)
        repo_slug = f"{parts[0]}/{parts[1]}"
        skill_path = parts[2]
        name = str(item.get("name") or skill_path.split("/")[-1]).strip()
        installs = item.get("installs")
        installs_hint = ""
        if isinstance(installs, int):
            installs_hint = f" · {installs:,} installs"
        description = f"Indexed by skills.sh from {repo_slug}{installs_hint}"
        return SkillMeta(
            name=name,
            description=description,
            source=self.source_id,
            identifier=wrap_identifier(canonical),
            trust_level=self._github.trust_level_for(repo_slug),
            repo=repo_slug,
            path=skill_path,
            extra={
                "installs": installs,
                "detail_url": f"{BASE_URL}/{canonical}",
                "repo_url": f"https://github.com/{repo_slug}",
            },
        )

    async def inspect(self, identifier_path: str) -> SkillMeta | None:
        try:
            canonical = normalize_skills_sh_path(identifier_path)
        except ValueError:
            return None
        detail = await self._fetch_detail_page(canonical)
        parts = canonical.split("/", 2)
        repo_slug = f"{parts[0]}/{parts[1]}"
        skill_path = parts[2]
        name = skill_path.split("/")[-1]
        description = f"Indexed by skills.sh from {repo_slug}"
        if isinstance(detail, dict):
            summary = str(detail.get("summary") or "").strip()
            if summary:
                description = summary[:500]
            title = str(detail.get("title") or "").strip()
            if title:
                name = title
        meta = SkillMeta(
            name=name,
            description=description,
            source=self.source_id,
            identifier=wrap_identifier(canonical),
            trust_level=self._github.trust_level_for(repo_slug),
            repo=repo_slug,
            path=skill_path,
            extra={
                "detail_url": f"{BASE_URL}/{canonical}",
                "repo_url": f"https://github.com/{repo_slug}",
            },
        )
        if isinstance(detail, dict):
            meta.extra.update(
                {
                    "install_command": detail.get("install_command"),
                    "github_path": detail.get("github_path"),
                }
            )
        return meta

    async def fetch_skill_md(self, identifier_path: str) -> str | None:
        try:
            canonical = normalize_skills_sh_path(identifier_path)
        except ValueError:
            return None
        detail = await self._fetch_detail_page(canonical)
        parts = canonical.split("/", 2)
        repo_slug = f"{parts[0]}/{parts[1]}"
        skill_name = parts[2].split("/")[-1]
        async with aiohttp.ClientSession() as session:
            found = await self._github._find_skill_md_path(session, repo_slug, skill_name)
            if found is not None:
                github_path = repo_slug if found == "" else f"{repo_slug}/{found}"
                content = await self._github.fetch_skill_md(github_path)
                if content:
                    return content
        for github_path in self._github_path_candidates(canonical, detail):
            content = await self._github.fetch_skill_md(github_path)
            if content:
                return content
        return None

    async def fetch_bundle(self, identifier_path: str) -> SkillBundle | None:
        try:
            canonical = normalize_skills_sh_path(identifier_path)
        except ValueError:
            return None
        detail = await self._fetch_detail_page(canonical)
        parts = canonical.split("/", 2)
        repo_slug = f"{parts[0]}/{parts[1]}"
        skill_name = parts[2].split("/")[-1]
        async with aiohttp.ClientSession() as session:
            candidates = self._github_path_candidates(canonical, detail)
            found = await self._github._find_skill_md_path(session, repo_slug, skill_name)
            if found is not None:
                tree_path = repo_slug if found == "" else f"{repo_slug}/{found}"
                if tree_path not in candidates:
                    candidates.insert(0, tree_path)
                else:
                    candidates.remove(tree_path)
                    candidates.insert(0, tree_path)
            resolved = await self._github.find_skill_path_by_frontmatter_name(
                session,
                repo_slug,
                skill_name,
            )
            if resolved is not None:
                resolved_path = repo_slug if resolved == "" else f"{repo_slug}/{resolved}"
                if resolved_path not in candidates:
                    candidates.insert(0, resolved_path)
                else:
                    candidates.remove(resolved_path)
                    candidates.insert(0, resolved_path)
            for github_path in candidates:
                bundle = await self._github.fetch_bundle(github_path)
                if bundle is None:
                    continue
                return self._wrap_skills_sh_bundle(
                    bundle,
                    canonical=canonical,
                    github_path=github_path,
                )
        return None

    def _wrap_skills_sh_bundle(
        self,
        bundle: SkillBundle,
        *,
        canonical: str,
        github_path: str,
    ) -> SkillBundle:
        skill_name = bundle.name
        identifier = wrap_identifier(canonical)
        desc = skill_description_from_bundle(bundle.files, skill_name)
        return SkillBundle(
            name=skill_name,
            files=bundle.files,
            source=self.source_id,
            identifier=identifier,
            trust_level=bundle.trust_level,
            metadata={
                "content_hash": content_hash(bundle.files),
                "skills_sh_id": canonical,
                "github_path": github_path,
                "detail_url": f"{BASE_URL}/{canonical}",
                "description": desc,
            },
        )

    def _github_path_candidates(self, canonical: str, detail: dict[str, Any] | None) -> list[str]:
        parts = canonical.split("/", 2)
        repo_slug = f"{parts[0]}/{parts[1]}"
        skill_token = parts[2]
        skill_name = skill_token.split("/")[-1]
        candidates: list[str] = []
        seen: set[str] = set()

        def add(path: str, *, priority: bool = False) -> None:
            key = path.strip("/")
            if not key or key in seen:
                return
            seen.add(key)
            if priority:
                candidates.insert(0, key)
            else:
                candidates.append(key)

        def add_variants(repo: str, token: str, *, priority: bool = False) -> None:
            add(f"{repo}/skills/{token}", priority=priority)
            add(f"{repo}/.agents/skills/{token}", priority=priority)
            if token.startswith("vercel-"):
                short = token[len("vercel-"):]
                add(f"{repo}/skills/{short}", priority=True)
                add(f"{repo}/.agents/skills/{short}", priority=True)
            add(f"{repo}/{token}")

        if isinstance(detail, dict):
            install_skill = str(detail.get("install_skill") or "").strip()
            install_repo = str(detail.get("repo") or repo_slug).strip()
            if install_skill:
                add_variants(install_repo, install_skill)
            gh = str(detail.get("github_path") or "").strip().strip("/")
            if gh:
                add(gh)
        add_variants(repo_slug, skill_name)
        if self._github._repo_basename_matches_skill(repo_slug, skill_name):
            add(repo_slug, priority=True)
        if skill_token != skill_name:
            add(f"{repo_slug}/{skill_token}")
        return candidates

    async def _fetch_detail_page(self, canonical: str) -> dict[str, Any] | None:
        url = f"{BASE_URL}/{canonical}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return None
                    html = await resp.text()
        except Exception as e:
            logging.warning("skills.sh detail fetch failed for %s: %s", canonical, e)
            return None
        return self._parse_detail_page(canonical, html)

    def _parse_detail_page(self, canonical: str, html: str) -> dict[str, Any]:
        parts = canonical.split("/", 2)
        repo_slug = f"{parts[0]}/{parts[1]}"
        skill_token = parts[2]
        install_skill = skill_token.split("/")[-1]
        repo = repo_slug
        install_command = None
        match = INSTALL_CMD_RE.search(html)
        if match:
            install_command = match.group(0).strip()
            repo_value = (match.group("repo") or "").strip()
            install_skill = (match.group("skill") or install_skill).strip()
            repo = self._extract_repo_slug(repo_value) or repo
        summary_match = PROSE_SUMMARY_RE.search(html)
        summary = ""
        if summary_match:
            summary = re.sub(r"<[^>]+>", "", summary_match.group("body"))
            summary = re.sub(r"\s+", " ", summary).strip()
        github_path = f"{repo}/skills/{install_skill}" if install_command else f"{repo}/{install_skill}"
        return {
            "repo": repo,
            "install_skill": install_skill,
            "github_path": github_path,
            "install_command": install_command,
            "summary": summary,
            "detail_url": f"{BASE_URL}/{canonical}",
        }

    @staticmethod
    def _extract_repo_slug(repo_value: str) -> str | None:
        text = (repo_value or "").strip().rstrip("/")
        if not text:
            return None
        if "github.com/" in text:
            tail = text.split("github.com/", 1)[1]
            parts = [p for p in tail.split("/") if p]
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
            return None
        parts = [p for p in text.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return None
