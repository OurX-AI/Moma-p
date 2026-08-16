from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_HEADER_CHARS = 12


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str = ""
    preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"label": self.label}
        if self.description:
            data["description"] = self.description
        if self.preview:
            data["preview"] = self.preview
        return data


@dataclass(frozen=True)
class AskQuestionItem:
    question: str
    options: tuple[QuestionOption, ...]
    header: str = ""
    multi_select: bool = False

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "question": self.question,
            "options": [o.to_dict() for o in self.options],
            "multiSelect": self.multi_select,
        }
        if self.header:
            data["header"] = self.header
        return data


@dataclass
class AskQuestionPayload:
    """结构化提问载荷（仅 object + options；用户仍可用自由文本作答）。"""

    questions: List[AskQuestionItem] = field(default_factory=list)
    answers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw_questions: Any, answers: Optional[Dict[str, Any]] = None) -> "AskQuestionPayload":
        if raw_questions is None:
            raise ValueError("[MISSING_REQUIRED] questions is required.")
        if not isinstance(raw_questions, list) or not raw_questions:
            raise ValueError("[INVALID_VALUE] questions must be a non-empty array.")
        if len(raw_questions) > MAX_QUESTIONS:
            raise ValueError(f"[OUT_OF_RANGE] questions must have at most {MAX_QUESTIONS} items.")

        items: List[AskQuestionItem] = []
        seen_questions: set[str] = set()
        for i, entry in enumerate(raw_questions):
            item = cls._parse_item(entry, index=i)
            if item.question in seen_questions:
                raise ValueError("[INVALID_VALUE] question texts must be unique.")
            seen_questions.add(item.question)
            items.append(item)

        ans: Dict[str, str] = {}
        if isinstance(answers, dict):
            for k, v in answers.items():
                if str(k).strip():
                    ans[str(k)] = str(v)

        return cls(questions=items, answers=ans)

    @staticmethod
    def _parse_item(entry: Any, *, index: int) -> AskQuestionItem:
        label = f"questions[{index}]"
        if isinstance(entry, str):
            raise ValueError(
                f"[TYPE_MISMATCH] {label} must be an object with question and options "
                f"(2-{MAX_OPTIONS} choices). Plain strings are not supported. "
                f"Fix: pass {{question, options:[{{label,...}},...]}}."
            )
        if not isinstance(entry, dict):
            raise ValueError(
                f"[TYPE_MISMATCH] {label} must be an object with question and options."
            )

        question = str(entry.get("question") or "").strip()
        if not question:
            raise ValueError(f"[MISSING_REQUIRED] {label}.question is required.")

        header = str(entry.get("header") or "").strip()
        if len(header) > MAX_HEADER_CHARS:
            header = header[:MAX_HEADER_CHARS]

        multi = bool(entry.get("multiSelect") or entry.get("multi_select") or False)
        raw_options = entry.get("options")
        if not isinstance(raw_options, list):
            raise ValueError(
                f"[MISSING_REQUIRED] {label}.options is required "
                f"(array of {MIN_OPTIONS}-{MAX_OPTIONS} choices)."
            )
        if len(raw_options) < MIN_OPTIONS or len(raw_options) > MAX_OPTIONS:
            raise ValueError(
                f"[OUT_OF_RANGE] {label}.options must have {MIN_OPTIONS}-{MAX_OPTIONS} items."
            )

        options: List[QuestionOption] = []
        seen_labels: set[str] = set()
        for j, opt in enumerate(raw_options):
            parsed = AskQuestionPayload._parse_option(opt, f"{label}.options[{j}]")
            low = parsed.label.lower()
            if low in seen_labels:
                raise ValueError(f"[INVALID_VALUE] {label} option labels must be unique.")
            seen_labels.add(low)
            options.append(parsed)

        return AskQuestionItem(
            question=question,
            header=header,
            options=tuple(options),
            multi_select=multi,
        )

    @staticmethod
    def _parse_option(opt: Any, label: str) -> QuestionOption:
        if isinstance(opt, str):
            text = opt.strip()
            if not text:
                raise ValueError(f"[INVALID_VALUE] {label} must be non-empty.")
            return QuestionOption(label=text)
        if not isinstance(opt, dict):
            raise ValueError(f"[TYPE_MISMATCH] {label} must be string or object.")
        name = str(opt.get("label") or "").strip()
        if not name:
            raise ValueError(f"[MISSING_REQUIRED] {label}.label is required.")
        return QuestionOption(
            label=name,
            description=str(opt.get("description") or "").strip(),
            preview=str(opt.get("preview") or "").strip(),
        )

    def format_user_message(self) -> str:
        blocks: List[str] = []
        for i, q in enumerate(self.questions, 1):
            title = q.header or f"Question {i}"
            select_hint = "multi-select allowed" if q.multi_select else "pick one"
            lines = [
                f"### {title}",
                q.question,
                f"Options ({select_hint}):",
            ]
            for j, opt in enumerate(q.options, 1):
                line = f"{j}. {opt.label}"
                if opt.description:
                    line += f" — {opt.description}"
                lines.append(line)
                if opt.preview:
                    lines.append(f"   preview: {opt.preview}")
            lines.append(
                "Reply with option number(s)/label(s), or free text if none fit."
            )
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def to_result_payload(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "status": "answered" if self.answers else "asked",
            "questions": [q.to_dict() for q in self.questions],
        }
        if self.answers:
            data["answers"] = dict(self.answers)
            data["guidance"] = "User answers are included. Continue with the selected options."
        else:
            data["guidance"] = (
                "Questions were shown to the user. This run ends after ask_question; "
                "treat their next message as the answer — option numbers/labels, or free text."
            )
        return data

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "ask_question": {
                "format": "structured",
                "questions": [q.to_dict() for q in self.questions],
                "answers": dict(self.answers) if self.answers else {},
            }
        }

    def to_json_result(self) -> str:
        return json.dumps(self.to_result_payload(), ensure_ascii=False, indent=2)
