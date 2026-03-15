from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from auto_novel import AutoNovelRunner, format_chapter_heading, normalize_chapter_draft_text, read_text, write_text


def parse_chapter_spec(spec: str) -> list[int]:
    numbers: set[int] = set()
    for part in (spec or '').split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            start_text, end_text = part.split('-', 1)
            start = int(start_text.strip())
            end = int(end_text.strip())
            if end < start:
                start, end = end, start
            numbers.update(range(start, end + 1))
            continue
        numbers.add(int(part))
    return sorted(numbers)


def infer_title_from_draft(chapter_number: int, draft_text: str, fallback_title: str) -> str:
    for raw_line in (draft_text or '').splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(f'第{chapter_number}章'):
            return line
        break
    return fallback_title or format_chapter_heading(chapter_number, fallback_title)


def build_runner_args(project_dir: Path, args: argparse.Namespace) -> SimpleNamespace:
    state_path = project_dir / 'state.json'
    if not state_path.exists():
        raise FileNotFoundError(f'Project state not found: {state_path}')

    state = json.loads(state_path.read_text(encoding='utf-8'))
    brief_file = state.get('brief_file') or str(project_dir / 'brief.md')

    return SimpleNamespace(
        project_dir=str(project_dir),
        brief_file=str(brief_file),
        brief_text='',
        main_model=state.get('main_model', 'gpt/gpt-5.4'),
        sub_model=state.get('sub_model', state.get('main_model', 'gpt/gpt-5.4')),
        completion_mode=state.get('completion_mode', 'hard_target'),
        target_chars=int(state.get('target_chars', 2_000_000) or 2_000_000),
        min_target_chars=int(state.get('min_target_chars', 0) or 0),
        force_finish_chars=int(state.get('force_finish_chars', 0) or 0),
        max_target_chars=int(state.get('max_target_chars', 0) or 0),
        chapter_char_target=int(state.get('chapter_char_target', 2200) or 2200),
        chapters_per_volume=int(state.get('chapters_per_volume', 30) or 30),
        chapters_per_batch=int(state.get('chapters_per_batch', 5) or 5),
        memory_refresh_interval=int(state.get('memory_refresh_interval', 5) or 5),
        planner_reasoning_effort='medium',
        writer_reasoning_effort='medium',
        sub_reasoning_effort='low',
        summary_reasoning_effort='low',
        critic_model=args.critic_model,
        critic_every_chapters=1,
        critic_reasoning_effort=args.critic_reasoning_effort,
        critic_max_passes=args.critic_max_passes,
        max_thread_num=1,
        max_retries=args.max_retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
        max_chapters=0,
        live_stream=args.live_stream,
        evaluate_completion_only=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Run chapter-level critic repair on completed novel chapters.')
    parser.add_argument('--project-dir', required=True)
    parser.add_argument('--chapters', default='', help='Comma/range chapter list, e.g. 12,18-20')
    parser.add_argument('--all-completed', action='store_true')
    parser.add_argument('--critic-model', default='')
    parser.add_argument('--critic-reasoning-effort', default='xhigh')
    parser.add_argument('--critic-max-passes', type=int, default=3)
    parser.add_argument('--max-retries', type=int, default=0)
    parser.add_argument('--retry-backoff-seconds', type=int, default=15)
    parser.add_argument('--live-stream', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_dir = Path(args.project_dir).resolve()
    runner_args = build_runner_args(project_dir, args)
    runner = AutoNovelRunner(runner_args)

    try:
        completed = runner.state.get('completed_chapters', [])
        completed_index = {int(item['chapter_number']): item for item in completed}

        if args.all_completed:
            chapter_numbers = sorted(completed_index)
        else:
            chapter_numbers = parse_chapter_spec(args.chapters)

        if not chapter_numbers:
            raise ValueError('No chapters selected. Use --chapters or --all-completed.')

        repaired = 0
        changed = 0
        for chapter_number in chapter_numbers:
            if chapter_number not in completed_index:
                raise ValueError(f'Chapter {chapter_number} is not in completed_chapters.')

            item = completed_index[chapter_number]
            draft_path = Path(item['draft_file'])
            if not draft_path.exists():
                raise FileNotFoundError(f'Draft not found: {draft_path}')

            plot_path = draft_path.with_name('plot.md')
            draft_text = read_text(draft_path).strip()
            plot_text = read_text(plot_path).strip()
            title = infer_title_from_draft(chapter_number, draft_text, item.get('title', ''))
            chapter = {
                'chapter_number': chapter_number,
                'title': title,
            }

            revised_text = runner.apply_chapter_critic(chapter, plot_text, draft_text, force=True)
            revised_text = normalize_chapter_draft_text(chapter_number, chapter['title'], revised_text)

            if revised_text != draft_text:
                write_text(draft_path, revised_text)
                changed += 1

            item['title'] = title
            item['chars'] = len(revised_text)
            repaired += 1
            runner.log(f'[critic_repair] 第{chapter_number}章处理完成，变更：{"是" if revised_text != draft_text else "否"}')

        runner.state['generated_chapters'] = len(completed)
        runner.state['generated_chars'] = sum(int(item.get('chars', 0) or 0) for item in completed)
        runner.rebuild_full_manuscript(export_chapters_txt=True)
        runner.log(f'[critic_repair] 完成，已处理 {repaired} 章，其中变更 {changed} 章。')
        return 0
    finally:
        runner.close()


if __name__ == '__main__':
    raise SystemExit(main())
