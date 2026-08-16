import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any,Dict,List,Optional,Tuple
from urllib.parse import unquote,urlparse
from sqlalchemy import select
from app.config.settings import settings
from ....repo_mgmt.models.git_repo_mgmt import GitRepository
from .models import LspClientState,LspServerInfo
from app.infrastructure.database import get_db_session


_DIAG_DEBOUNCE_SEC=0.15
_DIAG_WAIT_TIMEOUT_SEC=3.0
_WORKSPACE_SYMBOL_KINDS={5,6,7,8,9,10,11,12,13,14,23,10}


class CodeLSPService:
    """代码分析域的 LSP 服务管理器（同步文件、诊断、语义请求）。"""

    _servers:List[LspServerInfo]=[
        LspServerInfo(
            id="pyright",
            extensions=[".py",".pyi"],
            command=["pyright-langserver","--stdio"],
            root_markers=[
                "pyproject.toml",
                "setup.py",
                "requirements.txt",
                "Pipfile",
                "pyrightconfig.json",
                ".git",
            ],
        ),
        LspServerInfo(
            id="typescript",
            extensions=[".ts",".tsx",".js",".jsx",".mjs",".cjs",".mts",".cts"],
            command=["typescript-language-server","--stdio"],
            root_markers=["package.json","pnpm-lock.yaml","yarn.lock","package-lock.json",".git"],
        ),
        LspServerInfo(
            id="gopls",
            extensions=[".go"],
            command=["gopls"],
            root_markers=["go.work","go.mod",".git"],
        ),
        LspServerInfo(
            id="java",
            extensions=[".java"],
            command=["jdtls"],
            root_markers=["pom.xml","build.gradle","build.gradle.kts","settings.gradle","settings.gradle.kts",".project",".git"],
        ),
        LspServerInfo(
            id="clangd",
            extensions=[".c",".cc",".cpp",".cxx",".h",".hh",".hpp",".hxx"],
            command=["clangd","--background-index"],
            root_markers=["compile_commands.json","compile_flags.txt","CMakeLists.txt","Makefile",".git"],
        ),
    ]
    _clients:Dict[Tuple[str,str],LspClientState]={}
    _repo_root_cache:Dict[str,str]={}
    _lock:asyncio.Lock=asyncio.Lock()

    @staticmethod
    def _lsp_off()->bool:
        return not settings.lsp_enabled

    @staticmethod
    async def has_clients(file_path:str,repo_id:str="")->bool:
        """是否存在可处理该文件的 LSP server（不启动进程）。"""
        if CodeLSPService._lsp_off():
            return False
        if not (repo_id or "").strip():
            return False
        root=await CodeLSPService._resolve_repo_root(repo_id)
        if not root:
            return False
        ext=Path(file_path).suffix.lower()
        for server in CodeLSPService._servers:
            if ext in server.extensions:
                return True
        return False

    @staticmethod
    async def status()->List[Dict[str,Any]]:
        """返回当前已连接 LSP 客户端状态列表。"""
        async with CodeLSPService._lock:
            result:List[Dict[str,Any]]=[]
            for (sid,root),client in CodeLSPService._clients.items():
                result.append(
                    {
                        "id":sid,
                        "name":sid,
                        "root":root,
                        "status":"connected" if client.process.returncode is None else "error",
                    }
                )
            return result

    @staticmethod
    async def touch_file(path:str,wait_for_diagnostics:bool=False,repo_id:str="")->Dict[str,Any]:
        """触发文件 open/change；wait_for_diagnostics 为 True 时在收到 publishDiagnostics 后 debounce 再返回。"""
        if CodeLSPService._lsp_off():
            return {"touched":False,"reason":"lsp disabled"}
        if not (repo_id or "").strip():
            return {"touched":False,"reason":"repo_id is required"}

        file_path=Path(path).expanduser().resolve()
        if not file_path.exists() or not file_path.is_file():
            raise ValueError(f"file not found: {file_path}")

        norm=str(file_path.resolve())
        clients=await CodeLSPService._get_clients(str(file_path),repo_id=repo_id)
        if not clients:
            return {"touched":False,"reason":"no available lsp server for this file"}

        if wait_for_diagnostics:
            for client in clients:
                if norm not in client.diag_ready_events:
                    client.diag_ready_events[norm]=asyncio.Event()
                client.diag_ready_events[norm].clear()

        for client in clients:
            await CodeLSPService._open_or_change(client,str(file_path))

        if wait_for_diagnostics:
            await asyncio.gather(
                *[
                    CodeLSPService._wait_diagnostics_debounced(client,norm,_DIAG_WAIT_TIMEOUT_SEC)
                    for client in clients
                ],
                return_exceptions=True,
            )

        return {
            "touched":True,
            "file":str(file_path),
            "clients":[client.server_id for client in clients],
        }

    @staticmethod
    async def _wait_diagnostics_debounced(client:LspClientState,norm_path:str,timeout:float)->None:
        ev=client.diag_ready_events.get(norm_path)
        if not ev:
            return
        try:
            await asyncio.wait_for(ev.wait(),timeout=timeout)
        except asyncio.TimeoutError:
            pass

    @staticmethod
    async def diagnostics(path:Optional[str]=None)->Dict[str,List[Dict[str,Any]]]:
        """汇总所有客户端诊断，支持按单文件过滤。"""
        if CodeLSPService._lsp_off():
            return {}

        expected=None
        if path:
            expected=str(Path(path).expanduser().resolve())

        merged:Dict[str,List[Dict[str,Any]]]={}
        async with CodeLSPService._lock:
            for client in CodeLSPService._clients.values():
                for file_path,items in client.diagnostics.items():
                    if expected and str(Path(file_path).resolve())!=expected:
                        continue
                    merged.setdefault(file_path,[]).extend(items)
        return merged

    @staticmethod
    async def hover(file_path:str,line:int,character:int,repo_id:str="")->List[Any]:
        """textDocument/hover；line、character 为编辑器 1-based。"""
        return await CodeLSPService._textdoc_request(
            file_path,line,character,"textDocument/hover",lambda uri,l0,c0:{
                "textDocument":{"uri":uri},
                "position":{"line":l0,"character":c0},
            },
            repo_id=repo_id,
        )

    @staticmethod
    async def definition(file_path:str,line:int,character:int,repo_id:str="")->List[Any]:
        """textDocument/definition。"""
        out:List[Any]=[]
        for r in await CodeLSPService._textdoc_request(
            file_path,line,character,"textDocument/definition",lambda uri,l0,c0:{
                "textDocument":{"uri":uri},
                "position":{"line":l0,"character":c0},
            },
            repo_id=repo_id,
        ):
            if r is None:
                continue
            if isinstance(r,list):
                out.extend([x for x in r if x])
            else:
                out.append(r)
        return out

    @staticmethod
    async def references(file_path:str,line:int,character:int,repo_id:str="")->List[Any]:
        """textDocument/references。"""
        out:List[Any]=[]
        for r in await CodeLSPService._textdoc_request(
            file_path,line,character,"textDocument/references",lambda uri,l0,c0:{
                "textDocument":{"uri":uri},
                "position":{"line":l0,"character":c0},
                "context":{"includeDeclaration":True},
            },
            repo_id=repo_id,
        ):
            if r is None:
                continue
            if isinstance(r,list):
                out.extend([x for x in r if x])
            else:
                out.append(r)
        return out

    @staticmethod
    async def implementation(file_path:str,line:int,character:int,repo_id:str="")->List[Any]:
        """textDocument/implementation。"""
        out:List[Any]=[]
        for r in await CodeLSPService._textdoc_request(
            file_path,line,character,"textDocument/implementation",lambda uri,l0,c0:{
                "textDocument":{"uri":uri},
                "position":{"line":l0,"character":c0},
            },
            repo_id=repo_id,
        ):
            if r is None:
                continue
            if isinstance(r,list):
                out.extend([x for x in r if x])
            else:
                out.append(r)
        return out

    @staticmethod
    async def document_symbol(file_path:str,repo_id:str="")->List[Any]:
        """textDocument/documentSymbol。"""
        if CodeLSPService._lsp_off():
            return []

        p=Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"file not found: {p}")
        uri=p.as_uri()
        clients=await CodeLSPService._get_clients(str(p),repo_id=repo_id)
        if not clients:
            return []

        await CodeLSPService.touch_file(str(p),wait_for_diagnostics=True,repo_id=repo_id)
        merged:List[Any]=[]
        for c in clients:
            try:
                r=await CodeLSPService._send_request(
                    c,
                    "textDocument/documentSymbol",
                    {"textDocument":{"uri":uri}},
                    timeout=15.0,
                )
                if isinstance(r,list):
                    merged.extend([x for x in r if x])
                elif r:
                    merged.append(r)
            except Exception as e:
                logging.debug("LSP documentSymbol failed: %s",e)
        return merged

    @staticmethod
    async def workspace_symbol(query:str,repo_id:str="")->List[Any]:
        """workspace/symbol；合并各 client 结果并做简单 kind 过滤。"""
        if CodeLSPService._lsp_off():
            return []
        if not (repo_id or "").strip():
            return []
        root=await CodeLSPService._resolve_repo_root(repo_id)
        if not root:
            return []

        async with CodeLSPService._lock:
            clients=[c for c in CodeLSPService._clients.values() if c.root==root]
        merged:List[Any]=[]
        for c in clients:
            try:
                r=await CodeLSPService._send_request(
                    c,
                    "workspace/symbol",
                    {"query":query},
                    timeout=15.0,
                )
                if not isinstance(r,list):
                    continue
                for x in r:
                    k=x.get("kind") if isinstance(x,dict) else None
                    if k is not None and k in _WORKSPACE_SYMBOL_KINDS:
                        merged.append(x)
            except Exception as e:
                logging.debug("LSP workspace/symbol failed: %s",e)
        return merged[:10]

    @staticmethod
    async def prepare_call_hierarchy(file_path:str,line:int,character:int,repo_id:str="")->List[Any]:
        """textDocument/prepareCallHierarchy。"""
        raw=await CodeLSPService._textdoc_request(
            file_path,line,character,"textDocument/prepareCallHierarchy",lambda uri,l0,c0:{
                "textDocument":{"uri":uri},
                "position":{"line":l0,"character":c0},
            },
            repo_id=repo_id,
        )
        out:List[Any]=[]
        for r in raw:
            if r is None:
                continue
            if isinstance(r,list):
                out.extend([x for x in r if x])
            elif r:
                out.append(r)
        return out

    @staticmethod
    async def incoming_calls(file_path:str,line:int,character:int,repo_id:str="")->List[Any]:
        """callHierarchy/incomingCalls（每个 client 内先 prepare 再请求）。"""
        if CodeLSPService._lsp_off():
            return []

        p=Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"file not found: {p}")
        uri=p.as_uri()
        line0=line-1
        char0=character-1
        clients=await CodeLSPService._get_clients(str(p),repo_id=repo_id)
        if not clients:
            return []

        await CodeLSPService.touch_file(str(p),wait_for_diagnostics=True,repo_id=repo_id)
        out:List[Any]=[]
        for c in clients:
            try:
                items=await CodeLSPService._send_request(
                    c,
                    "textDocument/prepareCallHierarchy",
                    {"textDocument":{"uri":uri},"position":{"line":line0,"character":char0}},
                    timeout=15.0,
                )
                if not items:
                    continue
                arr=items if isinstance(items,list) else [items]
                if not arr:
                    continue
                r=await CodeLSPService._send_request(
                    c,
                    "callHierarchy/incomingCalls",
                    {"item":arr[0]},
                    timeout=15.0,
                )
                if isinstance(r,list):
                    out.extend(r)
                elif r:
                    out.append(r)
            except Exception as e:
                logging.debug("LSP incomingCalls failed: %s",e)
        return out

    @staticmethod
    async def outgoing_calls(file_path:str,line:int,character:int,repo_id:str="")->List[Any]:
        """callHierarchy/outgoingCalls（每个 client 内先 prepare 再请求）。"""
        if CodeLSPService._lsp_off():
            return []

        p=Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"file not found: {p}")
        uri=p.as_uri()
        line0=line-1
        char0=character-1
        clients=await CodeLSPService._get_clients(str(p),repo_id=repo_id)
        if not clients:
            return []

        await CodeLSPService.touch_file(str(p),wait_for_diagnostics=True,repo_id=repo_id)
        out:List[Any]=[]
        for c in clients:
            try:
                items=await CodeLSPService._send_request(
                    c,
                    "textDocument/prepareCallHierarchy",
                    {"textDocument":{"uri":uri},"position":{"line":line0,"character":char0}},
                    timeout=15.0,
                )
                if not items:
                    continue
                arr=items if isinstance(items,list) else [items]
                if not arr:
                    continue
                r=await CodeLSPService._send_request(
                    c,
                    "callHierarchy/outgoingCalls",
                    {"item":arr[0]},
                    timeout=15.0,
                )
                if isinstance(r,list):
                    out.extend(r)
                elif r:
                    out.append(r)
            except Exception as e:
                logging.debug("LSP outgoingCalls failed: %s",e)
        return out

    @staticmethod
    async def _textdoc_request(
        file_path:str,
        line:int,
        character:int,
        method:str,
        params_fn:Any,
        repo_id:str="",
    )->List[Any]:
        if CodeLSPService._lsp_off():
            return []

        p=Path(file_path).expanduser().resolve()
        if not p.is_file():
            raise ValueError(f"file not found: {p}")
        uri=p.as_uri()
        line0=line-1
        char0=character-1
        clients=await CodeLSPService._get_clients(str(p),repo_id=repo_id)
        if not clients:
            return []

        await CodeLSPService.touch_file(str(p),wait_for_diagnostics=True,repo_id=repo_id)
        params=params_fn(uri,line0,char0)
        results:List[Any]=[]
        for c in clients:
            try:
                r=await CodeLSPService._send_request(c,method,params,timeout=15.0)
                results.append(r)
            except Exception as e:
                logging.debug("LSP %s failed: %s",method,e)
                results.append(None)
        return results

    @staticmethod
    async def close_all()->None:
        """关闭所有 LSP 客户端连接与子进程。"""
        async with CodeLSPService._lock:
            clients=list(CodeLSPService._clients.values())
            CodeLSPService._clients.clear()

        for client in clients:
            for h in list(client.diag_debounce_timers.values()):
                try:
                    h.cancel()
                except Exception:
                    pass
            client.diag_debounce_timers.clear()
            try:
                await CodeLSPService._send_request(client,"shutdown",{},timeout=2.0)
            except Exception:
                pass
            try:
                await CodeLSPService._send_notification(client,"exit",{})
            except Exception:
                pass
            if client.reader_task and not client.reader_task.done():
                client.reader_task.cancel()
            if client.process.returncode is None:
                client.process.kill()

    @staticmethod
    async def _get_clients(file_path:str,repo_id:str="")->List[LspClientState]:
        """按文件后缀匹配可用 server，并返回对应 client。"""
        if CodeLSPService._lsp_off():
            return []
        if not (repo_id or "").strip():
            return []
        root=await CodeLSPService._resolve_repo_root(repo_id)
        if not root:
            return []

        ext=Path(file_path).suffix.lower()
        candidates=[server for server in CodeLSPService._servers if ext in server.extensions]

        result:List[LspClientState]=[]
        for server in candidates:
            client=await CodeLSPService._get_or_create(server,root)
            if client:
                result.append(client)
        return result

    @staticmethod
    async def _get_or_create(server:LspServerInfo,root:str)->Optional[LspClientState]:
        """获取已有 client，或创建并初始化一个新 client。"""
        key=(server.id,root)
        async with CodeLSPService._lock:
            existing=CodeLSPService._clients.get(key)
            if existing and existing.process.returncode is None:
                return existing

        process=await CodeLSPService._spawn(server.command,root)
        if not process or process.stdout is None or process.stdin is None:
            return None

        init_opts=dict(server.initialization) if server.initialization else {}
        client=LspClientState(
            server_id=server.id,
            root=root,
            process=process,
            reader=process.stdout,
            writer=process.stdin,
            initialization_options=init_opts,
        )
        client.reader_task=asyncio.create_task(CodeLSPService._run_reader(client))

        try:
            await CodeLSPService._send_request(
                client,
                "initialize",
                {
                    "rootUri":Path(root).as_uri(),
                    "processId":os.getpid(),
                    "workspaceFolders":[{"name":"workspace","uri":Path(root).as_uri()}],
                    "initializationOptions":init_opts,
                    "capabilities":{
                        "window":{"workDoneProgress":True},
                        "workspace":{
                            "configuration":True,
                            "didChangeWatchedFiles":{"dynamicRegistration":True},
                        },
                        "textDocument":{
                            "synchronization":{"didOpen":True,"didChange":True},
                            "publishDiagnostics":{"versionSupport":True},
                        },
                    },
                },
                timeout=15.0,
            )
            await CodeLSPService._send_notification(client,"initialized",{})
            if init_opts:
                await CodeLSPService._send_notification(
                    client,
                    "workspace/didChangeConfiguration",
                    {"settings":init_opts},
                )
        except Exception as e:
            logging.warning("LSP initialize failed: server=%s root=%s error=%s",server.id,root,e)
            process.kill()
            return None

        async with CodeLSPService._lock:
            CodeLSPService._clients[key]=client
        return client

    @staticmethod
    async def _spawn(command:List[str],cwd:str)->Optional[asyncio.subprocess.Process]:
        """启动 LSP 子进程。"""
        try:
            return await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logging.info("LSP server not found: %s"," ".join(command))
            return None
        except Exception as e:
            logging.warning("Failed to start LSP server %s: %s"," ".join(command),e)
            return None

    @staticmethod
    async def _run_reader(client:LspClientState)->None:
        """包装 reader 循环，统一处理退出日志。"""
        try:
            await CodeLSPService._reader_loop(client)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.debug("LSP reader stopped: server=%s root=%s error=%s",client.server_id,client.root,e)

    @staticmethod
    async def _send_request(
        client:LspClientState,
        method:str,
        params:Dict[str,Any],
        timeout:float=10.0,
    )->Any:
        """发送 JSON-RPC request 并等待响应。"""
        request_id=client.next_id
        client.next_id+=1

        future=asyncio.get_running_loop().create_future()
        client.pending[request_id]=future

        await CodeLSPService._write(
            client,
            {
                "jsonrpc":"2.0",
                "id":request_id,
                "method":method,
                "params":params,
            },
        )
        return await asyncio.wait_for(future,timeout=timeout)

    @staticmethod
    async def _send_notification(client:LspClientState,method:str,params:Dict[str,Any])->None:
        """发送 JSON-RPC notification。"""
        await CodeLSPService._write(
            client,
            {
                "jsonrpc":"2.0",
                "method":method,
                "params":params,
            },
        )

    @staticmethod
    async def _notify_watched_files(client:LspClientState,uri:str,change_type:int)->None:
        """workspace/didChangeWatchedFiles（在 didOpen/didChange 之前发送）。"""
        await CodeLSPService._send_notification(
            client,
            "workspace/didChangeWatchedFiles",
            {"changes":[{"uri":uri,"type":change_type}]},
        )

    @staticmethod
    async def _open_or_change(client:LspClientState,file_path:str)->None:
        """首次 didOpen 前发 watched created；变更前发 watched changed。"""
        text=Path(file_path).read_text(encoding="utf-8",errors="replace")
        uri=Path(file_path).as_uri()
        version=client.versions.get(file_path,-1)+1

        if file_path not in client.versions:
            client.diagnostics.pop(str(Path(file_path).resolve()),None)
            await CodeLSPService._notify_watched_files(client,uri,1)
            await CodeLSPService._send_notification(
                client,
                "textDocument/didOpen",
                {
                    "textDocument":{
                        "uri":uri,
                        "languageId":CodeLSPService._language_id(Path(file_path).suffix.lower()),
                        "version":version,
                        "text":text,
                    }
                },
            )
        else:
            await CodeLSPService._notify_watched_files(client,uri,2)
            await CodeLSPService._send_notification(
                client,
                "textDocument/didChange",
                {
                    "textDocument":{"uri":uri,"version":version},
                    "contentChanges":[{"text":text}],
                },
            )

        client.versions[file_path]=version

    @staticmethod
    async def _reader_loop(client:LspClientState)->None:
        """持续读取 LSP 响应与通知。"""
        while True:
            header=await client.reader.readuntil(b"\r\n\r\n")
            length=CodeLSPService._parse_content_length(header.decode("ascii",errors="ignore"))
            if length<=0:
                continue
            payload=await client.reader.readexactly(length)
            message=json.loads(payload.decode("utf-8",errors="replace"))
            await CodeLSPService._handle_message(client,message)

    @staticmethod
    async def _write(client:LspClientState,obj:Dict[str,Any])->None:
        """写入一条 LSP 协议消息（带 Content-Length 头）。"""
        body=json.dumps(obj,ensure_ascii=False).encode("utf-8")
        header=f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        client.writer.write(header+body)
        await client.writer.drain()

    @staticmethod
    async def _handle_message(client:LspClientState,msg:Dict[str,Any])->None:
        """分发响应、服务端请求与通知。"""
        if "result" in msg or "error" in msg:
            request_id=msg.get("id")
            future=client.pending.pop(request_id,None) if request_id is not None else None
            if future and not future.done():
                if "error" in msg:
                    future.set_exception(RuntimeError(str(msg["error"])))
                else:
                    future.set_result(msg.get("result"))
            return

        if "method" in msg and "id" in msg:
            await CodeLSPService._handle_server_request(client,msg)
            return

        if msg.get("method")=="textDocument/publishDiagnostics":
            params=msg.get("params") or {}
            uri=params.get("uri","")
            norm=CodeLSPService._uri_to_norm_path(uri)
            client.diagnostics[norm]=params.get("diagnostics") or []
            CodeLSPService._schedule_diag_notify(client,norm)
            return

    @staticmethod
    def _schedule_diag_notify(client:LspClientState,norm_path:str)->None:
        """publishDiagnostics 后 debounce，再唤醒等待方（默认 150ms）。"""
        old=client.diag_debounce_timers.pop(norm_path,None)
        if old:
            old.cancel()

        loop=asyncio.get_running_loop()

        def fire()->None:
            client.diag_debounce_timers.pop(norm_path,None)
            ev=client.diag_ready_events.get(norm_path)
            if ev:
                ev.set()

        h=loop.call_later(_DIAG_DEBOUNCE_SEC,fire)
        client.diag_debounce_timers[norm_path]=h

    @staticmethod
    async def _handle_server_request(client:LspClientState,msg:Dict[str,Any])->None:
        """处理语言服务发起的 JSON-RPC request。"""
        req_id=msg.get("id")
        method=msg.get("method","")
        result:Any=None

        if method=="workspace/configuration":
            result=[client.initialization_options]
        elif method=="window/workDoneProgress/create":
            result=None
        elif method in ("client/registerCapability","client/unregisterCapability"):
            result=None
        elif method=="workspace/workspaceFolders":
            result=[{"name":"workspace","uri":Path(client.root).as_uri()}]
        else:
            result=None

        await CodeLSPService._write(
            client,
            {"jsonrpc":"2.0","id":req_id,"result":result},
        )

    @staticmethod
    def _uri_to_norm_path(uri:str)->str:
        """file URI 转为规范化绝对路径（兼容 Windows）。"""
        if not uri.startswith("file:"):
            return str(Path(uri).resolve())
        u=urlparse(uri)
        p=u.path
        if os.name=="nt" and len(p)>=3 and p[0]=="/" and p[2]==":":
            p=p[1:]
        p=unquote(p)
        return str(Path(p).resolve())

    @staticmethod
    async def _resolve_repo_root(repo_id:str)->str:
        rid=(repo_id or "").strip()
        if not rid:
            return ""
        cached=CodeLSPService._repo_root_cache.get(rid)
        if cached and Path(cached).is_dir():
            return cached
        async with get_db_session() as db:
            repo=await db.scalar(select(GitRepository).where(GitRepository.id==rid))
            if not repo or not repo.local_path:
                return ""
            root=str(Path(repo.local_path).expanduser().resolve())
            if not Path(root).is_dir():
                return ""
            CodeLSPService._repo_root_cache[rid]=root
            return root

    @staticmethod
    def _parse_content_length(header:str)->int:
        """从消息头解析 Content-Length。"""
        for line in header.split("\r\n"):
            if line.lower().startswith("content-length:"):
                try:
                    return int(line.split(":",1)[1].strip())
                except Exception:
                    return 0
        return 0

    @staticmethod
    def _language_id(ext:str)->str:
        """按扩展名推断 languageId。"""
        mapping={
            ".py":"python",
            ".pyi":"python",
            ".ts":"typescript",
            ".tsx":"typescriptreact",
            ".js":"javascript",
            ".jsx":"javascriptreact",
            ".mjs":"javascript",
            ".cjs":"javascript",
            ".mts":"typescript",
            ".cts":"typescript",
        }
        return mapping.get(ext,"plaintext")
