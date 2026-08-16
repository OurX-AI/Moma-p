from typing import Dict, Type
from .base import BaseMemoryManager
from .default.memory import DefaultMemory
from ..schemes import AgentContext


_MEMORY_IMPLS: Dict[str, Type[BaseMemoryManager]] = {
    "default": DefaultMemory,
}

def register_memory(memory_type: str, ctx: AgentContext) -> BaseMemoryManager:
    impl = _MEMORY_IMPLS.get(memory_type)
    if impl is None:
        raise KeyError(f"unknown memory_type: {memory_type!r}")
    if not issubclass(impl, BaseMemoryManager):
        raise TypeError(
            f"memory impl {impl!r} must be a subclass of BaseMemoryManager",
        )
    return impl(ctx)
