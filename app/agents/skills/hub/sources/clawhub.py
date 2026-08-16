import logging
from ..clawhub_api import clawhub_fetch_files, clawhub_inspect, clawhub_search
from ..models import SkillBundle, SkillMeta
from ..utils import content_hash, skill_description_from_bundle, skill_name_from_bundle
from .base import SkillSource

def _install_block_reason(payload: dict) -> str | None:
    moderation = payload.get("moderation")
    if isinstance(moderation, dict):
        if moderation.get("isMalwareBlocked"):
            return "malware blocked by ClawHub moderation"
        verdict = str(moderation.get("verdict") or "").lower()
        if verdict == "malicious":
            return "malicious verdict from ClawHub moderation"
    return None


def _meta_from_inspect_payload(slug: str, payload: dict) -> SkillMeta:
    skill = payload.get("skill") if isinstance(payload.get("skill"), dict) else {}
    owner = payload.get("owner") if isinstance(payload.get("owner"), dict) else {}
    summary = str(skill.get("summary") or skill.get("displayName") or slug).strip()
    version = ""
    latest = payload.get("latestVersion")
    if isinstance(latest, dict):
        version = str(latest.get("version") or "")
    stats = skill.get("stats") if isinstance(skill.get("stats"), dict) else {}
    extra: dict = {
        "owner": owner.get("handle"),
        "version": version,
        "downloads": stats.get("downloads"),
        "stars": stats.get("stars"),
        "detail_url": f"https://clawhub.ai/skills/{slug}",
    }
    moderation = payload.get("moderation")
    if isinstance(moderation, dict):
        extra["moderation_verdict"] = moderation.get("verdict")
        extra["is_malware_blocked"] = moderation.get("isMalwareBlocked")
        extra["is_suspicious"] = moderation.get("isSuspicious")
    name = str(skill.get("slug") or slug)
    tags = [f"owner:{owner.get('handle', '')}"] if owner.get("handle") else []
    return SkillMeta(
        name=name,
        description=summary,
        source="clawhub",
        identifier=f"clawhub:{slug}",
        trust_level="community",
        path=slug,
        tags=tags,
        extra=extra,
    )


class ClawHubSkillSource(SkillSource):
    source_id = "clawhub"

    async def search(self, query: str, *, limit: int = 50, offset: int = 0) -> list[SkillMeta]:
        try:
            rows = await clawhub_search(query, limit=limit, offset=offset)
        except Exception as e:
            logging.warning("clawhub search failed: %s", e)
            raise
        items: list[SkillMeta] = []
        for row in rows:
            slug = str(row.get("slug") or "").strip()
            if not slug:
                continue
            owner = str(row.get("owner") or "").strip()
            summary = str(row.get("summary") or row.get("display_name") or slug).strip()
            items.append(
                SkillMeta(
                    name=slug,
                    description=summary,
                    source=self.source_id,
                    identifier=f"clawhub:{slug}",
                    trust_level="community",
                    path=slug,
                    tags=[f"owner:{owner}"] if owner else [],
                    extra={
                        "owner": owner,
                        "score": row.get("score"),
                        "stars": row.get("stars"),
                        "downloads": row.get("downloads"),
                        "display_name": row.get("display_name"),
                        "detail_url": f"https://clawhub.ai/skills/{slug}",
                    },
                )
            )
            if len(items) >= limit:
                break
        return items[:limit]

    async def inspect(self, identifier_path: str) -> SkillMeta | None:
        slug = (identifier_path or "").strip().strip("/")
        if not slug:
            return None
        try:
            payload = await clawhub_inspect(slug)
        except Exception as e:
            logging.warning("clawhub inspect failed for %s: %s", slug, e)
            return None
        return _meta_from_inspect_payload(slug, payload)

    async def fetch_bundle(self, identifier_path: str) -> SkillBundle | None:
        slug = (identifier_path or "").strip().strip("/")
        if not slug:
            return None
        payload = await clawhub_inspect(slug)
        block_reason = _install_block_reason(payload)
        if block_reason:
            raise ValueError(f"ClawHub blocked install: {block_reason}")
        meta = _meta_from_inspect_payload(slug, payload)
        version = str(meta.extra.get("version") or "").strip()
        files = await clawhub_fetch_files(slug, version=version or None)
        skill_name = skill_name_from_bundle(files, slug)
        desc = skill_description_from_bundle(files, skill_name)
        identifier = f"clawhub:{slug}"
        return SkillBundle(
            name=skill_name,
            files=files,
            source=self.source_id,
            identifier=identifier,
            trust_level="community",
            metadata={
                "content_hash": content_hash(files),
                "slug": slug,
                "version": version,
                "description": desc,
            },
        )
