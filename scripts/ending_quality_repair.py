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

from auto_novel import AutoNovelRunner

MAX_POLISH_STOP_MARKER = '终局质量自动回修已达到上限'
MAX_POLISH_EXIT_CODE = 2


def build_runner_args(project_dir: Path, args: argparse.Namespace) -> SimpleNamespace:
    state_path = project_dir / 'state.json'
    if not state_path.exists():
        raise FileNotFoundError(f'Project state not found: {state_path}')

    state = json.loads(state_path.read_text(encoding='utf-8'))
    brief_file = state.get('brief_file') or str(project_dir / 'brief.md')
    main_model = state.get('main_model', 'gpt/gpt-5.4')
    sub_model = state.get('sub_model', main_model)
    critic_model = args.critic_model or main_model
    ending_polish_model = args.ending_polish_model or critic_model

    return SimpleNamespace(
        project_dir=str(project_dir),
        brief_file=str(brief_file),
        brief_text='',
        main_model=main_model,
        sub_model=sub_model,
        completion_mode=state.get('completion_mode', 'min_chars_and_story_end'),
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
        critic_model=critic_model,
        critic_every_chapters=1,
        critic_reasoning_effort=args.critic_reasoning_effort,
        critic_max_passes=args.critic_max_passes,
        ending_polish_model=ending_polish_model,
        ending_polish_reasoning_effort=args.ending_polish_reasoning_effort,
        ending_polish_max_cycles=args.max_cycles,
        max_thread_num=1,
        max_retries=args.max_retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
        max_chapters=0,
        title_only_story=bool(getattr(args, 'title_only_story', False)),
        live_stream=args.live_stream,
        evaluate_completion_only=False,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Review and, if needed, rewrite a completed novel ending for higher ending quality.')
    parser.add_argument('--project-dir', action='append', required=True, help='Project directory; may be specified multiple times.')
    parser.add_argument('--critic-model', default='')
    parser.add_argument('--critic-reasoning-effort', default='high')
    parser.add_argument('--critic-max-passes', type=int, default=3)
    parser.add_argument('--ending-polish-model', default='')
    parser.add_argument('--ending-polish-reasoning-effort', default='high')
    parser.add_argument('--max-cycles', type=int, default=2, help='Maximum automatic ending polish cycles.')
    parser.add_argument('--max-retries', type=int, default=0)
    parser.add_argument('--retry-backoff-seconds', type=int, default=15)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--live-stream', action='store_true')
    parser.add_argument('--title-only-story', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    overall_exit_code = 0
    for raw_project in args.project_dir:
        project_dir = Path(raw_project).resolve()
        runner = AutoNovelRunner(build_runner_args(project_dir, args))
        try:
            runner.evaluate_completion_status(force=True)
            runner.ensure_opening_promise(force=True)
            runner.refresh_ending_guidance(force=True)
            runner.refresh_ending_quality_guidance(force=True)
            initial_report = runner.evaluate_ending_quality(force=True)

            if not args.check_only and initial_report.get('needs_polish'):
                reason_lines = [str(item).strip() for item in (initial_report.get('rewrite_goals') or []) if str(item).strip()]
                if initial_report.get('final_image_target'):
                    reason_lines.append(f'最后一屏目标：{initial_report.get("final_image_target")}')
                runner.rewind_from_chapter(
                    int(initial_report.get('rewrite_from_chapter', 0) or 0),
                    reason_lines=reason_lines,
                    polish_brief=str(initial_report.get('polish_brief_markdown', '') or '').strip(),
                )
                try:
                    runner.run()
                except RuntimeError as exc:
                    message = str(exc).strip()
                    if MAX_POLISH_STOP_MARKER not in message:
                        raise

                    runner.state['status'] = 'paused'
                    runner.state['last_error'] = message
                    runner._save_state()
                    runner.log(f'[ending_quality] {message}')

                    final_report = dict(runner.state.get('ending_quality_check') or {})
                    payload = {
                        'project_dir': str(project_dir),
                        'report_path': str(runner.ending_quality_review_path),
                        'quality_pass': bool(final_report.get('quality_pass')),
                        'needs_polish': bool(final_report.get('needs_polish', True)),
                        'quality_score': int(final_report.get('quality_score', 0) or 0),
                        'resonance_score': int(final_report.get('resonance_score', 0) or 0),
                        'thought_provoking_score': int(final_report.get('thought_provoking_score', 0) or 0),
                        'rewrite_from_chapter': int(final_report.get('rewrite_from_chapter', 0) or 0),
                        'stopped_reason': 'max_polish_cycles_reached',
                        'message': message,
                    }
                    print(json.dumps(payload, ensure_ascii=False))
                    overall_exit_code = max(overall_exit_code, MAX_POLISH_EXIT_CODE)
                    continue

            final_report = runner.evaluate_ending_quality(force=True)
            payload = {
                'project_dir': str(project_dir),
                'report_path': str(runner.ending_quality_review_path),
                'quality_pass': bool(final_report.get('quality_pass')),
                'needs_polish': bool(final_report.get('needs_polish')),
                'quality_score': int(final_report.get('quality_score', 0) or 0),
                'resonance_score': int(final_report.get('resonance_score', 0) or 0),
                'thought_provoking_score': int(final_report.get('thought_provoking_score', 0) or 0),
                'rewrite_from_chapter': int(final_report.get('rewrite_from_chapter', 0) or 0),
            }
            print(json.dumps(payload, ensure_ascii=False))
        finally:
            runner.close()
    return overall_exit_code


if __name__ == '__main__':
    raise SystemExit(main())
