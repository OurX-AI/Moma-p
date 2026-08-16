"""NL→Code 检索增强总开关。"""
from __future__ import annotations
from app.config.settings import settings


class NlToCodeEnhancement:
    """查询侧 NL→Code 增强（多视角 embed / 仓内词表 / 可选 LLM 改写 / token 加权）。"""

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(settings.code_analysis_nl_to_code_enabled)

    @classmethod
    async def lexicon_for_repo(cls, repo_id: str):
        if not cls.is_enabled():
            return None
        from .lexicon import RepoIdentifierLexicon

        return await RepoIdentifierLexicon.for_repo(repo_id)
