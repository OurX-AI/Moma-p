"""命令策略：长驻进程前后台纪律、测/构建超时。"""
from __future__ import annotations
import re


class CommandPolicy:
    """编码场景下的命令启发：不替代安全 deny，只服务跑测/构建稳定性。"""

    DEFAULT_TIMEOUT_SEC = 120
    VERIFY_TIMEOUT_SEC = 600

    # 前台容易空超时的长驻服务；要求 background=true
    _SERVER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\bnpm\s+(run\s+)?start\b",
            r"\bnpm\s+run\s+dev\b",
            r"\byarn\s+(run\s+)?(start|dev)\b",
            r"\bpnpm\s+(run\s+)?(start|dev)\b",
            r"\bbun\s+run\s+(start|dev)\b",
            r"\bnext\s+dev\b",
            r"\bvite\s+dev\b",
            r"\bvite(\s+--host|\s+--port|\s+-c|\s+--config)?\s*$",
            r"\bwrangler\s+dev\b",
            r"\buvicorn\b",
            r"\bgunicorn\b",
            r"\bhypercorn\b",
            r"\bflask\s+run\b",
            r"\bdjango(-admin)?\s+runserver\b",
            r"\bpython(\.exe)?\s+-m\s+http\.server\b",
            r"\bnodemon\b",
            r"\bwebpack-dev-server\b",
            r"\bdocker\s+compose\s+up\b",
            r"\bdocker-compose\s+up\b",
        )
    )

    # 测/构建：前台可跑，但默认加长超时
    _VERIFY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\bpytest\b",
            r"\bpy\.test\b",
            r"\bpython(\.exe)?\s+-m\s+pytest\b",
            r"\bnpm\s+test\b",
            r"\bnpm\s+run\s+test\b",
            r"\bnpx\s+(vitest|jest|playwright)\b",
            r"\bvitest\b",
            r"\bjest\b",
            r"\bcargo\s+test\b",
            r"\bgo\s+test\b",
            r"\bmvn\s+test\b",
            r"\bgradlew?(\.bat)?\s+test\b",
            r"\bdotnet\s+test\b",
            r"\bmake\s+test\b",
            r"\bnpm\s+run\s+build\b",
            r"\byarn\s+build\b",
            r"\bpnpm\s+(run\s+)?build\b",
            r"\bcargo\s+build\b",
            r"\bmvn\s+package\b",
            r"\bgradlew?(\.bat)?\s+build\b",
            r"\bdotnet\s+build\b",
            r"\btsc(\s|$)",
            r"\bwebpack\b",
        )
    )

    # 明确只读检查类命令：可与其它只读工具并行（保守匹配，宁可不并行也不误并行）
    _READONLY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"^(ls|dir|pwd|whoami|hostname|uname|date|echo|printf)\b",
            r"^get-(childitem|content|location|item|command|help|process|service)\b",
            r"^write-output\b",
            r"^write-host\b",
            r"^type\b",
            r"^cat\b",
            r"^head\b",
            r"^tail\b",
            r"^rg\b",
            r"^grep\b",
            r"^findstr\b",
            r"^where(\.exe)?\b",
            r"^which\b",
            r"^git\s+(status|log|diff|show|branch|remote|tag|blame|stash\s+list|rev-parse|ls-files|describe)\b",
            r"^npm\s+(ls|list|view|outdated|--version|-v)\b",
            r"^node\s+(-v|--version)\b",
            r"^python(\.exe)?\s+(-V|--version|-c\s+[\"']print)",
            r"^pip(\.exe)?\s+(list|show|freeze)\b",
            r"^cargo\s+(--version|-V|tree|metadata)\b",
            r"^go\s+(version|env|list)\b",
            r"^dotnet\s+(--info|--list-sdks|--list-runtimes)\b",
            r"^pytest\b.*--collect-only\b",
        )
    )

    # 命令中出现这些则视为有写/副作用，即使前缀像只读
    _SIDE_EFFECT_HINTS: tuple[re.Pattern[str], ...] = tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"[>\|]",
            r"\b(rm|del|rmdir|mkdir|mv|cp|copy|move|set-content|add-content|out-file|new-item|remove-item)\b",
            r"\bgit\s+(add|commit|push|pull|checkout|switch|merge|rebase|reset|clean|stash\s+push|stash\s+pop)\b",
            r"\bnpm\s+(i|install|uninstall|publish|run)\b",
            r"\bpip(\.exe)?\s+install\b",
        )
    )

    @classmethod
    def foreground_server_block_reason(
        cls,
        command: str,
        *,
        background: bool,
        tool_label: str = "bash",
    ) -> str | None:
        """长驻服务若在前台运行则拒绝，引导 background + shell_process。"""
        if background:
            return None
        cmd = (command or "").strip()
        if not cmd:
            return None
        for pattern in cls._SERVER_PATTERNS:
            if pattern.search(cmd):
                return (
                    "Error: This looks like a long-running server/dev process. "
                    f"Use {tool_label}(background=true), then shell_process(action=\"wait\") "
                    "or poll/log/kill as needed."
                )
        return None

    @classmethod
    def is_verify_command(cls, command: str) -> bool:
        cmd = (command or "").strip()
        if not cmd:
            return False
        return any(p.search(cmd) for p in cls._VERIFY_PATTERNS)

    @classmethod
    def resolve_timeout_sec(
        cls,
        command: str,
        timeout_ms: float | None,
        *,
        default_sec: int | None = None,
    ) -> int:
        """未显式传 timeout 时，测/构建使用更长默认超时。"""
        base = cls.DEFAULT_TIMEOUT_SEC if default_sec is None else default_sec
        if timeout_ms is not None:
            return max(1, int(float(timeout_ms) / 1000))
        if cls.is_verify_command(command):
            return max(base, cls.VERIFY_TIMEOUT_SEC)
        return max(1, int(base))

    @classmethod
    def is_readonly_command(cls, command: str) -> bool:
        """保守判定：仅明确检查类命令视为只读。"""
        cmd = (command or "").strip()
        if not cmd:
            return False
        if any(p.search(cmd) for p in cls._SIDE_EFFECT_HINTS):
            return False
        # 只取管道/链式前第一段做前缀匹配（&& / ;）
        head = re.split(r"\s*(?:&&|\|\||;)\s*", cmd, maxsplit=1)[0].strip()
        return any(p.search(head) for p in cls._READONLY_PATTERNS)

    @classmethod
    def is_parallel_safe_command(cls, command: str, *, background: bool = False) -> bool:
        """可并行：前台只读检查命令；后台任务一律串行（避免争用 session/输出）。"""
        if background:
            return False
        return cls.is_readonly_command(command)
