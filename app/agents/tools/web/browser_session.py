from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


logger = logging.getLogger(__name__)


@dataclass
class _BrowserSession:
    session_id: str
    playwright: Playwright
    browser: Browser
    context: BrowserContext
    page: Page
    headless: bool
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserSessionManager:
    """按 Agent session_id 复用 Chromium；进程退出时统一关闭。"""

    def __init__(self) -> None:
        self._sessions: dict[str, _BrowserSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        session_id: str,
        *,
        headless: bool = True,
    ) -> _BrowserSession:
        key = (session_id or "").strip() or "default"
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                if existing.headless != headless:
                    await self._close_unlocked(key)
                else:
                    return existing
            try:
                playwright = await async_playwright().start()
                browser = await playwright.chromium.launch(headless=headless)
                context = await browser.new_context()
                page = await context.new_page()
            except Exception as e:
                raise RuntimeError(
                    "Failed to start Chromium via Playwright. "
                    "Install browsers with: playwright install chromium. "
                    f"Detail: {e}"
                ) from e
            session = _BrowserSession(
                session_id=key,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                headless=headless,
            )
            self._sessions[key] = session
            logger.info("Browser session started: %s (headless=%s)", key, headless)
            return session

    async def close(self, session_id: str) -> bool:
        key = (session_id or "").strip() or "default"
        async with self._lock:
            return await self._close_unlocked(key)

    async def _close_unlocked(self, key: str) -> bool:
        session = self._sessions.pop(key, None)
        if session is None:
            return False
        await self._dispose(session)
        return True

    async def shutdown(self) -> None:
        async with self._lock:
            keys = list(self._sessions.keys())
            for key in keys:
                await self._close_unlocked(key)

    @staticmethod
    async def _dispose(session: _BrowserSession) -> None:
        for closer, label in (
            (session.context.close, "context"),
            (session.browser.close, "browser"),
            (session.playwright.stop, "playwright"),
        ):
            try:
                await closer()
            except Exception as e:
                logger.warning("Browser %s close failed for %s: %s", label, session.session_id, e)

    def has_session(self, session_id: str) -> bool:
        key = (session_id or "").strip() or "default"
        return key in self._sessions

    def session_count(self) -> int:
        return len(self._sessions)


BROWSER_SESSION_MANAGER = BrowserSessionManager()
