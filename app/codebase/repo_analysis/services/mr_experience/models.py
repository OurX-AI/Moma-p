from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class FileChange:
    path: str
    status: str
    additions: int = 0
    deletions: int = 0

    @property
    def churn(self) -> int:
        return int(self.additions) + int(self.deletions)


@dataclass
class GitHistoryEntry:
    commit_sha: str
    message: str
    committed_at: Optional[datetime]
    is_merge: bool
    files: List[FileChange] = field(default_factory=list)


@dataclass
class ExperienceStep:
    """兼容旧版 steps 字段；新管线优先使用 plan/patterns/anchors。"""

    file: str
    action: str


@dataclass
class ExperiencePattern:
    """可复用的开发经验：场景 + 模式。"""

    title: str
    scenario: str
    patterns: List[str]
    source_commits: List[str]
    commit_message: str = ""
    quality_score: float = 0.0
    plan: List[str] = field(default_factory=list)
    anchors: List[str] = field(default_factory=list)
    relevant_files: List[str] = field(default_factory=list)
    steps: List[ExperienceStep] = field(default_factory=list)

    def to_payload(self) -> dict:
        return {
            "title": self.title,
            "scenario": self.scenario,
            "patterns": list(self.patterns),
            "quality_score": float(self.quality_score or 0.0),
            "source_commits": list(self.source_commits),
            "anchors": list(self.anchors),
            "relevant_files": list(self.relevant_files),
            "plan": list(self.plan),
        }

    @classmethod
    def from_payload(cls, data: dict) -> Optional["ExperiencePattern"]:
        if not isinstance(data, dict):
            return None
        title = str(data.get("title") or "").strip()
        if not title:
            return None
        steps_raw = data.get("steps") or []
        steps: List[ExperienceStep] = []
        if isinstance(steps_raw, list):
            for item in steps_raw:
                if not isinstance(item, dict):
                    continue
                fp = str(item.get("file") or "").strip()
                action = str(item.get("action") or "").strip()
                if fp and action:
                    steps.append(ExperienceStep(file=fp, action=action))
        plan = [str(x).strip() for x in (data.get("plan") or []) if str(x).strip()]
        if not plan and steps:
            plan = [f"{s.file}: {s.action}" for s in steps]
        return cls(
            title=title,
            scenario=str(data.get("scenario") or "").strip(),
            plan=plan,
            patterns=[str(x).strip() for x in (data.get("patterns") or []) if str(x).strip()],
            anchors=[str(x).strip() for x in (data.get("anchors") or []) if str(x).strip()],
            source_commits=[str(x) for x in (data.get("source_commits") or []) if x],
            commit_message=str(data.get("commit_message") or "").strip(),
            quality_score=float(data.get("quality_score") or 0.0),
            relevant_files=[str(x).strip() for x in (data.get("relevant_files") or []) if str(x).strip()],
            steps=steps,
        )


@dataclass
class ExperienceExtractionResult:
    """单次 MR 经验提炼结果：可提取 / 跳过 / 失败由异常表示。"""

    extractable: bool
    skip_reason: str = ""
    patterns: List[ExperiencePattern] = field(default_factory=list)
