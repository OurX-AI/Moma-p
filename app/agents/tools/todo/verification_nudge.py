"""Todo 收工时的 verification nudge（对齐 Claude Code TodoWrite）。"""
import re
from typing import Any, Dict, List, Sequence


class TodoVerificationNudge:
    """判断关闭多步 todo 时是否应提示 spawn(verification)。"""

    VERIFY_RE = re.compile(r"verif", re.I)
    MIN_ITEMS = 3
    NOTE = (
        "\n\nNOTE: You just closed out 3+ tasks and none of them was a verification step. "
        "Before writing your final summary, spawn(type=verification) with the original user task, "
        "changed files, and approach. Do not self-assign PASS by listing caveats in the summary — "
        "only the verification agent issues a verdict."
    )

    @classmethod
    def active_todos(cls, todos: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        '''获取活跃的 todo 列表'''
        out: List[Dict[str, Any]] = []
        for item in todos or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "") == "cancelled":
                continue
            out.append(item)
        return out

    @classmethod
    def has_verification_item(cls, todos: Sequence[Dict[str, Any]]) -> bool:
        '''判断是否有 verification 项'''
        for item in todos:
            content = str(item.get("content") or "")
            if cls.VERIFY_RE.search(content):
                return True
        return False

    @classmethod
    def needed(
        cls,
        todos: Sequence[Dict[str, Any]],
        *,
        is_subagent: bool = False,
    ) -> bool:
        '''判断是否需要提示 spawn(verification)'''
        if is_subagent:
            return False
        active = cls.active_todos(todos)
        if len(active) < cls.MIN_ITEMS:
            return False
        if not all(str(t.get("status") or "") == "completed" for t in active): # 判断是否所有 todo 都已关闭
            return False
        return not cls.has_verification_item(active)

