from __future__ import annotations
from typing import Any, Dict, Optional


class ResolveIndexNotReady(Exception):
    """索引尚不可检索，resolve 应返回结构化失败而非空结果。"""

    error_code = "index_not_ready"

    def __init__(
        self,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})
