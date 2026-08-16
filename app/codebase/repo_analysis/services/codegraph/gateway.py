import logging
from typing import Optional
from app.config.settings import settings
from .base import (
    CodeGraphGeneratorBase,
    CodeGraphProvider,
    CodeGraphSearchBase,
)
from .providers import PROVIDER_REGISTRY, create_provider


class CodeGraphGateway:
    """CodeGraph 对外统一入口：按 ENV 选择 Provider（均继承 base.CodeGraphProvider）。"""

    _provider: Optional[CodeGraphProvider] = None

    @staticmethod
    def _normalize_provider_name(raw: str) -> str:
        name = (raw or "codegraph").strip().lower()
        if name not in PROVIDER_REGISTRY:
            supported = ", ".join(sorted(PROVIDER_REGISTRY.keys()))
            raise ValueError(
                f"不支持的 CODE_GRAPH_PROVIDER={raw!r}，请使用: {supported}"
            )
        return name

    @classmethod
    def get_provider(cls) -> CodeGraphProvider:
        if cls._provider is not None:
            return cls._provider
        name = cls._normalize_provider_name(settings.code_graph_provider)
        cls._provider = create_provider(name)
        logging.info("CodeGraph provider=%s enabled=%s", cls._provider.name, settings.code_graph_enabled)
        return cls._provider

    @classmethod
    def reset_provider(cls) -> None:
        cls._provider = None

    @classmethod
    def ensure_ready(cls) -> None:
        """CLI 启动时调用：检查/安装当前 provider 依赖。"""
        if not settings.code_graph_enabled:
            logging.info("CODE_GRAPH_ENABLED=false，跳过 CodeGraph 就绪检查")
            return
        cls.get_provider().ensure_ready()

    @classmethod
    def create_generator(
        cls,
        repo_id: str,
        repo_name: str,
        repo_local_path: str,
    ) -> CodeGraphGeneratorBase:
        return cls.get_provider().create_generator(repo_id, repo_name, repo_local_path)

    @classmethod
    def create_search(cls) -> CodeGraphSearchBase:
        return cls.get_provider().create_search()
