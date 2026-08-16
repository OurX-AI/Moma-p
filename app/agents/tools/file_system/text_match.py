"""文本匹配辅助：精确优先，失败时做引号/Unicode 归一再匹配。"""
from __future__ import annotations


LEFT_SINGLE = "\u2018"
RIGHT_SINGLE = "\u2019"
LEFT_DOUBLE = "\u201c"
RIGHT_DOUBLE = "\u201d"


def normalize_for_match(text: str) -> str:
    """归一化引号、破折号、省略号、不换行空格，便于模型输出与文件对齐。"""
    s = text or ""
    return (
        s.replace(LEFT_SINGLE, "'")
        .replace(RIGHT_SINGLE, "'")
        .replace("\u201a", "'")
        .replace("\u201b", "'")
        .replace(LEFT_DOUBLE, '"')
        .replace(RIGHT_DOUBLE, '"')
        .replace("\u201e", '"')
        .replace("\u201f", '"')
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2015", "-")
        .replace("\u2026", "...")
        .replace("\u00a0", " ")
    )


def normalize_quotes(text: str) -> str:
    """弯引号 → 直引号（兼容旧调用）。"""
    return normalize_for_match(text)


def find_actual_string(content: str, search: str) -> str | None:
    """在 content 中查找 search；精确失败时按 normalize_for_match 匹配，返回文件中的实际子串。"""
    if not search:
        return None
    if search in content:
        return search
    target = normalize_for_match(search)
    if not target:
        return None
    length = len(content)
    i = 0
    while i < length:
        built = ""
        j = i
        while j < length and len(built) < len(target):
            built += normalize_for_match(content[j])
            j += 1
            if built == target:
                return content[i:j]
            if not target.startswith(built):
                break
        i += 1
    return None


def _is_opening_context(text: str, index: int) -> bool:
    if index == 0:
        return True
    return text[index - 1] in " \t\n\r([{"


def preserve_quote_style(old_text: str, actual_old: str, new_text: str) -> str:
    """若匹配依赖弯引号归一，则把 new_text 的直引号改成文件中的弯引号风格。"""
    if not new_text or old_text == actual_old:
        return new_text
    has_double = LEFT_DOUBLE in actual_old or RIGHT_DOUBLE in actual_old
    has_single = LEFT_SINGLE in actual_old or RIGHT_SINGLE in actual_old
    if not has_double and not has_single:
        return new_text
    chars = list(new_text)
    if has_double:
        for i, ch in enumerate(chars):
            if ch == '"':
                chars[i] = LEFT_DOUBLE if _is_opening_context(new_text, i) else RIGHT_DOUBLE
    if has_single:
        for i, ch in enumerate(chars):
            if ch == "'":
                chars[i] = LEFT_SINGLE if _is_opening_context(new_text, i) else RIGHT_SINGLE
    return "".join(chars)
