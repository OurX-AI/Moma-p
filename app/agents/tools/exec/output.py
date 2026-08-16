"""命令输出格式化：结构化 exit/stdout/stderr，截断优先保留尾部。"""
from __future__ import annotations


class OutputFormatter:
    """把命令结果整理成模型易读的文本，长输出优先留尾部（失败日志多在末尾）。"""

    MAX_CHARS = 10000
    _OMIT_MARK = "... ({omitted} chars truncated from start) ..."

    @classmethod
    def truncate(cls, text: str, max_len: int | None = None, *, prefer_tail: bool = True) -> str:
        raw = text or ""
        limit = cls.MAX_CHARS if max_len is None else max_len
        if limit <= 0 or len(raw) <= limit:
            return raw

        if not prefer_tail:
            head = max(1, limit // 5)
            mid_tmpl = "\n... ({omitted} chars omitted) ...\n"
            # 预留 mid 长度：用 omitted 位数上界估一版再收敛
            mid = mid_tmpl.format(omitted=len(raw))
            tail = max(1, limit - head - len(mid))
            omitted = max(0, len(raw) - head - tail)
            mid = mid_tmpl.format(omitted=omitted)
            tail = max(1, limit - head - len(mid))
            return f"{raw[:head]}{mid}{raw[-tail:]}"

        marker = cls._OMIT_MARK.format(omitted=len(raw))
        if len(marker) + 1 >= limit:
            return raw[-limit:]
        keep = limit - len(marker) - 1
        marker = cls._OMIT_MARK.format(omitted=max(0, len(raw) - keep))
        if len(marker) + 1 >= limit:
            return raw[-limit:]
        keep = limit - len(marker) - 1
        return f"{marker}\n{raw[-keep:]}"

    @classmethod
    def format_result(
        cls,
        *,
        stdout: str,
        stderr: str,
        exit_code: int,
        max_chars: int | None = None,
    ) -> tuple[str, bool]:
        """返回 (正文, 是否发生截断)。"""
        budget = cls.MAX_CHARS if max_chars is None else max_chars
        out = (stdout or "").rstrip("\n")
        err = (stderr or "").rstrip("\n")
        truncated = False

        if out and err:
            out_budget = max(1, int(budget * 0.4))
            err_budget = max(1, budget - out_budget)
        elif err:
            out_budget, err_budget = 0, budget
        else:
            out_budget, err_budget = budget, 0

        lines = [f"exit_code: {exit_code}"]
        if out:
            lines.append("stdout:")
            piece = cls.truncate(out, out_budget, prefer_tail=True)
            truncated = truncated or piece != out
            lines.append(piece)
        else:
            lines.append("stdout: (empty)")
        if err:
            lines.append("stderr:")
            piece = cls.truncate(err, err_budget, prefer_tail=True)
            truncated = truncated or piece != err
            lines.append(piece)
        else:
            lines.append("stderr: (empty)")
        return "\n".join(lines), truncated

    @classmethod
    def format_timeout(
        cls,
        timeout_sec: int,
        *,
        hint_background: bool = True,
        tool_label: str = "bash",
    ) -> str:
        msg = f"Error: Command timed out after {timeout_sec} seconds."
        if hint_background:
            msg += (
                f" For long tests/builds/servers use {tool_label}(background=true)"
                " then shell_process(action=\"wait\")."
            )
        return msg
