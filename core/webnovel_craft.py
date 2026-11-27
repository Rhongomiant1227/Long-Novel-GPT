"""Compact, provider-neutral craft checks for automatic serial drafting."""

from collections.abc import Mapping
from typing import Any


def outline_craft_guidance(first_chapter: int, last_chapter: int) -> str:
    opening = ''
    if first_chapter == 1:
        opening = (
            '\n- 开篇尽早让读者看见本书卖点怎样改变主角处境。'
            '首章演出一次有结果的行动；后续承接它的后果，不重讲开局或把首次兑现拖出前三章。'
            if last_chapter > 1 else
            '\n- 若这是开篇，先演出一次有结果的行动，让读者看见本书卖点怎样改变主角处境。'
        )
    elif first_chapter <= 3:
        opening = '\n- 承接前章已经兑现的卖点，直接写它的后果，不重讲开局。'
    return (
        '【分章落点】\n'
        '- 先核对本批涉及的人物状态、知情边界、旧承诺和到期伏笔。'
        '每章至少写清一个选择造成的变化，下一章从该变化接起，不重置关系或局势。'
        + opening
    )


def plot_craft_guidance() -> str:
    return """【剧情落地检查】
- 明确人物眼下要什么、谁或什么挡住他、手上有什么筹码。
- 把设定和信息放进当场的行动、交易或争执；关键转折交代它怎样发生、谁作了选择、局面怎样改变。
- 重要关系戏写出双方各自想要什么，以及这次靠近、拒绝或试探怎样改变下一步；不要用重复外貌和心跳代替推进。
- 对齐已发生的事件、人物知情边界和手头资源；解决问题靠已有条件，不临时增设关键能力或道具。
- 结尾接一个由本章选择直接造成的动作、决定或新局面；不要为了钩子凭空追加危险。"""


def draft_craft_guidance() -> str:
    return """【正文落地检查】
- 本章梗概决定事件范围。可以补声音、动作和台词；不得另加关键人物、道具、能力或改变既有事实，不把远期计划写成本章已发生的事。
- 用正在发生的动作、对白和反应演出梗概，不用旁白复述刚发生了什么。
- 场景里的人各有眼前目的；让台词、暧昧、交易或冲突带着具体筹码，并改变彼此的判断或局势。
- 守住本章视角和人物已知信息，不替别的人解释心思。通过角色的选择呈现世界规则。
- 用具体行动写完梗概指定的落点就停。不要在落点之后续写新场景，不用旁白宣布“真正的较量才开始”。
- 不靠重复情绪词、套路反应或泛化比喻制造强度。按本书语气、视角和读者期待取舍，不套固定段长。"""


def continuity_review_guidance(sources: Mapping[str, str]) -> str:
    if not sources:
        return ''
    return (
        '\n【跨章核对，仅提示，不加入 issues】\n'
        '只检查人物知情、位移、物件归属或世界规则是否与下列依据直接矛盾。'
        '合理变化、留白、没有找到证据，都不算冲突。'
        '额外返回 continuity_alerts 数组，每项含 source_id、source_excerpt、draft_excerpt、explanation。'
        '两个 excerpt 都必须逐字摘录各自原文，不得改写或拼接；没有证据就返回空数组。\n\n'
        + '\n\n'.join(f'【{source_id}】\n{excerpt}' for source_id, excerpt in sources.items())
    )


def supported_continuity_alerts(
    payload: Any,
    sources: Mapping[str, str],
    draft: str,
) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        return []
    keys = ('source_id', 'source_excerpt', 'draft_excerpt', 'explanation')
    alerts = []
    seen = set()
    for value in payload:
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) for key in keys):
            continue
        alert = {key: value[key].strip() for key in keys}
        source_id, source_excerpt, draft_excerpt, explanation = (alert[key] for key in keys)
        if not explanation or not (6 <= len(source_excerpt) <= 200 and 6 <= len(draft_excerpt) <= 200):
            continue
        if source_excerpt not in sources.get(source_id, '') or draft_excerpt not in draft:
            continue
        evidence = (source_id, source_excerpt, draft_excerpt)
        if evidence not in seen:
            seen.add(evidence)
            alerts.append(alert)
    return alerts
