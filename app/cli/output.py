import re
import sys
from app.agents.output import OutboundMessage, OutboundMessageType
from app.cli.display_format import format_outbound_content

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
_THINK_CLOSE_RE = re.compile(r"</think\s*>", re.IGNORECASE)


class TerminalOutputHandler:
    def __init__(self) -> None:
        self._stream_open = False
        self._stream_buf = ""
        self._last_visible = ""

    @staticmethod
    def _strip_think(text: str, *, drop_open: bool = False) -> str:
        cleaned = _THINK_BLOCK_RE.sub("", text or "")
        if drop_open:
            cleaned = _THINK_OPEN_RE.sub("", cleaned)
        cleaned = _THINK_CLOSE_RE.sub("", cleaned)
        return cleaned

    async def __call__(self, msg: OutboundMessage) -> None:
        outbound_type = msg.outbound_type
        if outbound_type == OutboundMessageType.STREAM_START:
            if self._stream_open:
                return
            self._stream_open = True
            self._stream_buf = ""
            return
        if outbound_type == OutboundMessageType.STREAM_DELTA:
            if not msg.content:
                return
            if not self._stream_open:
                self._stream_open = True
                self._stream_buf = ""
            self._stream_buf += msg.content
            visible = self._strip_think(self._stream_buf, drop_open=True)
            # 流式：只打印相对上次可见增量，避免 think 标签闪现
            prev = getattr(self, "_last_visible", "")
            if visible.startswith(prev):
                delta = visible[len(prev):]
            else:
                delta = visible
            self._last_visible = visible
            if delta:
                sys.stdout.write(delta)
                sys.stdout.flush()
            return
        if outbound_type == OutboundMessageType.STREAM_END:
            self._stream_open = False
            self._stream_buf = ""
            self._last_visible = ""
            return
        if outbound_type == OutboundMessageType.RUN_END:
            if self._stream_open:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._stream_open = False
                self._stream_buf = ""
                self._last_visible = ""
            return
        if msg.content:
            if self._stream_open:
                sys.stdout.write("\n")
                self._stream_open = False
                self._stream_buf = ""
                self._last_visible = ""
            text = self._strip_think(msg.content).strip()
            if not text:
                return
            kind, formatted = format_outbound_content(text)
            if kind == "skip" or not formatted:
                return
            if kind == "tool":
                formatted = f"[tool] {formatted}"
            elif kind == "ask":
                formatted = f"[ask] {formatted}"
            sys.stdout.write(formatted)
            if not formatted.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
