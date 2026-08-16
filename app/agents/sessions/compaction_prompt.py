"""会话压缩提示词：提供 coding / general 两套常量。

切换方式：把 CompactionPrompt 内「当前生效」别名改成指向 GENERAL_* 即可。
本仓库默认指向 CODING_*。
"""
from __future__ import annotations
import re
from typing import Literal


CompactionVariant = Literal["base", "merge"]

# ------------------------------------------------------------------
# coding 套件
# ------------------------------------------------------------------
CODING_SYSTEM = (
    "You are a session summarization agent for a coding assistant. "
    "Do not continue the original task. Do not call tools. Summarize only."
)

CODING_SCOPE = (
    "Your task is to create a detailed summary of the conversation so far, "
    "paying close attention to the user's explicit requests and your previous actions.\n"
    "This summary should be thorough in capturing technical details, code patterns, "
    "and architectural decisions that would be essential for continuing development "
    "work without losing context.\n"
)

CODING_MERGE_SCOPE = (
    "Your task is to create one updated detailed summary by merging the previous "
    "compact summary with the NEW conversation messages provided in history.\n"
    "The previous summary already covers earlier turns; incorporate new facts, files, "
    "errors, user messages, and current work. Output a single complete summary "
    "(all sections), not a delta.\n"
)

CODING_ANALYSIS = (
    "Before providing your final summary, wrap your analysis in <analysis> tags "
    "to organize your thoughts and ensure you've covered all necessary points. "
    "In your analysis process:\n"
    "\n"
    "1. Chronologically analyze each message and section of the conversation. "
    "For each section thoroughly identify:\n"
    " - The user's explicit requests and intents\n"
    " - Your approach to addressing the user's requests\n"
    " - Key decisions, technical concepts and patterns\n"
    " - Specific details like file names, full code snippets, function signatures, "
    "file edits, errors and fixes\n"
    " - Pay special attention to specific user feedback, especially if the user "
    "told you to do something differently.\n"
    "2. Double-check for technical accuracy and completeness, addressing each "
    "required element thoroughly."
)

CODING_SECTIONS = """Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: CRITICAL - This section must preserve enough detail to avoid re-reading files. For each file:
   - File path and purpose
   - Key functions/classes with their signatures
   - Important code snippets (especially recent changes)
   - Configuration values and constants
   - Why this file is relevant to the current task
   Include COMPLETE code snippets for files that were recently edited or are central to ongoing work.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.
 If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
 [Detailed description]

2. Key Technical Concepts:
 - [Concept 1]
 - [Concept 2]
 - [...]

3. Files and Code Sections:
 - [File Name 1]
   - Purpose: [why this file matters]
   - Key code:
   ```python
   [Important code snippet]
   ```
   - Changes made: [what was modified]

 - [File Name 2]
   - Purpose: [why this file matters]
   - Key code:
   ```python
   [Important code snippet]
   ```

4. Errors and fixes:
 - [Detailed description of error 1]:
 - [How you fixed the error]
 - [User feedback on the error if any]
 - [...]

5. Problem Solving:
 [Description of solved problems and ongoing troubleshooting]

6. All user messages:
 - [Detailed non tool use user message]
 - [...]

7. Pending Tasks:
 - [Task 1]
 - [Task 2]
 - [...]

8. Current Work:
 [Precise description of current work]

9. Optional Next Step:
 [Optional Next step to take]

</summary>
"""

# ------------------------------------------------------------------
# general 套件（其它 Agent 需要时，把 CompactionPrompt 内别名改指到这些常量）
# ------------------------------------------------------------------
GENERAL_SYSTEM = (
    "You are a session summarization agent. "
    "Do not continue the original task. Do not call tools. Summarize only."
)

GENERAL_SCOPE = (
    "Your task is to create a detailed summary of the conversation so far, "
    "paying close attention to the user's explicit requests and your previous actions.\n"
    "This summary should be thorough in capturing decisions, constraints, and concrete "
    "details essential for continuing the conversation without losing context.\n"
)

GENERAL_MERGE_SCOPE = (
    "Your task is to create one updated detailed summary by merging the previous "
    "compact summary with the NEW conversation messages provided in history.\n"
    "The previous summary already covers earlier turns; incorporate new facts, "
    "artifacts, corrections, user messages, and current work. Output a single complete "
    "summary (all sections), not a delta.\n"
)

GENERAL_ANALYSIS = (
    "Before providing your final summary, wrap your analysis in <analysis> tags "
    "to organize your thoughts and ensure you've covered all necessary points. "
    "In your analysis process:\n"
    "\n"
    "1. Chronologically analyze each message and section of the conversation. "
    "For each section thoroughly identify:\n"
    " - The user's explicit requests and intents\n"
    " - Your approach to addressing the user's requests\n"
    " - Key decisions, facts, constraints, and domain concepts\n"
    " - Important artifacts (documents, data, links, decisions) and corrections\n"
    " - Pay special attention to specific user feedback, especially if the user "
    "told you to do something differently.\n"
    "2. Double-check for accuracy and completeness, addressing each required "
    "element thoroughly."
)

GENERAL_SECTIONS = """Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Concepts: List important domain concepts, constraints, preferences, and terminology discussed.
3. Artifacts and Details: Enumerate important artifacts (documents, data, links, decisions, examples). Include concrete details needed to continue without the full history.
4. Errors and Corrections: List mistakes, misunderstandings, or failed approaches, and how they were corrected. Pay special attention to user feedback that changed direction.
5. Problem Solving: Document problems solved and any ongoing open questions.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe precisely what was being worked on immediately before this summary request.
9. Optional Next Step: List the next step that is DIRECTLY in line with the user's most recent explicit requests. If the last task concluded, only list next steps the user explicitly asked for. Include verbatim quotes for where you left off when applicable.

Here's an example of how your output should be structured:

<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
 [Detailed description]

2. Key Concepts:
 - [Concept 1]
 - [Concept 2]

3. Artifacts and Details:
 - [Artifact / detail 1]
 - [Artifact / detail 2]

4. Errors and Corrections:
 - [Issue]:
 - [How it was corrected]

5. Problem Solving:
 [Description]

6. All user messages:
 - [Detailed non tool use user message]

7. Pending Tasks:
 - [Task 1]

8. Current Work:
 [Precise description of current work]

9. Optional Next Step:
 [Optional Next step to take]

</summary>
"""


class CompactionPrompt:
    """压缩提示与进模包装（静态工具类）。"""

    NO_TOOLS_PREAMBLE = (
        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
        "\n"
        "- Do NOT use any tool.\n"
        "- You already have all the context you need in the conversation above.\n"
        "- Tool calls will be REJECTED and will waste your only turn — you will fail the task.\n"
        "- Your entire response must be plain text: an <analysis> block followed by a <summary> block.\n"
        "\n"
    )

    NO_TOOLS_TRAILER = (
        "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
        "an <analysis> block followed by a <summary> block. "
        "Tool calls will be rejected and you will fail the task."
    )

    # 当前生效（默认 coding；其它 Agent 改为 GENERAL_*）
    SYSTEM = CODING_SYSTEM
    SCOPE = CODING_SCOPE
    MERGE_SCOPE = CODING_MERGE_SCOPE
    ANALYSIS = CODING_ANALYSIS
    SECTIONS = CODING_SECTIONS

    @classmethod
    def build_user_prompt(
        cls,
        *,
        variant: CompactionVariant = "base",
        previous_summary: str = "",
    ) -> str:
        """构造压缩调用的 user 侧长提示（使用类内当前生效常量）。"""
        scope = cls.MERGE_SCOPE if variant == "merge" else cls.SCOPE
        body = f"{scope}\n{cls.ANALYSIS}\n\n{cls.SECTIONS}\n"
        body += (
            "Please provide your summary following this structure and ensuring precision "
            "and thoroughness in your response."
        )

        prev = (previous_summary or "").strip()
        if prev:
            body += (
                "\n\n[Previous Summary]\n"
                f"{prev}\n"
                "\nMerge the previous summary with the new conversation history above into one "
                "updated summary that follows the required sections."
            )

        return cls.NO_TOOLS_PREAMBLE + body + cls.NO_TOOLS_TRAILER

    @staticmethod
    def format_summary(raw: str) -> str:
        """剥掉 <analysis> 草稿，规范 <summary> 为可读正文（对齐 formatCompactSummary）。"""
        text = (raw or "").strip()
        if not text:
            return ""
        text = re.sub(r"<analysis>[\s\S]*?</analysis>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
        match = re.search(r"<summary>([\s\S]*?)</summary>", text, flags=re.IGNORECASE)
        if match:
            text = f"Summary:\n{match.group(1).strip()}"
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def wrap_for_context(
        summary_body: str,
        *,
        recent_messages_preserved: bool = True,
    ) -> str:
        """进模包装：续会话 preamble + 摘要正文（对齐 getCompactUserSummaryMessage）。"""
        body = (summary_body or "").strip()
        wrapped = (
            "This session is being continued from a previous conversation that ran out of context. "
            "The summary below covers the earlier portion of the conversation.\n"
            f"\n{body}"
        )
        if recent_messages_preserved:
            wrapped += "\n\nRecent messages are preserved verbatim."
        return wrapped
