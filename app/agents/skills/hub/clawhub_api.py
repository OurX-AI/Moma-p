import logging
from typing import Any
import aiohttp

BASE_URL = "https://clawhub.ai"
SEARCH_URL = f"{BASE_URL}/api/v1/search"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
_TEXT_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")
_TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".js", ".ts", ".toml", ".ini", ".cfg")


def _is_text_file(path: str, content_type: str) -> bool:
    ct = (content_type or "").lower()
    if any(ct.startswith(prefix) for prefix in _TEXT_PREFIXES):
        return True
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in _TEXT_SUFFIXES)


async def _get_json(session: aiohttp.ClientSession, url: str, *, params: dict[str, Any] | None = None) -> Any:
    async with session.get(url, params=params, timeout=_REQUEST_TIMEOUT) as resp:
        if resp.status != 200:
            body = (await resp.text())[:200]
            raise RuntimeError(f"clawhub API {resp.status}: {body}")
        return await resp.json(content_type=None)


async def _get_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, Any] | None = None,
) -> str:
    async with session.get(url, params=params, timeout=_REQUEST_TIMEOUT) as resp:
        if resp.status != 200:
            body = (await resp.text())[:200]
            raise RuntimeError(f"clawhub API {resp.status}: {body}")
        return await resp.text()


def _normalize_clawhub_rows(rows: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        owner = row.get("owner") if isinstance(row.get("owner"), dict) else {}
        owner_handle = str(row.get("ownerHandle") or owner.get("handle") or "").strip()
        display_name = str(row.get("displayName") or "").strip()
        stats = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        items.append(
            {
                "slug": slug,
                "owner": owner_handle,
                "summary": str(row.get("summary") or display_name or slug).strip(),
                "display_name": display_name or None,
                "score": row.get("score"),
                "stars": stats.get("stars"),
                "downloads": stats.get("downloads"),
            }
        )
    return items


async def clawhub_list(*, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    cap = max(1, min(limit, 500))
    skip = max(0, offset)
    params: dict[str, Any] = {"limit": cap}
    if skip:
        params["offset"] = skip
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, f"{BASE_URL}/api/v1/skills", params=params)
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return _normalize_clawhub_rows(rows)[:cap]


async def clawhub_search(query: str, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    q = (query or "").strip()
    cap = max(1, min(limit, 500))
    skip = max(0, offset)
    if not q:
        return await clawhub_list(limit=cap, offset=skip)
    async with aiohttp.ClientSession() as session:
        params: dict[str, Any] = {"q": q, "limit": cap}
        if skip:
            params["offset"] = skip
        data = await _get_json(session, SEARCH_URL, params=params)
    rows = data.get("results") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return _normalize_clawhub_rows(rows)[:cap]


async def clawhub_inspect(slug: str) -> dict[str, Any]:
    key = (slug or "").strip()
    if not key:
        raise ValueError("clawhub slug is required")
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, f"{BASE_URL}/api/v1/skills/{key}")
    if not isinstance(data, dict):
        raise ValueError("clawhub inspect returned invalid payload")
    return data


async def _resolve_version(session: aiohttp.ClientSession, slug: str) -> str:
    payload = await _get_json(session, f"{BASE_URL}/api/v1/skills/{slug}")
    latest = payload.get("latestVersion") if isinstance(payload, dict) else None
    if isinstance(latest, dict):
        version = str(latest.get("version") or "").strip()
        if version:
            return version
    skill = payload.get("skill") if isinstance(payload, dict) else None
    if isinstance(skill, dict):
        tags = skill.get("tags")
        if isinstance(tags, dict):
            tag_version = str(tags.get("latest") or "").strip()
            if tag_version:
                return tag_version
    raise RuntimeError(f"clawhub skill has no version: {slug}")


async def clawhub_fetch_skill_md(slug: str, *, version: str | None = None) -> str | None:
    key = (slug or "").strip()
    if not key:
        raise ValueError("clawhub slug is required")
    async with aiohttp.ClientSession() as session:
        resolved = (version or "").strip() or await _resolve_version(session, key)
        try:
            return await _get_text(
                session,
                f"{BASE_URL}/api/v1/skills/{key}/file",
                params={"path": "SKILL.md", "version": resolved},
            )
        except Exception:
            return None


async def clawhub_fetch_files(slug: str, *, version: str | None = None) -> dict[str, str]:
    key = (slug or "").strip()
    if not key:
        raise ValueError("clawhub slug is required")
    async with aiohttp.ClientSession() as session:
        resolved = (version or "").strip() or await _resolve_version(session, key)
        version_payload = await _get_json(session, f"{BASE_URL}/api/v1/skills/{key}/versions/{resolved}")
        version_obj = version_payload.get("version") if isinstance(version_payload, dict) else None
        security = version_obj.get("security") if isinstance(version_obj, dict) else None
        if isinstance(security, dict):
            status = str(security.get("status") or "").lower()
            if status == "malicious":
                raise RuntimeError(f"clawhub security status malicious: {key}@{resolved}")
        file_rows = version_obj.get("files") if isinstance(version_obj, dict) else None
        if not isinstance(file_rows, list) or not file_rows:
            raise RuntimeError(f"clawhub version has no files: {key}@{resolved}")
        files: dict[str, str] = {}
        for entry in file_rows:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip().lstrip("/")
            if not path or path.startswith(".") or "/." in f"/{path}/":
                continue
            content_type = str(entry.get("contentType") or "")
            if not _is_text_file(path, content_type):
                logging.warning("skip non-text clawhub file: %s", path)
                continue
            try:
                content = await _get_text(
                    session,
                    f"{BASE_URL}/api/v1/skills/{key}/file",
                    params={"path": path, "version": resolved},
                )
            except Exception as e:
                logging.warning("skip clawhub file %s: %s", path, e)
                continue
            files[path] = content
        if "SKILL.md" not in files:
            raise RuntimeError(f"clawhub bundle missing SKILL.md: {key}")
        return files
