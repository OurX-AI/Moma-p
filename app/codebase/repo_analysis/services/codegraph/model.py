from dataclasses import dataclass
from typing import Any,Dict


@dataclass
class QueryResponse:
    result: bool
    content: Dict[str,Any]
    message: str = ""