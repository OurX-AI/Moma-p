from __future__ import annotations
import json
import logging
import re
from typing import List, Optional
from app.infrastructure.llms import llm_factory
from .change_filter import ChangeFilter
from .models import (
    ExperienceExtractionResult,
    ExperiencePattern,
    FileChange,
)


PATTERN_PROMPT = """你是资深架构师，从历史合入中提炼「对后续 Agent/研发有复用价值的经验」。

核心标准（最重要）：
- 经验必须能指导「未来新的、不同的需求」，不是复述「这次 MR 改了什么」
- scenario：什么情况下会遇到同类问题（抽象场景，不要绑定本次具体文件名/commit）
- patterns：跨项目可套用的约定/架构决策/迁移套路，不要写操作清单或 changelog

quality_score 按「未来复用性」打分，不是按「本次改动大小」：
- >=0.7：架构/分层/目录约定/协议桥接等，换需求仍适用（如 skill Hub、目录迁移范式）
- 0.55~0.69：有一定泛化，但偏窄
- <0.55：仅描述本次操作、一次性批量改动、单点文件/单 skill 特例 → 不要输出该条

应丢弃的模式（不要输出，或 quality_score < 0.55）：
- 「对全部 X 做批量删减/瘦身」「一次性处理 N 个文件」→ 这是本次运维操作，不是模式
- 「在 xxx 目录下新增 run_stage.py」→ 绑定单个 skill 实现细节
- 「新增 SkillsHubPanel.vue / 删减 88 行 manager」→ 复述 diff，未抽象成决策
- patterns 里堆具体文件名、行数、PR 动作，而没有可迁移的「为什么/怎么选」

好的 patterns 示例：
- 「skill 按领域分类存 data/skills/{domain}/，而非按 agent 私有目录，便于跨 agent 复用」
- 「外部协议接入用独立 Bridge 层，Agent 核心不感知协议细节」

重要原则：
1. 并非每次合入都有可提炼经验；无复用价值时 extractable=false
2. 一个复杂 MR 可拆多条，但每条必须是不同「可复用场景」；不要为凑数拆 changelog
3. 不要写成 changelog 或逐文件 diff

输入 JSON 含 commit_message 与 files（path/status/churn/hint_action）。

输出严格 JSON（不要 markdown 代码块），每条经验字段如下：
{
  "extractable": true,
  "skip_reason": "",
  "experiences": [
    {
      "title": "短标题（抽象场景，不要复述 commit message）",
      "scenario": "什么情况下适用",
      "patterns": ["可复用的架构/约定/决策"],
      "plan": ["可执行步骤1", "步骤2"],
      "anchors": ["关键目录或模块锚点，如 app/utils/auth/"],
      "relevant_files": ["本次强相关相对路径，如 app/utils/auth/jwt_validator.py"],
      "quality_score": 0.75
    }
  ]
}

当 extractable=false 时：
{"extractable":false,"skip_reason":"原因","experiences":[]}
"""


class PatternSummarizerError(RuntimeError):
    """LLM 经验总结失败（不回退规则文案）。"""


class PatternSummarizer:
    """调用 LLM 判断是否可提炼，并生成场景化经验；失败抛错供任务记 failed。"""

    _CHANGELOG_HINTS = (
        "一次性批量",
        "纯删减",
        "净删减",
        "零新增",
        "只删除",
        "批量处理所有",
        "新增 skillsHubPanel",
        "新增 hub/service",
        "run_stage.py",
        "删减",
        "行)",
        "行，",
    )

    @staticmethod
    async def summarize(
        commit_message: str,
        files: List[FileChange],
        commit_sha: str,
    ) -> ExperienceExtractionResult:
        skip = ChangeFilter.prefilter_skip_reason(commit_message, files)
        if skip:
            return ExperienceExtractionResult(extractable=False, skip_reason=skip)

        payload = {
            "commit_message": (commit_message or "").strip(),
            "files": [
                {
                    "path": f.path,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "churn": f.churn,
                    "hint_action": ChangeFilter.status_action(f.status),
                }
                for f in files
            ],
        }
        user_question = json.dumps(payload, ensure_ascii=False)
        try:
            llm = llm_factory.create_model()
            stream, _usage = await llm.chat_stream(
                system_prompt="你擅长从历史合入沉淀可复用的开发模式，输出必须是合法 JSON。",
                user_prompt=PATTERN_PROMPT,
                user_question=user_question,
            )
            chunks: List[str] = []
            async for chunk in stream:
                chunks.append(chunk)
            raw = "".join(chunks).strip()
        except Exception as e:
            raise PatternSummarizerError(f"LLM 调用失败: {e}") from e

        if not raw or raw.startswith("llm error:") or "max retries exceeded" in raw:
            raise PatternSummarizerError(f"LLM 返回无效: {raw[:200]}")

        data = PatternSummarizer._parse_json(raw)
        extractable = bool(data.get("extractable"))
        skip_reason = str(data.get("skip_reason") or "").strip()
        if not extractable:
            if not skip_reason:
                skip_reason = "LLM 判定无可复用经验"
            return ExperienceExtractionResult(extractable=False, skip_reason=skip_reason)

        patterns = PatternSummarizer._build_patterns(
            data=data,
            commit_sha=commit_sha,
            commit_message=(commit_message or "").strip(),
            files=files,
        )
        if not patterns:
            return ExperienceExtractionResult(extractable=False, skip_reason="LLM 未产出有效经验")
        return ExperienceExtractionResult(extractable=True, patterns=patterns)

    @staticmethod
    def _build_patterns(
        data: dict,
        commit_sha: str,
        commit_message: str,
        files: Optional[List[FileChange]] = None,
    ) -> List[ExperiencePattern]:
        raw_items = data.get("experiences")
        if not isinstance(raw_items, list):
            raw_items = [data]
        fallback_files = PatternSummarizer._fallback_relevant_files(files or [])
        out: List[ExperiencePattern] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            scenario = str(item.get("scenario") or "").strip()
            patterns = PatternSummarizer._str_list(item.get("patterns"))
            if not title or not scenario or not patterns:
                continue
            quality_score = PatternSummarizer._cap_score_if_mr_summary(
                title=title,
                scenario=scenario,
                patterns=patterns,
                score=PatternSummarizer._score(item.get("quality_score")),
            )
            if quality_score < 0.55:
                continue
            plan = PatternSummarizer._str_list(item.get("plan"))
            anchors = PatternSummarizer._str_list(item.get("anchors"))
            relevant_files = [
                p.replace("\\", "/")
                for p in PatternSummarizer._str_list(item.get("relevant_files"))
            ]
            if not relevant_files:
                relevant_files = list(fallback_files)
            if not anchors and relevant_files:
                anchors = PatternSummarizer._anchors_from_files(relevant_files)
            out.append(
                ExperiencePattern(
                    title=title,
                    scenario=scenario,
                    patterns=patterns,
                    source_commits=[commit_sha],
                    commit_message=commit_message,
                    quality_score=quality_score,
                    plan=plan,
                    anchors=anchors,
                    relevant_files=relevant_files,
                )
            )
        return out

    @staticmethod
    def _fallback_relevant_files(files: List[FileChange], *, limit: int = 8) -> List[str]:
        ranked = sorted(files or [], key=lambda f: int(f.churn), reverse=True)
        out: List[str] = []
        for f in ranked:
            p = str(f.path or "").replace("\\", "/").strip()
            if not p or p in out:
                continue
            out.append(p)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _anchors_from_files(files: List[str], *, limit: int = 4) -> List[str]:
        out: List[str] = []
        for fp in files:
            parts = [x for x in fp.replace("\\", "/").split("/") if x]
            if len(parts) >= 2:
                anchor = "/".join(parts[:-1]) + "/"
            else:
                anchor = fp
            if anchor and anchor not in out:
                out.append(anchor)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _cap_score_if_mr_summary(
        title: str,
        scenario: str,
        patterns: List[str],
        score: float,
    ) -> float:
        blob = f"{title} {scenario} {' '.join(patterns)}".lower()
        hits = sum(1 for hint in PatternSummarizer._CHANGELOG_HINTS if hint.lower() in blob)
        concrete_files = len(re.findall(r"[\w/\\-]+\.(py|vue|md|json|ts|tsx|go|java)", blob, flags=re.I))
        capped = score
        if hits >= 2:
            capped = min(capped, 0.5)
        elif hits >= 1:
            capped = min(capped, 0.58)
        if concrete_files >= 3:
            capped = min(capped, 0.58)
        return capped

    @staticmethod
    def _score(raw: object) -> float:
        try:
            score = float(raw)
        except (TypeError, ValueError):
            return 0.0
        if score < 0:
            return 0.0
        if score > 1:
            return 1.0
        return score

    @staticmethod
    def _str_list(raw: object) -> List[str]:
        if not isinstance(raw, list):
            return []
        out: List[str] = []
        for item in raw:
            text = str(item or "").strip()
            if text:
                out.append(text)
        return out

    @staticmethod
    def _parse_json(raw: str) -> dict:
        text = raw.strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise PatternSummarizerError(f"无法解析 JSON: {text[:200]}")
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError as e:
                raise PatternSummarizerError(f"无法解析 JSON: {e}") from e
        if not isinstance(data, dict):
            raise PatternSummarizerError("JSON 根节点不是对象")
        return data
