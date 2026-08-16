import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any
from app.agents.contants import BUILTIN_SKILLS_DIR
from .agent_enable import enable_skill_for_agents, build_skill_enabled_agents_map
from .constants import AUDIT_LOG, QUARANTINE_DIR
from .preinstalled import invalidate_preinstalled_cache, is_preinstalled_skill
from .guard import format_scan_report, scan_skill_dir, should_allow_install
from .identifiers import normalize_identifier, parse_identifier
from .lock import (
    append_audit,
    get_lock_entry,
    get_lock_entry_by_identifier,
    list_installed,
    remove_lock_entry,
    upsert_lock_entry,
)
from .taps import add_github_tap, list_github_taps, remove_github_tap
from .models import SkillBundle, SkillMeta
from .sources import (
    BuiltinSkillSource,
    ClawHubSkillSource,
    GitHubSkillSource,
    LobeHubSkillSource,
    SkillsShSkillSource,
    WellKnownSkillSource,
)
from .wellknown_hosts import add_wellknown_host, list_wellknown_hosts, remove_wellknown_host
from .ranking import popularity_label, popularity_score, sort_skill_metas
from ..paths import DEFAULT_SKILL_CATEGORY, find_skill
from ..remarks import remove_skill_remark, skill_remark_payload
from .utils import (
    content_hash,
    copy_bundle_to_dir,
    remove_skill_directory,
    resolve_hub_skill_dir,
    resolve_install_dest,
    skill_name_from_bundle,
    write_bundle_to_dir,
)

ALL_SOURCE_ORDER = (
    "builtin",
    "github",
    "clawhub",
    "skills-sh",
    "well-known",
    "lobehub",
)
SOURCE_FETCH_CAPS: dict[str, int] = {
    "builtin": 10_000,
    "github": 500,
    "clawhub": 500,
    "skills-sh": 500,
    "well-known": 10_000,
    "lobehub": 500,
}
SEARCH_CACHE_TTL_SEC = 300


class SkillsHubService:
    def __init__(self) -> None:
        self._builtin = BuiltinSkillSource()
        self._github = GitHubSkillSource()
        self._clawhub = ClawHubSkillSource()
        self._skills_sh = SkillsShSkillSource()
        self._well_known = WellKnownSkillSource()
        self._lobehub = LobeHubSkillSource()
        self._search_cache: dict[tuple[str, str], tuple[float, list[SkillMeta]]] = {}

    def _normalize_source(self, source: str) -> str:
        src = (source or "all").strip().lower()
        if src == "bundled":
            return "builtin"
        if src == "skills.sh":
            return "skills-sh"
        if src == "wellknown":
            return "well-known"
        return src

    async def _collect_source_items(self, src: str, q: str) -> list[SkillMeta]:
        cap = SOURCE_FETCH_CAPS.get(src, 500)
        if src == "builtin":
            return self._builtin.search(q, limit=cap, offset=0)
        if src == "github":
            return await self._github.search(q, limit=cap, offset=0)
        if src == "clawhub":
            return await self._clawhub.search(q, limit=cap, offset=0)
        if src == "skills-sh":
            return await self._skills_sh.search(q, limit=cap, offset=0)
        if src == "well-known":
            return await self._well_known.search(q, limit=cap, offset=0)
        if src == "lobehub":
            return await self._lobehub.search(q, limit=cap, offset=0)
        raise ValueError(f"unsupported source: {src}")

    @staticmethod
    def _merge_dedupe(batches: list[list[SkillMeta]]) -> list[SkillMeta]:
        seen: set[str] = set()
        merged: list[SkillMeta] = []
        for batch in batches:
            for item in batch:
                key = item.identifier
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        return merged

    async def _get_catalog_items(self, src: str, q: str) -> list[SkillMeta]:
        key = (src, q)
        now = time.time()
        if src != "all":
            all_cached = self._search_cache.get(("all", q))
            if all_cached and now - all_cached[0] < SEARCH_CACHE_TTL_SEC:
                items = [m for m in all_cached[1] if (m.source or "unknown") == src]
                if items:
                    self._search_cache[key] = (now, items)
                    return items
        cached = self._search_cache.get(key)
        if cached and now - cached[0] < SEARCH_CACHE_TTL_SEC:
            if src == "all" or cached[1]:
                return cached[1]
        if src == "all":
            batches: list[list[SkillMeta]] = []
            for sub in ALL_SOURCE_ORDER:
                try:
                    batches.append(await self._collect_source_items(sub, q))
                except Exception as e:
                    logging.warning("%s skills search failed during all-source browse: %s", sub, e)
            items = self._merge_dedupe(batches)
        else:
            items = await self._collect_source_items(src, q)
        items = sort_skill_metas(items, query=q)
        self._search_cache[key] = (now, items)
        return items

    async def search(
        self,
        query: str = "",
        *,
        source: str = "all",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        q = (query or "").strip()
        src = self._normalize_source(source)
        page_size = max(1, min(limit, 100))
        start = max(0, offset)
        if src not in ("all", *ALL_SOURCE_ORDER):
            raise ValueError(f"unsupported source: {source}")
        try:
            catalog = await self._get_catalog_items(src, q)
        except Exception as e:
            if src in ("clawhub", "skills-sh", "well-known", "lobehub"):
                raise ValueError(f"{src} search failed: {e}") from e
            raise
        total = len(catalog)
        page = catalog[start:start + page_size]
        payload: dict[str, Any] = {
            "skills": [self._meta_to_dict(m, query=q) for m in page],
            "count": len(page),
            "total": total,
            "offset": start,
            "limit": page_size,
            "has_more": start + page_size < total,
            "query": q,
            "source": src,
        }
        if src == "all":
            counts: dict[str, int] = {}
            for item in catalog:
                key = item.source or "unknown"
                counts[key] = counts.get(key, 0) + 1
            payload["source_counts"] = counts
        return payload

    @staticmethod
    def _skill_md_preview_body(raw: str, *, limit: int = 4000) -> str:
        body = raw
        if raw.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", raw, re.DOTALL)
            if match:
                body = raw[match.end():].strip()
        return body[:limit]

    async def _fetch_preview_md(self, source: str, path: str, *, meta: SkillMeta | None = None) -> str | None:
        if source == "builtin":
            entry = find_skill(BUILTIN_SKILLS_DIR, path.strip())
            skill_dir = entry.abs_path if entry else None
            if skill_dir is None:
                rel = path.strip().replace("\\", "/")
                candidate = (BUILTIN_SKILLS_DIR / rel).resolve()
                if (candidate / "SKILL.md").is_file():
                    skill_dir = candidate
            if skill_dir and (skill_dir / "SKILL.md").is_file():
                return (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            return None
        if source == "github":
            return await self._github.fetch_skill_md(path)
        if source == "skills-sh":
            return await self._skills_sh.fetch_skill_md(path)
        if source == "lobehub":
            from .lobehub_api import lobehub_fetch_skill_md
            version = ""
            if meta and meta.extra:
                version = str(meta.extra.get("version") or "").strip()
            return await lobehub_fetch_skill_md(path, version=version or None)
        if source == "clawhub":
            from .clawhub_api import clawhub_fetch_skill_md
            version = ""
            if meta and meta.extra:
                version = str(meta.extra.get("version") or "").strip()
            return await clawhub_fetch_skill_md(path, version=version or None)
        if source == "well-known":
            bundle = await self._well_known.fetch_bundle(path)
            if bundle and bundle.files.get("SKILL.md"):
                return bundle.files["SKILL.md"]
        return None

    async def _fetch_bundle(self, source: str, path: str) -> SkillBundle | None:
        if source == "builtin":
            return self._builtin.fetch_bundle(path)
        if source == "github":
            return await self._github.fetch_bundle(path)
        if source == "clawhub":
            return await self._clawhub.fetch_bundle(path)
        if source == "skills-sh":
            return await self._skills_sh.fetch_bundle(path)
        if source == "well-known":
            return await self._well_known.fetch_bundle(path)
        if source == "lobehub":
            return await self._lobehub.fetch_bundle(path)
        return None

    async def inspect(self, identifier: str, *, include_content: bool = False) -> dict[str, Any]:
        norm_id = normalize_identifier(identifier)
        installed = get_lock_entry_by_identifier(norm_id)
        if installed:
            skill_name = str(installed.get("name") or "").strip()
            skill_dir = resolve_hub_skill_dir(BUILTIN_SKILLS_DIR, skill_name, installed)
            local_md = skill_dir / "SKILL.md"
            if skill_name and local_md.is_file():
                meta = self._builtin.inspect(skill_name)
                if meta:
                    payload = self._meta_to_dict(meta)
                    payload["source"] = str(installed.get("source") or meta.source)
                    payload["identifier"] = norm_id
                    payload["trust_level"] = str(installed.get("trust_level") or meta.trust_level)
                    payload["installable"] = True
                    payload["hub_installed"] = True
                    payload["hub_lock"] = installed
                    if include_content:
                        payload["content_preview"] = self._skill_md_preview_body(
                            local_md.read_text(encoding="utf-8"),
                        )
                    return payload
        source, path = parse_identifier(norm_id)
        meta: SkillMeta | None
        if source == "builtin":
            meta = self._builtin.inspect(path)
        elif source == "github":
            meta = await self._github.inspect(path)
        elif source == "clawhub":
            meta = await self._clawhub.inspect(path)
        elif source == "skills-sh":
            meta = await self._skills_sh.inspect(path)
        elif source == "well-known":
            meta = await self._well_known.inspect(path)
        elif source == "lobehub":
            meta = await self._lobehub.inspect(path)
        else:
            raise ValueError(f"unsupported source: {source}")
        if meta is None:
            raise ValueError(f"skill not found: {identifier}")
        payload = self._meta_to_dict(meta)
        lock = get_lock_entry(meta.name)
        payload["installable"] = True
        payload["hub_installed"] = lock is not None
        if lock:
            payload["hub_lock"] = lock
        if include_content:
            fallback = (meta.description or "").strip()
            local_md = None
            if lock:
                skill_dir = resolve_hub_skill_dir(BUILTIN_SKILLS_DIR, meta.name, lock)
                candidate = skill_dir / "SKILL.md"
                if candidate.is_file():
                    local_md = candidate
            if local_md is not None:
                payload["content_preview"] = self._skill_md_preview_body(
                    local_md.read_text(encoding="utf-8"),
                )
                return payload
            try:
                raw = await self._fetch_preview_md(source, path, meta=meta)
                if raw:
                    payload["content_preview"] = self._skill_md_preview_body(raw)
                elif fallback:
                    payload["content_preview"] = fallback[:4000]
                    payload["content_notice"] = "未能拉取 SKILL.md 正文，以下为索引描述"
            except Exception as e:
                logging.warning("skill content preview failed for %s: %s", identifier, e)
                if fallback:
                    payload["content_preview"] = fallback[:4000]
                payload["content_notice"] = str(e)
        return payload

    async def install(
        self,
        identifier: str,
        *,
        force: bool = False,
        enable_for_agents: list[str] | None = None,
    ) -> dict[str, Any]:
        source, path = parse_identifier(normalize_identifier(identifier))
        bundle: SkillBundle | None
        if source == "builtin":
            bundle = self._builtin.fetch_bundle(path)
        elif source == "github":
            bundle = await self._github.fetch_bundle(path)
        elif source == "clawhub":
            bundle = await self._clawhub.fetch_bundle(path)
        elif source == "skills-sh":
            bundle = await self._skills_sh.fetch_bundle(path)
        elif source == "well-known":
            bundle = await self._well_known.fetch_bundle(path)
        elif source == "lobehub":
            bundle = await self._lobehub.fetch_bundle(path)
        else:
            raise ValueError(f"unsupported source: {source}")
        if bundle is None:
            raise ValueError(f"unable to fetch skill bundle: {identifier}")

        skill_name = bundle.name
        if not skill_name:
            skill_name = skill_name_from_bundle(bundle.files, path.rsplit("/", 1)[-1])

        invalidate_preinstalled_cache()
        existing = find_skill(BUILTIN_SKILLS_DIR, skill_name)
        existing_lock = get_lock_entry(skill_name)
        if existing is not None and existing_lock is None:
            if source == "builtin" or bundle.trust_level == "builtin":
                raise ValueError(
                    f"skill '{skill_name}' already exists in data/skills and is not hub-managed; "
                    "enable it in Agent config instead of Hub install"
                )
            removed = remove_skill_directory(BUILTIN_SKILLS_DIR, skill_name)
            if removed is None:
                raise ValueError(
                    f"skill already exists and is not hub-managed: {skill_name}. "
                    "Remove it manually or choose another skill."
                )
            invalidate_preinstalled_cache()
            append_audit(
                f"install removed orphan name={skill_name} "
                f"path={removed.relative_to(BUILTIN_SKILLS_DIR).as_posix()}"
            )
            existing = None

        install_category = ""
        if existing_lock and str(existing_lock.get("rel_path") or "").strip():
            dest_dir = resolve_hub_skill_dir(BUILTIN_SKILLS_DIR, skill_name, existing_lock)
        else:
            dest_dir, install_category = resolve_install_dest(
                BUILTIN_SKILLS_DIR,
                skill_name,
                bundle.files,
                metadata=bundle.metadata,
                identifier_path=path,
            )

        quarantine_dir = QUARANTINE_DIR / skill_name
        if quarantine_dir.exists():
            shutil.rmtree(quarantine_dir)
        try:
            copy_bundle_to_dir(bundle.files, quarantine_dir)
            scan = scan_skill_dir(
                quarantine_dir,
                source=bundle.source,
                trust_level=bundle.trust_level,
                skill_name=skill_name,
            )
            allowed, reason = should_allow_install(scan, force=force)
            if not allowed:
                append_audit(f"install blocked name={skill_name} id={bundle.identifier} reason={reason}")
                raise ValueError(
                    f"Security scan blocked install ({reason}).\n{format_scan_report(scan)}"
                )

            write_bundle_to_dir(bundle.files, dest_dir)
            installed = find_skill(BUILTIN_SKILLS_DIR, skill_name)
            install_category = installed.category if installed else install_category
            lock_payload: dict[str, Any] = {
                "identifier": bundle.identifier,
                "source": bundle.source,
                "trust_level": bundle.trust_level,
                "content_hash": bundle.metadata.get("content_hash") or content_hash(bundle.files),
                "scan_verdict": scan.verdict,
                "rel_path": installed.rel_path if installed else str(dest_dir.relative_to(BUILTIN_SKILLS_DIR)).replace("\\", "/"),
            }
            if bundle.metadata.get("version"):
                lock_payload["upstream_version"] = bundle.metadata.get("version")
            entry = upsert_lock_entry(skill_name, lock_payload)
            invalidate_preinstalled_cache()
            append_audit(f"install ok name={skill_name} id={bundle.identifier} verdict={scan.verdict}")
            enabled_agents: list[dict[str, Any]] = []
            targets = [str(x).strip() for x in (enable_for_agents or []) if str(x).strip()]
            if targets:
                enabled_agents = enable_skill_for_agents(skill_name, targets)
                ok_count = sum(1 for row in enabled_agents if row.get("status") == "enabled")
                append_audit(f"enable agents name={skill_name} ok={ok_count}/{len(targets)}")
            return {
                "skill": {
                    "name": skill_name,
                    "identifier": bundle.identifier,
                    "source": bundle.source,
                    "trust_level": bundle.trust_level,
                    "abs_path": str(dest_dir),
                    "category": install_category,
                    "scan_verdict": scan.verdict,
                },
                "lock_entry": entry,
                "enabled_agents": enabled_agents,
            }
        finally:
            if quarantine_dir.exists():
                shutil.rmtree(quarantine_dir, ignore_errors=True)

    def list_installed(self) -> list[dict[str, Any]]:
        items = list_installed()
        enabled_map = build_skill_enabled_agents_map()
        result: list[dict[str, Any]] = []
        for entry in items:
            name = str(entry.get("name") or "")
            skill_dir = resolve_hub_skill_dir(BUILTIN_SKILLS_DIR, name, entry)
            source = str(entry.get("source") or "").strip()
            identifier = str(entry.get("identifier") or "").strip()
            if not source and ":" in identifier:
                source = identifier.split(":", 1)[0].strip()
            category = DEFAULT_SKILL_CATEGORY
            indexed = find_skill(BUILTIN_SKILLS_DIR, name)
            if indexed is not None:
                category = indexed.category
            result.append(
                {
                    **entry,
                    "source": source,
                    "category": category,
                    "exists": skill_dir.is_dir(),
                    "abs_path": str(skill_dir),
                    "enabled_agents": enabled_map.get(name, []),
                    **skill_remark_payload(name),
                }
            )
        return result

    def list_preinstalled(self) -> dict[str, Any]:
        from .preinstalled import get_preinstalled_skill_names

        enabled_map = build_skill_enabled_agents_map()
        skills: list[dict[str, Any]] = []
        for name in sorted(get_preinstalled_skill_names()):
            meta = self._builtin.inspect(name)
            if meta:
                row = self._meta_to_dict(meta, preinstalled=True)
                row.update(skill_remark_payload(name))
                row["enabled_agents"] = enabled_map.get(name, [])
                skills.append(row)
        return {"count": len(skills), "skills": skills}

    def uninstall(self, name: str) -> dict[str, Any]:
        key = (name or "").strip()
        if not key:
            raise ValueError("skill name is required")
        entry = get_lock_entry(key)
        if not entry:
            raise ValueError(f"skill is not managed by hub: {key}")
        removed = remove_skill_directory(BUILTIN_SKILLS_DIR, key, lock_entry=entry)
        if removed is None:
            logging.warning("hub uninstall: skill directory not found for %s, removing lock only", key)
        remove_lock_entry(key)
        remove_skill_remark(key)
        invalidate_preinstalled_cache()
        removed_note = str(removed) if removed else "missing"
        append_audit(f"uninstall ok name={key} path={removed_note}")
        return {"name": key, "removed": True, "path": removed_note}

    async def check_updates(self, name: str | None = None) -> list[dict[str, Any]]:
        entries = list_installed()
        if name:
            key = name.strip()
            entry = get_lock_entry(key)
            if not entry:
                raise ValueError(f"skill is not managed by hub: {key}")
            entries = [entry]
        results: list[dict[str, Any]] = []
        for entry in entries:
            skill_name = str(entry.get("name") or "")
            identifier = str(entry.get("identifier") or "")
            current_hash = str(entry.get("content_hash") or "")
            row: dict[str, Any] = {
                "name": skill_name,
                "identifier": identifier,
                "current_hash": current_hash,
                "update_available": False,
                "status": "ok",
            }
            if not identifier:
                row["status"] = "missing_identifier"
                results.append(row)
                continue
            try:
                source, path = parse_identifier(normalize_identifier(identifier))
                bundle = await self._fetch_bundle(source, path)
            except Exception as e:
                row["status"] = "fetch_failed"
                row["error"] = str(e)
                results.append(row)
                continue
            if bundle is None:
                row["status"] = "not_found"
                results.append(row)
                continue
            upstream_hash = bundle.metadata.get("content_hash") or content_hash(bundle.files)
            row["upstream_hash"] = upstream_hash
            upstream_version = str(bundle.metadata.get("version") or "")
            locked_version = str(entry.get("upstream_version") or "")
            hash_changed = bool(current_hash and upstream_hash != current_hash)
            version_changed = bool(locked_version and upstream_version and locked_version != upstream_version)
            row["update_available"] = hash_changed or version_changed
            if upstream_version:
                row["upstream_version"] = upstream_version
            if locked_version:
                row["locked_version"] = locked_version
            results.append(row)
        return results

    async def update(self, name: str, *, force: bool = False) -> dict[str, Any]:
        key = (name or "").strip()
        entry = get_lock_entry(key)
        if not entry:
            raise ValueError(f"skill is not managed by hub: {key}")
        identifier = str(entry.get("identifier") or "")
        if not identifier:
            raise ValueError(f"hub entry missing identifier: {key}")
        checks = await self.check_updates(key)
        if checks and not checks[0].get("update_available"):
            return {
                "name": key,
                "updated": False,
                "message": "already up to date",
                "lock_entry": entry,
            }
        result = await self.install(identifier, force=force)
        append_audit(f"update ok name={key} id={identifier}")
        result["updated"] = True
        return result

    def audit(self, name: str | None = None) -> list[dict[str, Any]]:
        entries = list_installed()
        if name:
            key = name.strip()
            entry = get_lock_entry(key)
            if not entry:
                raise ValueError(f"skill is not managed by hub: {key}")
            entries = [entry]
        results: list[dict[str, Any]] = []
        for entry in entries:
            skill_name = str(entry.get("name") or "")
            skill_dir = resolve_hub_skill_dir(BUILTIN_SKILLS_DIR, skill_name, entry)
            if not skill_dir.is_dir():
                results.append(
                    {
                        "name": skill_name,
                        "status": "missing",
                        "scan_verdict": None,
                    }
                )
                continue
            scan = scan_skill_dir(
                skill_dir,
                source=str(entry.get("source") or "unknown"),
                trust_level=str(entry.get("trust_level") or "community"),
                skill_name=skill_name,
            )
            upsert_lock_entry(skill_name, {"scan_verdict": scan.verdict})
            append_audit(f"audit name={skill_name} verdict={scan.verdict}")
            results.append(
                {
                    "name": skill_name,
                    "status": "ok",
                    "scan_verdict": scan.verdict,
                    "findings_count": len(scan.findings),
                    "summary": scan.summary,
                }
            )
        return results

    def list_github_taps(self) -> dict[str, Any]:
        return list_github_taps()

    def add_github_tap(self, repo: str) -> dict[str, Any]:
        return add_github_tap(repo)

    def remove_github_tap(self, repo: str) -> dict[str, Any]:
        return remove_github_tap(repo)

    def list_wellknown_hosts(self) -> dict[str, Any]:
        return list_wellknown_hosts()

    def add_wellknown_host(self, host: str) -> dict[str, Any]:
        return add_wellknown_host(host)

    def remove_wellknown_host(self, host: str) -> dict[str, Any]:
        return remove_wellknown_host(host)

    def enable_for_agents(self, skill_name: str, agent_types: list[str]) -> list[dict[str, Any]]:
        return enable_skill_for_agents(skill_name, agent_types)

    def sync_for_agents(self, skill_name: str, agent_types: list[str]) -> list[dict[str, Any]]:
        from .agent_enable import sync_skill_for_agents
        return sync_skill_for_agents(skill_name, agent_types)

    def list_audit_log(self, *, limit: int = 50) -> list[str]:
        cap = max(1, min(limit, 200))
        if not AUDIT_LOG.is_file():
            return []
        try:
            lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return lines[-cap:]

    def hub_metadata_for_skill(self, name: str) -> dict[str, Any] | None:
        entry = get_lock_entry(name)
        if not entry:
            return None
        return {
            "hub_managed": True,
            "hub_identifier": entry.get("identifier"),
            "hub_source": entry.get("source"),
            "hub_trust_level": entry.get("trust_level"),
            "hub_installed_at": entry.get("installed_at"),
            "hub_updated_at": entry.get("updated_at"),
        }

    @staticmethod
    def _meta_to_dict(meta: SkillMeta, *, query: str = "", preinstalled: bool | None = None) -> dict[str, Any]:
        label = popularity_label(meta)
        extra = meta.extra if isinstance(meta.extra, dict) else {}
        category = str(extra.get("category") or DEFAULT_SKILL_CATEGORY).strip() or DEFAULT_SKILL_CATEGORY
        payload = {
            "name": meta.name,
            "description": meta.description,
            "source": meta.source,
            "identifier": meta.identifier,
            "trust_level": meta.trust_level,
            "repo": meta.repo,
            "path": meta.path,
            "tags": meta.tags,
            "extra": meta.extra,
            "category": category,
            "popularity_score": popularity_score(meta, query=query),
            "popularity_label": label,
        }
        if preinstalled is True or (preinstalled is not False and is_preinstalled_skill(meta.name)):
            payload["preinstalled"] = True
        return payload


HUB_SERVICE = SkillsHubService()
