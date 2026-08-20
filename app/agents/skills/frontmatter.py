import json
import re
from io import StringIO
from typing import Any
from ruamel.yaml import YAML


_yaml_loader = YAML(typ="safe")
_yaml_dumper = YAML()
_yaml_dumper.default_flow_style = False


class SkillFrontmatter:
    """SKILL.md frontmatter 与元数据（name / description / meta）的原子操作。"""

    NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
    _TOP_LEVEL_META_KEYS = frozenset({"always", "preinstalled"})

    @staticmethod
    def parse(content: str) -> dict[str, Any] | None:
        """解析 SKILL.md 开头的 YAML frontmatter；无有效 frontmatter 时返回 None。"""
        if content.startswith("\ufeff"):
            content = content[1:]
        if not content.startswith("---"):
            return None
        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return None
        try:
            data = _yaml_loader.load(match.group(1))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def strip(content: str) -> str:
        """去掉 SKILL.md 开头的 YAML frontmatter（---...---），只保留正文。"""
        if content.startswith("\ufeff"):
            content = content[1:]
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content

    @staticmethod
    def dump(data: dict[str, Any]) -> str:
        """将 frontmatter 字典序列化为 YAML 文本（不含 --- 分隔符）。"""
        buf = StringIO()
        _yaml_dumper.dump(data, buf)
        return buf.getvalue().rstrip()

    @staticmethod
    def name(fm: dict[str, Any]) -> str:
        """从 frontmatter 取合法 name；缺失或格式不对时返回空字符串。"""
        name = str(fm.get("name") or "").strip()
        if name and SkillFrontmatter.NAME_PATTERN.match(name):
            return name
        return ""

    @staticmethod
    def description(fm: dict[str, Any]) -> str:
        return str(fm.get("description") or "").strip()

    @staticmethod
    def _parse_metadata_block(fm: dict[str, Any]) -> dict[str, Any]:
        raw = fm.get("metadata")
        if not raw:
            return {}
        if isinstance(raw, dict):
            return dict(raw)
        try:
            data = json.loads(str(raw))
            return dict(data) if isinstance(data, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def _agent_block(metadata: dict[str, Any]) -> dict[str, Any]:
        block = (
            metadata.get("momacoder")
            or metadata.get("openclaw")
            or {}
        )
        return dict(block) if isinstance(block, dict) else {}

    @staticmethod
    def meta(fm: dict[str, Any]) -> dict[str, Any]:
        """从 frontmatter 提取完整 meta（metadata.momacoder/pando/openclaw + 顶层 always/preinstalled）。"""
        meta = SkillFrontmatter._agent_block(SkillFrontmatter._parse_metadata_block(fm))
        for key in SkillFrontmatter._TOP_LEVEL_META_KEYS:
            if fm.get(key) is not None:
                meta[key] = fm[key]
        return meta

    @staticmethod
    def fields(fm: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """一次解析 frontmatter 字典，返回 (name, description, meta)。"""
        return (
            SkillFrontmatter.name(fm),
            SkillFrontmatter.description(fm),
            SkillFrontmatter.meta(fm),
        )
