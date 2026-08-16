"""
技能以「目录/SKILL.md」形式存在。
来源：workspace（<workspace>/skills/，只读，最高优先级）、builtin（data/skills，可写）、external（skills.external_dirs，只读）。
布局：skills/<name>/（category=general）或 skills/<category>/<name>/（category 由首段目录决定）。
支持 frontmatter 中的 description、metadata（momacoder/openclaw：requires.bins/env、always 等）。
常驻技能（always=true）全文进 system prompt，其余仅进摘要，由 Agent 用 skill_view 按需加载。
"""
import logging
import os
import shutil
from pathlib import Path
from typing import Any
from app.utils.common import increase_md_heading_levels, normalize_rel_path
from ..contants import BUILTIN_SKILLS_DIR, workspace_skills_dir
from .frontmatter import SkillFrontmatter
from .paths import (
    DEFAULT_SKILL_CATEGORY,
    SkillEntry,
    build_skill_install_path,
    normalize_category,
    scan_root,
)

SKILL_SOURCE_EXTERNAL = "external"
SKILL_SOURCE_BUILTIN = "builtin"
SKILL_SOURCE_WORKSPACE = "workspace"
READONLY_SKILL_SOURCES = frozenset({SKILL_SOURCE_EXTERNAL, SKILL_SOURCE_WORKSPACE})


class SkillsManager:
    """
    技能加载器：从 workspace、builtin、external 列举/读取 SKILL.md（同名 workspace 优先），
    为 ContextBuilder 提供常驻技能全文与全体技能摘要（按需加载时 Agent 用 skill_view 读 name）。
    """

    def __init__(
        self,
        agent_type: str = "",
        filter_skills: list[str] | None = None,
        allow_manage: bool = False,
        external_dirs: list[str] | None = None,
        workspace_path: Path | str | None = None,
    ):
        self.builtin_skills_dir = BUILTIN_SKILLS_DIR
        self.agent_type = (agent_type or "").strip()
        self.filter_skills = filter_skills
        self.allow_manage = allow_manage
        self._root_index_cache: dict[str, tuple[float, dict[str, SkillEntry], dict[str, float]]] = {}
        self.workspace_skills_dir = workspace_skills_dir(workspace_path)
        self.external_skills_dirs: list[Path] = []
        if external_dirs:
            for raw in external_dirs:
                text = str(raw).strip()
                if not text:
                    continue
                path = Path(text).expanduser()
                if path.is_dir():
                    self.external_skills_dirs.append(path.resolve())

    def is_skill_allowed(self, name: str) -> bool:
        key = (name or "").strip()
        if not key:
            return False
        found = self.find_skill(key)
        if found and found[1] == SKILL_SOURCE_WORKSPACE:
            return True
        if not self.filter_skills:
            return True
        return key in self.filter_skills

    def is_skill_readonly(self, name: str) -> bool:
        """workspace / external 技能不可通过 skill_manage 修改或删除。"""
        found = self.find_skill(name)
        return found is not None and found[1] in READONLY_SKILL_SOURCES

    @staticmethod
    def validate_skill_name(name: str) -> str:
        key = (name or "").strip()
        if not key or not SkillFrontmatter.NAME_PATTERN.match(key):
            raise ValueError("Skill name must start with a letter and contain only letters, digits, underscore, hyphen.")
        return key

    def _invalidate_caches(self, name: str | None = None) -> None:
        _ = name
        self._root_index_cache.clear()
        from .hub.preinstalled import invalidate_preinstalled_cache
        invalidate_preinstalled_cache()

    @staticmethod
    def _md_stamps(entries: dict[str, SkillEntry]) -> dict[str, float]:
        """扫描后记录各 skill 的 SKILL.md mtime，供缓存校验用。"""
        stamps: dict[str, float] = {}
        for name, entry in entries.items():
            try:
                stamps[name] = entry.abs_md_path.stat().st_mtime
            except OSError:
                stamps[name] = 0.0
        return stamps

    @staticmethod
    def _root_cache_valid(
        skills_root: Path,
        root_stamp: float,
        entries: dict[str, SkillEntry],
        md_stamps: dict[str, float],
    ) -> bool:
        """判断 skills 根索引缓存是否仍有效：根目录 mtime 未变，且各 SKILL.md 仍存在且 mtime 未变。"""
        try:
            if skills_root.stat().st_mtime != root_stamp:
                return False
        except OSError:
            return False
        for name, entry in entries.items():
            md = entry.abs_md_path
            try:
                if not md.is_file() or md.stat().st_mtime != md_stamps.get(name):
                    return False
            except OSError:
                return False
        return True

    def _skills_for_root(self, skills_root: Path) -> dict[str, SkillEntry]:
        key = str(skills_root.resolve())
        try:
            stamp = skills_root.stat().st_mtime
        except OSError:
            return {}

        cached = self._root_index_cache.get(key)
        if cached:
            root_stamp, entries, md_stamps = cached
            if self._root_cache_valid(skills_root, root_stamp, entries, md_stamps):
                return entries
        skill_entries = scan_root(skills_root)
        md_stamps = self._md_stamps(skill_entries)
        self._root_index_cache[key] = (stamp, skill_entries, md_stamps)
        return skill_entries

    def find_skill(self, name: str) -> tuple[SkillEntry, str] | None:
        """跨层按名查找 skill：(entry, source)。"""
        key = (name or "").strip()
        if not key:
            return None
        for skills_root, source in self._skill_layers():
            if not skills_root:
                continue
            entry = self._skills_for_root(skills_root).get(key)
            if entry:
                return entry, source
        return None

    def _skill_layers(self) -> tuple[tuple[Path | None, str], ...]:
        """技能来源层：workspace → builtin → external（同名 workspace 优先）。"""
        layers: list[tuple[Path | None, str]] = []
        if self.workspace_skills_dir is not None:
            layers.append((self.workspace_skills_dir, SKILL_SOURCE_WORKSPACE))

        layers.append((self.builtin_skills_dir, SKILL_SOURCE_BUILTIN))
        
        for ext_dir in self.external_skills_dirs:
            layers.append((ext_dir, SKILL_SOURCE_EXTERNAL))
        return tuple(layers)

    @staticmethod
    def _entry_to_row(entry: SkillEntry, source: str) -> dict[str, Any]:
        return {
            "name": entry.name,
            "description": entry.description,
            "rel_path": entry.rel_path,
            "category": entry.category,
            "abs_path": str(entry.abs_path),
            "source": source,
            "always": entry.always,
            "meta": entry.meta,
        }

    def list_skills(
        self,
        filter_unavailable: bool = True,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        列举当前上下文可见的全部 skill（唯一对外列举入口）。
        按 workspace → builtin → external 合并，同名保留先出现的层。
        """
        skills: list[dict[str, Any]] = []
        seen: set[str] = set()
        for skills_root, source in self._skill_layers():
            if not skills_root:
                continue
            for entry in self._skills_for_root(skills_root).values():
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                skills.append(self._entry_to_row(entry, source))

        if filter_unavailable:
            skills = [
                s for s in skills
                if self._check_requirements(s.get("meta") or {})
            ]

        if category and category.strip():
            want = normalize_category(category)
            skills = [s for s in skills if s.get("category") == want]
        return skills

    def get_always_skills_content_for_context(self) -> str:
        """常驻技能（entry.always）正文，拼成 system prompt 的 # Always Skills 段；依赖未满足或 filter 排除的不包含。"""
        parts = []
        seen: set[str] = set()
        for skills_root, _source in self._skill_layers():
            if not skills_root:
                continue
            for entry in self._skills_for_root(skills_root).values():
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                if not entry.always or not self._check_requirements(entry.meta):
                    continue
                if not self.is_skill_allowed(entry.name):
                    continue
                content = self.get_skill_content(entry.name, entry=entry)
                if content:
                    content = increase_md_heading_levels(content, levels=2)
                    parts.append(f"## Skill: {entry.name}\n\n{content}")
        return "\n\n---\n\n".join(parts) if parts else ""

    # ===============================
    # skill相关操作
    # ===============================
    def load_skill(self, name: str, *, entry: SkillEntry | None = None) -> str | None:
        """按技能名读取 SKILL.md 全文；传入 entry 时跳过查找。"""
        if entry is None:
            found = self.find_skill(name)
            if not found:
                return None
            entry = found[0]
        return entry.abs_md_path.read_text(encoding="utf-8")

    def get_skill_content(
        self,
        name: str,
        file_path: str | None = None,
        *,
        entry: SkillEntry | None = None,
    ) -> str | None:
        """读取技能内容。默认返回 SKILL.md 正文（去 frontmatter）；file_path 为技能目录内相对路径时读该文件原文。"""
        if entry is None:
            found = self.find_skill(name)
            if not found:
                return None
            entry = found[0]
        abs_path = entry.abs_path

        # 默认读取 SKILL.md 正文（去 frontmatter）
        if not file_path or not file_path.strip():
            return SkillFrontmatter.strip(entry.abs_md_path.read_text(encoding="utf-8"))
        
        # 读取技能目录内相对路径的文件原文
        rel = normalize_rel_path(file_path)
        if rel is None:
            return None
        file = (abs_path / rel).resolve()
        try:
            file.relative_to(abs_path)
        except ValueError:
            return None
        if not file.is_file():
            return None
        return file.read_text(encoding="utf-8")
    
    @staticmethod
    def _rel_paths_under(skill_dir: Path, subdir: str, *, patterns: list[str] | None = None) -> list[str]:
        """列出技能目录内附属文件，按 patterns 匹配的文件。
        parameters:
        skill_dir: 技能目录
        subdir: 子目录
        patterns: 匹配模式
        return: 附属文件列表
        """ 
        base = skill_dir / subdir
        if not base.is_dir():
            return []
        paths: list[str] = []
        if patterns:
            seen: set[str] = set()
            for pattern in patterns:
                for path in base.rglob(pattern):
                    if not path.is_file():
                        continue
                    rel = str(path.relative_to(skill_dir)).replace("\\", "/")
                    if rel not in seen:
                        seen.add(rel)
                        paths.append(rel)
        else:
            for path in base.rglob("*"):
                if path.is_file():
                    paths.append(str(path.relative_to(skill_dir)).replace("\\", "/"))
        return sorted(paths)

    def list_linked_files(self, entry: SkillEntry) -> dict[str, list[str]]:
        """列出技能目录内附属文件，按 references/templates/scripts/assets 分类；无标准子目录时回退为 other。"""
        abs_path = entry.abs_path
        linked: dict[str, list[str]] = {}
        # /references/*.md
        references = self._rel_paths_under(abs_path, "references", patterns=["*.md"])
        if references:
            linked["references"] = references
        # /templates/*.md, *.py, *.yaml, *.yml, *.json, *.tex, *.sh
        templates = self._rel_paths_under(
            abs_path,
            "templates",
            patterns=["*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"],
        )
        if templates:
            linked["templates"] = templates
        # /scripts/*.py, *.sh, *.bash, *.js, *.ts, *.rb
        scripts = self._rel_paths_under(
            abs_path,
            "scripts",
            patterns=["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"],
        )
        if scripts:
            linked["scripts"] = scripts
        # /assets/* (all files under assets/)
        assets = self._rel_paths_under(abs_path, "assets")
        if assets:
            linked["assets"] = assets
        if linked:
            return linked
        # 其他文件
        other: list[str] = []
        for path in abs_path.rglob("*"):
            if not path.is_file() or path.name == "SKILL.md":
                continue
            other.append(str(path.relative_to(abs_path)).replace("\\", "/"))
            if len(other) >= 50:
                break
        if other:
            linked["other"] = sorted(other)
        return linked

    def build_skills_summary(self) -> str:
        """
        生成技能的 XML 摘要（按 category 分组：name、description、available、缺失的 requires），
        放入 system prompt 的「# Skills」，供 Agent 按需用 skill_view(name) 加载。
        filter_skills 非空时只包含名单内的技能（按目录名匹配）；为 None 时列举全部。
        """
        all_skills = self.list_skills(filter_unavailable=False)
        all_skills = [s for s in all_skills if self.is_skill_allowed(s["name"])]
        if not all_skills:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        all_skills.sort(key=lambda s: (s.get("category", DEFAULT_SKILL_CATEGORY), s["name"]))

        lines = ["<skills>"]
        current_category: str | None = None
        for s in all_skills:
            cat = s.get("category", DEFAULT_SKILL_CATEGORY)
            if cat != current_category:
                if current_category is not None:
                    lines.append("  </category>")
                lines.append(f"  <category name=\"{escape_xml(cat)}\">")
                current_category = cat

            name = escape_xml(s["name"])
            desc = escape_xml(s.get("description") or "")
            skill_meta = s.get("meta") or {}
            available = self._check_requirements(skill_meta)

            lines.append(f"    <skill available=\"{str(available).lower()}\">")
            lines.append(f"      <name>{name}</name>")
            lines.append(f"      <description>{desc}</description>")
            source = s.get("source") or SKILL_SOURCE_BUILTIN
            if source != SKILL_SOURCE_BUILTIN:
                lines.append(f"      <source>{escape_xml(source)}</source>")

            if not available:
                missing = self._format_missing_requirements(skill_meta)
                if missing:
                    lines.append(f"      <requires>{escape_xml(missing)}</requires>")

            lines.append("    </skill>")

        if current_category is not None:
            lines.append("  </category>")
        lines.append("</skills>")

        return "\n".join(lines)

    def list_skill_categories(self) -> list[str]:
        """返回当前可见技能的去重分类列表（已排序）。"""
        all_skills = self.list_skills(filter_unavailable=False)
        all_skills = [s for s in all_skills if self.is_skill_allowed(s["name"])]
        return sorted({s.get("category", DEFAULT_SKILL_CATEGORY) for s in all_skills})

    def get_skill_setup_info(self, meta: dict[str, Any]) -> dict:
        """返回技能依赖/setup 结构化信息，供 skill_view / skills_list 使用。"""
        skill_meta = meta or {}
        missing_bins = self._list_missing_bins(skill_meta)
        missing_env = self._list_missing_env(skill_meta)
        setup_needed = bool(missing_bins or missing_env)
        available = self._check_requirements(skill_meta)
        requires = self._format_missing_requirements(skill_meta) if setup_needed else ""
        usage_hint = self._build_setup_usage_hint(skill_meta, missing_bins, missing_env)
        info: dict = {
            "available": available,
            "setup_needed": setup_needed,
        }
        if setup_needed:
            if requires:
                info["requires"] = requires
            if missing_bins:
                info["missing_bins"] = missing_bins
            if missing_env:
                info["missing_env"] = missing_env
        if usage_hint:
            info["usage_hint"] = usage_hint
        return info

    def _list_missing_bins(self, skill_meta: dict) -> list[str]:
        requires = skill_meta.get("requires", {})
        return [b for b in requires.get("bins", []) if not shutil.which(b)]

    def _list_missing_env(self, skill_meta: dict) -> list[str]:
        requires = skill_meta.get("requires", {})
        return [env for env in requires.get("env", []) if not os.environ.get(env)]

    def _format_missing_requirements(self, skill_meta: dict) -> str:
        """将未满足的 bins/env 格式化为 requires 字符串（CLI:/ENV: 前缀，逗号分隔）。"""
        parts = [f"CLI: {b}" for b in self._list_missing_bins(skill_meta)]
        parts += [f"ENV: {env}" for env in self._list_missing_env(skill_meta)]
        return ", ".join(parts)

    def _check_requirements(self, skill_meta: dict) -> bool:
        """检查技能依赖是否满足：requires.bins 均在 PATH，requires.env 均已设置。"""
        return not self._list_missing_bins(skill_meta) and not self._list_missing_env(skill_meta)

    def _build_setup_usage_hint(
        self,
        skill_meta: dict,
        missing_bins: list[str],
        missing_env: list[str],
    ) -> str:
        hints: list[str] = []
        setup = skill_meta.get("setup") or skill_meta.get("setup_hint")
        if setup:
            hints.append(str(setup).strip())
        for b in missing_bins:
            hints.append(f"Install CLI '{b}' (apt/brew/pip) and ensure it is on PATH.")
        for env in missing_env:
            hints.append(f"Set environment variable {env}.")
        return " ".join(hints)

    # ===============================
    # skill创建、修改相关操作
    # ===============================
    def _readonly_skill_dir(self, name: str) -> Path | None:
        """在 external 中查找只读 skill 目录（用于 fork 到 builtin；不含 workspace）。"""
        for skills_dir in self.external_skills_dirs:
            entry = self._skills_for_root(skills_dir).get(name)
            if entry:
                return entry.abs_path
        return None

    def _writable_dest(
        self,
        name: str,
        *,
        category: str | None = None,
    ) -> tuple[Path, str]:
        """解析 builtin 可写目标目录（external 同名 skill 写入时落到 builtin 副本）。"""
        found = self.find_skill(name)
        if found:
            entry, source = found
            if source == SKILL_SOURCE_BUILTIN:
                return entry.abs_path, SKILL_SOURCE_BUILTIN
        dest = build_skill_install_path(self.builtin_skills_dir, name, category)
        return dest.resolve(), SKILL_SOURCE_BUILTIN

    def _ensure_writable_dir(
        self,
        name: str,
        *,
        category: str | None = None,
    ) -> tuple[Path, str]:
        """写/改 skill 时解析 builtin 目录；external 同名 skill 先 copytree 到 builtin。"""
        dest, source = self._writable_dest(name, category=category)
        if (dest / "SKILL.md").is_file():
            return dest, source

        # 有同名只读skill，复制到可修改目标目录
        readonly_dir = self._readonly_skill_dir(name)
        if readonly_dir:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(readonly_dir, dest)
            self._invalidate_caches(name)

        # 返回目标目录和源层
        return dest, source

    def resolve_target_file(
        self,
        name: str,
        rel_path: str,
    ) -> Path:
        """解析 builtin 可写目标文件路径（不触发 overlay）。"""
        key = self.validate_skill_name(name)
        rel = normalize_rel_path(rel_path)
        if rel is None:
            raise ValueError("Invalid file path")
        dest, _source = self._writable_dest(key)
        return self._skill_file_path(dest, key, rel)

    @staticmethod
    def _skill_file_path(skill_dir: Path, name: str, rel_path: Path) -> Path:
        """解析技能文件路径，确保路径在技能目录内。"""
        file = (skill_dir / rel_path).resolve()
        try:
            file.relative_to(skill_dir.resolve())
        except ValueError as e:
            raise ValueError(f"Path escapes skill directory: {name}/{rel_path.as_posix()}") from e
        return file

    def write_skill_file(
        self,
        name: str,
        rel_path: str,
        content: str,
        *,
        category: str | None = None,
    ) -> dict[str, str]:
        """完整写 skill 文件到 builtin；external 同名 skill 会先 fork 到 builtin。"""
        key = self.validate_skill_name(name)
        rel = normalize_rel_path(rel_path)
        if rel is None:
            raise ValueError("Invalid file path")

        dest, source = self._ensure_writable_dir(key, category=category)
        skill_file = dest / "SKILL.md"
        if not skill_file.is_file() and rel.as_posix() != "SKILL.md":
            raise ValueError("New skills must start with SKILL.md")

        dest_file = self._skill_file_path(dest, key, rel)
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(content, encoding="utf-8")
        self._invalidate_caches(key)
        return {
            "name": key,
            "file": rel.as_posix(),
            "abs_path": str(dest),
            "source": source,
        }

    def replace_skill_file(
        self,
        name: str,
        rel_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> dict[str, str | int]:
        """部分修改 builtin skill 文件；external 同名 skill 会先 fork 到 builtin。"""
        if old_text is None or new_text is None or old_text == "":
            raise ValueError("old_text must not be empty; use write_file for full overwrite")
        if old_text == new_text:
            raise ValueError("No changes to apply: old_text and new_text are identical")
        key = self.validate_skill_name(name)
        rel = normalize_rel_path(rel_path)
        if rel is None:
            raise ValueError("Invalid file path")

        dest, source = self._ensure_writable_dir(key)
        file = self._skill_file_path(dest, key, rel)
        if not file.is_file():
            raise ValueError(f"Skill file not found: {key}/{rel.as_posix()}")

        content = file.read_text(encoding="utf-8")
        if old_text not in content:
            raise ValueError(f"old_text not found in {key}/{rel.as_posix()}")
        count = content.count(old_text)
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_text appears {count} times; provide more context or set replace_all=true"
            )
        if replace_all:
            new_content = content.replace(old_text, new_text)
            replaced = count
        else:
            new_content = content.replace(old_text, new_text, 1)
            replaced = 1
        if new_content == content:
            raise ValueError("No changes to apply")
        file.write_text(new_content, encoding="utf-8")
        self._invalidate_caches(key)
        return {
            "name": key,
            "file": rel.as_posix(),
            "abs_path": str(dest),
            "source": source,
            "replaced": replaced,
        }

    def _format_skill_md(
        self,
        name: str,
        description: str,
        body: str | None = None,
    ) -> str:
        desc = (description or "").strip() or f"Skill {name}"
        frontmatter = {
            "description": desc,
        }
        yaml_text = SkillFrontmatter.dump(frontmatter)
        md_body = (body or "").strip() or f"# {name}\n\nDescribe how to use this skill."
        return f"---\n{yaml_text}\n---\n\n{md_body}\n"

    def _resolve_create_skill_md(
        self,
        name: str,
        *,
        description: str = "",
        category: str = DEFAULT_SKILL_CATEGORY,
        content: str | None = None,
    ) -> str:
        """生成新建 SKILL.md：有 frontmatter 的 content 视为完整文件；否则拼 header + 正文。"""
        if content is not None:
            text = content.strip()
            if text and SkillFrontmatter.parse(text) is not None:
                return text if text.endswith("\n") else f"{text}\n"
            return self._format_skill_md(name, description, body=content)
        return self._format_skill_md(name, description)

    def _grant_skill_access(self, name: str) -> None:
        """创建 skill 后：白名单模式下追加会话 filter；若有 agent_type 则持久化到 config.json。"""
        key = (name or "").strip()
        if not key:
            return
        if self.filter_skills is not None and key not in self.filter_skills:
            self.filter_skills.append(key)
        if not self.agent_type:
            return
        try:
            from .hub.agent_enable import enable_skill_for_agents
            results = enable_skill_for_agents(
                key,
                [self.agent_type],
                require_in_catalog=False,
            )
            if results and results[0].get("status") != "enabled":
                logging.warning(
                    "Failed to persist skill permission for %s: %s",
                    key,
                    results[0],
                )
        except OSError as e:
            logging.warning("Failed to persist skill permission for %s: %s", key, e)

    def create_skill(
        self,
        name: str,
        *,
        description: str = "",
        category: str = DEFAULT_SKILL_CATEGORY,
        content: str | None = None,
    ) -> dict[str, str]:
        """在 builtin 目录新建 skill（名称全局唯一）。"""
        key = self.validate_skill_name(name)
        if self.find_skill(key) is not None:
            raise ValueError(f"Skill already exists: {key}")

        dest, _source = self._writable_dest(key, category=category)
        if dest.is_dir() and (dest / "SKILL.md").is_file():
            raise ValueError(f"Skill already exists: {key}")

        md_text = self._resolve_create_skill_md(
            key,
            description=description,
            category=category,
            content=content,
        )
        result = self.write_skill_file(key, "SKILL.md", md_text, category=category)
        self._grant_skill_access(key)
        return result

    def is_skill_deletable(self, name: str) -> bool:
        """是否可删除：仅 builtin 层本体，workspace/external 只读不可删。"""
        found = self.find_skill(name)
        return found is not None and found[1] == SKILL_SOURCE_BUILTIN

    def delete_skill(self, name: str) -> dict[str, str]:
        """删除 builtin 目录下的 skill。"""
        key = self.validate_skill_name(name)
        found = self.find_skill(key)
        if not found:
            raise ValueError(f"Skill not found: {key}")
        entry, source = found
        if source != SKILL_SOURCE_BUILTIN:
            raise ValueError(f"Cannot delete {source} skill: {key}")
        abs_path = entry.abs_path
        shutil.rmtree(abs_path)
        self._invalidate_caches(key)
        return {"name": key, "source": SKILL_SOURCE_BUILTIN, "abs_path": str(abs_path)}
