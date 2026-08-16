from app.agents.contants import BUILTIN_SKILLS_DIR
from ..paths import scan_root
from .lock import load_lock

_preinstalled_names_cache: frozenset[str] | None = None


def get_preinstalled_skill_names() -> frozenset[str]:
    """data/skills 中未通过 Hub 安装（无 lock 记录）的 skill，均视为系统自带。"""
    global _preinstalled_names_cache
    if _preinstalled_names_cache is not None:
        return _preinstalled_names_cache
    if not BUILTIN_SKILLS_DIR.is_dir():
        _preinstalled_names_cache = frozenset()
        return _preinstalled_names_cache
    hub_managed = set(load_lock().keys())
    index = scan_root(BUILTIN_SKILLS_DIR)
    result = {name for name in index if name not in hub_managed}
    _preinstalled_names_cache = frozenset(result)
    return _preinstalled_names_cache


def invalidate_preinstalled_cache() -> None:
    global _preinstalled_names_cache
    _preinstalled_names_cache = None


def is_preinstalled_skill(name: str) -> bool:
    key = (name or "").strip()
    if not key:
        return False
    return key in get_preinstalled_skill_names()
