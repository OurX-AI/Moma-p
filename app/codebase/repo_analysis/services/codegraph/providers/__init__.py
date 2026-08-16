"""CodeGraph Provider 注册表。

新增开源/自研实现时：
1. 在 providers/<name>/ 下新建子包，实现 CodeGraphProvider（继承 base.CodeGraphProvider）
2. 在本模块 PROVIDER_REGISTRY 登记 name -> 工厂
3. ENV CODE_GRAPH_PROVIDER=<name> 即可切换
"""
from typing import Callable, Dict
from ..base import CodeGraphProvider


def _builtin() -> CodeGraphProvider:
    from .builtin import BuiltinCodeGraphProvider
    return BuiltinCodeGraphProvider()


def _codegraph() -> CodeGraphProvider:
    from .codegraph import CodeGraphCliProvider
    return CodeGraphCliProvider()


PROVIDER_REGISTRY: Dict[str, Callable[[], CodeGraphProvider]] = {
    "builtin": _builtin,
    "codegraph": _codegraph,
}


def create_provider(name: str) -> CodeGraphProvider:
    factory = PROVIDER_REGISTRY.get(name)
    if factory is None:
        supported = ", ".join(sorted(PROVIDER_REGISTRY.keys()))
        raise ValueError(f"未知 CODE_GRAPH_PROVIDER={name!r}，已支持: {supported}")
    return factory()
