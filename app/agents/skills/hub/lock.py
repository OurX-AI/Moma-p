import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .constants import HUB_DIR, LOCK_FILE


def _ensure_hub_dir() -> None:
    HUB_DIR.mkdir(parents=True, exist_ok=True)


def load_lock() -> dict[str, dict[str, Any]]:
    _ensure_hub_dir()
    if not LOCK_FILE.is_file():
        return {}
    try:
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            entries = data.get("entries")
            if isinstance(entries, dict):
                return entries
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_lock(entries: dict[str, dict[str, Any]]) -> None:
    _ensure_hub_dir()
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    LOCK_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def get_lock_entry(name: str) -> dict[str, Any] | None:
    return load_lock().get(name)


def get_lock_entry_by_identifier(identifier: str) -> dict[str, Any] | None:
    key = (identifier or "").strip()
    if not key:
        return None
    for entry in load_lock().values():
        if str(entry.get("identifier") or "").strip() == key:
            return entry
    return None


def upsert_lock_entry(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    entries = load_lock()
    now = datetime.now(timezone.utc).isoformat()
    merged = dict(entries.get(name) or {})
    merged.update(entry)
    merged["name"] = name
    merged["installed_at"] = merged.get("installed_at") or now
    merged["updated_at"] = now
    entries[name] = merged
    save_lock(entries)
    return merged


def remove_lock_entry(name: str) -> bool:
    entries = load_lock()
    if name not in entries:
        return False
    del entries[name]
    save_lock(entries)
    return True


def list_installed() -> list[dict[str, Any]]:
    entries = load_lock()
    items = list(entries.values())
    items.sort(key=lambda x: str(x.get("name", "")))
    return items


def append_audit(line: str) -> None:
    _ensure_hub_dir()
    audit_path = HUB_DIR / "audit.log"
    stamp = datetime.now(timezone.utc).isoformat()
    with audit_path.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} {line}\n")
