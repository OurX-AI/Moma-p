from typing import Any, Dict, List, Optional
from ..catalog import register_tool
from ..base import BaseTool
from ..schemes import ToolErrorResult, ToolResult, ToolSuccessResult
from ...schemes import AgentContext, RuntimeContext
from ...sessions.message import Message
from .ask_question_model import AskQuestionPayload


@register_tool(name="ask_question", toolset="ask")
class AskQuestion(BaseTool):
    """向用户提出结构化选择题（对齐 Claude AskUserQuestion 有用能力）。"""

    @property
    def name(self) -> str:
        return "ask_question"

    def description(self, params=None) -> str:
        return """Ask the user structured multiple-choice questions when a decision is required.

When to use:
- Ambiguous product/design choices that cannot be inferred from code.
- Truly blocking decisions after investigation.

When NOT to use:
- Information available via read/search/run tools.
- Routine implementation details you can decide safely.

Execution rules:
- Each question needs 2-4 options (label + optional description/preview).
- Use multiSelect when choices are not mutually exclusive.
- Prefer completing the task independently; ask only when necessary.

Failure recovery:
- If the user replies with free text, interpret intent and continue; do not re-ask the same question unnecessarily."""

    @property
    def parameters(self) -> Dict[str, Any]:
        option_schema = {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Concise option label (1-5 words) shown to the user.",
                },
                "description": {
                    "type": "string",
                    "description": "What this option means / trade-offs if chosen.",
                },
                "preview": {
                    "type": "string",
                    "description": "Optional preview (mockup, snippet) when focusing this option.",
                },
            },
            "required": ["label"],
        }
        question_schema = {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Full question text, ending with '?'.",
                },
                "header": {
                    "type": "string",
                    "description": "Very short chip/tag label (max ~12 chars), e.g. Auth, Library.",
                },
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 4,
                    "items": option_schema,
                    "description": "2-4 choices (unless multiSelect, treat as mutually exclusive).",
                },
                "multiSelect": {
                    "type": "boolean",
                    "description": "Allow selecting multiple options. Default false.",
                    "default": False,
                },
            },
            "required": ["question", "options"],
        }
        return {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "description": "1-4 structured questions, each with question + options.",
                    "items": question_schema,
                },
                "answers": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "Optional map question text -> selected answer "
                        "(filled by UI when available; multi-select comma-separated)."
                    ),
                },
            },
            "required": ["questions"],
        }

    def is_readonly(self, params=None) -> bool:
        return True

    def is_parallel(self, params=None) -> bool:
        return False

    async def execute(
        self,
        agent_ctx: AgentContext,
        run_ctx: RuntimeContext,
        questions: List[Any],
        answers: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        try:
            payload = AskQuestionPayload.parse(questions, answers)
        except ValueError as e:
            return ToolErrorResult(str(e))

        user_text = payload.format_user_message()
        if user_text and run_ctx.notify_user_callback is not None:
            await run_ctx.notify_user_callback(Message.assistant_message(user_text))

        return ToolSuccessResult(
            payload.to_json_result(),
            metadata=payload.to_metadata(),
        )
