import io
import json
import logging
import os
import platform
import socket
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
import aiohttp
import jwt
from .constants import HUB_DIR

DEFAULT_BASE_URL = "https://market.lobehub.com"
CREDENTIALS_FILE = HUB_DIR / "lobehub_credentials.json"
DEVICE_ID_FILE = HUB_DIR / "lobehub_device_id.txt"
USER_AGENT = "MomaCoder-SkillsHub/1.0"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=90)
_TEXT_PREFIXES = ("text/", "application/json", "application/xml", "application/javascript")
_TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".js", ".ts", ".toml", ".ini", ".cfg")

_token_cache: dict[str, Any] = {"access_token": "", "expires_at": 0.0}


def _is_text_file(path: str) -> bool:
    lower = path.lower()
    return any(lower.endswith(suffix) for suffix in _TEXT_SUFFIXES)


def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save_credentials(payload: dict[str, Any]) -> None:
    HUB_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_user_cli_credentials() -> dict[str, Any] | None:
    home = Path.home() / ".lobehub-market" / "credentials.json"
    data = _load_json_file(home)
    if not data:
        return None
    client_id = str(data.get("clientId") or data.get("client_id") or "").strip()
    client_secret = str(data.get("clientSecret") or data.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        return None
    return {
        "base_url": str(data.get("baseUrl") or data.get("base_url") or DEFAULT_BASE_URL).strip(),
        "client_id": client_id,
        "client_secret": client_secret,
        "source": "user-cli",
    }


def _resolve_credentials() -> dict[str, str]:
    env_id = (os.getenv("MARKET_CLIENT_ID") or "").strip()
    env_secret = (os.getenv("MARKET_CLIENT_SECRET") or "").strip()
    env_base = (os.getenv("MARKET_BASE_URL") or "").strip()
    if env_id and env_secret:
        return {
            "base_url": env_base or DEFAULT_BASE_URL,
            "client_id": env_id,
            "client_secret": env_secret,
            "source": "env",
        }
    file_data = _load_json_file(CREDENTIALS_FILE)
    if file_data:
        client_id = str(file_data.get("client_id") or "").strip()
        client_secret = str(file_data.get("client_secret") or "").strip()
        if client_id and client_secret:
            return {
                "base_url": str(file_data.get("base_url") or DEFAULT_BASE_URL).strip(),
                "client_id": client_id,
                "client_secret": client_secret,
                "source": "hub-file",
            }
    cli_data = _load_user_cli_credentials()
    if cli_data:
        return {
            "base_url": cli_data["base_url"],
            "client_id": cli_data["client_id"],
            "client_secret": cli_data["client_secret"],
            "source": "user-cli",
        }
    raise RuntimeError(
        "LobeHub credentials missing. Set MARKET_CLIENT_ID and MARKET_CLIENT_SECRET in .env, "
        "run `npx -y @lobehub/market-cli register`, or let Hub auto-register on first use."
    )


def _device_id() -> str:
    if DEVICE_ID_FILE.is_file():
        cached = DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
        if cached:
            return cached
    host = socket.gethostname()
    device = f"device-{uuid.uuid5(uuid.NAMESPACE_DNS, host).hex[:16]}"
    HUB_DIR.mkdir(parents=True, exist_ok=True)
    DEVICE_ID_FILE.write_text(device, encoding="utf-8")
    return device


async def _register_client(session: aiohttp.ClientSession, base_url: str) -> dict[str, str]:
    payload = {
        "clientName": "MomaCoder",
        "clientType": "cli",
        "description": "MomaCoder Skills Hub integration",
        "deviceId": _device_id(),
        "metadata": {"python": platform.python_version()},
        "platform": platform.machine() or "unknown",
        "source": "momacoder",
        "version": "1.0.0",
    }
    url = f"{base_url.rstrip('/')}/api/v1/clients/register"
    async with session.post(url, json=payload, headers={"User-Agent": USER_AGENT}, timeout=_REQUEST_TIMEOUT) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"LobeHub register failed {resp.status}: {body[:300]}")
        data = json.loads(body)
    client_id = str(data.get("client_id") or "").strip()
    client_secret = str(data.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError("LobeHub register returned invalid credentials")
    creds = {
        "base_url": base_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "source": "auto-register",
        "registered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _save_credentials(creds)
    logging.warning("lobehub auto-registered client %s", client_id)
    return creds


async def _ensure_credentials(session: aiohttp.ClientSession) -> dict[str, str]:
    try:
        return _resolve_credentials()
    except RuntimeError:
        return await _register_client(session, os.getenv("MARKET_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL)


def _create_client_assertion(client_id: str, client_secret: str, token_endpoint: str) -> str:
    now = int(time.time())
    payload = {
        "iss": client_id,
        "sub": client_id,
        "aud": token_endpoint,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(payload, client_secret, algorithm="HS256")


async def _get_access_token(session: aiohttp.ClientSession) -> tuple[str, str]:
    creds = await _ensure_credentials(session)
    base_url = creds["base_url"]
    cache_key = f"{base_url}:{creds['client_id']}"
    cached_token = str(_token_cache.get("access_token") or "")
    cached_key = str(_token_cache.get("cache_key") or "")
    expires_at = float(_token_cache.get("expires_at") or 0)
    if cached_token and cached_key == cache_key and time.time() < expires_at:
        return cached_token, base_url
    token_endpoint = f"{base_url.rstrip('/')}/oauth/token"
    assertion = _create_client_assertion(creds["client_id"], creds["client_secret"], token_endpoint)
    form = {
        "grant_type": "client_credentials",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": assertion,
    }
    async with session.post(
        token_endpoint,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        timeout=_REQUEST_TIMEOUT,
    ) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"LobeHub token exchange failed {resp.status}: {body[:300]}")
        data = json.loads(body)
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("LobeHub token exchange returned empty access_token")
    expires_in = int(data.get("expires_in") or 3600)
    _token_cache["access_token"] = access_token
    _token_cache["cache_key"] = cache_key
    _token_cache["expires_at"] = time.time() + max(60, expires_in - 60)
    return access_token, base_url


async def _api_json(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    token, base_url = await _get_access_token(session)
    url = f"{base_url.rstrip('/')}/api{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    async with session.request(method, url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
        body = await resp.text()
        if resp.status != 200:
            raise RuntimeError(f"LobeHub API {resp.status} {path}: {body[:300]}")
        if not body:
            return {}
        return json.loads(body)


async def lobehub_search(query: str, *, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    q = (query or "").strip()
    cap = max(1, min(limit, 100))
    skip = max(0, offset)
    page_size = cap
    page = skip // page_size + 1
    params: dict[str, Any] = {"page": page, "pageSize": page_size, "locale": "en-US"}
    if q:
        params["q"] = q
    async with aiohttp.ClientSession() as session:
        data = await _api_json(
            session,
            "GET",
            "/v1/skills",
            params=params,
        )
    rows = data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    items: list[dict[str, Any]] = []
    skip_in_page = skip % page_size
    for row in rows:
        if not isinstance(row, dict):
            continue
        identifier = str(row.get("identifier") or "").strip()
        if not identifier:
            continue
        if skip_in_page > 0:
            skip_in_page -= 1
            continue
        items.append(row)
        if len(items) >= cap:
            break
    return items


async def lobehub_inspect(identifier: str) -> dict[str, Any]:
    key = (identifier or "").strip().strip("/")
    if not key:
        raise ValueError("lobehub identifier is required")
    async with aiohttp.ClientSession() as session:
        return await _api_json(
            session,
            "GET",
            f"/v1/skills/{key}",
            params={"locale": "en-US"},
        )


def _extract_text_files_from_zip(raw: bytes) -> dict[str, str]:
    files: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").lstrip("/")
            if not name or name.startswith(".") or "/." in f"/{name}/":
                continue
            if not _is_text_file(name):
                logging.warning("skip non-text lobehub file: %s", name)
                continue
            try:
                content = archive.read(info).decode("utf-8")
            except UnicodeDecodeError:
                logging.warning("skip binary lobehub file: %s", name)
                continue
            files[name] = content
    return files


def _extract_skill_md_from_zip(raw: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        skill_md_name = ""
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").lstrip("/")
            if name == "SKILL.md":
                skill_md_name = name
                break
            if name.endswith("/SKILL.md") and (not skill_md_name or len(name) < len(skill_md_name)):
                skill_md_name = name
        if not skill_md_name:
            return None
        try:
            return archive.read(skill_md_name).decode("utf-8")
        except UnicodeDecodeError:
            return None


async def lobehub_fetch_skill_md(identifier: str, *, version: str | None = None) -> str | None:
    key = (identifier or "").strip().strip("/")
    if not key:
        raise ValueError("lobehub identifier is required")
    async with aiohttp.ClientSession() as session:
        token, base_url = await _get_access_token(session)
        params: dict[str, str] = {}
        if version:
            params["version"] = version.strip()
        url = f"{base_url.rstrip('/')}/api/v1/skills/{key}/download"
        headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        async with session.get(url, params=params or None, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise RuntimeError(f"LobeHub download failed {resp.status}: {body}")
            raw = await resp.read()
    return _extract_skill_md_from_zip(raw)


async def lobehub_fetch_files(identifier: str, *, version: str | None = None) -> dict[str, str]:
    key = (identifier or "").strip().strip("/")
    if not key:
        raise ValueError("lobehub identifier is required")
    async with aiohttp.ClientSession() as session:
        token, base_url = await _get_access_token(session)
        params: dict[str, str] = {}
        if version:
            params["version"] = version.strip()
        url = f"{base_url.rstrip('/')}/api/v1/skills/{key}/download"
        headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        async with session.get(url, params=params or None, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                body = (await resp.text())[:300]
                raise RuntimeError(f"LobeHub download failed {resp.status}: {body}")
            raw = await resp.read()
    files = _extract_text_files_from_zip(raw)
    if "SKILL.md" not in files:
        raise RuntimeError(f"LobeHub bundle missing SKILL.md: {key}")
    return files
