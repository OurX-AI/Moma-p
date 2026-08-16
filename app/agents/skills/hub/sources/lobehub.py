import logging
from typing import Any
from ..lobehub_api import lobehub_fetch_files, lobehub_inspect, lobehub_search
from ..models import SkillBundle, SkillMeta
from ..utils import content_hash, skill_description_from_bundle, skill_name_from_bundle
from .base import SkillSource


def wrap_identifier(identifier: str) -> str:
    key = (identifier or "").strip().strip("/")
    if not key:
        raise ValueError("lobehub identifier is required")
    return f"lobehub:{key}"


def _meta_from_row(identifier: str, row: dict[str, Any]) -> SkillMeta:
    name = str(row.get("name") or identifier).strip()
    description = str(row.get("description") or name).strip()
    version = str(row.get("version") or "").strip()
    author = row.get("author")
    if isinstance(author, dict):
        author_name = str(author.get("name") or "").strip()
    else:
        author_name = str(author or "").strip()
    github = row.get("github") if isinstance(row.get("github"), dict) else {}
    extra: dict[str, Any] = {
        "version": version,
        "author": author_name,
        "install_count": row.get("installCount"),
        "rating_avg": row.get("ratingAvg") or row.get("ratingAverage"),
        "rating_count": row.get("ratingCount"),
        "category": row.get("category"),
        "homepage": row.get("homepage"),
        "detail_url": f"https://lobehub.com/skills/{identifier}",
    }
    if github.get("url"):
        extra["github_url"] = github.get("url")
    if github.get("stars") is not None:
        extra["github_stars"] = github.get("stars")
    tags = [f"author:{author_name}"] if author_name else []
    if row.get("category"):
        tags.append(f"category:{row.get('category')}")
    return SkillMeta(
        name=name,
        description=description,
        source="lobehub",
        identifier=wrap_identifier(identifier),
        trust_level="community",
        path=identifier,
        tags=tags,
        extra=extra,
    )


class LobeHubSkillSource(SkillSource):
    source_id = "lobehub"

    async def search(self, query: str, *, limit: int = 50, offset: int = 0) -> list[SkillMeta]:
        try:
            rows = await lobehub_search(query, limit=limit, offset=offset)
        except Exception as e:
            logging.warning("lobehub search failed: %s", e)
            raise
        items: list[SkillMeta] = []
        for row in rows:
            identifier = str(row.get("identifier") or "").strip()
            if not identifier:
                continue
            items.append(_meta_from_row(identifier, row))
            if len(items) >= limit:
                break
        return items[:limit]

    async def inspect(self, identifier_path: str) -> SkillMeta | None:
        key = (identifier_path or "").strip().strip("/")
        if not key:
            return None
        try:
            payload = await lobehub_inspect(key)
        except Exception as e:
            logging.warning("lobehub inspect failed for %s: %s", key, e)
            return None
        if not isinstance(payload, dict):
            return None
        payload = {**payload, "identifier": payload.get("identifier") or key}
        return _meta_from_row(key, payload)

    async def fetch_bundle(self, identifier_path: str) -> SkillBundle | None:
        key = (identifier_path or "").strip().strip("/")
        if not key:
            return None
        payload = await lobehub_inspect(key)
        version = str(payload.get("version") or "").strip() if isinstance(payload, dict) else ""
        files = await lobehub_fetch_files(key, version=version or None)
        skill_name = skill_name_from_bundle(files, key.split("-")[-1])
        desc = skill_description_from_bundle(files, skill_name)
        identifier = wrap_identifier(key)
        category = payload.get("category") if isinstance(payload, dict) else None
        return SkillBundle(
            name=skill_name,
            files=files,
            source=self.source_id,
            identifier=identifier,
            trust_level="community",
            metadata={
                "content_hash": content_hash(files),
                "identifier": key,
                "version": version,
                "description": desc,
                "category": category,
            },
        )
