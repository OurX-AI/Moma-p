from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from ..base import BaseTool
from ..catalog import register_tool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ..truncation import Truncate
from ...schemes import AgentContext, RuntimeContext
from .browser_session import BROWSER_SESSION_MANAGER, BrowserSessionManager


_SNAPSHOT_MAX_CHARS = 50000
_DEFAULT_TIMEOUT_MS = 30000


def validate_browser_url(url: str) -> tuple[bool, str]:
    """浏览器导航 URL 校验：仅 http/https；允许 localhost（本地前端验证）。"""
    text = (url or "").strip()
    if not text:
        return False, "url is required"
    try:
        parsed = urlparse(text)
    except Exception as e:
        return False, str(e)
    if parsed.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{parsed.scheme or 'none'}'"
    if not (parsed.hostname or "").strip():
        return False, "Missing domain"
    return True, ""


def _clip(text: str, max_chars: int = _SNAPSHOT_MAX_CHARS) -> tuple[str, bool]:
    body = text or ""
    if len(body) <= max_chars:
        return body, False
    return body[:max_chars] + f"\n\n...[truncated {len(body) - max_chars} chars]", True


@register_tool(name="browser", toolset="web")
class BrowserTool(BaseTool):
    """Playwright 可控浏览器：导航、交互、快照、截图。"""

    def __init__(
        self,
        agent_ctx: Optional[AgentContext] = None,
        *,
        session_manager: Optional[BrowserSessionManager] = None,
    ) -> None:
        super().__init__(agent_ctx=agent_ctx)
        self._sessions = session_manager or BROWSER_SESSION_MANAGER

    @property
    def name(self) -> str:
        return "browser"

    def description(self, params=None) -> str:
        return """Control a Chromium browser for interactive web verification.

When to use:
- SPA pages, forms, UI flows that need click/type/wait/screenshot.
- Localhost app verification after starting a server.

When NOT to use:
- Static docs/API pages (prefer `web_fetch`).
- Open-ended web research (prefer `web_search`).

Actions: navigate, snapshot, click, type, wait, screenshot, close.
Sessions reuse per agent session_id until close. Requires: playwright install chromium.

Failure recovery:
- Selector miss -> `snapshot` then retry with updated selector/text.
- Page not ready -> `wait` / longer timeout_ms, then snapshot again.
- Browser unavailable -> report environment limit; fall back to HTTP checks if possible."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "navigate",
                        "snapshot",
                        "click",
                        "type",
                        "wait",
                        "screenshot",
                        "close",
                    ],
                    "description": "Browser action to perform",
                },
                "url": {
                    "type": "string",
                    "description": "For navigate: http/https URL (localhost allowed)",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector for click/type/wait",
                },
                "text": {
                    "type": "string",
                    "description": "For type: text to input; for click: optional visible text fallback",
                },
                "clear": {
                    "type": "boolean",
                    "description": "For type: clear existing value first (default true)",
                    "default": True,
                },
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 120000,
                    "description": "Action timeout in milliseconds (default 30000)",
                },
                "wait_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 60000,
                    "description": "For wait without selector: sleep milliseconds",
                },
                "headed": {
                    "type": "boolean",
                    "description": "Launch headed Chromium (default false/headless)",
                    "default": False,
                },
                "full_page": {
                    "type": "boolean",
                    "description": "For screenshot: capture full page (default false)",
                    "default": False,
                },
            },
            "required": ["action"],
        }

    def is_readonly(self, params=None) -> bool:
        return False

    def is_parallel(self, params=None) -> bool:
        return False

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        action: str,
        url: Optional[str] = None,
        selector: Optional[str] = None,
        text: Optional[str] = None,
        clear: bool = True,
        timeout_ms: Optional[int] = None,
        wait_ms: Optional[int] = None,
        headed: bool = False,
        full_page: bool = False,
    ) -> ToolResult:
        act = (action or "").strip().lower()
        if act not in {
            "navigate",
            "snapshot",
            "click",
            "type",
            "wait",
            "screenshot",
            "close",
        }:
            return ToolErrorResult(f"Unsupported action: {action!r}")

        session_key = agent_ctx.session_id or "default"
        timeout = int(timeout_ms) if timeout_ms is not None else _DEFAULT_TIMEOUT_MS

        if act == "close":
            closed = await self._sessions.close(session_key)
            return ToolSuccessResult(
                json.dumps({"ok": True, "closed": closed}, ensure_ascii=False)
            )

        if act == "navigate":
            ok, err = validate_browser_url(url or "")
            if not ok:
                return ToolErrorResult(err)

        try:
            session = await self._sessions.get_or_create(
                session_key,
                headless=not bool(headed),
            )
        except Exception as e:
            return ToolErrorResult(str(e))

        async with session.lock:
            try:
                if act == "navigate":
                    return await self._navigate(session.page, url, timeout)
                if act == "snapshot":
                    return await self._snapshot(session.page)
                if act == "click":
                    return await self._click(session.page, selector, text, timeout)
                if act == "type":
                    return await self._type(
                        session.page, selector, text, clear=clear, timeout=timeout
                    )
                if act == "wait":
                    return await self._wait(session.page, selector, wait_ms, timeout)
                if act == "screenshot":
                    return await self._screenshot(
                        agent_ctx, session.page, full_page=full_page
                    )
            except Exception as e:
                return ToolErrorResult(f"browser {act} failed: {e}")

        return ToolErrorResult(f"Unhandled action: {act}")

    async def _navigate(self, page: Any, url: Optional[str], timeout: int) -> ToolResult:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        status = response.status if response is not None else None
        title = await page.title()
        return ToolSuccessResult(
            json.dumps(
                {
                    "ok": True,
                    "url": page.url,
                    "status": status,
                    "title": title,
                },
                ensure_ascii=False,
            )
        )

    async def _snapshot(self, page: Any) -> ToolResult:
        title = await page.title()
        body = await page.inner_text("body")
        clipped, truncated = _clip(body)
        return ToolSuccessResult(
            json.dumps(
                {
                    "ok": True,
                    "url": page.url,
                    "title": title,
                    "truncated": truncated,
                    "text": clipped,
                },
                ensure_ascii=False,
            )
        )

    async def _click(
        self,
        page: Any,
        selector: Optional[str],
        text: Optional[str],
        timeout: int,
    ) -> ToolResult:
        sel = (selector or "").strip()
        label = (text or "").strip()
        if not sel and not label:
            return ToolErrorResult("click requires selector and/or text")
        if sel:
            await page.click(sel, timeout=timeout)
            used = sel
        else:
            await page.get_by_text(label, exact=False).first.click(timeout=timeout)
            used = f"text={label}"
        return ToolSuccessResult(
            json.dumps({"ok": True, "clicked": used, "url": page.url}, ensure_ascii=False)
        )

    async def _type(
        self,
        page: Any,
        selector: Optional[str],
        text: Optional[str],
        *,
        clear: bool,
        timeout: int,
    ) -> ToolResult:
        sel = (selector or "").strip()
        if not sel:
            return ToolErrorResult("type requires selector")
        value = text if text is not None else ""
        if clear:
            await page.fill(sel, value, timeout=timeout)
        else:
            await page.focus(sel, timeout=timeout)
            await page.keyboard.type(value)
        return ToolSuccessResult(
            json.dumps(
                {"ok": True, "typed": True, "selector": sel, "url": page.url},
                ensure_ascii=False,
            )
        )

    async def _wait(
        self,
        page: Any,
        selector: Optional[str],
        wait_ms: Optional[int],
        timeout: int,
    ) -> ToolResult:
        sel = (selector or "").strip()
        if sel:
            await page.wait_for_selector(sel, timeout=timeout)
            return ToolSuccessResult(
                json.dumps(
                    {"ok": True, "waited": "selector", "selector": sel},
                    ensure_ascii=False,
                )
            )
        ms = int(wait_ms) if wait_ms is not None else 1000
        await page.wait_for_timeout(ms)
        return ToolSuccessResult(
            json.dumps({"ok": True, "waited": "timeout", "wait_ms": ms}, ensure_ascii=False)
        )

    async def _screenshot(
        self,
        agent_ctx: AgentContext,
        page: Any,
        *,
        full_page: bool,
    ) -> ToolResult:
        out_dir = Truncate._tool_output_dir()
        name = f"browser_{int(time.time() * 1000):x}_{secrets.token_hex(4)}.png"
        path = out_dir / name
        await page.screenshot(path=str(path), full_page=bool(full_page))
        return ToolSuccessResult(
            json.dumps(
                {
                    "ok": True,
                    "path": str(path),
                    "url": page.url,
                    "full_page": bool(full_page),
                },
                ensure_ascii=False,
            )
        )
