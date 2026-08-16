import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple
from app.agents.contants import AGENT_CONFIG_DIR, AGENT_CONFIG_FILE, DEFAULT_AGENT_TYPE

AGENT_TYPE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
PROTECTED_AGENT_DIRS = frozenset({".example", "SubAgent"})


class AgentConfigError(ValueError):
    """Agent 配置业务错误。"""


def resolved_agents_root() -> Path:
    root = AGENT_CONFIG_DIR.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_agent_type(agent_type: str) -> str:
    key = (agent_type or "").strip()
    if not key or not AGENT_TYPE_PATTERN.match(key):
        raise AgentConfigError(
            "agent_type 须以字母开头，仅含字母、数字、下划线与连字符"
        )
    if key.startswith("."):
        raise AgentConfigError("agent_type 不能以 . 开头")
    return key


def agent_dir(agent_type: str) -> Path:
    validate_agent_type(agent_type)
    return resolved_agents_root() / agent_type


def load_meta(agent_dir_path: Path) -> Tuple[str, str, bool]:
    path = agent_dir_path / AGENT_CONFIG_FILE
    if not path.is_file():
        return DEFAULT_AGENT_TYPE, "", False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return DEFAULT_AGENT_TYPE, "", False
        name = data.get("name_zh") or DEFAULT_AGENT_TYPE
        desc = data.get("description_zh") or ""
        internal = bool(data.get("internal"))
        return str(name), str(desc), internal
    except Exception as e:
        logging.warning("Failed to load agent meta for %s: %s", agent_dir_path, e)
        return DEFAULT_AGENT_TYPE, "", False


def load_config(agent_dir_path: Path) -> Dict[str, Any]:
    path = agent_dir_path / AGENT_CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError as e:
        raise AgentConfigError(f"config.json 不是合法 JSON: {e}") from e


def parse_external_dirs(config: Dict[str, Any]) -> List[str]:
    skills = config.get("skills") if isinstance(config, dict) else None
    if not isinstance(skills, dict):
        return []
    raw = skills.get("external_dirs")
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def skills_manager_for_agent(agent_type: str, config: Dict[str, Any] | None = None):
    from app.agents.skills.manager import SkillsManager

    key = validate_agent_type(agent_type)
    path = agent_dir(key)
    if config is None:
        config = load_config(path)
    external = parse_external_dirs(config)
    return SkillsManager(key, external_dirs=external or None)


def list_agents(*, include_internal: bool = False) -> List[Dict[str, Any]]:
    root = resolved_agents_root()
    if not root.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if child.name in PROTECTED_AGENT_DIRS:
            continue
        try:
            key = validate_agent_type(child.name)
        except AgentConfigError:
            continue
        if not (child / AGENT_CONFIG_FILE).is_file():
            continue
        name, description, internal = load_meta(child)
        if internal and not include_internal:
            continue
        out.append(
            {
                "agent_type": key,
                "name": name,
                "description": description,
                "protected": key == DEFAULT_AGENT_TYPE,
                "internal": internal,
            }
        )
    return out
