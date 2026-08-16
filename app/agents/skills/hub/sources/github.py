import os
from pathlib import PurePosixPath
from typing import Any
import aiohttp
from app.config.settings import settings
from ..constants import TRUSTED_REPOS
from ..taps import get_search_repos
from ..models import SkillBundle, SkillMeta
from ..utils import content_hash, skill_description_from_bundle, skill_name_from_bundle
from .base import SkillSource

_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".bin", ".wasm",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".pyc", ".class", ".o", ".a",
})

class GitHubSkillSource(SkillSource):
    source_id = "github"

    def __init__(self) -> None:
        self._tree_cache: dict[str, tuple[str, list[dict[str, Any]]]] = {}

    def _github_token(self) -> str:
        return (
            settings.github_token
            or settings.gh_token
            or os.getenv("GITHUB_TOKEN")
            or os.getenv("GH_TOKEN")
            or ""
        ).strip()

    def _headers(self) -> dict[str, str]:
        token = self._github_token()
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def trust_level_for(self, repo: str) -> str:
        return "trusted" if repo in TRUSTED_REPOS else "community"

    @staticmethod
    def _is_binary_path(path: str) -> bool:
        name = PurePosixPath(path.replace("\\", "/")).name.lower()
        if not name or name.startswith("."):
            return True
        suffix = PurePosixPath(name).suffix
        return suffix in _BINARY_EXTENSIONS

    @staticmethod
    def _decode_raw_bytes(data: bytes) -> str | None:
        if not data:
            return ""
        if b"\x00" in data[:8192]:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return data.decode("utf-8-sig")
            except UnicodeDecodeError:
                return data.decode("utf-8", errors="replace")

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        raw: bool = False,
        allow_404: bool = False,
    ) -> Any:
        headers = self._headers()
        if raw:
            headers = {**headers, "Accept": "application/vnd.github.v3.raw"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 404 and allow_404:
                return None
            if resp.status != 200:
                text = await resp.text()
                if resp.status == 403 and "rate limit" in text.lower():
                    if self._github_token():
                        raise RuntimeError(
                            "GitHub API 访问受限（已配置 Token 但仍触发限流）。"
                            "请稍后重试，或检查 Token 权限与额度。"
                        )
                    raise RuntimeError(
                        "GitHub API 访问受限（未配置 Token 或已触发限流）。"
                        "请在 env 文件中设置 GITHUB_TOKEN 或 GH_TOKEN（Personal Access Token，无需特殊权限）后重启后端服务。"
                    )
                raise RuntimeError(f"GitHub API {resp.status}: {text[:300]}")
            if raw:
                return self._decode_raw_bytes(await resp.read())
            return await resp.json()

    async def _get_repo_tree(self, session: aiohttp.ClientSession, repo: str) -> tuple[str, list[dict[str, Any]]] | None:
        if repo in self._tree_cache:
            return self._tree_cache[repo]
        repo_data = await self._get_json(session, f"https://api.github.com/repos/{repo}")
        branch = str(repo_data.get("default_branch") or "main")
        tree_data = await self._get_json(
            session,
            f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1",
        )
        if tree_data.get("truncated"):
            raise RuntimeError(
                f"GitHub 仓库 {repo} 文件树过大（API 已截断）。"
                f"请用完整路径安装，例如 github:{repo}/path/to/skill"
            )
        entries = tree_data.get("tree") or []
        if not isinstance(entries, list):
            return None
        self._tree_cache[repo] = (branch, entries)
        return branch, entries

    async def _download_directory(self, session: aiohttp.ClientSession, repo: str, path: str) -> dict[str, str]:
        path = path.strip("/")
        cached = await self._get_repo_tree(session, repo)
        if cached is not None:
            _branch, entries = cached
            prefix = f"{path}/" if path else ""
            files: dict[str, str] = {}
            for item in entries:
                if item.get("type") != "blob":
                    continue
                item_path = str(item.get("path") or "")
                if path and not item_path.startswith(prefix):
                    continue
                if not path and "/" in item_path:
                    continue
                rel = item_path[len(prefix):] if prefix else item_path
                if not rel or rel.startswith(".") or self._is_binary_path(rel):
                    continue
                try:
                    content = await self._get_json(
                        session,
                        f"https://api.github.com/repos/{repo}/contents/{item_path}",
                        raw=True,
                        allow_404=True,
                    )
                except (RuntimeError, UnicodeDecodeError):
                    continue
                if isinstance(content, str):
                    files[rel] = content
            if files:
                return files
        return await self._download_directory_recursive(session, repo, path)

    async def _download_directory_recursive(
        self,
        session: aiohttp.ClientSession,
        repo: str,
        path: str,
    ) -> dict[str, str]:
        url = f"https://api.github.com/repos/{repo}/contents/{path.strip('/')}"
        entries = await self._get_json(session, url, allow_404=True)
        if entries is None or not isinstance(entries, list):
            return {}
        files: dict[str, str] = {}
        for entry in entries:
            name = str(entry.get("name") or "")
            entry_type = str(entry.get("type") or "")
            entry_path = str(entry.get("path") or "")
            if entry_type == "file":
                if self._is_binary_path(name):
                    continue
                try:
                    content = await self._get_json(
                        session,
                        f"https://api.github.com/repos/{repo}/contents/{entry_path}",
                        raw=True,
                        allow_404=True,
                    )
                except (RuntimeError, UnicodeDecodeError):
                    continue
                if isinstance(content, str):
                    files[name] = content
            elif entry_type == "dir":
                sub = await self._download_directory_recursive(session, repo, entry_path)
                for sub_name, sub_content in sub.items():
                    files[f"{name}/{sub_name}"] = sub_content
        return files

    def _parse_repo_path(self, identifier_path: str) -> tuple[str, str, str]:
        parts = [p for p in identifier_path.split("/") if p]
        if len(parts) < 2:
            raise ValueError("github identifier must be owner/repo[/path/to/skill]")
        repo = f"{parts[0]}/{parts[1]}"
        if len(parts) == 2:
            return repo, "", parts[1]
        skill_path = "/".join(parts[2:])
        skill_name = parts[-1]
        return repo, skill_path, skill_name

    _SKILL_DIR_PREFIX_STRIPS: tuple[str, ...] = ("vercel-",)

    @classmethod
    def _logical_name_variants(cls, logical_name: str) -> list[str]:
        key = (logical_name or "").strip()
        if not key:
            return []
        variants: list[str] = [key]
        seen: set[str] = {key}
        for prefix in cls._SKILL_DIR_PREFIX_STRIPS:
            if key.startswith(prefix):
                short = key[len(prefix):]
                if short and short not in seen:
                    seen.add(short)
                    variants.append(short)
        return variants

    @staticmethod
    def _skill_md_rel_path(skill_path: str) -> str:
        path = (skill_path or "").strip("/")
        return "SKILL.md" if not path else f"{path}/SKILL.md"

    def _repo_basename_matches_skill(self, repo: str, skill_name: str) -> bool:
        repo_base = repo.split("/")[-1]
        variants = {variant.lower() for variant in self._logical_name_variants(skill_name)}
        return repo_base.lower() in variants

    @staticmethod
    def _tree_has_root_skill_md(entries: list[dict[str, Any]]) -> bool:
        return any(
            item.get("type") == "blob" and str(item.get("path") or "") == "SKILL.md"
            for item in entries
        )

    async def _find_skill_md_path(self, session: aiohttp.ClientSession, repo: str, skill_name: str) -> str | None:
        cached = await self._get_repo_tree(session, repo)
        if cached is None:
            return None
        _branch, entries = cached
        for variant in self._logical_name_variants(skill_name):
            suffix = f"/{variant}/SKILL.md"
            for item in entries:
                if item.get("type") != "blob":
                    continue
                path = str(item.get("path") or "")
                if path.endswith(suffix) or path == f"{variant}/SKILL.md":
                    return path[: -len("/SKILL.md")]
        if self._tree_has_root_skill_md(entries) and self._repo_basename_matches_skill(repo, skill_name):
            return ""
        return None

    async def find_skill_path_by_frontmatter_name(
        self,
        session: aiohttp.ClientSession,
        repo: str,
        logical_name: str,
    ) -> str | None:
        """skills.sh slug 与目录名不一致时，按目录名变体或 SKILL.md frontmatter name 定位路径。"""
        key = (logical_name or "").strip()
        if not key:
            return None
        paths = await self._discover_skill_paths_from_tree(session, repo)
        variants = set(self._logical_name_variants(key))
        for skill_path in paths:
            if not skill_path:
                if self._repo_basename_matches_skill(repo, key):
                    return ""
                continue
            dir_name = skill_path.rstrip("/").split("/")[-1]
            if dir_name in variants:
                return skill_path
        from ...frontmatter import SkillFrontmatter
        for skill_path in paths:
            md_path = self._skill_md_rel_path(skill_path)
            try:
                content = await self._get_json(
                    session,
                    f"https://api.github.com/repos/{repo}/contents/{md_path}",
                    raw=True,
                    allow_404=True,
                )
            except (RuntimeError, UnicodeDecodeError):
                continue
            if not isinstance(content, str):
                continue
            fm = SkillFrontmatter.parse(content)
            if fm and SkillFrontmatter.name(fm) == key:
                return skill_path
        return None

    def _skill_path_matches_filter(self, skill_path: str, filter_text: str) -> bool:
        if not filter_text:
            return True
        needle = filter_text.lower()
        segments = [segment.lower() for segment in skill_path.split("/") if segment]
        return needle in skill_path.lower() or any(needle in segment for segment in segments)

    async def _discover_skill_paths_from_tree(
        self,
        session: aiohttp.ClientSession,
        repo: str,
    ) -> list[str]:
        try:
            cached = await self._get_repo_tree(session, repo)
        except RuntimeError:
            return []
        if cached is None:
            return []
        _branch, entries = cached
        paths: list[str] = []
        if self._tree_has_root_skill_md(entries):
            paths.append("")
        for item in entries:
            if item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            if not path.endswith("/SKILL.md"):
                continue
            skill_path = path[: -len("/SKILL.md")]
            if skill_path:
                paths.append(skill_path)
        paths.sort()
        return paths

    async def _meta_from_skill_path(
        self,
        session: aiohttp.ClientSession,
        repo: str,
        skill_path: str,
    ) -> SkillMeta | None:
        skill_md_path = self._skill_md_rel_path(skill_path)
        try:
            content = await self._get_json(
                session,
                f"https://api.github.com/repos/{repo}/contents/{skill_md_path}",
                raw=True,
            )
        except RuntimeError:
            return None
        if not isinstance(content, str):
            return None
        files = {"SKILL.md": content}
        fallback_name = skill_path.rstrip("/").split("/")[-1]
        try:
            skill_name = skill_name_from_bundle(files, fallback_name)
        except ValueError:
            return None
        desc = skill_description_from_bundle(files, skill_name)
        identifier = f"github:{repo}/{skill_path}"
        return SkillMeta(
            name=skill_name,
            description=desc,
            source=self.source_id,
            identifier=identifier,
            trust_level=self.trust_level_for(repo),
            repo=repo,
            path=skill_path,
        )

    async def search(self, query: str, *, limit: int = 50, offset: int = 0) -> list[SkillMeta]:
        q = (query or "").strip()
        start = max(0, offset)
        page_size = max(1, limit)
        fetch_cap = min(start + page_size, 500)
        if "/" in q and not q.startswith("http"):
            parts = [p for p in q.split("/") if p]
            if len(parts) >= 2:
                repo = f"{parts[0]}/{parts[1]}"
                filter_text = parts[-1].lower() if len(parts) > 2 else ""
                items = await self._list_repo_skills(repo, filter_text=filter_text, limit=fetch_cap)
                return items[start:start + page_size]
        items: list[SkillMeta] = []
        seen: set[str] = set()
        filter_text = "" if not q else q.lower()
        async with aiohttp.ClientSession() as session:
            for repo in sorted(get_search_repos()):
                found = await self._list_repo_skills(
                    repo,
                    filter_text=filter_text,
                    limit=fetch_cap,
                    session=session,
                )
                for meta in found:
                    if meta.identifier in seen:
                        continue
                    seen.add(meta.identifier)
                    items.append(meta)
                    if len(items) >= fetch_cap:
                        break
                if len(items) >= fetch_cap:
                    break
        return items[start:start + page_size]

    async def _list_repo_skills(
        self,
        repo: str,
        *,
        filter_text: str = "",
        limit: int = 50,
        session: aiohttp.ClientSession | None = None,
    ) -> list[SkillMeta]:
        async def _append_from_paths(
            sess: aiohttp.ClientSession,
            skill_paths: list[str],
            *,
            items: list[SkillMeta],
            seen: set[str],
        ) -> None:
            for skill_path in skill_paths:
                if not self._skill_path_matches_filter(skill_path, filter_text):
                    continue
                identifier = f"github:{repo}/{skill_path}"
                if identifier in seen:
                    continue
                meta = await self._meta_from_skill_path(sess, repo, skill_path)
                if meta:
                    seen.add(meta.identifier)
                    items.append(meta)
                if len(items) >= limit:
                    return

        async def _run(sess: aiohttp.ClientSession) -> list[SkillMeta]:
            items: list[SkillMeta] = []
            seen: set[str] = set()
            tree_paths = await self._discover_skill_paths_from_tree(sess, repo)
            if tree_paths:
                await _append_from_paths(sess, tree_paths, items=items, seen=seen)
                if items:
                    return items
            url = f"https://api.github.com/repos/{repo}/contents/"
            try:
                entries = await self._get_json(sess, url)
            except RuntimeError:
                return items
            if not isinstance(entries, list):
                return items
            for entry in entries:
                if entry.get("type") != "dir":
                    continue
                dir_name = str(entry.get("name") or "")
                if dir_name.startswith((".", "_")):
                    continue
                if filter_text and filter_text not in dir_name.lower():
                    continue
                try:
                    meta = await self.inspect(f"{repo}/{dir_name}", session=sess)
                except (RuntimeError, UnicodeDecodeError):
                    continue
                if meta and meta.identifier not in seen:
                    seen.add(meta.identifier)
                    items.append(meta)
                if len(items) >= limit:
                    break
            return items

        if session is not None:
            return await _run(session)
        async with aiohttp.ClientSession() as sess:
            return await _run(sess)

    async def inspect(self, identifier_path: str, *, session: aiohttp.ClientSession | None = None) -> SkillMeta | None:
        async def _run(sess: aiohttp.ClientSession) -> SkillMeta | None:
            try:
                repo, skill_path, fallback_name = self._parse_repo_path(identifier_path)
            except ValueError:
                return None
            if not skill_path:
                return None
            files = await self._download_directory(sess, repo, skill_path)
            if "SKILL.md" not in files:
                resolved = await self._find_skill_md_path(sess, repo, fallback_name)
                if resolved is not None:
                    files = await self._download_directory(sess, repo, resolved)
                    skill_path = resolved
            if "SKILL.md" not in files:
                return None
            skill_name = skill_name_from_bundle(files, fallback_name)
            desc = skill_description_from_bundle(files, skill_name)
            identifier = f"github:{repo}/{skill_path}"
            return SkillMeta(
                name=skill_name,
                description=desc,
                source=self.source_id,
                identifier=identifier,
                trust_level=self.trust_level_for(repo),
                repo=repo,
                path=skill_path,
                extra={
                    "detail_url": f"https://github.com/{repo}/blob/HEAD/{skill_path}/SKILL.md",
                    "repo_url": f"https://github.com/{repo}",
                },
            )

        if session is not None:
            return await _run(session)
        async with aiohttp.ClientSession() as sess:
            return await _run(sess)

    async def fetch_skill_md(self, identifier_path: str) -> str | None:
        async with aiohttp.ClientSession() as session:
            try:
                repo, skill_path, fallback_name = self._parse_repo_path(identifier_path)
            except ValueError:
                return None
            if not skill_path:
                found = await self._find_skill_md_path(session, repo, fallback_name)
                if found is None:
                    return None
                skill_path = found
            skill_md_path = self._skill_md_rel_path(skill_path)
            content = await self._get_json(
                session,
                f"https://api.github.com/repos/{repo}/contents/{skill_md_path}",
                raw=True,
                allow_404=True,
            )
            return content if isinstance(content, str) else None

    async def fetch_bundle(self, identifier_path: str) -> SkillBundle | None:
        async with aiohttp.ClientSession() as session:
            try:
                repo, skill_path, fallback_name = self._parse_repo_path(identifier_path)
            except ValueError:
                return None
            if not skill_path:
                found = await self._find_skill_md_path(session, repo, fallback_name)
                if found is not None:
                    skill_path = found
                else:
                    return None
            files = await self._download_directory(session, repo, skill_path)
            if "SKILL.md" not in files and skill_path:
                root_files = await self._download_directory(session, repo, "")
                if "SKILL.md" in root_files:
                    files = root_files
                    skill_path = ""
            if "SKILL.md" not in files:
                return None
            skill_name = skill_name_from_bundle(files, fallback_name)
            identifier = f"github:{repo}/{skill_path}"
            return SkillBundle(
                name=skill_name,
                files=files,
                source=self.source_id,
                identifier=identifier,
                trust_level=self.trust_level_for(repo),
                metadata={"content_hash": content_hash(files), "repo": repo, "path": skill_path},
            )
