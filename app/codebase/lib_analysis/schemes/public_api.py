from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class PublicApi:
    """Lib 公开接口文档模型（与 code 符号摘要分离）。"""

    name: str
    kind: str
    signature: str
    file_path: str
    language: str
    start_line: int
    end_line: int
    params: List[str] = field(default_factory=list)
    param_types: List[str] = field(default_factory=list)
    returns: List[str] = field(default_factory=list)
    return_types: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    source_code: str = ""
    class_name: Optional[str] = None

    def display_name(self) -> str:
        if self.class_name and self.kind == "method":
            return f"{self.class_name}.{self.name}"
        return self.name
