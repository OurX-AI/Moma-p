import hashlib
import shutil
from pathlib import Path
from typing import Any
from ..frontmatter import SkillFrontmatter
from ..paths import DEFAULT_SKILL_CATEGORY, build_skill_install_path, find_skill, normalize_category


def resolve_hub_skill_dir(
    skills_root: Path,
    name: str,
    lock_entry: dict[str, Any] | None = None,
) -> Path:
    """解析 Hub 已安装 skill 目录：优先 lock rel_path，再按名扫描。"""
    if lock_entry:
        rel_path = str(lock_entry.get("rel_path") or lock_entry.get("install_path") or "").strip()
        if rel_path:
            candidate = (skills_root / rel_path.replace("\\", "/")).resolve()
            if candidate.is_dir():
                return candidate
    entry = find_skill(skills_root, name)
    if entry:
        return entry.abs_path
    raise ValueError(f"skill directory not found: {name}")


def remove_skill_directory(
    skills_root: Path,
    name: str,
    *,
    lock_entry: dict[str, Any] | None = None,
) -> Path | None:
    """按 lock rel_path 或 skill 名删除目录，返回已删路径；未找到则 None。"""
    candidates: list[Path] = []
    seen: set[str] = set()
    if lock_entry:
        rel_path = str(lock_entry.get("rel_path") or lock_entry.get("install_path") or "").strip()
        if rel_path:
            candidates.append((skills_root / rel_path.replace("\\", "/")).resolve())
    entry = find_skill(skills_root, name)
    if entry:
        candidates.append(entry.abs_path)
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            shutil.rmtree(path)
            return path
    return None


def skill_name_from_bundle(files: dict[str, str], dir_name: str) -> str:
    skill_md = files.get("SKILL.md")
    if skill_md:
        name = SkillFrontmatter.name(SkillFrontmatter.parse(skill_md) or {})
        if name:
            return name
    key = (dir_name or "").strip()
    if key and SkillFrontmatter.NAME_PATTERN.match(key):
        return key
    raise ValueError("unable to determine skill name from bundle")


def skill_description_from_bundle(files: dict[str, str], default: str = "") -> str:
    skill_md = files.get("SKILL.md")
    if skill_md:
        desc = SkillFrontmatter.description(SkillFrontmatter.parse(skill_md) or {})
        if desc:
            return desc
    return (default or "").strip() or "Skill"


def category_from_install_hint(identifier_path: str = "") -> str:
    """Hub 安装时从 identifier 路径首段推断 category（如 github:owner/repo/devops/foo → devops）。"""
    path = (identifier_path or "").strip().strip("/")
    if path and "/" in path:
        head = path.split("/", 1)[0]
        normalized = normalize_category(head)
        if normalized:
            return normalized
    return DEFAULT_SKILL_CATEGORY


def resolve_install_dest(
    skills_root: Path,
    skill_name: str,
    files: dict[str, str],
    *,
    metadata: dict[str, Any] | None = None,
    identifier_path: str = "",
    category: str | None = None,
) -> tuple[Path, str]:
    """返回 (安装目录, category)。category 仅来自显式参数或 identifier 路径段。"""
    _ = files, metadata
    if category and str(category).strip():
        resolved = normalize_category(str(category)) or DEFAULT_SKILL_CATEGORY
    else:
        resolved = category_from_install_hint(identifier_path)
    dest = build_skill_install_path(skills_root, skill_name, resolved)
    return dest, resolved


def content_hash(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[rel].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def copy_bundle_to_dir(files: dict[str, str], dest_dir: Path) -> None:
    if dest_dir.exists():
        raise ValueError(f"skill directory already exists: {dest_dir.name}")
    dest_dir.mkdir(parents=True, exist_ok=False)
    for rel, text in files.items():
        rel_norm = rel.replace("\\", "/")
        if ".." in rel_norm.split("/"):
            raise ValueError(f"unsafe path in bundle: {rel}")
        target = dest_dir / rel_norm
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def write_bundle_to_dir(files: dict[str, str], dest_dir: Path) -> None:
    import shutil
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    copy_bundle_to_dir(files, dest_dir)
