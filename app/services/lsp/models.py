import asyncio
from dataclasses import dataclass,field
from typing import Any,Dict,List,Optional


@dataclass
class LspServerInfo:
    """LSP 服务端配置。"""

    id:str
    extensions:List[str]
    command:List[str]
    root_markers:List[str]
    initialization:Dict[str,Any]=field(default_factory=dict)


@dataclass
class LspClientState:
    """LSP 客户端运行时状态。"""

    server_id:str
    root:str
    process:asyncio.subprocess.Process
    reader:asyncio.StreamReader
    writer:asyncio.StreamWriter
    pending:Dict[int,asyncio.Future]=field(default_factory=dict)
    diagnostics:Dict[str,List[Dict[str,Any]]]=field(default_factory=dict)
    versions:Dict[str,int]=field(default_factory=dict)
    next_id:int=1
    reader_task:Optional[asyncio.Task]=None
    initialization_options:Dict[str,Any]=field(default_factory=dict)
    diag_debounce_timers:Dict[str,asyncio.TimerHandle]=field(default_factory=dict)
    diag_ready_events:Dict[str,asyncio.Event]=field(default_factory=dict)
