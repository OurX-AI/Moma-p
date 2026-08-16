import os
import re
import traceback
from datetime import datetime
from pathlib import Path
import tomllib


def get_project_meta(package_name: str = "knowledge-service"):
    """从 pyproject.toml 读取项目元数据"""
    toml_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    if not toml_path.exists():
        return {
            "name": "unknown-project",
            "version": "0.0.0",
            "description": "",
        }
    
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    poetry = data.get("tool", {}).get("poetry", {})
    return {
        "name": poetry.get("name", "unknown-project"),
        "version": poetry.get("version", "0.0.0"),
        "description": poetry.get("description", ""),
    }

def is_chinese(text: str) -> bool:
    """判断文本是否包含中文字符"""
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False

def is_english(text: str) -> bool:
    """判断文本是否只包含英文字符"""
    for char in text:
        if not ('a' <= char.lower() <= 'z' or char == ' ' or char == '\n' or char == '\t'):
            return False
    return True

def local_now_iso() -> str:
    return datetime.now().isoformat()

def normalize_path(path: str) -> str:
    """规范化路径，统一使用正斜杠"""
    return path.replace("\\", "/")

def normalize_rel_path(rel_path: str) -> Path | None:
    """解析安全的相对路径；拒绝空串、绝对路径与含 .. 的路径。"""
    raw = (rel_path or "").strip()
    if not raw:
        return None
    rel = Path(raw.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    return rel

def increase_md_heading_levels(content: str, levels: int = 1) -> str:
    """将 markdown 标题层级整体增加 levels 级（# -> ##，## -> ###，最多 6 级）。"""
    if not content or levels <= 0:
        return content

    def repl(m):
        prefix, hashes, space_rest = m.group(1), m.group(2), m.group(3)
        new_level = min(len(hashes) + levels, 6)
        return prefix + "#" * new_level + space_rest

    return re.sub(r"^(\s*)(#{1,6})(\s+.*)$", repl, content, flags=re.MULTILINE)

def strip_utf8_bom(text: str) -> str:
    """去除 UTF-8 BOM（U+FEFF），避免 ast.parse 等解析器报 SyntaxError。"""
    if text.startswith("\ufeff"):
        return text[1:]
    return text

def exc_summary(exc: BaseException) -> str:
    """单行摘要：异常类型 + 消息 + 最内层 traceback 位置（便于日志首行定位）。"""
    tb = exc.__traceback__
    if tb is None:
        return f"{type(exc).__name__}: {exc}"
    frames = traceback.extract_tb(tb)
    if not frames:
        return f"{type(exc).__name__}: {exc}"
    last = frames[-1]
    try:
        path = os.path.relpath(last.filename)
    except ValueError:
        path = last.filename
    return f"{path}:{last.lineno} in {last.name} | {type(exc).__name__}: {exc}"
