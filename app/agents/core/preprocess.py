import json as _json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..schemes import AgentContext, RuntimeContext
from ...infrastructure.llms.utils import call_with_llm_fallback

_ONLY_REFINE_PREFIX = "Rewrite the user's question to be clearer.\n\n"
_REFINE_RULES = (
    "refined_question rules:\n"
    "- Rewrite the user's question to be clearer. Fix typos, disambiguate pronouns using conversation history.\n"
    "- Preserve original meaning; do not add new requirements.\n"
    "- If the question is already clear, return it as-is.\n"
    "- If conversation history references prior context (e.g., 'this function'), replace with the specific name from history.\n"
)
_ONLY_REFINE_OUTPUT = (
    "\n\nOutput ONLY valid JSON, no markdown fences, no explanation:\n"
    '{"refined_question": "..."}'
)
_CLASSIFICATION_AND_REFINE_OUTPUT = (
    "\n\nOutput ONLY valid JSON, no markdown fences, no explanation:\n"
    '{"task_type": "...", "refined_question": "..."}'
)


class Preprocess:

    @staticmethod
    async def preprocess_user_question(
        question: str,
        history: List[Dict[str, Any]],
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
    ) -> Tuple[str, str]:
        if not question or not question.strip():
            return question or "", ""

        config = agent_ctx.agent_config or {}
        tc_config = config.get("task_classification") or {}
        valid_types = tc_config.get("valid_types") or []
        default_type = tc_config.get("default_type") or ""
        classification_md = Preprocess._classification_prompt(agent_ctx.agent_path, tc_config)

        # 根据情况组装系统提示词和用户提示词
        if valid_types and classification_md:
            sys_prompt = classification_md + "\n\n" + _REFINE_RULES + "\n\n" + _CLASSIFICATION_AND_REFINE_OUTPUT
            user_msg = f"Classify the following user message:\n\nUser message:\n{question}\n\nOutput JSON now:"
        else:
            sys_prompt = _ONLY_REFINE_PREFIX + "\n\n" + _REFINE_RULES + "\n\n" + _ONLY_REFINE_OUTPUT
            user_msg = f"Rewrite the following user message to be clearer:\n\nUser message:\n{question}\n\nOutput JSON now:"
        
        # 调用模型分析
        async def _impl(llm):
            if run_ctx.is_aborted():
                raise RuntimeError("aborted")
            resp, _usage = await llm.chat(system_prompt=sys_prompt, user_prompt="", user_question=user_msg, history=history)
            if run_ctx.is_aborted():
                raise RuntimeError("aborted")
            if not resp or not resp.success:
                raise RuntimeError(f"LLM chat failed: {resp}")
            return resp.content

        try:
            content, _llm = await call_with_llm_fallback(agent_ctx.llms_list, _impl)
        except Exception as e:
            logging.warning("Preprocess LLM call failed: %s", e)
            return question, ""
        
        task_type, refined_question = Preprocess._parse_output_json(
            content, original_question=question, valid_types=valid_types, default_type=default_type,
        )

        prompt_section = ""
        if task_type:
            prompt_section = Preprocess._task_guidance_prompt(agent_ctx.agent_path, tc_config, task_type)

        return refined_question, prompt_section

    @staticmethod
    def _classification_prompt(agent_path: str, tc_config: dict) -> str:
        if not agent_path: 
            return ""
        md_path = tc_config.get("classification_prompt")
        if not md_path:
            return ""
        try:
            full_path = Path(agent_path) / md_path
            return full_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logging.warning("Failed to read MD %s: %s", md_path, e)
            return ""

    @staticmethod
    def _task_guidance_prompt(agent_path: str, tc_config: dict, task_type: str) -> str:
        if not agent_path:
            return ""
        guidance_map = tc_config.get("task_guidance_prompt") or {}
        if not guidance_map:
            return ""
        md_path = guidance_map.get(task_type)
        if not md_path:
            return ""
        try:
            full_path = Path(agent_path) / md_path
            return full_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logging.warning("Failed to read MD %s: %s", md_path, e)
            return ""

    @staticmethod
    def _parse_output_json(content: str, original_question: str, valid_types: list, default_type: str) -> Tuple[str, str]:
        if not content or not content.strip():
            return default_type, original_question

        text = Preprocess._extract_json_text(content)
        try:
            data = _json.loads(text)
            task_type = str(data.get("task_type", "")).lower().strip()
            if not task_type or task_type not in valid_types:
                task_type = default_type
            
            refined = data.get("refined_question", "")
            if not isinstance(refined, str) or not refined.strip():
                refined = original_question
        
            return task_type, refined
        except (ValueError, AttributeError, TypeError) as e:
            logging.warning("Intent JSON parse failed: %s; content[:200]=%s", e, content[:200])
            return default_type, original_question

    @staticmethod
    def _extract_json_text(content: str) -> str:
        text = content.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines).strip()
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            text = text[first:last + 1]
        return text
