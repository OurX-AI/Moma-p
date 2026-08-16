import json
import re
from datetime import datetime, timezone
from typing import Any
from .constants import HUB_DIR
from .lock import append_audit

WELLKNOWN_HOSTS_FILE = HUB_DIR / "wellknown_hosts.json"
HOST_PATTERN = re.compile(
    r"^(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)
BUILTIN_WELLKNOWN_HOSTS = frozenset({
    "developers.cloudflare.com",
})


def _ensure_hub_dir() -> None:
    HUB_DIR.mkdir(parents=True, exist_ok=True)


def normalize_wellknown_host(raw: str) -> str:
    text = (raw or "").strip().lower()
    if not text:
        raise ValueError("well-known host is required")
    for prefix in ("well-known:", "wellknown:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text.startswith("http://"):
        text = text[7:]
    elif text.startswith("https://"):
        text = text[8:]
    text = text.split("/")[0].strip()
    if not HOST_PATTERN.match(text):
        raise ValueError(f"invalid well-known host: {raw}")
    return text


def load_custom_wellknown_hosts() -> list[str]:
    _ensure_hub_dir()
    if not WELLKNOWN_HOSTS_FILE.is_file():
        return []
    try:
        data = json.loads(WELLKNOWN_HOSTS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        raw = data.get("hosts")
        if not isinstance(raw, list):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for item in raw:
            try:
                host = normalize_wellknown_host(str(item))
            except ValueError:
                continue
            if host in seen or host in BUILTIN_WELLKNOWN_HOSTS:
                continue
            seen.add(host)
            result.append(host)
        return result
    except (OSError, json.JSONDecodeError):
        return []


def get_search_hosts() -> list[str]:
    hosts = sorted(BUILTIN_WELLKNOWN_HOSTS | set(load_custom_wellknown_hosts()))
    return hosts


def list_wellknown_hosts() -> dict[str, Any]:
    custom = load_custom_wellknown_hosts()
    return {
        "builtin": sorted(BUILTIN_WELLKNOWN_HOSTS),
        "custom": custom,
        "search_hosts": get_search_hosts(),
    }


def add_wellknown_host(host: str) -> dict[str, Any]:
    key = normalize_wellknown_host(host)
    if key in BUILTIN_WELLKNOWN_HOSTS:
        raise ValueError(f"host already builtin: {key}")
    custom = load_custom_wellknown_hosts()
    if key in custom:
        raise ValueError(f"host already registered: {key}")
    custom.append(key)
    _save_hosts(custom)
    append_audit(f"wellknown host add {key}")
    return {"host": key, "custom": custom}


def remove_wellknown_host(host: str) -> dict[str, Any]:
    key = normalize_wellknown_host(host)
    custom = load_custom_wellknown_hosts()
    if key not in custom:
        raise ValueError(f"custom well-known host not found: {key}")
    custom = [h for h in custom if h != key]
    _save_hosts(custom)
    append_audit(f"wellknown host remove {key}")
    return {"host": key, "removed": True, "custom": custom}


def _save_hosts(hosts: list[str]) -> None:
    _ensure_hub_dir()
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "hosts": hosts,
    }
    WELLKNOWN_HOSTS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
