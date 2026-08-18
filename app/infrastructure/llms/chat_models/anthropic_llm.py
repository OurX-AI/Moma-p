import asyncio
import json
from typing import Any,AsyncGenerator,AsyncIterator,Dict,List,Literal,Optional,Tuple
import logging
from anthropic import AsyncAnthropic
from .base import LLM, MAX_RETRY_ATTEMPTS, ContextOverflowError, InvalidResponseError
from .schemes import (
    AskToolResponse,
    ChatResponse,
    LLMStreamEvent,
    StreamEnd,
    StreamTextDelta,
    StreamToolCallReady,
    TokenUsage,
    ToolArgsParser,
    ToolInfo,
)



class AnthropicModels(LLM):
    """Anthropic Claude模型系列"""
    
    def __init__(self, api_key: str, model_provider: str, model_name: str = "claude-3-5-sonnet-20241022", base_url: str = "https://api.anthropic.com", language: str = "Chinese", **kwargs):
        """
        初始化Claude模型
        
        Args:
            api_key (str): Anthropic API密钥
            model_name (str): 模型名称，默认为claude-3-5-sonnet-20241022
            base_url (str): API基础URL，默认为Anthropic官方API
            language (str): 语言设置
            **kwargs: 其他参数
        """
        super().__init__(api_key, model_provider, model_name, base_url, language, **kwargs)
        # 创建Claude客户端
        self.client = AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=60.0
        )

    def _cache_control(self) -> Dict[str, Any]:
        """构建 cache_control 字典。使用 Anthropic 默认 5 分钟 TTL。"""
        return {"type": "ephemeral"}

    def _extract_usage(self, response) -> TokenUsage:
        """从 Claude 非流式响应中提取 token usage（含缓存字段）。

        Claude SDK usage 字段与 OpenAI 不同：
        - input_tokens / output_tokens（非 prompt_tokens / completion_tokens）
        - cache_read_input_tokens / cache_creation_input_tokens（非 cache_read_tokens / cache_write_tokens）
        """
        usage = getattr(response, "usage", None)
        if usage is None:
            return TokenUsage()
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
        total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            total_tokens=total_tokens,
        )

    def _update_usage_from_stream(self, usage: TokenUsage, stream_usage, is_delta: bool = False) -> None:
        """从流式事件 usage 对象就地更新 TokenUsage。

        message_start 事件: usage 含 input_tokens + cache 字段（is_delta=False）
        message_delta 事件: usage 含 output_tokens（is_delta=True）
        """
        if stream_usage is None:
            return
        if not is_delta:
            input_tokens = getattr(stream_usage, "input_tokens", None)
            if isinstance(input_tokens, int):
                usage.input_tokens = input_tokens
            cache_read = getattr(stream_usage, "cache_read_input_tokens", None)
            if isinstance(cache_read, int):
                usage.cache_read_tokens = cache_read
            cache_write = getattr(stream_usage, "cache_creation_input_tokens", None)
            if isinstance(cache_write, int):
                usage.cache_write_tokens = cache_write
        output_tokens = getattr(stream_usage, "output_tokens", None)
        if isinstance(output_tokens, int):
            usage.output_tokens = output_tokens
        usage.total_tokens = (
            (usage.input_tokens or 0)
            + (usage.output_tokens or 0)
            + (usage.cache_read_tokens or 0)
            + (usage.cache_write_tokens or 0)
        )

    def _format_user_message(
        self,
        user_prompt: str,
        user_question: str,
        history: Optional[List[Dict[str, Any]]] = None
    ) -> List[Dict[str, Any]]:
        """格式化消息为 Claude API 所需的格式（system prompt 通过 system 参数单独传入）"""
        try:
            messages = []
            
            # 添加历史消息
            if history:
                messages.extend(self._sanitize_history(history))
 
            # 添加当前用户消息
            if user_question:
                user_message = f"{user_prompt}\n{user_question}" if user_prompt else user_question
                messages.append({"role": "user", "content": user_message})
        
            if not messages:
                logging.error("Messages are empty")
                raise ValueError("Messages are empty")

            self._add_message_cache_breakpoints(messages)
            return messages
        except Exception as e:
            logging.error(f"Error in _format_message: {e}")
            raise e

    def _add_message_cache_breakpoints(
        self,
        messages: List[Dict[str, Any]],
    ) -> None:
        """在最后一条 user 和 assistant 消息上添加 cache_control 断点。

        将缓存前缀从 system + tools 扩展到 system + tools + 历史消息，
        使多轮对话中不变的历史消息部分也能命中缓存。
        仅对 content 为列表的消息生效（string content 会先转为 block 格式）。
        """
        last_user_idx = -1
        last_assistant_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            role = messages[i].get("role")
            if role == "user" and last_user_idx == -1:
                last_user_idx = i
            elif role == "assistant" and last_assistant_idx == -1:
                last_assistant_idx = i
            if last_user_idx != -1 and last_assistant_idx != -1:
                break

        cc = self._cache_control()
        for idx in (last_user_idx, last_assistant_idx):
            if idx == -1:
                continue
            msg = messages[idx]
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = [
                    {"type": "text", "text": content,
                     "cache_control": cc}
                ]
            elif isinstance(content, list) and content:
                last_block = content[-1]
                if isinstance(last_block, dict):
                    last_block["cache_control"] = cc

    def _build_system_param(
        self,
        system_prompt: str,
        system_prompt_dynamic: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """构建 Claude API system 参数，静态块添加 cache_control 断点启用 prompt caching。

        静态部分（agent prompt、项目规则、技能等）加 cache_control 可跨轮缓存；
        动态部分（memory、task_prompt_section）不加 cache_control，变化不影响静态缓存。
        """
        if not system_prompt and not system_prompt_dynamic:
            return None
        blocks: List[Dict[str, Any]] = []
        if system_prompt:
            blocks.append({
                "type": "text",
                "text": system_prompt,
                "cache_control": self._cache_control(),
            })
        if system_prompt_dynamic:
            blocks.append({
                "type": "text",
                "text": system_prompt_dynamic,
            })
        return blocks if blocks else None

    def _sanitize_history(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """转换并清洗历史消息，输出 Anthropic 原生 messages 格式。"""
        sanitized: List[Dict[str, Any]] = []
        pending_tool_ids: set[str] = set()
        pending_assistant_index: Optional[int] = None

        def _normalize_text(content: Any) -> str:
            if content is None:
                return ""
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: List[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text")
                        if isinstance(text, str) and text:
                            parts.append(text)
                    elif isinstance(item, str) and item:
                        parts.append(item)
                return "\n".join(parts).strip()
            return str(content)

        def _drop_unresolved_tool_use() -> None:
            nonlocal pending_assistant_index
            if pending_assistant_index is None:
                pending_tool_ids.clear()
                return
            if 0 <= pending_assistant_index < len(sanitized):
                assistant_msg = dict(sanitized[pending_assistant_index])
                content = assistant_msg.get("content")
                if isinstance(content, list):
                    filtered = [b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_use")]
                    if filtered:
                        assistant_msg["content"] = filtered
                        sanitized[pending_assistant_index] = assistant_msg
                    else:
                        sanitized.pop(pending_assistant_index)
            pending_tool_ids.clear()
            pending_assistant_index = None

        for msg in history:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                continue

            if role in {"system", "user"}:
                if pending_tool_ids:
                    _drop_unresolved_tool_use()
                text = _normalize_text(msg.get("content"))
                if not text:
                    continue
                if role == "system":
                    text = f"[System]\n{text}"
                sanitized.append({"role": "user", "content": text})
                continue

            if role == "assistant":
                if pending_tool_ids:
                    _drop_unresolved_tool_use()

                blocks: List[Dict[str, Any]] = []
                text = _normalize_text(msg.get("content"))
                if text:
                    blocks.append({"type": "text", "text": text})

                ids: set[str] = set()
                tool_calls = msg.get("tool_calls")
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        tool_id = tc.get("id")
                        function = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                        tool_name = function.get("name") or tc.get("name")
                        raw_args = function.get("arguments", tc.get("arguments", {}))
                        parsed_args: Dict[str, Any]
                        if isinstance(raw_args, str):
                            try:
                                loaded = json.loads(raw_args)
                                parsed_args = loaded if isinstance(loaded, dict) else {}
                            except Exception:
                                parsed_args = {}
                        elif isinstance(raw_args, dict):
                            parsed_args = raw_args
                        else:
                            parsed_args = {}
                        if isinstance(tool_id, str) and tool_id and isinstance(tool_name, str) and tool_name:
                            blocks.append(
                                {
                                    "type": "tool_use",
                                    "id": tool_id,
                                    "name": tool_name,
                                    "input": parsed_args,
                                }
                            )
                            ids.add(tool_id)

                if not blocks:
                    continue
                sanitized.append({"role": "assistant", "content": blocks})
                if ids:
                    pending_tool_ids = set(ids)
                    pending_assistant_index = len(sanitized) - 1
                else:
                    pending_tool_ids.clear()
                    pending_assistant_index = None
                continue

            # role == "tool"
            tool_call_id = msg.get("tool_call_id")
            if not (pending_tool_ids and isinstance(tool_call_id, str) and tool_call_id in pending_tool_ids):
                continue
            tool_content = _normalize_text(msg.get("content"))
            sanitized.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call_id,
                            "content": tool_content or "",
                        }
                    ],
                }
            )
            pending_tool_ids.remove(tool_call_id)
            if not pending_tool_ids:
                pending_assistant_index = None

        if pending_tool_ids:
            _drop_unresolved_tool_use()

        return sanitized

    async def chat(self, 
                  system_prompt: str,
                  user_prompt: str,
                  user_question: str,
                  system_prompt_dynamic: Optional[str] = None,
                  history: List[Dict[str, Any]] = None,
                  with_think: Optional[bool] = False,
                  **kwargs) -> Tuple[ChatResponse, TokenUsage]:
        """Claude风格的聊天实现，支持失败重试"""
        messages = self._format_user_message(
            user_prompt, user_question, history
        )

        # 构建参数
        params = {
            "model": self.model_name,
            "messages": messages
        }
        system_param = self._build_system_param(system_prompt, system_prompt_dynamic)
        if system_param:
            params["system"] = system_param
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.messages.create(**params)
                
                # 检查响应结构是否有效
                if not response.content or len(response.content) == 0:
                    raise InvalidResponseError("llm error: Invalid response structure")
                
                # 获取回答内容
                content = response.content[0].text.strip()
                
                # 检查是否因长度限制截断
                if response.stop_reason == "max_tokens":
                    content = self._add_truncate_notify(content)
                usage=self._extract_usage(response)
                self._log_cache_usage(usage, "chat")
                return ChatResponse(content=content,success=True), usage
            
            except Exception as e:
                if self._is_context_overflow_error(e):
                    logging.error(f"Error in chat (context overflow): {e}")
                    raise ContextOverflowError("llm error: context_overflow") from e
                # 检查是否需要重试
                if not self._is_retryable_error(e) or attempt == MAX_RETRY_ATTEMPTS - 1:
                    logging.error(f"Error in chat (attempt {attempt + 1}): {e}")
                    raise RuntimeError(f"llm error: {e}") from e

                # 重试延迟（指数退避）
                delay = self._get_delay(attempt)
                logging.warning(f"Retryable error in chat (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)

        raise RuntimeError("llm error: max retries exceeded")

    async def chat_stream(self, 
                  system_prompt: str,
                  user_prompt: str,
                  user_question: str,
                  system_prompt_dynamic: Optional[str] = None,
                  history: List[Dict[str, Any]] = None,
                  with_think: Optional[bool] = False,
                  **kwargs) -> Tuple[AsyncGenerator[str, None], TokenUsage]:
        """Claude风格的流式聊天实现，支持失败重试"""
        messages = self._format_user_message(
            user_prompt, user_question, history
        )

        # 构建参数
        params = {
            "model": self.model_name,
            "messages": messages,
            "stream": True
        }
        system_param = self._build_system_param(system_prompt, system_prompt_dynamic)
        if system_param:
            params["system"] = system_param
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.messages.create(**params)
                
                # 检查响应结构是否有效
                if not response:
                    raise InvalidResponseError("llm error: Invalid response structure")
                
                usage = TokenUsage()
                
                async def stream_response():
                    nonlocal usage
                    
                    try:
                        async for chunk in response:
                            content = ""

                            if chunk.type == "content_block_delta":
                                if hasattr(chunk.delta, 'text'):
                                    content = chunk.delta.text
                            elif chunk.type == "message_start":
                                msg = getattr(chunk, "message", None)
                                if msg is not None:
                                    self._update_usage_from_stream(usage, getattr(msg, "usage", None), is_delta=False)
                            elif chunk.type == "message_delta":
                                self._update_usage_from_stream(usage, getattr(chunk, "usage", None), is_delta=True)

                            # 如果超长截断，则添加截断提示
                            if hasattr(chunk, 'stop_reason') and chunk.stop_reason == "max_tokens":
                                content = self._add_truncate_notify(content)

                            if content:
                                yield content

                    except Exception as e:
                        logging.error(f"Error in stream response: {e}")
                        raise
                    finally:
                        self._log_cache_usage(usage, "chat_stream")
                        await self._close_stream(response)

                # 返回流式响应和token数量
                return stream_response(), usage

            except Exception as e:
                if self._is_context_overflow_error(e):
                    logging.error(f"Error in chat_stream (context overflow): {e}")
                    raise ContextOverflowError("llm error: context_overflow") from e
                # 检查是否需要重试
                if not self._is_retryable_error(e) or attempt == MAX_RETRY_ATTEMPTS - 1:
                    logging.error(f"Error in chat_stream (attempt {attempt + 1}): {e}")
                    raise RuntimeError(f"llm error: {e}") from e

                # 重试延迟（指数退避）
                delay = self._get_delay(attempt)
                logging.warning(f"Retryable error in chat_stream (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)

        raise RuntimeError("llm error: max retries exceeded")

    async def ask_tools(self,
                       system_prompt: str,
                       user_prompt: str,
                       user_question: str,
                       system_prompt_dynamic: Optional[str] = None,
                       history: List[Dict[str, Any]] = None,
                       tools: Optional[List[dict]] = None,
                       tool_choice: Literal["none", "auto", "required"] = "auto",
                       with_think: Optional[bool] = False,
                       **kwargs) -> Tuple[AskToolResponse, TokenUsage]:
        """Claude风格的工具调用实现，支持失败重试"""
        if tool_choice == "required" and not tools:
            return AskToolResponse(
                content="llm error: tool_choice 为 'required' 时必须提供 tools",
                success=False
            ),TokenUsage()
        
        messages = self._format_user_message(
            user_prompt, user_question, history
        )

        params = {
            "model": self.model_name,
            "messages": messages
        }
        system_param = self._build_system_param(system_prompt, system_prompt_dynamic)
        if system_param:
            params["system"] = system_param

        if tools and tool_choice != "none":
            # 转换工具格式为Claude格式
            claude_tools = []
            for tool in tools:
                claude_tool = {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"]["parameters"]
                }
                claude_tools.append(claude_tool)
            # 最后一个工具添加 cache_control 断点
            if claude_tools:
                claude_tools[-1]["cache_control"] = self._cache_control()
            params["tools"] = claude_tools

            if tool_choice == "required":
                params["tool_choice"] = {"type": "any"}
            elif tool_choice == "auto":
                params["tool_choice"] = {"type": "auto"}

        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.messages.create(**params)
                
                # 检查响应结构是否有效
                if not response.content:
                    raise InvalidResponseError("llm error: Invalid response structure")
                
                # 处理响应
                content = ""
                tool_calls = []
                
                for content_block in response.content:
                    if content_block.type == "text":
                        content += content_block.text
                    elif content_block.type == "tool_use":
                        tool_calls.append(ToolInfo(
                            id=content_block.id,
                            name=content_block.name,
                            args=content_block.input
                        ))
                
                usage=self._extract_usage(response)
                self._log_cache_usage(usage, "ask_tools")
                return AskToolResponse(content=content,tool_calls=tool_calls,success=True), usage

            except Exception as e:
                if self._is_context_overflow_error(e):
                    logging.error(f"Error in ask_tools (context overflow): {e}")
                    raise ContextOverflowError("llm error: context_overflow") from e
                # 检查是否需要重试
                if not self._is_retryable_error(e) or attempt == MAX_RETRY_ATTEMPTS - 1:
                    logging.error(f"Error in ask_tools (attempt {attempt + 1}): {e}")
                    raise RuntimeError(f"llm error: {e}") from e

                # 重试延迟（指数退避）
                delay = self._get_delay(attempt)
                logging.warning(f"Retryable error in ask_tools (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)

        raise RuntimeError("llm error: max retries exceeded")

    async def ask_tools_stream(self,
                       system_prompt: str,
                       user_prompt: str,
                       user_question: str,
                       system_prompt_dynamic: Optional[str] = None,
                       history: List[Dict[str, Any]] = None,
                       tools: Optional[List[dict]] = None,
                       tool_choice: Literal["none", "auto", "required"] = "auto",
                       with_think: Optional[bool] = False,
                       **kwargs) -> Tuple[AsyncIterator[LLMStreamEvent], TokenUsage]:
        """Claude 工具流：yield StreamTextDelta / StreamToolCallReady / StreamEnd。"""
        if tool_choice == "required" and not tools:
            raise ValueError("llm error: tool_choice 为 'required' 时必须提供 tools")
        
        messages = self._format_user_message(
            user_prompt, user_question, history
        )

        params = {
            "model": self.model_name,
            "messages": messages,
            "stream": True
        }
        system_param = self._build_system_param(system_prompt, system_prompt_dynamic)
        if system_param:
            params["system"] = system_param

        if tools and tool_choice != "none":
            # 转换工具格式为Claude格式
            claude_tools = []
            for tool in tools:
                claude_tool = {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"]["parameters"]
                }
                claude_tools.append(claude_tool)
            # 最后一个工具添加 cache_control 断点
            if claude_tools:
                claude_tools[-1]["cache_control"] = self._cache_control()
            params["tools"] = claude_tools
            
            if tool_choice == "required":
                params["tool_choice"] = {"type": "any"}
            elif tool_choice == "auto":
                params["tool_choice"] = {"type": "auto"}
        
        # 添加其他参数，避免重复
        for key, value in kwargs.items():
            if key not in params:
                params[key] = value

        # 实现重试策略
        for attempt in range(MAX_RETRY_ATTEMPTS):
            try:
                response = await self.client.messages.create(**params)
                
                # 检查响应结构是否有效
                if not response:
                    raise InvalidResponseError("llm error: Invalid response structure")
                
                usage = TokenUsage()
                
                async def stream_response():
                    nonlocal usage
                    tool_calls_collected = {}
                    blocks_by_index: Dict[int, str] = {}

                    def yield_tool_ready(tool_id: str):
                        item = tool_calls_collected.get(tool_id)
                        if not item:
                            return
                        name = (item.get("name") or "").strip()
                        if not name:
                            logging.warning("claude tool call %s missing name at stream end, dropped", tool_id)
                            tool_calls_collected.pop(tool_id, None)
                            return
                        raw_args = item.get("arguments") or ""
                        if isinstance(raw_args, dict):
                            args = raw_args
                        else:
                            args = ToolArgsParser.parse(str(raw_args))
                        resolved_id = (item.get("id") or "").strip() or tool_id
                        tool_calls_collected.pop(tool_id, None)
                        yield StreamToolCallReady(id=resolved_id, name=name, arguments=args)
                    
                    try:
                        async for chunk in response:
                            if chunk.type == "content_block_delta":
                                delta = chunk.delta
                                if hasattr(delta, "text") and delta.text:
                                    yield StreamTextDelta(delta.text)
                                elif getattr(delta, "type", None) == "input_json_delta":
                                    partial = getattr(delta, "partial_json", "") or ""
                                    block_index = getattr(chunk, "index", None)
                                    tool_id = blocks_by_index.get(block_index) if block_index is not None else None
                                    if tool_id and tool_id in tool_calls_collected:
                                        tool_calls_collected[tool_id]["arguments"] += partial
                            elif chunk.type == "content_block_start":
                                block = getattr(chunk, "content_block", None)
                                if block and getattr(block, "type", None) == "tool_use":
                                    tool_id = block.id
                                    tool_calls_collected[tool_id] = {
                                        "id": tool_id,
                                        "name": block.name,
                                        "arguments": "",
                                    }
                                    block_index = getattr(chunk, "index", None)
                                    if block_index is not None:
                                        blocks_by_index[block_index] = tool_id
                            elif chunk.type == "content_block_stop":
                                block_index = getattr(chunk, "index", None)
                                tool_id = blocks_by_index.pop(block_index, None) if block_index is not None else None
                                if tool_id:
                                    for ready in yield_tool_ready(tool_id):
                                        yield ready
                            elif chunk.type == "tool_use_block_start":
                                # 开始工具调用
                                tool_id = chunk.tool_use.id
                                tool_calls_collected[tool_id] = {
                                    "id": tool_id,
                                    "name": chunk.tool_use.name,
                                    "arguments": "",
                                }
                            elif chunk.type == "tool_use_block_delta":
                                # 累积工具参数
                                if chunk.delta and chunk.delta.partial_json:
                                    tool_id = chunk.tool_use_id
                                    if tool_id not in tool_calls_collected:
                                        tool_calls_collected[tool_id] = {
                                            "id": tool_id,
                                            "name": "",
                                            "arguments": "",
                                        }
                                    tool_calls_collected[tool_id]["arguments"] += chunk.delta.partial_json
                            elif chunk.type == "tool_use_block_stop":
                                tool_id = getattr(chunk, "tool_use_id", None)
                                if tool_id:
                                    for ready in yield_tool_ready(tool_id):
                                        yield ready
                            elif chunk.type == "message_start":
                                msg = getattr(chunk, "message", None)
                                if msg is not None:
                                    self._update_usage_from_stream(usage, getattr(msg, "usage", None), is_delta=False)
                            elif chunk.type == "message_delta":
                                self._update_usage_from_stream(usage, getattr(chunk, "usage", None), is_delta=True)

                        for tool_id in list(tool_calls_collected.keys()):
                            for ready in yield_tool_ready(tool_id):
                                yield ready
                        self._log_cache_usage(usage, "ask_tools_stream")
                        yield StreamEnd(usage)
                    
                    except Exception as e:
                        logging.error(f"Error in stream response: {e}")
                        raise
                    finally:
                        await self._close_stream(response)
                
                # 返回流式响应和token数量
                return stream_response(), usage

            except Exception as e:
                if self._is_context_overflow_error(e):
                    logging.error(f"Error in ask_tools_stream (context overflow): {e}")
                    raise ContextOverflowError("llm error: context_overflow") from e
                # 检查是否需要重试
                if not self._is_retryable_error(e) or attempt == MAX_RETRY_ATTEMPTS - 1:
                    logging.error(f"Error in ask_tools_stream (attempt {attempt + 1}): {e}")
                    raise RuntimeError(f"llm error: {e}") from e

                # 重试延迟（指数退避）
                delay = self._get_delay(attempt)
                logging.warning(f"Retryable error in ask_tools_stream (attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}): {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)

        raise RuntimeError("llm error: max retries exceeded")
