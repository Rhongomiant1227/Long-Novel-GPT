from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from auto_novel import (
    MAX_CHAPTER_TITLE_LENGTH,
    ensure_unique_chapter_title,
    normalize_chapter_draft_text,
    normalize_heading_compare_text,
    normalize_outline_text,
    parse_chapter_heading_line,
    read_text,
    safe_title,
    write_text,
)
from split_full_novel import normalize_heading_line


def first_non_empty_line(text: str) -> str:
    for line in (text or '').splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ''


def optional_path(value: object) -> Path | None:
    raw = str(value or '').strip()
    if not raw or raw == '.':
        return None
    return Path(raw)


def resolve_outline_path(item: dict, draft_path: Path | None) -> Path | None:
    outline_path = optional_path(item.get('outline_file', ''))
    if outline_path and outline_path.exists():
        return outline_path
    if draft_path:
        inferred = draft_path.with_name('outline.md')
        if inferred.exists():
            return inferred
    return outline_path


def preferred_heading_title(chapter_number: int, text: str) -> str:
    heading_titles: list[str] = []
    for line in (text or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parsed_number, parsed_title = parse_chapter_heading_line(stripped)
        if parsed_number == chapter_number and parsed_title:
            heading_titles.append(parsed_title)
            if len(heading_titles) >= 3:
                break
            continue
        if heading_titles:
            break

    if not heading_titles:
        return ''

    preferred = heading_titles[0]
    for candidate in heading_titles[1:]:
        normalized_preferred = normalize_heading_compare_text(preferred)
        normalized_candidate = normalize_heading_compare_text(candidate)
        if not normalized_candidate or len(candidate) > MAX_CHAPTER_TITLE_LENGTH:
            continue
        if normalized_candidate.startswith(normalized_preferred) and len(candidate) > len(preferred):
            preferred = candidate
    return preferred


def normalized_chapter_title(chapter_number: int, text: str, existing_titles: list[str] | None = None) -> str:
    preferred_title = preferred_heading_title(chapter_number, text) or safe_title(text)
    return ensure_unique_chapter_title(
        chapter_number,
        preferred_title,
        existing_titles or [],
        text,
    )


def iter_chapter_records(state: dict) -> list[tuple[str, dict]]:
    records: list[tuple[str, dict]] = []
    for key in ('pending_chapters', 'completed_chapters'):
        for item in state.get(key, []) or []:
            if isinstance(item, dict):
                records.append((key, item))
    records.sort(key=lambda pair: int(pair[1].get('chapter_number', 0) or 0))
    return records


def validate_record_title(project_dir: Path, item: dict, seen_titles: set[str]) -> list[str]:
    errors: list[str] = []
    chapter_number = int(item.get('chapter_number', 0) or 0)
    title = str(item.get('title', '') or '').strip()
    normalized = title
    if chapter_number <= 0:
        return errors
    if not title:
        errors.append(f'第{chapter_number}章标题为空')
    if len(title) > MAX_CHAPTER_TITLE_LENGTH:
        errors.append(f'第{chapter_number}章标题超长:{len(title)}/{MAX_CHAPTER_TITLE_LENGTH}:{title}')
    if normalized in seen_titles:
        errors.append(f'第{chapter_number}章标题重复:{title}')
    if normalized:
        seen_titles.add(normalized)

    draft_path = optional_path(item.get('draft_file', ''))
    if draft_path and draft_path.exists():
        first_line = first_non_empty_line(read_text(draft_path))
        parsed_number, parsed_title = parse_chapter_heading_line(first_line)
        if parsed_number != chapter_number or parsed_title != title:
            errors.append(
                f'第{chapter_number}章正文首行不匹配:{project_dir.name}:{draft_path}'
            )
    return errors


def is_title_only_story_project(state: dict) -> bool:
    completed = state.get('completed_chapters', []) or []
    pending = state.get('pending_chapters', []) or []
    if len(completed) + len(pending) != 1:
        return False

    sample = None
    for item in completed + pending:
        draft_path = optional_path(item.get('draft_file', ''))
        outline_path = optional_path(item.get('outline_file', ''))
        if draft_path and draft_path.exists():
            sample = first_non_empty_line(read_text(draft_path))
            break
        if outline_path and outline_path.exists():
            sample = first_non_empty_line(read_text(outline_path))
            break

    if not sample:
        return False
    parsed_number, _ = parse_chapter_heading_line(sample)
    return parsed_number is None


def repair_project(project_dir: Path) -> dict[str, object]:
    state_path = project_dir / 'state.json'
    if not state_path.exists():
        return repair_chapter_txt_only_project(project_dir)

    state = json.loads(read_text(state_path, '{}') or '{}')
    if is_title_only_story_project(state):
        return {'project': project_dir.name, 'status': 'skipped', 'reason': 'title_only_story'}

    changed_files = 0
    changed_titles = 0
    repaired_format_only = 0
    existing_titles: list[str] = []

    for key, item in iter_chapter_records(state):
        chapter_number = int(item.get('chapter_number', 0) or 0)
        if chapter_number <= 0:
            continue

        draft_path = optional_path(item.get('draft_file', ''))
        outline_path = resolve_outline_path(item, draft_path)

        outline_text = ''
        if outline_path and outline_path.exists():
            original_outline = read_text(outline_path)
            outline_text = normalize_outline_text(chapter_number, original_outline)
            if outline_text != original_outline:
                write_text(outline_path, outline_text)
                changed_files += 1

        title_source = '\n\n'.join(
            part
            for part in (
                str(item.get('title', '') or '').strip(),
                outline_text.strip(),
            )
            if part
        )
        original_draft = ''
        if draft_path and draft_path.exists():
            original_draft = read_text(draft_path)
            if original_draft.strip():
                title_source = '\n\n'.join(part for part in (original_draft, title_source) if part)

        title = normalized_chapter_title(chapter_number, title_source, existing_titles)
        existing_titles.append(title)

        if outline_path and outline_path.exists():
            original_outline = read_text(outline_path)
            normalized_outline = normalize_outline_text(chapter_number, original_outline)
            final_outline = normalize_outline_text(chapter_number, normalized_outline)
            final_outline = final_outline.replace(
                first_non_empty_line(final_outline),
                f'第{chapter_number}章 {title}',
                1,
            )
            if final_outline != original_outline:
                write_text(outline_path, final_outline)
                changed_files += 1

        if draft_path and draft_path.exists() and original_draft.strip():
            draft_text = normalize_chapter_draft_text(chapter_number, title, original_draft)
            if draft_text != original_draft:
                write_text(draft_path, draft_text)
                changed_files += 1
            elif str(item.get('title', '') or '').strip() == title:
                repaired_format_only += 1

        if str(item.get('title', '') or '').strip() != title:
            item['title'] = title
            changed_titles += 1

    if not (changed_files or changed_titles):
        seen: set[str] = set()
        errors: list[str] = []
        for _, item in iter_chapter_records(state):
            errors.extend(validate_record_title(project_dir, item, seen))
        return {
            'project': project_dir.name,
            'status': 'unchanged' if not errors else 'invalid',
            'changed_files': 0,
            'changed_titles': 0,
            'format_checked': repaired_format_only,
            'completed_chapters': len(state.get('completed_chapters', []) or []),
            'errors': errors[:50],
        }

    manuscript_dir = project_dir / 'manuscript'
    chapters_dir = manuscript_dir / 'chapters_txt'
    manifest_path = chapters_dir / 'chapters_manifest.json'
    full_novel_path = manuscript_dir / 'full_novel.txt'
    manuscript_dir.mkdir(parents=True, exist_ok=True)
    chapters_dir.mkdir(parents=True, exist_ok=True)

    for path in chapters_dir.glob('ch_*.txt'):
        path.unlink()

    chunks: list[str] = []
    manifest: list[dict[str, object]] = []
    completed = sorted(
        state.get('completed_chapters', []) or [],
        key=lambda item: int(item.get('chapter_number', 0) or 0),
    )
    for item in completed:
        chapter_number = int(item.get('chapter_number', 0) or 0)
        draft_path = optional_path(item.get('draft_file', ''))
        if chapter_number <= 0 or not draft_path or not draft_path.exists():
            continue
        draft_text = read_text(draft_path).strip()
        if not draft_text:
            continue

        write_text(chapters_dir / f'ch_{chapter_number:04d}.txt', draft_text.rstrip() + '\n')
        heading_line = first_non_empty_line(draft_text)
        manifest.append(
            {
                'source_number': chapter_number,
                'output_number': chapter_number,
                'heading_line': normalize_heading_line(heading_line, new_number=chapter_number),
            }
        )
        chunks.append(draft_text)

    if manifest:
        write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    elif manifest_path.exists():
        manifest_path.unlink()

    if chunks:
        write_text(full_novel_path, '\n\n'.join(chunks).rstrip() + '\n')
    elif full_novel_path.exists():
        full_novel_path.unlink()

    state['manuscript_last_appended_chapter'] = len(state.get('completed_chapters', []) or [])
    state['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + '\n')

    seen: set[str] = set()
    errors: list[str] = []
    for _, item in iter_chapter_records(state):
        errors.extend(validate_record_title(project_dir, item, seen))

    return {
        'project': project_dir.name,
        'status': 'updated' if not errors else 'invalid',
        'changed_files': changed_files,
        'changed_titles': changed_titles,
        'format_checked': repaired_format_only,
        'completed_chapters': len(completed),
        'errors': errors[:50],
    }


def repair_chapter_txt_only_project(project_dir: Path) -> dict[str, object]:
    manuscript_dir = project_dir / 'manuscript'
    chapters_dir = manuscript_dir / 'chapters_txt'
    full_novel_path = manuscript_dir / 'full_novel.txt'
    manifest_path = chapters_dir / 'chapters_manifest.json'
    if not chapters_dir.exists():
        return {'project': project_dir.name, 'status': 'skipped', 'reason': 'missing_state'}

    changed_files = 0
    changed_titles = 0
    existing_titles: list[str] = []
    chunks: list[str] = []
    manifest: list[dict[str, object]] = []

    chapter_paths = sorted(chapters_dir.glob('ch_*.txt'))
    for path in chapter_paths:
        match = path.stem.lower().removeprefix('ch_')
        if not match.isdigit():
            continue
        chapter_number = int(match)
        original_text = read_text(path)
        if not original_text.strip():
            continue
        title = normalized_chapter_title(chapter_number, original_text, existing_titles)
        existing_titles.append(title)

        first_line = first_non_empty_line(original_text)
        _, current_title = parse_chapter_heading_line(first_line)
        if current_title != title:
            changed_titles += 1

        normalized_text = normalize_chapter_draft_text(chapter_number, title, original_text)
        if normalized_text != original_text.strip():
            write_text(path, normalized_text.rstrip() + '\n')
            changed_files += 1
        chunks.append(normalized_text.rstrip())
        manifest.append(
            {
                'source_number': chapter_number,
                'output_number': chapter_number,
                'heading_line': normalize_heading_line(first_non_empty_line(normalized_text), new_number=chapter_number),
            }
        )

    if chunks:
        write_text(full_novel_path, '\n\n'.join(chunks).rstrip() + '\n')
        write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')

    errors: list[str] = []
    seen_titles: set[str] = set()
    for path in chapter_paths:
        match = path.stem.lower().removeprefix('ch_')
        if not match.isdigit():
            continue
        chapter_number = int(match)
        first_line = first_non_empty_line(read_text(path))
        parsed_number, title = parse_chapter_heading_line(first_line)
        if parsed_number != chapter_number:
            errors.append(f'第{chapter_number}章正文首行章节号不匹配:{path}')
        if not title:
            errors.append(f'第{chapter_number}章标题为空')
        if len(title) > MAX_CHAPTER_TITLE_LENGTH:
            errors.append(f'第{chapter_number}章标题超长:{len(title)}/{MAX_CHAPTER_TITLE_LENGTH}:{title}')
        if title in seen_titles:
            errors.append(f'第{chapter_number}章标题重复:{title}')
        if title:
            seen_titles.add(title)

    return {
        'project': project_dir.name,
        'status': 'updated' if (changed_files or changed_titles) and not errors else ('unchanged' if not errors else 'invalid'),
        'changed_files': changed_files,
        'changed_titles': changed_titles,
        'completed_chapters': len(chunks),
        'errors': errors[:50],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Repair overlong chapter titles in active projects.')
    parser.add_argument(
        '--project-dir',
        action='append',
        default=[],
        help='Specific project directory to repair. Can be passed multiple times.',
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.project_dir:
        project_dirs = [Path(path).resolve() for path in args.project_dir]
    else:
        project_dirs = sorted((Path('auto_projects')).glob('*'))

    results = []
    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        results.append(repair_project(project_dir))

    payload = json.dumps(results, ensure_ascii=False, indent=2)
    print(payload)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
