from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillMeta:
    name: str
    description: str
    source: str
    identifier: str
    trust_level: str
    repo: str | None = None
    path: str | None = None
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillBundle:
    name: str
    files: dict[str, str]
    source: str
    identifier: str
    trust_level: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanFinding:
    pattern_id: str
    severity: str
    category: str
    file: str
    line: int
    match: str
    description: str


@dataclass
class ScanResult:
    skill_name: str
    source: str
    trust_level: str
    verdict: str
    findings: list[ScanFinding] = field(default_factory=list)
    summary: str = ""
