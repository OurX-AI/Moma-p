from typing import Any, Awaitable, Callable, Optional


InternalMessageHandler = Callable[..., Awaitable[None]]

_handler: Optional[InternalMessageHandler] = None


def set_internal_message_handler(handler: Optional[InternalMessageHandler]) -> None:
    global _handler
    _handler = handler


async def dispatch_internal_message(**kwargs: Any) -> None:
    if _handler is None:
        return
    await _handler(**kwargs)
