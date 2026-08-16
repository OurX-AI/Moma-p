import logging
from pathlib import Path
from typing import Optional
from .api_extract import PublicApiExtractor
from .api_vector import ApiVectorService
from ...repo_analysis.services.analysis_service import AnalysisService
from ...repo_analysis.services.codeast.ast_analyzer import FileAstAnalyzer
from app.utils.common import strip_utf8_bom


class LibFileProcessor:
    """单文件 Lib 分析：AST → 公开接口 → LLM 摘要 → 向量。"""

    @staticmethod
    async def analyze_file(
        repo_id: str,
        repo_path: str,
        rel_file_path: str,
        abs_file_path: str,
    ) -> tuple[bool, Optional[str]]:
        try:
            if PublicApiExtractor.should_skip_file(rel_file_path):
                await ApiVectorService.delete_file_vector_records(repo_id, rel_file_path)
                return True, None

            ext = Path(abs_file_path).suffix.lower()
            if ext not in AnalysisService.CODE_EXTENSIONS:
                await ApiVectorService.delete_file_vector_records(repo_id, rel_file_path)
                return True, None

            source = strip_utf8_bom(Path(abs_file_path).read_text(encoding="utf-8", errors="ignore"))
            file_info = await FileAstAnalyzer(repo_path, abs_file_path).analyze_file(source=source)
            apis = PublicApiExtractor.extract(file_info, source=source)
            await ApiVectorService.vectorize_and_store_apis(repo_id, rel_file_path, apis)
            return True, None
        except Exception as e:
            logging.error(
                "Lib 文件分析失败 repo_id=%s file_path=%s error=%s",
                repo_id,
                rel_file_path,
                e,
            )
            return False, str(e)
