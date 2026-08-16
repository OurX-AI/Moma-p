from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ReadyResult:
    ok: bool
    repo_id: str = ""
    repo_path: str = ""
    status: str = ""
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolveResult:
    ok: bool
    repo_id: str = ""
    query: str = ""
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalyzeResult:
    ok: bool
    repo_id: str = ""
    status: str = ""
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IndexStatusResult:
    ok: bool
    repo_id: str = ""
    status: str = ""
    index_age_seconds: Optional[float] = None
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
