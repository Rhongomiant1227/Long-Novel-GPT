from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from auto_novel import now_str, read_text, split_full_novel, write_text  # noqa: E402


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + f'.backup.{int(time.time())}')
    shutil.copy2(path, backup)
    return backup


def load_state(project_dir: Path) -> dict:
    state_path = project_dir / 'state.json'
    if not state_path.exists():
        raise FileNotFoundError(f'Project state not found: {state_path}')
    return json.loads(state_path.read_text(encoding='utf-8-sig'))


def chapter_volume_number(chapter_number: int, chapters_per_volume: int) -> tuple[int, int]:
    volume = (chapter_number - 1) // chapters_per_volume + 1
    chapter_in_volume = (chapter_number - 1) % chapters_per_volume + 1
    return volume, chapter_in_volume


def rebuild_full_novel(project_dir: Path, completed: list[dict]) -> int:
    full_novel_path = project_dir / 'manuscript' / 'full_novel.txt'
    manuscript_dir = project_dir / 'manuscript' / 'chapters_txt'
    chunks: list[str] = []
    for item in completed:
        draft_path = Path(item['draft_file'])
        text = read_text(draft_path).strip()
        if text:
            chunks.append(text)
    if chunks:
        write_text(full_novel_path, '\n\n'.join(chunks).rstrip() + '\n')
    elif full_novel_path.exists():
        full_novel_path.unlink()

    manuscript_dir.mkdir(parents=True, exist_ok=True)
    for txt in manuscript_dir.glob('ch_*.txt'):
        txt.unlink()
    if chunks:
        chapters = split_full_novel(read_text(full_novel_path))
        for chapter in chapters:
            chapter_number = int(chapter['source_number'])
            chapter_text = str(chapter['text']).rstrip() + '\n'
            write_text(manuscript_dir / f'ch_{chapter_number:04d}.txt', chapter_text)
    return len(chunks)


def delete_future_content(project_dir: Path, rewrite_from_chapter: int, chapters_per_volume: int) -> None:
    volumes_dir = project_dir / 'volumes'
    if not volumes_dir.exists():
        return
    target_volume, _ = chapter_volume_number(rewrite_from_chapter, chapters_per_volume)
    for volume_dir in sorted(volumes_dir.glob('vol_*')):
        try:
            volume_number = int(volume_dir.name.split('_')[-1])
        except ValueError:
            continue
        if volume_number < target_volume:
            continue
        plan_path = volume_dir / 'plan.md'
        if plan_path.exists():
            plan_path.unlink()
        if volume_number > target_volume:
            shutil.rmtree(volume_dir, ignore_errors=True)
            continue
        chapters_dir = volume_dir / 'chapters'
        if chapters_dir.exists():
            for chapter_dir in sorted(chapters_dir.glob('ch_*')):
                try:
                    chapter_number = int(chapter_dir.name.split('_')[-1])
                except ValueError:
                    continue
                if chapter_number >= rewrite_from_chapter:
                    shutil.rmtree(chapter_dir, ignore_errors=True)


def reset_memory_files(project_dir: Path) -> None:
    memory_dir = project_dir / 'memory'
    for name in (
        'completion_report.md',
        'auto_ending_guidance.md',
        'auto_ending_quality_guidance.md',
        'ending_quality_review.md',
        'ending_polish_brief.md',
    ):
        path = memory_dir / name
        if path.exists():
            path.unlink()


def reset_story_memory(project_dir: Path, rewrite_from_chapter: int) -> None:
    story_memory_path = project_dir / 'memory' / 'story_memory.md'
    backup_file(story_memory_path)
    content = f"""# 故事至今
## 当前状态
- 本项目已从第{rewrite_from_chapter}章开始进入终局回修模式。
- 第{rewrite_from_chapter}章及之后的旧正文、旧摘要、旧完结判断一律作废。
- 当前有效前情以保留正文、系列圣经、最近章节摘要为准。

## 回修提醒
- 下一次续写前，应优先依据保留章节重新刷新终局记忆。
- 后续规划不得引用旧终局中的重复宣判、重复尾声、时间倒退或拖尾内容。
"""
    story_memory_path.write_text(content.strip() + '\n', encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(description='Rewind a project to regenerate from a specific chapter.')
    parser.add_argument('--project-dir', required=True)
    parser.add_argument('--rewrite-from-chapter', type=int, required=True)
    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    rewrite_from_chapter = int(args.rewrite_from_chapter)
    if rewrite_from_chapter <= 0:
        raise ValueError('rewrite-from-chapter must be greater than 0')

    state_path = project_dir / 'state.json'
    backup_file(state_path)
    state = load_state(project_dir)

    completed = list(state.get('completed_chapters') or [])
    retained = [item for item in completed if int(item.get('chapter_number', 0) or 0) < rewrite_from_chapter]
    already_rewound = (
        rewrite_from_chapter > 1
        and len(retained) == len(completed)
        and int(state.get('next_chapter_number', 0) or 0) == rewrite_from_chapter
        and int(state.get('generated_chapters', 0) or 0) == len(completed)
    )
    if rewrite_from_chapter > 1 and len(retained) == len(completed) and not already_rewound:
        raise ValueError(f'No completed chapters at or after chapter {rewrite_from_chapter} to rewind.')

    chapters_per_volume = int(state.get('chapters_per_volume', 30) or 30)
    target_volume, _ = chapter_volume_number(rewrite_from_chapter, chapters_per_volume)

    delete_future_content(project_dir, rewrite_from_chapter, chapters_per_volume)
    reset_memory_files(project_dir)
    reset_story_memory(project_dir, rewrite_from_chapter)

    recalculated_completed: list[dict] = []
    generated_chars = 0
    for item in retained:
        chapter_number = int(item.get('chapter_number', 0) or 0)
        draft_file = str(item.get('draft_file', '') or '')
        summary_file = str(item.get('summary_file', '') or '')
        draft_text = read_text(Path(draft_file))
        summary_text = read_text(Path(summary_file)).strip()
        chars = len(draft_text.strip())
        generated_chars += chars
        recalculated_completed.append({
            'chapter_number': chapter_number,
            'volume_number': int(item.get('volume_number', 0) or chapter_volume_number(chapter_number, chapters_per_volume)[0]),
            'chapter_in_volume': int(item.get('chapter_in_volume', 0) or chapter_volume_number(chapter_number, chapters_per_volume)[1]),
            'title': str(item.get('title', '') or '').strip(),
            'draft_file': draft_file,
            'summary_file': summary_file,
            'chars': chars,
            'summary_preview': summary_text or str(item.get('summary_preview', '') or '').strip(),
        })

    retained_count = rebuild_full_novel(project_dir, recalculated_completed)

    state['status'] = 'initialized'
    state['updated_at'] = now_str()
    state['generated_chars'] = generated_chars
    state['generated_chapters'] = len(recalculated_completed)
    state['next_chapter_number'] = rewrite_from_chapter
    state['current_volume'] = target_volume
    state['pending_chapters'] = []
    state['completed_chapters'] = recalculated_completed
    state['manuscript_last_appended_chapter'] = retained_count
    state['last_memory_refresh_chapter'] = 0
    state['last_opening_promise_refresh_chapter'] = int(state.get('last_opening_promise_refresh_chapter', 0) or 0)
    state['last_ending_guidance_refresh_chapter'] = 0
    state['last_ending_quality_guidance_refresh_chapter'] = 0
    state['rewrite_required_from_chapter'] = rewrite_from_chapter
    state['last_error'] = ''
    state['current_stage'] = ''
    state['stage_started_at'] = ''
    state['last_stage_heartbeat_at'] = ''
    state['completion_check'] = {}
    state['completion_check_history'] = []
    state['ending_quality_check'] = {}
    state['ending_quality_history'] = []
    state['ending_polish_cycles'] = 0
    state['ending_polish_last_rewrite_from_chapter'] = rewrite_from_chapter

    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')

    payload = {
        'project_dir': str(project_dir),
        'rewrite_from_chapter': rewrite_from_chapter,
        'retained_chapters': len(recalculated_completed),
        'retained_chars': generated_chars,
        'next_chapter_number': rewrite_from_chapter,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
