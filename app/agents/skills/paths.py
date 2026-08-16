"""
单个 skills 根目录下的路径/layout 解析（底层，不面向 agent 列举）。

Agent 侧「有哪些 skill、来自哪一层」请用 SkillsManager.list_skills。
Hub 扫描 data/skills 等单根场景可调用 find_skill / scan_root。
新建/安装目录布局见 build_skill_install_path。
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from .frontmatter import SkillFrontmatter


EXCLUDED_SKILL_DIRS = frozenset({".git", ".github", ".hub", "__pycache__"})
MAX_SKILL_REL_DEPTH = 2
DEFAULT_SKILL_CATEGORY = "general"


@dataclass(frozen=True, slots=True)
class SkillEntry:
    name: str
    description: str
    root_path: Path
    rel_path: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def abs_path(self) -> Path:
        return (self.root_path / self.rel_path.replace("\\", "/")).resolve()

    @property
    def abs_md_path(self) -> Path:
        return self.abs_path / "SKILL.md"

    @property
    def category(self) -> str:
        return _category_from_rel_path(self.rel_path)

    @property
    def preinstalled(self) -> bool:
        return bool(self.meta.get("preinstalled"))

    @property
    def always(self) -> bool:
        return bool(self.meta.get("always"))


def normalize_category(value: str) -> str:
    """将 category 规范为小写、空格转连字符的 slug。"""
    return str(value).strip().lower().replace(" ", "-")


def _rel_skill_path(skill_dir: Path, skills_root: Path) -> Path | None:
    """返回 skill 目录相对 skills 根的路径；不在根下则返回 None。"""
    try:
        return skill_dir.resolve().relative_to(skills_root.resolve())
    except ValueError:
        return None


def _category_from_rel_path(rel_path: str) -> str:
    parts = Path(rel_path.replace("\\", "/")).parts
    if len(parts) >= 2:
        normalized = normalize_category(parts[0])
        return normalized or DEFAULT_SKILL_CATEGORY
    return DEFAULT_SKILL_CATEGORY


def _is_valid_skill_md(skill_md: Path, skills_root: Path) -> bool:
    """有效 skill 入口：skills/<name>/ 或 skills/<category>/<name>/（相对深度 1–2）。"""
    if skill_md.name != "SKILL.md" or not skill_md.is_file():
        return False
    rel_dir = _rel_skill_path(skill_md.parent, skills_root)
    if rel_dir is None:
        return False
    parts = rel_dir.parts
    if len(parts) < 1 or len(parts) > MAX_SKILL_REL_DEPTH:
        return False
    return not any(part in EXCLUDED_SKILL_DIRS or part.startswith(".") for part in parts)


def _iter_valid_skill_md(skills_root: Path) -> Iterator[Path]:
    if not skills_root.is_dir():
        return
    for skill_md in skills_root.rglob("SKILL.md"):
        if _is_valid_skill_md(skill_md, skills_root):
            yield skill_md


def _build_entry(skill_md: Path, skills_root: Path, raw: str) -> SkillEntry:
    skill_dir = skill_md.parent.resolve()
    fm = SkillFrontmatter.parse(raw) or {}
    rel = _rel_skill_path(skill_dir, skills_root)
    if rel is None:
        raise ValueError(f"skill directory is not under skills root: {skill_dir}")
    
    rel_path = str(rel).replace("\\", "/")
    _name, description, meta = SkillFrontmatter.fields(fm)
    return SkillEntry(
        name=_name or skill_dir.name,
        description=description,
        rel_path=rel_path,
        root_path=skills_root.resolve(),
        meta=meta,
    )


def scan_root(skills_root: Path) -> dict[str, SkillEntry]:
    """扫描单个 skills 根目录，返回逻辑名 → 条目（name / description / meta / rel_path / root_path）。"""
    root = skills_root.resolve()
    if not root.is_dir():
        return {}
    
    skill_entries: dict[str, SkillEntry] = {}
    for skill_md in _iter_valid_skill_md(root):
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        entry = _build_entry(skill_md, root, raw)
        skill_entries[entry.name] = entry
    return skill_entries


def find_skill(skills_root: Path | None, name: str) -> SkillEntry | None:
    """在单个 skills 根目录下按逻辑名查找（不跨 workspace/agent/builtin 层）。"""
    key = (name or "").strip()
    if not key or skills_root is None or not skills_root.is_dir():
        return None
    return scan_root(skills_root.resolve()).get(key)


def build_skill_install_path(skills_root: Path, skill_name: str, category: str | None) -> Path:
    """计算 skill 安装/创建目标目录。无显式 category（或 general）→ skills/<name>/；否则 skills/<category>/<name>/。"""
    key = (skill_name or "").strip()
    if not key:
        raise ValueError("skill name required")
    
    raw = str(category).strip() if category else ""
    cat = normalize_category(raw) if raw else ""
    if not cat or cat == DEFAULT_SKILL_CATEGORY:
        return (skills_root / key).resolve()
    return (skills_root / cat / key).resolve()
