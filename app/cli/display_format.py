"""把 Agent outbound / to_user_message 载荷格式化为终端可读文本。"""
from __future__ import annotations
import json
import re
from typing import Any, Literal


DisplayKind = Literal["text", "tool", "ask", "skip"]

_FENCE_RE = re.compile(r"^```(?:json|text)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


def format_outbound_content(text: str) -> tuple[DisplayKind, str]:
    """解析用户可见 content，返回 (展示类型, 文本)。

    - tool_result / tool_call JSON → 短摘要 + 可读正文
    - ask_question 结果 → 简短提示（题目已由工具侧 format_user_message 展示）
    - 其它原样
    """
    raw = (text or "").strip()
    if not raw:
        return "skip", ""
    if not raw.startswith("{"):
        return "text", raw
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return "text", raw
    if not isinstance(obj, dict):
        return "text", raw

    kind = str(obj.get("kind") or "").strip()
    if kind == "tool_result":
        return _format_tool_result(obj)
    if kind == "tool_call":
        return _format_tool_call(obj)
    return "text", raw


def extract_tool_call_id(text: str) -> str | list[str] | None:
    """从 tool_call / tool_result JSON 取 tool_call_id；非 JSON 或无 id 返回 None。

    - tool_result: 返回单个 tool_call_id (str)
    - tool_call: 返回所有 tool 的 tool_call_id 列表 (list[str])，确保每个工具调用都能创建 ▶ widget
    """
    raw = (text or "").strip()
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    kind = str(obj.get("kind") or "").strip()
    if kind == "tool_result":
        tid = str(obj.get("tool_call_id") or "").strip()
        return tid or None
    if kind == "tool_call":
        tools = obj.get("tools") or []
        if isinstance(tools, list):
            ids: list[str] = []
            for t in tools:
                if isinstance(t, dict):
                    tid = str(t.get("tool_call_id") or "").strip()
                    if tid:
                        ids.append(tid)
            return ids or None
    return None


def _format_tool_result(obj: dict[str, Any]) -> tuple[DisplayKind, str]:
    name = str(obj.get("tool_name") or "tool").strip()
    if name.endswith(" Executed"):
        name = name[: -len(" Executed")].strip()
    body = _unwrap_fenced(str(obj.get("tool_result") or "").strip())

    if name == "ask_question":
        return _format_ask_question_result(body)

    # 单行摘要：tool_name(params)，完整 body 不再上屏（太啰嗦）；success 由 UI 层上色
    params = obj.get("tool_params") or {}
    summary = _summarize_params(params if isinstance(params, dict) else {})
    single_line = f"{name}({summary})" if summary else f"{name}()"
    return "tool", single_line


def _format_tool_call(obj: dict[str, Any]) -> tuple[DisplayKind, str]:
    tools = obj.get("tools") or []
    lines: list[str] = []
    preface = str(obj.get("text") or "").strip()
    if preface:
        lines.append(preface)
    if isinstance(tools, list):
        for item in tools:
            if not isinstance(item, dict):
                continue
            name = str(item.get("tool_name") or "tool").strip()
            params = item.get("tool_params") or {}
            summary = _summarize_params(params if isinstance(params, dict) else {})
            lines.append(f"{name}({summary})" if summary else f"{name}()")
    if not lines:
        return "skip", ""
    return "tool", " → ".join(lines) if len(lines) == 1 else "\n".join(lines)


def _format_ask_question_result(body: str) -> tuple[DisplayKind, str]:
    data = _try_json_object(body)
    if data is None:
        return "ask", "Waiting for your answer — reply with option number/label or free text"
    status = str(data.get("status") or "").strip()
    questions = data.get("questions") or []
    n = len(questions) if isinstance(questions, list) else 0
    if status == "answered":
        answers = data.get("answers") or {}
        if isinstance(answers, dict) and answers:
            parts = [f"{k}: {v}" for k, v in answers.items()]
            return "ask", "Answered — " + "; ".join(parts)
        return "ask", "Answered"
    label = f"{n} question(s)" if n else "question(s)"
    return "ask", f"{label} shown — reply with option number/label or free text"


def _unwrap_fenced(text: str) -> str:
    m = _FENCE_RE.match((text or "").strip())
    if m:
        return (m.group(1) or "").strip()
    return (text or "").strip()


def _try_json_object(text: str) -> dict[str, Any] | None:
    raw = _unwrap_fenced(text)
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _summarize_params(params: dict[str, Any], *, max_len: int = 200) -> str:
    if not params:
        return ""
    preferred = ("path", "filePath", "pattern", "command", "query", "url", "action", "name", "offset", "limit")
    # 路径类参数允许更长显示
    path_keys = {"path", "filePath", "file_path"}
    parts: list[str] = []
    for key in preferred:
        if key not in params:
            continue
        val = params[key]
        if val is None or val == "":
            continue
        text = str(val).replace("\n", " ")
        limit = 120 if key in path_keys else 80
        if len(text) > limit:
            text = text[: limit - 3] + "..."
        parts.append(f"{key}={text}")
        if len(parts) >= 3:
            break
    if not parts:
        # 兜底：第一个短字段
        for key, val in params.items():
            if str(key).startswith("_"):
                continue
            text = str(val).replace("\n", " ")
            if len(text) > 40:
                text = text[:37] + "..."
            parts.append(f"{key}={text}")
            break
    summary = ", ".join(parts)
    if len(summary) > max_len:
        return summary[: max_len - 3] + "..."
    return summary
