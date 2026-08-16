"""Skill 用户备注：独立 JSON 存储，不写入 SKILL.md，Hub 更新不会覆盖。"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.agents.contants import BUILTIN_SKILLS_DIR


REMARKS_FILE = BUILTIN_SKILLS_DIR / ".remarks.json"


def _ensure_parent() -> None:
    REMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_remarks() -> dict[str, dict[str, Any]]:
    _ensure_parent()
    if not REMARKS_FILE.is_file():
        return {}
    try:
        data = json.loads(REMARKS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            entries = data.get("entries")
            if isinstance(entries, dict):
                return entries
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_remarks(entries: dict[str, dict[str, Any]]) -> None:
    _ensure_parent()
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    REMARKS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_skill_remark_entry(name: str) -> dict[str, Any] | None:
    key = (name or "").strip()
    if not key:
        return None
    row = load_remarks().get(key)
    if not isinstance(row, dict):
        return None
    remark = str(row.get("remark") or "").strip()
    if not remark:
        return None
    updated_at = str(row.get("updated_at") or "").strip()
    return {"remark": remark, "updated_at": updated_at or None}


def get_skill_remark(name: str) -> str:
    entry = get_skill_remark_entry(name)
    return entry["remark"] if entry else ""


def skill_remark_payload(name: str) -> dict[str, Any]:
    """返回 remark / remark_updated_at，无备注时 remark 为空字符串。"""
    entry = get_skill_remark_entry(name)
    return {
        "remark": entry["remark"] if entry else "",
        "remark_updated_at": entry.get("updated_at") if entry else None,
    }


def set_skill_remark(name: str, remark: str) -> dict[str, Any]:
    key = (name or "").strip()
    if not key:
        raise ValueError("skill name required")
    cleaned = (remark or "").strip()
    entries = load_remarks()
    now = datetime.now(timezone.utc).isoformat()
    if cleaned:
        entries[key] = {"remark": cleaned, "updated_at": now}
    else:
        entries.pop(key, None)
    save_remarks(entries)
    if cleaned:
        return {"name": key, "remark": cleaned, "updated_at": now}
    return {"name": key, "remark": "", "updated_at": None}


def remove_skill_remark(name: str) -> None:
    key = (name or "").strip()
    if not key:
        return
    entries = load_remarks()
    if key in entries:
        entries.pop(key, None)
        save_remarks(entries)
