from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional


class OutboundMessageType(str, Enum):
    RESPONSE = "response"
    STREAM_START = "stream_start"
    STREAM_DELTA = "stream_delta"
    STREAM_END = "stream_end"
    RUN_END = "run_end"
    SUBAGENT_DONE = "subagent_done"


@dataclass
class OutboundMessage:
    session_id: str
    user_id: str
    content: str
    outbound_type: OutboundMessageType = OutboundMessageType.RESPONSE
    metadata: dict[str, Any] = field(default_factory=dict)


OutputHandler = Callable[[OutboundMessage], Awaitable[None]]

_output_handler: Optional[OutputHandler] = None


def set_output_handler(handler: Optional[OutputHandler]) -> None:
    global _output_handler
    _output_handler = handler


async def emit_output(msg: OutboundMessage) -> None:
    if _output_handler is not None:
        await _output_handler(msg)
        return
    if msg.outbound_type == OutboundMessageType.STREAM_DELTA:
        print(msg.content, end="", flush=True)
    elif msg.outbound_type == OutboundMessageType.RESPONSE and msg.content:
        print(msg.content)
    elif msg.outbound_type == OutboundMessageType.SUBAGENT_DONE and msg.content:
        print(msg.content)
    elif msg.outbound_type == OutboundMessageType.RUN_END:
        print()
