import json
from pathlib import Path
from typing import Any, Optional
from app.config.settings import settings


# ===============Agent相关的配置文件===========
AGENT_CONFIG_DIR = Path(settings.runtime_data_dir) / "agents"
DEFAULT_AGENT_TYPE = "Coder"
AGENT_CONTEXT_PATH = "prompts"
SUBAGENT_DIR_NAME = "subagents"
AGENT_CONTEXT_FILES = ["AGENT.md", "SOUL.md", "USER.md", "TOOLS.md", "RUNTIME.md"]
AGENT_CONFIG_FILE = "config.json"
AGENT_TYPE_PLANNING = "Planning"
PLANNING_PROMPT_USER = "PLANNING_USER.md"
PLANNING_PROMPT_JUDGE_USER = "JUDGE_USER.md"


def is_planning_agent_type(agent_type: str) -> bool:
    """会话 agent_type 为 Planning 时走 PlanningAgent 执行器。"""
    return (agent_type or "").strip().lower() == AGENT_TYPE_PLANNING.lower()


def resolve_subagent_dir(parent_agent_type: str, subagent_type: str) -> Path:
    """子 Agent 配置目录：``data/agents/{parent}/subagents/{type}``。"""
    parent = (parent_agent_type or "").strip()
    key = (subagent_type or "").strip()
    if not parent:
        raise ValueError("parent_agent_type is required")
    if not key:
        raise ValueError("subagent_type is required")
    return (AGENT_CONFIG_DIR / parent / SUBAGENT_DIR_NAME / key).resolve()

def load_subagent_config(
    parent_agent_type: str,
    subagent_type: str,
    *,
    parent_agent_config: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """指定主/子 Agent 类型，校验子 Agent 有效并返回其 config.json。

    校验：
    - 若传入 ``parent_agent_config``，则 ``subagent_type`` 须在 ``tools.spawn.allow_types`` 中
    - ``data/agents/{parent}/subagents/{type}/config.json`` 存在且可解析为对象
    """
    parent = (parent_agent_type or "").strip()
    chosen = (subagent_type or "").strip()
    if not parent:
        raise ValueError("parent_agent_type is required")
    if not chosen:
        raise ValueError("subagent_type is required")

    if parent_agent_config is not None:
        tools_block = (
            parent_agent_config.get("tools")
            if isinstance(parent_agent_config.get("tools"), dict)
            else {}
        )
        spawn_cfg = tools_block.get("spawn") if isinstance(tools_block.get("spawn"), dict) else {}
        allow_types = (
            [str(x).strip() for x in (spawn_cfg.get("allow_types") or []) if str(x).strip()]
            if isinstance(spawn_cfg.get("allow_types"), list)
            else []
        )
        if not allow_types:
            raise ValueError("spawn is not configured: tools.spawn.allow_types is empty")
        if chosen not in allow_types:
            raise ValueError(f"spawn type {chosen!r} is not in allow_types={allow_types}")

    subagent_dir = resolve_subagent_dir(parent, chosen)
    subagent_cfg_path = subagent_dir / AGENT_CONFIG_FILE
    if not subagent_cfg_path.is_file():
        raise ValueError(f"subagent type {chosen!r} is not installed at {subagent_dir}")
    try:
        raw = json.loads(subagent_cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to load subagent config {subagent_cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"subagent config must be a JSON object: {subagent_cfg_path}")
    return raw

def any_spawn_type_installed(parent_agent_type: str, allow_types: list[str] | None) -> bool:
    """父 Agent 的 allow_types 中是否至少有一个已安装的子 Agent 目录配置。"""
    if not isinstance(allow_types, list) or not allow_types:
        return False
    parent = (parent_agent_type or "").strip()
    if not parent:
        return False
    for raw in allow_types:
        chosen = str(raw).strip()
        if not chosen:
            continue
        try:
            load_subagent_config(parent, chosen)
            return True
        except ValueError:
            continue
    return False


# ==============存放技能的目录===============
SKILLS_DIR_NAME = "skills"
MEMORY_DIR_NAME = ".memory"

BUILTIN_SKILLS_DIR = (Path(settings.runtime_data_dir) / SKILLS_DIR_NAME).resolve()

def workspace_skills_dir(workspace_path: Path | str | None) -> Path | None:
    if not workspace_path:
        return None
    candidate = Path(workspace_path).expanduser().resolve() / SKILLS_DIR_NAME
    return candidate if candidate.is_dir() else None

def default_workspace_path(
    user_id: str,
    agent_type: str,
    workspace_path: Optional[str] = None,
) -> Path:
    """有 workspace_path 则用用户路径，否则默认沙箱 data/.workspace/{user_id}/{agent_type}/。"""
    raw = (workspace_path or "").strip()
    if raw:
        path = Path(raw).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    path = (Path(settings.runtime_data_dir) / ".workspace" / user_id / agent_type).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path
