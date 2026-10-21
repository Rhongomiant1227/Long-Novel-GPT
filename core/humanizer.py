"""Controlled prose-quality post-processing for completed chapter drafts.

This module intentionally does not try to evade AI detectors. It gives the
writer a bounded editorial pass that targets mechanical prose patterns while
protecting chapter identity, continuity facts, and file boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


META_PREFIX_RE = re.compile(
    r"^\s*(?:正文|小说正文|改写后的正文|润色后的正文)\s*[:：]\s*",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^\s*(?:第\s*\d+\s*章|chapter\s+\d+)\b.*$", re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"^\s*```(?:text|markdown|正文)?\s*|\s*```\s*$", re.IGNORECASE)
META_LEAK_RE = re.compile(
    r"(?:作为AI|作为人工智能|以下是改写|以下为正文|我无法|抱歉|修改说明|"
    r"改写说明|字数约|希望这段|如果需要我可以)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class HumanizerValidation:
    valid: bool
    reason: str = ""
    original_chars: int = 0
    revised_chars: int = 0
    protected_missing: tuple[str, ...] = ()


def _clean_lines(text: str) -> list[str]:
    return [line.rstrip() for line in str(text or "").replace("\r\n", "\n").split("\n")]


def normalize_humanizer_output(text: str, *, chapter_number: int = 0, title: str = "") -> str:
    """Extract body prose and remove common wrapper noise from an LLM response."""
    lines = _clean_lines(text)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    if lines and CODE_FENCE_RE.fullmatch(lines[0].strip()):
        lines.pop(0)
    if lines and CODE_FENCE_RE.fullmatch(lines[-1].strip()):
        lines.pop()

    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and HEADING_RE.match(lines[0].strip()):
        lines.pop(0)
    elif lines and title and lines[0].strip() == title.strip():
        lines.pop(0)

    body = "\n".join(lines).strip()
    body = META_PREFIX_RE.sub("", body, count=1).strip()
    if chapter_number:
        body = re.sub(
            rf"^\s*第\s*{int(chapter_number)}\s*章(?:\s+|\s*[:：-]\s*)[^\n]*\n?",
            "",
            body,
            count=1,
            flags=re.IGNORECASE,
        ).strip()
    return body


def build_humanizer_prompt(
    *,
    chapter_number: int,
    title: str,
    body: str,
    outline: str = "",
    plot: str = "",
    previous_summary: str = "",
    protected_terms: Iterable[str] = (),
    chunk_label: str = "",
) -> str:
    terms = [str(item).strip() for item in protected_terms if str(item).strip()]
    protected = "、".join(dict.fromkeys(terms)) or "（无额外专名；不得自行新增事实）"
    return f"""
你正在做一轮“中文网文质量编辑”后处理，不是重写剧情，也不是规避任何检测。
请在保留原剧情事实、人物关系、世界规则、因果顺序和章末状态的前提下，
修正机械化表达，让正文更像经过作者编辑的自然稿件。

本轮只允许处理：
- 句式过于整齐、连续同构句、空泛总结、解释腔和流程图腔
- 不必要的重复转折、同义复述、过密排比和僵硬情绪标签
- 段落节奏、动作与对白的衔接，以及过于平均的句子长度

硬性禁止：
- 不得新增、删除或替换关键事件、人物、地点、物件、规则、时间和因果
- 不得改变人物立场、伤势、关系状态、线索真假或章末悬念
- 不得改写标题、章号或输出任何标题行
- 不得补写下一章已经发生的事件
- 不得输出解释、批注、修改说明、Markdown 代码块或“作为AI”的元话语

必须保留的专名/标签（出现次数不得减少）：{protected}

【章节{chunk_label}】
第{chapter_number}章 {title}

【本章大纲】
{outline or "（无）"}

【本章剧情】
{plot or "（无）"}

【上一章摘要】
{previous_summary or "（无）"}

【待处理正文】
{body}

只输出处理后的完整正文，不要标题。
""".strip()


def build_humanizer_review_prompt(
    *,
    chapter_number: int,
    title: str,
    body: str,
    outline: str = "",
    plot: str = "",
) -> str:
    return f"""
请审阅第{chapter_number}章《{title}》的正文语言质量，只检查“机械化表达”与“叙事可读性”，
不要判断是否像AI，也不要提出剧情重写。

检查项：
1. 是否存在连续同构句、空泛总结、过密排比或流程图腔。
2. 是否存在不必要的同义重复、解释性复述或情绪标签堆叠。
3. 是否有段落节奏过于平均、对白和动作衔接僵硬的问题。
4. 是否会触碰标题、章号、人物事实、线索因果或章末状态。

只输出 JSON：
{{
  "quality_score": 0,
  "should_rewrite": false,
  "issues": ["问题，必须是语言层问题"],
  "rewrite_focus": ["如果改写，最多列出三项语言层目标"]
}}

【大纲】
{outline or "（无）"}

【剧情】
{plot or "（无）"}

【正文】
{body}
""".strip()


def build_humanizer_integrity_review_prompt(
    *,
    chapter_number: int,
    title: str,
    original_body: str,
    revised_body: str,
    outline: str = "",
    plot: str = "",
    previous_summary: str = "",
) -> str:
    """Ask for a strict semantic comparison before a candidate can be applied."""
    return f"""
你是小说连续性审校员。下面是一章原稿及其“仅语言层改写”的候选稿。
你的职责不是评价文笔，而是严格判断候选稿是否完整保留了原稿的剧情事实。

必须逐项核对：
1. 出场人物、人物身份、人物关系、立场、伤势和已知信息。
2. 事件顺序、地点、物件、线索、规则、时间与因果关系。
3. 对话中已经确认或否认的事实。
4. 章末发生的动作、决定、悬念和角色状态。
5. 是否新增原稿中没有的设定、人物、关系、事件或结论。

语言措辞、句式、段落切分、修辞和叙述视角的小幅调整可以不同；剧情事实不得不同。
如果任何一项无法确认保留，必须判定为不通过。

只输出合法 JSON：
{{
  "approved": true,
  "violations": [],
  "summary": "不超过80字，只说明核对结论"
}}

【章节】
第{chapter_number}章 {title}

【大纲】
{outline or "（无）"}

【剧情】
{plot or "（无）"}

【上一章摘要】
{previous_summary or "（无）"}

【原稿】
{original_body}

【候选稿】
{revised_body}
""".strip()


def validate_humanizer_revision(
    original: str,
    revised: str,
    *,
    protected_terms: Iterable[str] = (),
    min_length_ratio: float = 0.72,
    max_length_ratio: float = 1.35,
) -> HumanizerValidation:
    original_body = str(original or "").strip()
    revised_body = str(revised or "").strip()
    if not revised_body:
        return HumanizerValidation(False, "empty_revision", len(original_body), 0)
    if META_LEAK_RE.search(revised_body[:500]):
        return HumanizerValidation(False, "meta_text_leak", len(original_body), len(revised_body))
    if original_body and len(revised_body) < int(len(original_body) * min_length_ratio):
        return HumanizerValidation(
            False,
            "revision_too_short",
            len(original_body),
            len(revised_body),
        )
    if original_body and len(revised_body) > int(len(original_body) * max_length_ratio):
        return HumanizerValidation(
            False,
            "revision_too_long",
            len(original_body),
            len(revised_body),
        )

    missing = []
    for term in dict.fromkeys(str(item).strip() for item in protected_terms if str(item).strip()):
        if original_body.count(term) > revised_body.count(term):
            missing.append(term)
    if missing:
        return HumanizerValidation(
            False,
            "protected_term_count_decreased",
            len(original_body),
            len(revised_body),
            tuple(missing),
        )
    return HumanizerValidation(True, "ok", len(original_body), len(revised_body))
