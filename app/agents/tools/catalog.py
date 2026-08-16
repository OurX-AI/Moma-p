import importlib
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Type
from .base import BaseTool

# 全局的工具目录
_tools_catalog: Dict[str, "ToolCatalogSpec"] = {}
_tools_catalog_lock = threading.RLock()
_tools_catalog_loaded = False

@dataclass(frozen=True)
class ToolCatalogSpec:
    """工具目录注册项；instant(agent_ctx=...) 创建工具实例。"""
    name: str
    toolset: str
    cls: Type[BaseTool]
    instant: Callable[..., BaseTool]

# 装饰器：注册工具到工具目录
def register_tool(
    *,
    name: str,
    toolset: str,
) -> Callable[[Type[BaseTool]], Type[BaseTool]]:
    """装饰器：import 模块时登记 catalog，实例化统一为 cls(**kwargs)。"""

    def decorator(cls: Type[BaseTool]) -> Type[BaseTool]:
        with _tools_catalog_lock:
            existing = _tools_catalog.get(name)
            if existing is not None and existing.toolset != toolset:
                logging.error(
                    "tool catalog registration rejected: %s (%s) would shadow %s",
                    name,
                    toolset,
                    existing.toolset,
                )
                return cls
            _tools_catalog[name] = ToolCatalogSpec(
                name=name,
                toolset=toolset,
                cls=cls,
                instant=lambda tool_cls=cls, **kwargs: tool_cls(**kwargs),
            )
        return cls

    return decorator

def get_tool_catalog(name: str) -> Optional[ToolCatalogSpec]:
    with _tools_catalog_lock:
        return _tools_catalog.get(name)

def iter_catalog_specs() -> List[ToolCatalogSpec]:
    with _tools_catalog_lock:
        return list(_tools_catalog.values())

def catalog_tool_names() -> List[str]:
    ensure_tools_catalog_loaded()
    with _tools_catalog_lock:
        return sorted(_tools_catalog.keys())

def catalog_toolsets() -> List[str]:
    ensure_tools_catalog_loaded()
    with _tools_catalog_lock:
        return sorted({spec.toolset for spec in _tools_catalog.values()})

def tool_names_for_toolset(toolset: str) -> List[str]:
    ensure_tools_catalog_loaded()
    key = (toolset or "").strip()
    with _tools_catalog_lock:
        return sorted(name for name, spec in _tools_catalog.items() if spec.toolset == key)

_tool_readonly_cache: Dict[str, bool] = {}
def catalog_tool_is_readonly(name: str) -> Optional[bool]:
    """catalog 内建工具的 is_readonly；非 catalog 工具（如 MCP）返回 None。"""
    key = (name or "").strip()
    if not key:
        return None
    ensure_tools_catalog_loaded()
    with _tools_catalog_lock:
        if key in _tool_readonly_cache:
            return _tool_readonly_cache[key]
        spec = _tools_catalog.get(key)
        if spec is None:
            return None
        try:
            readonly = bool(spec.instant().is_readonly())
        except Exception:
            logging.exception("failed to read is_readonly for tool %s", key)
            readonly = False
        _tool_readonly_cache[key] = readonly
        return readonly

_SKIP_TOOL_PACKAGES = frozenset({"mcp"})
_SKIP_TOOL_MODULES = frozenset(
    {"base", "catalog", "factory", "policy", "schemes", "utils", "truncation", "__init__"},
)

def _discover_tool_modules() -> List[str]:
    """扫描 app.agents.tools 下 .py 模块（不要求子目录为 package）；import 后 @register_tool 写入 catalog。"""
    tools_pkg = importlib.import_module(__package__.rsplit(".", 1)[0])

    root = Path(tools_pkg.__file__).resolve().parent
    prefix = f"{tools_pkg.__name__}."
    names: List[str] = []
    for py in root.rglob("*.py"):
        if py.name.startswith("_"):
            continue
        if py.stem in _SKIP_TOOL_MODULES:
            continue
        rel_parts = py.relative_to(root).parts
        if not rel_parts:
            continue
        if rel_parts[0] in _SKIP_TOOL_PACKAGES:
            continue
        if len(rel_parts) == 1:
            names.append(f"{prefix}{py.stem}")
        else:
            names.append(f"{prefix}{'.'.join(rel_parts[:-1] + (py.stem,))}")
    return sorted(set(names))

def ensure_tools_catalog_loaded() -> None:
    global _tools_catalog_loaded
    if _tools_catalog_loaded:
        return
    for mod in _discover_tool_modules():
        try:
            importlib.import_module(mod)
        except Exception:
            logging.exception("failed to import tool module: %s", mod)
    _tools_catalog_loaded = True
