import argparse
import asyncio
import atexit
import getpass
import logging
import os
import sys
from pathlib import Path
from typing import Optional
from app.agents.contants import DEFAULT_AGENT_TYPE
from app.agents.internal_dispatch import set_internal_message_handler
from app.agents.output import set_output_handler
from app.config.settings import APP_NAME, APP_VERSION
from app.cli.lifecycle import shutdown, startup
from app.cli.output import TerminalOutputHandler
from app.cli.runner import AGENT_RUNNER
from app.cli.tui import run_tui


def _restore_windows_console() -> None:
    """恢复 Windows 终端原始模式（atexit fallback + finally 兜底）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        kernel32.SetConsoleMode(handle, 0x0007)
    except Exception:
        pass


atexit.register(_restore_windows_console)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="moma",
        description=f"{APP_NAME} 编码 Agent CLI",
    )
    parser.add_argument(
        "-p", "--prompt",
        dest="prompt",
        default=None,
        help="单次执行提示词后退出（非交互模式）",
    )
    parser.add_argument(
        "-w", "--workspace",
        dest="workspace",
        default=None,
        help="工作区路径，默认为启动 CLI 时的当前目录",
    )
    parser.add_argument(
        "--agent-type",
        dest="agent_type",
        default=DEFAULT_AGENT_TYPE,
        help=f"Agent 类型，默认 {DEFAULT_AGENT_TYPE}",
    )
    parser.add_argument(
        "--resume",
        dest="resume_session",
        default=None,
        help="恢复已有 session_id",
    )
    parser.add_argument(
        "--model",
        dest="model",
        default=None,
        help="模型，格式 provider/model",
    )
    parser.add_argument(
        "--plain",
        dest="plain",
        action="store_true",
        help="使用朴素终端 REPL，而非 TUI",
    )
    parser.add_argument(
        "--tui",
        dest="tui",
        action="store_true",
        help="强制使用 TUI（即使非交互终端）",
    )
    sub = parser.add_subparsers(dest="command")
    cb = sub.add_parser("codebase", help="查看/触发 CodeBase 预分析")
    cb.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "rescan", "experience"],
        help="status（默认）查看进度；rescan 强制重新触发扫描；experience 查看经验提取状态",
    )
    return parser


def _should_use_tui(args: argparse.Namespace) -> bool:
    if args.prompt:
        return False
    if args.plain:
        return False
    if args.tui:
        return True
    # sys.stdout可能为None（如无头环境、输出重定向等）
    if sys.stdout is None or not hasattr(sys.stdout, 'isatty'):
        return False
    return sys.stdout.isatty()


def _parse_model(model: str | None) -> tuple[str, str]:
    raw = (model or "").strip()
    if not raw:
        return "", ""
    if "/" not in raw:
        return "", raw
    provider, name = raw.split("/", 1)
    return provider.strip(), name.strip()


def _print_banner(workspace: Path) -> None:
    sys.stdout.write(f"{APP_NAME} v{APP_VERSION} | workspace: {workspace}\n")
    sys.stdout.write("输入消息开始对话；/new 新会话，/status 状态，/stop 中断，/exit 退出\n")
    sys.stdout.flush()


async def _ensure_session(
    *,
    workspace: Path,
    agent_type: str,
    resume_session: str | None,
    llm_provider: str,
    llm_model: str,
) -> str | None:
    """workspace 初始化 + resume 检查。**不主动创建新 session**——由调用方按需创建
    （TUI 路径延迟到用户发第一条消息时才创建，避免打开就退留下空 session）。"""
    user_id = getpass.getuser() or os.getenv("USER") or "cli"
    try:
        from app.codebase.integration.orchestrator import AutoAnalyzeOrchestrator
        await AutoAnalyzeOrchestrator.ensure_workspace_ready(
            workspace_path=str(workspace),
            user_id=user_id,
        )
    except Exception as exc:
        logging.warning("workspace 自动扫描触发失败: %s", exc)
    if resume_session:
        from app.agents.sessions.manager import SESSION_MANAGER
        session = await SESSION_MANAGER.get_session(resume_session)
        if session is None:
            raise SystemExit(f"会话不存在: {resume_session}")
        AGENT_RUNNER.bind_session(resume_session)
        return resume_session
    return None


async def _handle_command(line: str) -> bool:
    cmd = line.strip().lower()
    if cmd in ("/exit", "/quit", "/q"):
        return False
    if cmd == "/status":
        sys.stdout.write(AGENT_RUNNER.status_text() + "\n")
        sys.stdout.flush()
        return True
    if cmd == "/stop":
        sys.stdout.write(await AGENT_RUNNER.stop_current() + "\n")
        sys.stdout.flush()
        return True
    if cmd == "/kill":
        sys.stdout.write(await AGENT_RUNNER.stop_current(hard=True) + "\n")
        sys.stdout.flush()
        return True
    if cmd == "/new":
        workspace = Path.cwd().resolve()
        session_id = await AGENT_RUNNER.create_session(
            user_id=getpass.getuser() or "cli",
            workspace_path=str(workspace),
        )
        sys.stdout.write(f"已创建新会话: {session_id}\n")
        sys.stdout.flush()
        return True
    return True


async def _run_interactive(session_id: str | None, workspace: Path) -> None:
    while True:
        try:
            line = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.stdout.write("\n")
            break
        if not line:
            continue
        if line.startswith("/"):
            cont = await _handle_command(line)
            if not cont:
                break
            if line.strip().lower() in ("/new",):
                session_id = AGENT_RUNNER.current_session_id or session_id
            continue
        # 延迟创建 session：用户发第一条消息时才落库
        if session_id is None:
            session_id = await AGENT_RUNNER.create_session(
                user_id=getpass.getuser() or "cli",
                workspace_path=str(workspace),
            )
        await AGENT_RUNNER.run_turn(session_id, line)


async def _run_once(session_id: str, prompt: str) -> None:
    await AGENT_RUNNER.run_turn(session_id, prompt)


async def _run_codebase_command(args: argparse.Namespace, workspace: Path, user_id: str) -> int:
    """`moma codebase [status|rescan|experience]`：查看/触发 CodeBase 预分析。

    不走 Agent session，不需要 TUI；只启动 DB/scheduler 后即调用 CodeBase 服务。
    rescan 会阻塞到目录扫描完成（文件级 embedding 在后台 worker 继续跑，需 TUI/长驻进程保持）。
    """
    from app.codebase.integration.status_formatter import format_codebase_status, format_experience_status
    from app.codebase.integration.orchestrator import AutoAnalyzeOrchestrator
    from app.codebase.repo_analysis.models.analysis_status import RepoAnalysisStatus
    from app.codebase.repo_analysis.services.analysis_service import AnalysisService

    action = getattr(args, "action", "status")
    if action == "experience":
        text = await format_experience_status(str(workspace))
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        return 0
    if action == "rescan":
        try:
            await AutoAnalyzeOrchestrator.trigger_reanalyze(
                workspace_path=str(workspace),
                user_id=user_id,
            )
        except Exception as exc:
            logging.error("CodeBase 重新扫描触发失败: %s", exc)
            sys.stdout.write(f"触发失败: {exc}\n")
            sys.stdout.flush()
            return 1
        sys.stdout.write("已触发 CodeBase 重新扫描，等待目录扫描完成...\n")
        sys.stdout.flush()
        # 阻塞到 scan_status 离开 RUNNING（目录扫描完成或失败）
        # 否则进程退出会取消 asyncio task，DB 永远停在 RUNNING
        max_wait_sec = 120
        waited = 0
        repo_id = await _resolve_workspace_repo_id(str(workspace))
        while waited < max_wait_sec and repo_id:
            await asyncio.sleep(2)
            waited += 2
            try:
                scan = await AnalysisService.get_scan_status(repo_id=repo_id)
            except Exception:
                break
            status_val = str(scan.get("scan_status") or "")
            sys.stdout.write(".")
            sys.stdout.flush()
            if status_val != RepoAnalysisStatus.RUNNING.value:
                break
        sys.stdout.write("\n")
        sys.stdout.flush()

    text = await format_codebase_status(str(workspace), user_id)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
    return 0


async def _resolve_workspace_repo_id(workspace_path: str) -> Optional[str]:
    """workspace -> repo_id（未注册返回 None）。"""
    from app.codebase.repo_mgmt.services.repo_resolver import RepoResolver
    from app.infrastructure.database import get_db_session
    async with get_db_session() as db:
        repo = await RepoResolver.get_by_path(db, workspace_path)
        return repo.id if repo else None


async def _async_main(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace or os.getcwd()).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"工作区不存在: {workspace}")
    os.chdir(workspace)
    llm_provider, llm_model = _parse_model(args.model)
    user_id = getpass.getuser() or os.getenv("USER") or "cli"

    # `moma codebase ...`：仅启动 DB/scheduler，不走 Agent/TUI 流程
    if getattr(args, "command", None) == "codebase":
        await startup(
            quiet_console=True,
            workspace_path=str(workspace),
            user_id=user_id,
        )
        try:
            return await _run_codebase_command(args, workspace, user_id)
        finally:
            await shutdown()

    use_tui = _should_use_tui(args)
    if not use_tui:
        set_output_handler(TerminalOutputHandler())
        set_internal_message_handler(AGENT_RUNNER.handle_internal_message)
    await startup(
        quiet_console=use_tui,
        workspace_path=str(workspace),
        user_id=user_id,
    )
    try:
        session_id = await _ensure_session(
            workspace=workspace,
            agent_type=args.agent_type,
            resume_session=args.resume_session,
            llm_provider=llm_provider,
            llm_model=llm_model,
        )
        if args.prompt:
            # --once 路径必须立即创建 session（单次执行模式）
            if session_id is None:
                session_id = await AGENT_RUNNER.create_session(
                    user_id=user_id,
                    workspace_path=str(workspace),
                    agent_type=args.agent_type,
                    llm_provider=llm_provider,
                    llm_model=llm_model,
                )
            await _run_once(session_id, args.prompt)
        elif use_tui:
            await run_tui(
                session_id=session_id,
                workspace=workspace,
                user_id=user_id,
                llm_provider=llm_provider,
                llm_model=llm_model,
            )
        else:
            _print_banner(workspace)
            await _run_interactive(session_id, workspace)
    finally:
        await shutdown()
        _restore_windows_console()
    return 0


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(_async_main(args)))
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        _restore_windows_console()
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
