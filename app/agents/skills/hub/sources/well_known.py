import hashlib
import io
import logging
import tarfile
import zipfile
from typing import Any
from urllib.parse import urljoin, urlparse
import aiohttp
from ..models import SkillBundle, SkillMeta
from ..utils import content_hash, skill_description_from_bundle, skill_name_from_bundle
from ..wellknown_hosts import get_search_hosts, normalize_wellknown_host
from .base import SkillSource

INDEX_PATHS = (
    "/.well-known/agent-skills/index.json",
    "/.well-known/skills/index.json",
)
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
_TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".js", ".ts", ".toml", ".ini", ".cfg")


def origin_from_host(host: str) -> str:
    text = (host or "").strip().rstrip("/")
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        return f"{parsed.scheme}://{parsed.netloc}"
    return f"https://{normalize_wellknown_host(text)}"


def parse_wellknown_identifier(path: str) -> tuple[str, str]:
    text = (path or "").strip().strip("/")
    if not text:
        raise ValueError("well-known identifier path is required")
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        segments = [p for p in parsed.path.split("/") if p]
        if segments and segments[-1] in ("index.json", "SKILL.md"):
            segments = segments[:-1]
        if segments[:2] == [".well-known", "agent-skills"] or segments[:2] == [".well-known", "skills"]:
            segments = segments[2:]
        skill_name = segments[-1] if segments else ""
        return origin, skill_name
    parts = [p for p in text.split("/") if p]
    if not parts:
        raise ValueError("well-known path requires host[/skill]")
    origin = origin_from_host(parts[0])
    skill_name = "/".join(parts[1:]) if len(parts) > 1 else ""
    return origin, skill_name


def wrap_identifier(origin: str, skill_name: str) -> str:
    host = urlparse(origin).netloc or origin.replace("https://", "").replace("http://", "")
    if skill_name:
        return f"well-known:{host}/{skill_name}"
    return f"well-known:{host}"


def _is_text_path(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in _TEXT_SUFFIXES)


def _verify_digest(content: bytes, digest: str) -> None:
    value = (digest or "").strip().lower()
    if not value.startswith("sha256:"):
        return
    expected = value[7:]
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise RuntimeError("well-known artifact digest mismatch")


def _extract_archive(data: bytes) -> dict[str, str]:
    files: dict[str, str] = {}
    if data[:2] == b"\x1f\x8b" or data[:4] == b"\x1f\x8b\x08":
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            for member in members:
                if not member.isfile():
                    continue
                rel = member.name.replace("\\", "/").lstrip("./")
                if not rel or rel.startswith(".") or "/." in f"/{rel}/":
                    continue
                if not _is_text_path(rel):
                    continue
                extracted = archive.extractfile(member)
                if not extracted:
                    continue
                try:
                    files[rel] = extracted.read().decode("utf-8")
                except UnicodeDecodeError:
                    logging.warning("skip non-text archive file: %s", rel)
    elif data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for rel in archive.namelist():
                if rel.endswith("/") or rel.startswith(".") or "/." in f"/{rel}/":
                    continue
                if not _is_text_path(rel):
                    continue
                try:
                    files[rel] = archive.read(rel).decode("utf-8")
                except (UnicodeDecodeError, KeyError):
                    logging.warning("skip non-text zip file: %s", rel)
    return _normalize_archive_files(files)


def _normalize_archive_files(files: dict[str, str]) -> dict[str, str]:
    if "SKILL.md" in files:
        return files
    skill_md_key = ""
    for path in files:
        if path.endswith("/SKILL.md") or path == "SKILL.md":
            skill_md_key = path
            break
    if not skill_md_key:
        return files
    prefix = skill_md_key[: -len("SKILL.md")]
    normalized: dict[str, str] = {}
    for path, content in files.items():
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix):].lstrip("/")
        if rel:
            normalized[rel] = content
    return normalized if "SKILL.md" in normalized else files


class WellKnownSkillSource(SkillSource):
    source_id = "well-known"

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> Any:
        async with session.get(url, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                raise RuntimeError(f"well-known fetch {resp.status}: {body}")
            return await resp.json(content_type=None)

    async def _get_bytes(self, session: aiohttp.ClientSession, url: str) -> bytes:
        async with session.get(url, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                body = (await resp.text())[:200]
                raise RuntimeError(f"well-known fetch {resp.status}: {body}")
            return await resp.read()

    async def _fetch_index(self, session: aiohttp.ClientSession, origin: str) -> tuple[dict[str, Any], str]:
        base = origin.rstrip("/")
        last_error = ""
        for rel in INDEX_PATHS:
            url = f"{base}{rel}"
            try:
                data = await self._get_json(session, url)
                if isinstance(data, dict) and isinstance(data.get("skills"), list):
                    return data, url
            except Exception as e:
                last_error = str(e)
        raise RuntimeError(f"no well-known index at {origin}: {last_error}")

    def _filter_entries(self, entries: list[Any], filter_text: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        needle = (filter_text or "").strip().lower()
        for item in entries:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            desc = str(item.get("description") or name).strip()
            if needle and needle not in name.lower() and needle not in desc.lower():
                continue
            rows.append(item)
        return rows

    async def search(self, query: str, *, limit: int = 50, offset: int = 0) -> list[SkillMeta]:
        q = (query or "").strip()
        start = max(0, offset)
        page_size = max(1, limit)
        if not q:
            hosts = get_search_hosts()
            filter_text = ""
        else:
            parts = q.split(None, 1)
            filter_text = parts[1] if len(parts) > 1 else ""
            hosts = []
            if "." in parts[0]:
                try:
                    hosts = [normalize_wellknown_host(parts[0])]
                except ValueError:
                    hosts = []
            if not hosts:
                hosts = get_search_hosts()
                filter_text = q.lower()
        items: list[SkillMeta] = []
        async with aiohttp.ClientSession() as session:
            for host in hosts:
                origin = origin_from_host(host)
                try:
                    index, index_url = await self._fetch_index(session, origin)
                except Exception as e:
                    logging.warning("well-known index failed for %s: %s", host, e)
                    continue
                entries = self._filter_entries(index.get("skills") or [], filter_text)
                for entry in entries:
                    name = str(entry.get("name") or "").strip()
                    if not name:
                        continue
                    items.append(
                        SkillMeta(
                            name=name,
                            description=str(entry.get("description") or name).strip(),
                            source=self.source_id,
                            identifier=wrap_identifier(origin, name),
                            trust_level="community",
                            path=f"{urlparse(origin).netloc}/{name}",
                            extra={
                                "host": urlparse(origin).netloc,
                                "type": entry.get("type"),
                                "version": entry.get("version"),
                            },
                        )
                    )
        return items[start:start + page_size]

    async def _find_entry(
        self,
        session: aiohttp.ClientSession,
        origin: str,
        skill_name: str,
    ) -> tuple[dict[str, Any], str]:
        index, index_url = await self._fetch_index(session, origin)
        key = (skill_name or "").strip()
        for entry in index.get("skills") or []:
            if isinstance(entry, dict) and str(entry.get("name") or "").strip() == key:
                return entry, index_url
        raise RuntimeError(f"skill not found in well-known index: {key}")

    async def inspect(self, identifier_path: str) -> SkillMeta | None:
        try:
            origin, skill_name = parse_wellknown_identifier(identifier_path)
        except ValueError:
            return None
        if not skill_name:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                entry, _index_url = await self._find_entry(session, origin, skill_name)
        except Exception as e:
            logging.warning("well-known inspect failed for %s: %s", identifier_path, e)
            return None
        host = urlparse(origin).netloc
        return SkillMeta(
            name=skill_name,
            description=str(entry.get("description") or skill_name).strip(),
            source=self.source_id,
            identifier=wrap_identifier(origin, skill_name),
            trust_level="community",
            path=f"{host}/{skill_name}",
            extra={
                "host": host,
                "type": entry.get("type"),
                "version": entry.get("version"),
                "digest": entry.get("digest"),
            },
        )

    async def fetch_bundle(self, identifier_path: str) -> SkillBundle | None:
        origin, skill_name = parse_wellknown_identifier(identifier_path)
        if not skill_name:
            raise ValueError("well-known install requires host/skill-name")
        async with aiohttp.ClientSession() as session:
            entry, index_url = await self._find_entry(session, origin, skill_name)
            artifact_url = urljoin(index_url, str(entry.get("url") or ""))
            if not artifact_url:
                raise RuntimeError(f"well-known entry missing url: {skill_name}")
            skill_type = str(entry.get("type") or "skill-md").lower()
            if skill_type == "skill-md":
                text = (await self._get_bytes(session, artifact_url)).decode("utf-8")
                files = {"SKILL.md": text}
            elif skill_type == "archive":
                raw = await self._get_bytes(session, artifact_url)
                _verify_digest(raw, str(entry.get("digest") or ""))
                files = _extract_archive(raw)
            else:
                raise RuntimeError(f"unsupported well-known skill type: {skill_type}")
            if "SKILL.md" not in files:
                raise RuntimeError(f"well-known bundle missing SKILL.md: {skill_name}")
        bundle_name = skill_name_from_bundle(files, skill_name)
        desc = skill_description_from_bundle(files, bundle_name)
        host = urlparse(origin).netloc
        return SkillBundle(
            name=bundle_name,
            files=files,
            source=self.source_id,
            identifier=wrap_identifier(origin, skill_name),
            trust_level="community",
            metadata={
                "content_hash": content_hash(files),
                "host": host,
                "version": entry.get("version") or "",
                "description": desc,
            },
        )
