from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from auto_novel import cap_model_output_tokens, clone_model_config, extract_json_payload, now_str  # noqa: E402
from backend.backend_utils import get_model_config_from_provider_model  # noqa: E402
from llm_api import ChatMessages, stream_chat  # noqa: E402


SYSTEM_PROMPT = '你是中文超长篇网文的终局复盘编辑，擅长判断一部长篇是否真正自然完结，以及若要最小代价回修，应从第几章开始重写。'


def read_text(path: Path, default: str = '') -> str:
    if not path.exists():
        return default
    return path.read_text(encoding='utf-8')


def truncate_text(text: str, limit: int) -> str:
    source = (text or '').strip()
    if len(source) <= limit:
        return source
    head = int(limit * 0.7)
    tail = limit - head - 10
    return source[:head] + '\n...(中略)...\n' + source[-max(tail, 0):]


def non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or '').splitlines() if line.strip()]


def chapter_heading_from_txt(path: Path) -> str:
    for line in non_empty_lines(read_text(path)):
        return line
    return path.stem


def call_llm_json(prompt: str, provider_model: str, reasoning_effort: str, max_output_tokens: int) -> dict[str, Any]:
    base_model = get_model_config_from_provider_model(provider_model)
    model = clone_model_config(
        base_model,
        reasoning_effort=reasoning_effort,
        **cap_model_output_tokens(base_model, max_output_tokens),
    )
    messages = [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': prompt},
    ]

    gen = stream_chat(model, messages, response_json=True)
    final_messages: ChatMessages | None = None
    while True:
        try:
            current = next(gen)
        except StopIteration as exc:
            if exc.value is not None:
                final_messages = exc.value
            break
        if isinstance(current, ChatMessages):
            final_messages = current

    if not final_messages or not (final_messages.response or '').strip():
        raise RuntimeError('ending review LLM returned empty response.')
    return extract_json_payload(final_messages.response or '')


def build_tail_summary_block(completed: list[dict[str, Any]], limit: int) -> str:
    selected = completed[-limit:]
    lines: list[str] = []
    for item in selected:
        chapter_number = int(item.get('chapter_number', 0) or 0)
        title = str(item.get('title', '') or '').strip() or f'第{chapter_number}章'
        summary_text = truncate_text(read_text(Path(item.get('summary_file', ''))), 320)
        if not summary_text:
            summary_text = '（无摘要）'
        lines.append(f'[{chapter_number}] {title}\n{summary_text}')
    return '\n\n'.join(lines).strip()


def build_tail_fulltext_block(project_dir: Path, completed: list[dict[str, Any]], limit: int) -> str:
    selected = completed[-limit:]
    lines: list[str] = []
    for item in selected:
        chapter_number = int(item.get('chapter_number', 0) or 0)
        txt_path = project_dir / 'manuscript' / 'chapters_txt' / f'ch_{chapter_number:04d}.txt'
        if txt_path.exists():
            heading = chapter_heading_from_txt(txt_path)
            chapter_text = truncate_text(read_text(txt_path), 6000)
        else:
            draft_path = Path(item.get('draft_file', ''))
            heading = str(item.get('title', '') or f'第{chapter_number}章').strip()
            chapter_text = truncate_text(read_text(draft_path), 6000)
        lines.append(f'=== 第{chapter_number}章：{heading} ===\n{chapter_text}')
    return '\n\n'.join(lines).strip()


def build_context(project_dir: Path, summary_window: int, full_window: int) -> tuple[str, dict[str, Any]]:
    state_path = project_dir / 'state.json'
    if not state_path.exists():
        raise FileNotFoundError(f'Project state not found: {state_path}')
    state = json.loads(state_path.read_text(encoding='utf-8'))
    completed = list(state.get('completed_chapters') or [])
    if not completed:
        raise ValueError(f'Project has no completed chapters: {project_dir}')

    memory_dir = project_dir / 'memory'
    manuscript_dir = project_dir / 'manuscript'
    full_novel_path = manuscript_dir / 'full_novel.txt'

    story_memory = truncate_text(read_text(memory_dir / 'story_memory.md'), 6000)
    completion_report = truncate_text(read_text(memory_dir / 'completion_report.md'), 5000)
    ending_guidance = truncate_text(read_text(memory_dir / 'ending_guidance.md'), 4000)
    series_bible_short = truncate_text(read_text(memory_dir / 'series_bible_short.md'), 4500)
    full_novel_tail = truncate_text(read_text(full_novel_path), 18000)
    tail_summaries = build_tail_summary_block(completed, summary_window)
    tail_fulltext = build_tail_fulltext_block(project_dir, completed, full_window)

    first_heading = chapter_heading_from_txt(project_dir / 'manuscript' / 'chapters_txt' / 'ch_0001.txt') if (project_dir / 'manuscript' / 'chapters_txt' / 'ch_0001.txt').exists() else ''
    book_title = ''
    if first_heading.startswith('《') and first_heading.endswith('》'):
        book_title = first_heading
    elif '《' in first_heading and '》' in first_heading:
        book_title = first_heading[first_heading.find('《'):first_heading.find('》') + 1]

    meta = {
        'project_name': project_dir.name,
        'book_title': book_title or str(state.get('book_title', '') or '').strip(),
        'total_chapters': int(state.get('generated_chapters', 0) or len(completed)),
        'total_chars': int(state.get('generated_chars', 0) or 0),
        'summary_window_start': int(completed[max(0, len(completed) - summary_window)].get('chapter_number', 1) or 1),
        'full_window_start': int(completed[max(0, len(completed) - full_window)].get('chapter_number', 1) or 1),
        'last_chapter': int(completed[-1].get('chapter_number', 0) or 0),
    }

    prompt = f"""
请评估这部长篇小说的“现有终局版本”是否真的自然完结；如果需要回修，请给出**最小必要回修起点**，精确到“从第几章开始重写”。

你的身份不是挑字句毛病的校对，也不是继续写小说的人，而是终局复盘编辑。请优先判断：
1. 现有结尾是否已经完成主线真相、主角命运、关键关系、制度/世界层后果、尾声/后日谈中的必要部分。
2. 结尾是否存在拖尾、过度解释、假完结、重复收束、情绪下坠、尾声过长、终局火力不够、伏笔回收失衡。
3. 如果要回修，最小代价应从哪一章开始。必须给**一个明确章节号**，不能说“大概后期”。

硬规则：
- `rewrite_from_chapter` 必须是 0，或者一个明确存在的章节号。
- 若你认为无需回修，`rewrite_from_chapter` 必须为 0。
- 若你认为只需微调最后一章，也必须把起点写成最后一章章节号。
- 若问题是在结尾前的收束布局就已经失控，必须把起点提前到布局真正出问题的第一章，而不是只改最后一章。
- 不要为了显得稳妥而随便给一个很早的章节号；必须遵循“最小必要回修范围”。
- 如果现有完结评估报告过于乐观，你可以明确推翻它。

请返回严格 JSON，结构如下：
{{
  "project_name": "{meta['project_name']}",
  "book_title": "{meta['book_title']}",
  "is_naturally_complete": true,
  "ending_score": 0,
  "confidence": 0,
  "recommend_rewrite": false,
  "rewrite_from_chapter": 0,
  "rewrite_scope_reason": "为何从这一章开始改是最小必要范围",
  "major_issues": ["问题1", "问题2"],
  "strengths": ["优点1", "优点2"],
  "evidence": [
    {{"chapter": 0, "point": "证据说明"}}
  ],
  "rewrite_goals": ["若重改，应优先修什么"],
  "editor_summary": "用一段话总结你的专业判断"
}}

【作品信息】
- 项目：{meta['project_name']}
- 书名：{meta['book_title'] or '（未显式记录）'}
- 总章数：{meta['total_chapters']}
- 总字数：{meta['total_chars']}
- 本次提供的尾段摘要范围：第 {meta['summary_window_start']} 章至第 {meta['last_chapter']} 章
- 本次提供的尾段正文范围：第 {meta['full_window_start']} 章至第 {meta['last_chapter']} 章

【当前完结评估报告】
{completion_report or '（无）'}

【现有收束指导】
{ending_guidance or '（无）'}

【故事记忆】
{story_memory or '（无）'}

【系列短版设定】
{series_bible_short or '（无）'}

【尾段章节摘要】
{tail_summaries or '（无）'}

【最后若干章正文】
{tail_fulltext or '（无）'}

【整本小说结尾长摘】
{full_novel_tail or '（无）'}
""".strip()
    return prompt, meta


def normalize_result(result: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(result)
    normalized['project_name'] = str(result.get('project_name') or meta['project_name']).strip()
    normalized['book_title'] = str(result.get('book_title') or meta['book_title']).strip()
    normalized['is_naturally_complete'] = bool(result.get('is_naturally_complete'))
    normalized['recommend_rewrite'] = bool(result.get('recommend_rewrite'))
    normalized['ending_score'] = max(0, min(100, int(result.get('ending_score', 0) or 0)))
    normalized['confidence'] = max(0, min(100, int(result.get('confidence', 0) or 0)))
    rewrite_from = int(result.get('rewrite_from_chapter', 0) or 0)
    if rewrite_from and rewrite_from < meta['summary_window_start']:
        rewrite_from = meta['summary_window_start']
    if rewrite_from > meta['last_chapter']:
        rewrite_from = meta['last_chapter']
    if not normalized['recommend_rewrite']:
        rewrite_from = 0
    normalized['rewrite_from_chapter'] = rewrite_from
    normalized['rewrite_scope_reason'] = str(result.get('rewrite_scope_reason', '') or '').strip()
    normalized['editor_summary'] = str(result.get('editor_summary', '') or '').strip()
    normalized['major_issues'] = [str(item).strip() for item in (result.get('major_issues') or []) if str(item).strip()]
    normalized['strengths'] = [str(item).strip() for item in (result.get('strengths') or []) if str(item).strip()]
    normalized['rewrite_goals'] = [str(item).strip() for item in (result.get('rewrite_goals') or []) if str(item).strip()]

    evidence_items: list[dict[str, Any]] = []
    for item in result.get('evidence') or []:
        if not isinstance(item, dict):
            continue
        chapter = int(item.get('chapter', 0) or 0)
        if chapter < meta['summary_window_start']:
            chapter = meta['summary_window_start']
        if chapter > meta['last_chapter']:
            chapter = meta['last_chapter']
        point = str(item.get('point', '') or '').strip()
        if not point:
            continue
        evidence_items.append({'chapter': chapter, 'point': point})
    normalized['evidence'] = evidence_items
    return normalized


def write_report(project_dir: Path, result: dict[str, Any]) -> Path:
    report_path = project_dir / 'memory' / 'ending_rewrite_review.md'
    lines = [
        f"# 终局回修评估（{now_str()}）",
        '',
        f"- 项目：{result['project_name']}",
        f"- 书名：{result['book_title'] or '（未记录）'}",
        f"- 是否自然完结：{'是' if result['is_naturally_complete'] else '否'}",
        f"- 终局评分：{result['ending_score']}",
        f"- 置信度：{result['confidence']}",
        f"- 是否建议回修：{'是' if result['recommend_rewrite'] else '否'}",
        f"- 建议回修起点：{result['rewrite_from_chapter']}",
        '',
        '## 回修范围理由',
        result['rewrite_scope_reason'] or '（无）',
        '',
        '## 主要问题',
    ]
    if result['major_issues']:
        lines.extend(f'- {item}' for item in result['major_issues'])
    else:
        lines.append('- 无')
    lines.extend([
        '',
        '## 优点',
    ])
    if result['strengths']:
        lines.extend(f'- {item}' for item in result['strengths'])
    else:
        lines.append('- 无')
    lines.extend([
        '',
        '## 证据',
    ])
    if result['evidence']:
        lines.extend(f"- 第{item['chapter']}章：{item['point']}" for item in result['evidence'])
    else:
        lines.append('- 无')
    lines.extend([
        '',
        '## 若回修，优先目标',
    ])
    if result['rewrite_goals']:
        lines.extend(f'- {item}' for item in result['rewrite_goals'])
    else:
        lines.append('- 无')
    lines.extend([
        '',
        '## 编辑结论',
        result['editor_summary'] or '（无）',
        '',
        '## 原始 JSON',
        '```json',
        json.dumps(result, ensure_ascii=False, indent=2),
        '```',
    ])
    report_path.write_text('\n'.join(lines).strip() + '\n', encoding='utf-8')
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Evaluate whether a completed novel ending needs rewrite and from which chapter.')
    parser.add_argument('--project-dir', action='append', required=True, help='Project directory; may be specified multiple times.')
    parser.add_argument('--model', default='sub2api/gpt-5.4')
    parser.add_argument('--reasoning-effort', default='xhigh')
    parser.add_argument('--summary-window', type=int, default=240)
    parser.add_argument('--full-window', type=int, default=16)
    parser.add_argument('--max-output-tokens', type=int, default=6000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for raw_project in args.project_dir:
        project_dir = Path(raw_project).resolve()
        prompt, meta = build_context(project_dir, args.summary_window, args.full_window)
        payload = call_llm_json(
            prompt=prompt,
            provider_model=args.model,
            reasoning_effort=args.reasoning_effort,
            max_output_tokens=args.max_output_tokens,
        )
        result = normalize_result(payload, meta)
        report_path = write_report(project_dir, result)
        print(json.dumps({'project_dir': str(project_dir), 'report_path': str(report_path), 'result': result}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
