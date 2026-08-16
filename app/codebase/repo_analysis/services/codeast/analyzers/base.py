from abc import ABC, abstractmethod
from typing import Optional
from app.utils.common import strip_utf8_bom
from ..model import FileInfo


class LanguageAnalyzer(ABC):
    def __init__(self, base_path: str, file_path: str):
        """初始化基类"""
        self.base_path = base_path
        self.file_path = file_path

    def _read_source_file(self) -> str:
        """与 CodeChunkService 一致：UTF-8，非法字节忽略。"""
        with open(self.file_path, "r", encoding="utf-8", errors="ignore") as f:
            return strip_utf8_bom(f.read())

    @abstractmethod
    async def analyze_file(self, source: Optional[str] = None) -> Optional[FileInfo]:
        """具体的文件分析逻辑；传入 source 时不再读盘（与切片共用一次读取）。"""
        pass