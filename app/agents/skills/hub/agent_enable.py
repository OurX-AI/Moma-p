import json
import logging
from pathlib import Path
from typing import Any
from app.agents.contants import AGENT_CONFIG_FILE
PERMISSION_ALLOW = "allow"


def _skill_enabled_in_config(config: dict[str, Any], skill_name: str) -> bool:
    skills = config.get("skills")
    if not isinstance(skills, dict):
        return True
    perms = skills.get("permissions")
    if not isinstance(perms, dict) or not perms:
        return True
    return str(perms.get(skill_name) or "").strip().lower() == PERMISSION_ALLOW


def build_skill_enabled_agents_map() -> dict[str, list[dict[str, str]]]:
    """扫描各 Agent config，返回 skill 名 → 已启用该 Skill 的 Agent 列表。"""
    from app.agents.api.agents import _list_agents
    from app.agents.api.common import agent_dir, load_config, load_meta, skills_manager_for_agent

    result: dict[str, list[dict[str, str]]] = {}
    for agent in _list_agents(include_internal=False):
        agent_type = str(agent.get("agent_type") or "").strip()
        if not agent_type:
            continue
        try:
            path = agent_dir(agent_type)
            if not path.is_dir():
                continue
            config = load_config(path)
            sm = skills_manager_for_agent(agent_type, config)
            catalog_names = {s["name"] for s in sm.list_skills(filter_unavailable=False)}
            name_zh, _, _ = load_meta(path)
            for skill_name in catalog_names:
                if not _skill_enabled_in_config(config, skill_name):
                    continue
                result.setdefault(skill_name, []).append(
                    {
                        "agent_type": agent_type,
                        "name": name_zh or agent_type,
                    }
                )
        except Exception as e:
            logging.warning("build_skill_enabled_agents_map: %s failed: %s", agent_type, e)
    for skill_name in result:
        result[skill_name].sort(key=lambda row: row["agent_type"])
    return result


def agents_with_skill_enabled(skill_name: str) -> list[dict[str, str]]:
    key = (skill_name or "").strip()
    if not key:
        return []
    return list(build_skill_enabled_agents_map().get(key, []))


def _set_skill_allowed(
    config: dict[str, Any],
    skill_name: str,
    allowed: bool,
    all_skill_names: list[str],
) -> dict[str, Any]:
    skills = config.setdefault("skills", {})
    if not isinstance(skills, dict):
        skills = {}
        config["skills"] = skills
    perms = dict(skills.get("permissions") or {})
    if not isinstance(perms, dict):
        perms = {}

    if not perms and not allowed:
        for name in all_skill_names:
            if name != skill_name:
                perms[name] = PERMISSION_ALLOW
        skills["permissions"] = perms
        return config

    if allowed:
        perms[skill_name] = PERMISSION_ALLOW
    else:
        perms.pop(skill_name, None)

    allowed_count = sum(
        1 for name in all_skill_names if str(perms.get(name) or "").lower() == PERMISSION_ALLOW
    )
    if allowed_count >= len(all_skill_names):
        skills["permissions"] = {}
    else:
        skills["permissions"] = perms
    return config


def _save_config(agent_path: Path, config: dict[str, Any]) -> None:
    config_path = agent_path / AGENT_CONFIG_FILE
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def enable_skill_for_agents(
    skill_name: str,
    agent_types: list[str],
    *,
    require_in_catalog: bool = True,
) -> list[dict[str, Any]]:
    return _apply_skill_for_agents(
        skill_name,
        agent_types,
        allowed=True,
        require_in_catalog=require_in_catalog,
    )


def sync_skill_for_agents(skill_name: str, agent_types: list[str]) -> list[dict[str, Any]]:
    """按勾选结果同步各 Agent 对该 Skill 的允许状态（仅处理目录中已有该 Skill 的 Agent）。"""
    from app.agents.api.agents import _list_agents

    key = (skill_name or "").strip()
    if not key:
        raise ValueError("skill name is required")
    target_set = {str(x).strip() for x in agent_types if str(x).strip()}
    results: list[dict[str, Any]] = []
    for agent in _list_agents(include_internal=False):
        agent_type = str(agent.get("agent_type") or "").strip()
        if not agent_type:
            continue
        allowed = agent_type in target_set
        row = _apply_skill_for_agents(
            key,
            [agent_type],
            allowed=allowed,
            require_in_catalog=True,
        )
        if row:
            results.append(row[0])
        else:
            results.append({"agent_type": agent_type, "status": "skill_not_in_catalog"})
    return results


def _apply_skill_for_agents(
    skill_name: str,
    agent_types: list[str],
    *,
    allowed: bool,
    require_in_catalog: bool = True,
) -> list[dict[str, Any]]:
    from app.agents.api.common import agent_dir, load_config, skills_manager_for_agent

    key = (skill_name or "").strip()
    if not key:
        raise ValueError("skill name is required")
    targets = [str(x).strip() for x in agent_types if str(x).strip()]
    if not targets:
        return []
    results: list[dict[str, Any]] = []
    for agent_type in targets:
        try:
            path = agent_dir(agent_type)
        except Exception as e:
            results.append({"agent_type": agent_type, "status": "error", "error": str(e)})
            continue
        if not path.is_dir():
            results.append({"agent_type": agent_type, "status": "not_found"})
            continue
        try:
            config = load_config(path)
            sm = skills_manager_for_agent(agent_type, config)
            catalog_names = [s["name"] for s in sm.list_skills(filter_unavailable=False)]
            if key not in catalog_names:
                if require_in_catalog:
                    results.append({"agent_type": agent_type, "status": "skill_not_in_catalog"})
                    continue
                catalog_names.append(key)
            if _skill_enabled_in_config(config, key) == allowed:
                results.append({"agent_type": agent_type, "status": "unchanged"})
                continue
            config = _set_skill_allowed(config, key, allowed, catalog_names)
            _save_config(path, config)
            results.append({"agent_type": agent_type, "status": "enabled" if allowed else "disabled"})
        except Exception as e:
            logging.warning("apply skill %s for %s failed: %s", key, agent_type, e)
            results.append({"agent_type": agent_type, "status": "error", "error": str(e)})
    return results
