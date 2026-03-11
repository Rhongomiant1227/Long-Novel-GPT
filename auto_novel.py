from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Callable, Iterable

from backend.backend_utils import get_model_config_from_provider_model
from core.draft_writer import DraftWriter
from core.outline_writer import OutlineWriter
from core.plot_writer import PlotWriter
from core.writer_utils import KeyPointMsg
from llm_api import ModelConfig, stream_chat
from llm_api.chat_messages import ChatMessages


DEFAULT_SYSTEM_PROMPT = "你是资深中文网文总编、商业策划和长篇连载统筹。"


def now_str() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_text(path: Path, default: str = '') -> str:
    if not path.exists():
        return default
    return path.read_text(encoding='utf-8')


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding='utf-8')


def truncate_text(text: str, limit: int) -> str:
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head - 10
    return text[:head] + '\n...(中略)...\n' + text[-max(tail, 0):]


def tail_text(text: str, limit: int) -> str:
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return '...(前略)...\n' + text[-limit:]


def clone_model_config(model_config: ModelConfig, **overrides) -> ModelConfig:
    data = dict(model_config)
    for key, value in overrides.items():
        if value in (None, ''):
            continue
        data[key] = value
    return ModelConfig(**data)


def cap_model_output_tokens(model_config: ModelConfig, cap: int) -> dict:
    current = int(model_config.get('max_output_tokens', model_config.get('max_tokens', cap)))
    target = min(current, cap)
    return {
        'max_output_tokens': target,
        'max_tokens': target,
    }


def parse_completion_report(text: str) -> dict:
    report = {
        'is_complete': False,
        'confidence': 0,
        'missing': [],
        'remaining_chapters': 0,
        'remaining_chars': 0,
        'summary': '',
        'next_phase_goal': '',
        'raw_text': (text or '').strip(),
    }

    def extract_line(label: str) -> str:
        match = re.search(rf'^{re.escape(label)}[：:]\s*(.+)$', text or '', re.MULTILINE)
        return match.group(1).strip() if match else ''

    def extract_int(value: str) -> int:
        normalized = (value or '').replace(',', '').replace('，', '')
        match = re.search(r'-?\d+', normalized)
        return int(match.group(0)) if match else 0

    complete_text = extract_line('是否完结')
    report['is_complete'] = complete_text.startswith(('是', '已', '完成'))
    report['confidence'] = max(0, min(100, extract_int(extract_line('置信度'))))
    report['remaining_chapters'] = max(0, extract_int(extract_line('建议还需章节')))
    report['remaining_chars'] = max(0, extract_int(extract_line('建议还需字数')))
    report['summary'] = extract_line('说明')
    report['next_phase_goal'] = extract_line('下一阶段目标')

    missing_match = re.search(
        r'仍缺内容[：:]\s*(.*?)(?:\n(?:建议还需章节|建议还需字数|说明|下一阶段目标)[：:]|\Z)',
        text or '',
        re.DOTALL,
    )
    if missing_match:
        missing = []
        for raw_line in missing_match.group(1).splitlines():
            clean = raw_line.strip().lstrip('-').lstrip('•').strip()
            if clean:
                missing.append(clean)
        report['missing'] = missing

    return report


SERIES_BIBLE_PART_SPECS = [
    (
        '设定总纲',
        '本部分只负责：推荐书名/备选书名、核心卖点、一句话宣传语、世界观、核心规则、力量体系、作品气质。'
        '要求信息密度高，目标 1800~2600 字。',
    ),
    (
        '人物与势力',
        '本部分只负责：主角、主要配角、人物弧光、关系张力、势力版图、关键地点、关键资源、规则禁忌。'
        '要求关系与冲突逻辑清晰，目标 1800~2600 字。',
    ),
    (
        '主线与长篇路线',
        '本部分只负责：主线谜团、阶段性冲突、终局方向、长篇节奏策略、卷级路线图、文风提醒与禁忌清单。'
        '要求适合超长连载推进，目标 1800~2600 字。',
    ),
]


def safe_title(text: str) -> str:
    first_line = ''
    for line in text.splitlines():
        line = line.strip()
        if line:
            first_line = line
            break
    if not first_line:
        return '未命名章节'
    return re.sub(r'\s+', ' ', first_line)


def format_chapter_heading(chapter_number: int, title: str) -> str:
    clean_title = re.sub(r'^\s*第\s*\d+\s*章\s*', '', (title or '').strip())
    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
    if not clean_title:
        clean_title = '未命名章节'
    return f'第{chapter_number}章 {clean_title}'


class AutoNovelRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root = Path(__file__).resolve().parent
        self.project_dir = Path(args.project_dir).resolve()
        self.logs_dir = ensure_dir(self.project_dir / 'logs')
        self.memory_dir = ensure_dir(self.project_dir / 'memory')
        self.volumes_dir = ensure_dir(self.project_dir / 'volumes')
        self.manuscript_dir = ensure_dir(self.project_dir / 'manuscript')
        self.state_path = self.project_dir / 'state.json'
        self.events_path = self.logs_dir / 'events.log'
        self.live_stage_path = self.logs_dir / 'live_stage.json'
        self.brief_path = self.project_dir / 'brief.md'
        self.full_manuscript_path = self.manuscript_dir / 'full_novel.txt'
        self.series_bible_path = self.memory_dir / 'series_bible.md'
        self.series_bible_short_path = self.memory_dir / 'series_bible_short.md'
        self.series_bible_parts_dir = ensure_dir(self.memory_dir / 'series_bible_parts')
        self.story_memory_path = self.memory_dir / 'story_memory.md'
        self.ending_guidance_path = self.memory_dir / 'ending_guidance.md'
        self.completion_report_path = self.memory_dir / 'completion_report.md'
        self.logger = self._build_logger()
        self._stream_cache: dict[str, str] = {}
        self._last_stage_state_save_ts = 0.0
        self.heartbeat_path = self.logs_dir / 'runner_heartbeat.json'
        self._stop_heartbeat = threading.Event()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name='auto_novel_heartbeat', daemon=True)
        self._heartbeat_error_logged = False
        self._last_heartbeat_write_ts = 0.0
        self._last_live_stage_write_ts = 0.0
        self._last_live_stage_payload_key = ''
        self._live_stream_available = True
        self._live_stream_error_logged = False

        ensure_dir(self.project_dir)
        self._prepare_brief()

        base_main = get_model_config_from_provider_model(args.main_model)
        base_sub = get_model_config_from_provider_model(args.sub_model)

        self.writer_model = clone_model_config(
            base_main,
            reasoning_effort=args.writer_reasoning_effort,
        )
        self.sub_model = clone_model_config(
            base_sub,
            reasoning_effort=args.sub_reasoning_effort,
            **cap_model_output_tokens(base_sub, 12_000),
        )
        self.planner_model = clone_model_config(
            base_main,
            reasoning_effort=args.planner_reasoning_effort,
            **cap_model_output_tokens(base_main, 16_000),
        )
        self.summary_model = clone_model_config(
            base_sub,
            reasoning_effort=args.summary_reasoning_effort,
            **cap_model_output_tokens(base_sub, 6_000),
        )

        self.state = self._load_or_init_state()
        self._sync_manuscript()
        self._write_heartbeat(force=True)
        self._write_live_stage_snapshot(
            label=self.state.get('current_stage', ''),
            full_text='',
            force=True,
            finish=not bool(self.state.get('current_stage')),
        )
        self._heartbeat_thread.start()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f'auto_novel_{id(self)}')
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        formatter = logging.Formatter('%(asctime)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        file_handler = logging.FileHandler(self.logs_dir / 'runner.log', encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        return logger

    def log(self, message: str) -> None:
        self.logger.info(message)
        with self.events_path.open('a', encoding='utf-8') as handle:
            handle.write(f'[{now_str()}] {message}\n')

    def _log_event_only(self, message: str) -> None:
        try:
            with self.events_path.open('a', encoding='utf-8') as handle:
                handle.write(f'[{now_str()}] {message}\n')
        except Exception:
            pass

    def _heartbeat_snapshot(self) -> dict:
        return {
            'at': now_str(),
            'pid': os.getpid(),
            'status': self.state.get('status', ''),
            'current_stage': self.state.get('current_stage', ''),
            'stage_started_at': self.state.get('stage_started_at', ''),
            'last_error': self.state.get('last_error', ''),
            'generated_chapters': self.state.get('generated_chapters', 0),
            'generated_chars': self.state.get('generated_chars', 0),
            'next_chapter_number': self.state.get('next_chapter_number', 0),
        }

    def _write_heartbeat(self, force: bool = False) -> None:
        current_time = time.time()
        if not force and current_time - self._last_heartbeat_write_ts < 10:
            return
        try:
            snapshot = self._heartbeat_snapshot()
            tmp_path = self.heartbeat_path.with_suffix('.json.tmp')
            tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp_path.replace(self.heartbeat_path)
            self._last_heartbeat_write_ts = current_time
        except Exception as exc:
            if not self._heartbeat_error_logged:
                self._heartbeat_error_logged = True
                self._log_event_only(f'[heartbeat] 后台心跳写入失败：{exc}')

    def _heartbeat_loop(self) -> None:
        while not self._stop_heartbeat.wait(15):
            if not self.state.get('current_stage'):
                continue
            self._write_heartbeat(force=True)

    def close(self) -> None:
        self._stop_heartbeat.set()
        if self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.0)

    def live_print(self, text: str) -> None:
        if not self.args.live_stream or not self._live_stream_available or not text:
            return
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except (OSError, ValueError, UnicodeEncodeError) as exc:
            self._live_stream_available = False
            if not self._live_stream_error_logged:
                self._live_stream_error_logged = True
                self.log(f'[live_stream] 控制台实时输出已关闭：{exc}')

    def _extract_stage_chapter_number(self, label: str) -> int | None:
        if not label:
            return None
        match = re.search('\u7b2c\\s*(\\d+)\\s*\u7ae0', label)
        if match:
            return int(match.group(1))
        match = re.search(r'ch[_-]?(\d+)', label, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _infer_stage_type(self, label: str) -> str:
        lower_label = (label or '').lower()
        if '正文' in label or 'draft' in lower_label:
            return 'draft'
        if '剧情' in label or 'plot' in lower_label:
            return 'plot'
        if '章节' in label or '大纲' in label or 'outline' in lower_label:
            return 'outline'
        if '设定' in label or '圣经' in label or 'bible' in lower_label:
            return 'series_bible'
        if '记忆' in label or 'memory' in lower_label:
            return 'memory'
        if '摘要' in label or '总结' in label or 'summary' in lower_label:
            return 'summary'
        if '规划' in label or '计划' in label or 'plan' in lower_label:
            return 'plan'
        return 'llm'

    def _write_live_stage_snapshot(
        self,
        label: str,
        full_text: str,
        *,
        reset: bool = False,
        finish: bool = False,
        force: bool = False,
    ) -> None:
        snapshot = {
            'project': self.project_dir.name,
            'label': label,
            'stage_type': self._infer_stage_type(label),
            'chapter_number': self._extract_stage_chapter_number(label),
            'text': full_text or '',
            'text_length': len(full_text or ''),
            'active': bool(label) and not finish,
            'finished': finish,
            'reset': reset,
            'status': self.state.get('status', ''),
            'generated_chapters': self.state.get('generated_chapters', 0),
            'generated_chars': self.state.get('generated_chars', 0),
            'updated_at': now_str(),
        }
        payload_key = json.dumps({k: v for k, v in snapshot.items() if k != 'updated_at'}, ensure_ascii=False, sort_keys=True)
        current_time = time.time()
        if not force:
            if payload_key == self._last_live_stage_payload_key and current_time - self._last_live_stage_write_ts < 0.4:
                return
            if current_time - self._last_live_stage_write_ts < 0.4 and not reset and not finish:
                return
        try:
            tmp_path = self.live_stage_path.with_suffix('.json.tmp')
            tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp_path.replace(self.live_stage_path)
            self._last_live_stage_write_ts = current_time
            self._last_live_stage_payload_key = payload_key
        except Exception as exc:
            self._log_event_only(f'[live_stage] 写入失败：{exc}')

    def _stream_text(self, label: str, full_text: str, reset: bool = False, finish: bool = False) -> None:

        full_text = full_text or ''
        if reset or label not in self._stream_cache:
            self._stream_cache[label] = ''
            self.live_print(f'\n===== [{label}] 实时输出开始 =====\n')

        previous = self._stream_cache.get(label, '')
        if full_text and not full_text.startswith(previous):
            previous = ''
            self.live_print(f'\n[刷新 {label}]\n')

        delta = full_text[len(previous):]
        if delta:
            self.live_print(delta)
            self._stream_cache[label] = full_text

        if reset or delta or finish:
            self._write_live_stage_snapshot(label, self._stream_cache.get(label, full_text), reset=reset, finish=finish)

        if finish and label in self._stream_cache:
            if self._stream_cache[label] and not self._stream_cache[label].endswith('\n'):
                self.live_print('\n')
            self.live_print(f'===== [{label}] 实时输出结束 =====\n')
            self._stream_cache.pop(label, None)

    def _prepare_brief(self) -> None:
        brief_text = ''
        if self.args.brief_file:
            source_path = Path(self.args.brief_file).resolve()
            if source_path.exists():
                brief_text = read_text(source_path)
        if not brief_text and self.args.brief_text:
            brief_text = self.args.brief_text.strip()

        if brief_text and not self.brief_path.exists():
            write_text(self.brief_path, brief_text)
        elif brief_text and self.brief_path.exists() and read_text(self.brief_path).strip() != brief_text.strip():
            backup = self.project_dir / f'brief.backup.{int(time.time())}.md'
            shutil.copyfile(self.brief_path, backup)
            write_text(self.brief_path, brief_text)

        if not self.brief_path.exists():
            raise FileNotFoundError('未提供小说设定，请通过 --brief-file 或 --brief-text 传入。')

    def _load_or_init_state(self) -> dict:
        if self.state_path.exists():
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            state.setdefault('min_target_chars', 0)
            state.setdefault('max_target_chars', 0)
            state.setdefault('completion_mode', 'hard_target')
            state.setdefault('completion_check', {})
            self.log(f'检测到已有项目状态，准备续跑：{self.state_path}')
            return state

        state = {
            'version': 1,
            'status': 'initialized',
            'created_at': now_str(),
            'updated_at': now_str(),
            'brief_file': str(self.brief_path),
            'main_model': self.args.main_model,
            'sub_model': self.args.sub_model,
            'target_chars': self.args.target_chars,
            'min_target_chars': self.args.min_target_chars,
            'max_target_chars': self.args.max_target_chars,
            'completion_mode': self.args.completion_mode,
            'chapter_char_target': self.args.chapter_char_target,
            'chapters_per_volume': self.args.chapters_per_volume,
            'chapters_per_batch': self.args.chapters_per_batch,
            'memory_refresh_interval': self.args.memory_refresh_interval,
            'generated_chars': 0,
            'generated_chapters': 0,
            'next_chapter_number': 1,
            'current_volume': 1,
            'pending_chapters': [],
            'completed_chapters': [],
            'manuscript_last_appended_chapter': 0,
            'last_memory_refresh_chapter': 0,
            'book_title': '',
            'last_error': '',
            'current_stage': '',
            'stage_started_at': '',
            'last_stage_heartbeat_at': '',
            'completion_check': {},
        }
        self._save_state(state)
        return state

    def _save_state(self, state: dict | None = None) -> None:
        if state is not None:
            self.state = state
        self.state['updated_at'] = now_str()
        tmp_path = self.state_path.with_suffix('.json.tmp')
        tmp_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp_path.replace(self.state_path)

    def clear_error(self) -> None:
        if self.state.get('last_error'):
            self.state['last_error'] = ''
            self._save_state()

    def mark_stage(self, label: str, force: bool = False) -> None:
        current_time = time.time()
        current_label = self.state.get('current_stage', '')
        if force or current_label != label:
            self.state['current_stage'] = label
            self.state['stage_started_at'] = now_str()
            self.state['last_stage_heartbeat_at'] = now_str()
            self._save_state()
            self._write_heartbeat(force=True)
            self._write_live_stage_snapshot(label, self._stream_cache.get(label, ''), reset=True, force=True)
            self._last_stage_state_save_ts = current_time
            return

        if current_time - self._last_stage_state_save_ts >= 20:
            self.state['last_stage_heartbeat_at'] = now_str()
            self._save_state()
            self._write_heartbeat(force=True)
            self._last_stage_state_save_ts = current_time

    def clear_stage(self) -> None:
        if self.state.get('current_stage') or self.state.get('stage_started_at') or self.state.get('last_stage_heartbeat_at'):
            previous_label = self.state.get('current_stage', '')
            self.state['current_stage'] = ''
            self.state['stage_started_at'] = ''
            self.state['last_stage_heartbeat_at'] = now_str()
            self._save_state()
            self._write_heartbeat(force=True)
            self._write_live_stage_snapshot(previous_label, self._stream_cache.get(previous_label, ''), finish=True, force=True)
            self._last_stage_state_save_ts = time.time()

    def _sync_manuscript(self) -> None:
        completed = self.state.get('completed_chapters', [])
        appended = self.state.get('manuscript_last_appended_chapter', 0)
        if appended >= len(completed):
            return

        for item in completed[appended:]:
            draft_path = Path(item['draft_file'])
            if not draft_path.exists():
                continue
            text = read_text(draft_path)
            with self.full_manuscript_path.open('a', encoding='utf-8') as handle:
                handle.write(text.rstrip() + '\n\n')
            self.state['manuscript_last_appended_chapter'] += 1
        self._save_state()

    def _volume_dir(self, volume_number: int) -> Path:
        return ensure_dir(self.volumes_dir / f'vol_{volume_number:03d}')

    def _chapter_dir(self, volume_number: int, chapter_number: int) -> Path:
        return ensure_dir(self._volume_dir(volume_number) / 'chapters' / f'ch_{chapter_number:04d}')

    def _chapter_volume_number(self, chapter_number: int) -> tuple[int, int]:
        volume = (chapter_number - 1) // self.args.chapters_per_volume + 1
        chapter_in_volume = (chapter_number - 1) % self.args.chapters_per_volume + 1
        return volume, chapter_in_volume

    def _planning_target_chars(self) -> int:
        current_chars = int(self.state.get('generated_chars', 0) or 0)
        if self._completion_mode() == 'hard_target':
            return max(current_chars, int(self.args.target_chars or 0))

        min_chars = self._effective_min_target_chars()
        completion_check = self.state.get('completion_check') or {}
        remaining_chars = int(completion_check.get('remaining_chars', 0) or 0)
        if remaining_chars > 0:
            return max(current_chars, current_chars + remaining_chars)
        return max(current_chars, min_chars, current_chars + self.args.chapter_char_target)

    def _remaining_target_chapters(self) -> int:
        current_chars = int(self.state.get('generated_chars', 0) or 0)
        if self._completion_mode() == 'hard_target':
            return max(1, math.ceil((self.args.target_chars - current_chars) / self.args.chapter_char_target))

        min_chars = self._effective_min_target_chars()
        if current_chars < min_chars:
            return max(1, math.ceil((min_chars - current_chars) / self.args.chapter_char_target))

        completion_check = self.state.get('completion_check') or {}
        remaining_chapters = int(completion_check.get('remaining_chapters', 0) or 0)
        return max(1, remaining_chapters or 1)

    def _estimated_total_chapters(self) -> int:
        return max(1, math.ceil(self._planning_target_chars() / self.args.chapter_char_target))

    def _estimated_total_volumes(self) -> int:
        return max(1, math.ceil(self._estimated_total_chapters() / self.args.chapters_per_volume))

    def call_llm(self, label: str, user_prompt: str, model: ModelConfig, system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> str:
        messages = [
            {'role': 'system', 'content': system_prompt.strip()},
            {'role': 'user', 'content': user_prompt.strip()},
        ]

        start_time = time.time()
        last_log_time = start_time
        last_length = 0
        fallback_logged = False
        final_messages: ChatMessages | None = None

        self.mark_stage(label, force=True)
        gen = stream_chat(model, messages)
        while True:
            try:
                current = next(gen)
            except StopIteration as exc:
                final_messages = exc.value
                break

            if isinstance(current, ChatMessages):
                final_messages = current
                response_text = current.response or ''
                if getattr(current, 'stream_fallback', False) and not fallback_logged:
                    reason = getattr(current, 'stream_fallback_reason', '') or 'stream transport error'
                    self.log(f'[{label}] 流式链路异常，已自动降级为非流式补全：{reason}')
                    fallback_logged = True
                if response_text:
                    self._stream_text(label, response_text)
                    self.mark_stage(label)
                if response_text and time.time() - last_log_time >= 8 and len(response_text) != last_length:
                    self.log(f'[{label}] 正在生成，当前约 {len(response_text)} 字')
                    last_log_time = time.time()
                    last_length = len(response_text)

        if not final_messages or not (final_messages.response or '').strip():
            raise RuntimeError(f'{label} 未返回有效内容。')

        response = final_messages.response.strip()
        self._stream_text(label, response, finish=True)
        self.clear_stage()
        self.log(f'[{label}] 完成，用时 {time.time() - start_time:.1f}s，输出 {len(response)} 字，成本 {final_messages.cost_info}')
        return response

    def run_writer(self, label: str, writer, user_prompt: str, pair_span: tuple[int, int] | None = None) -> list[tuple[str, str]]:
        start_time = time.time()
        last_log_time = start_time
        pair_span = pair_span or (0, len(writer.xy_pairs))
        generator = writer.write(user_prompt, pair_span=pair_span)
        current_stream_label = label
        self.mark_stage(label, force=True)

        while True:
            try:
                item = next(generator)
            except StopIteration:
                break

            if isinstance(item, KeyPointMsg):
                name = item.prompt_name or item.title or '阶段'
                status = '完成' if item.is_finished() else '开始'
                phase_label = f'{label}/{name}'
                if item.is_finished():
                    self._stream_text(phase_label, self._stream_cache.get(phase_label, ''), finish=True)
                else:
                    current_stream_label = phase_label
                    self._stream_text(current_stream_label, '', reset=True)
                    self.mark_stage(current_stream_label, force=True)
                self.log(f'[{label}] {name} {status}')
                continue

            if isinstance(item, list):
                merged_parts = []
                for entry in item:
                    if not entry:
                        continue
                    output, _chunk = entry
                    if not output:
                        continue
                    response_msgs = output.get('response_msgs') if isinstance(output, dict) else None
                    text_part = getattr(response_msgs, 'response', '') if response_msgs is not None else ''
                    text_part = text_part or (output.get('text', '') if isinstance(output, dict) else '')
                    if text_part and '正在建立映射关系' not in text_part:
                        merged_parts.append(text_part)
                merged_text = '\n'.join(part for part in merged_parts if part)
                if merged_text.strip():
                    self._stream_text(current_stream_label, merged_text)
                    self.mark_stage(current_stream_label)

            if time.time() - last_log_time >= 8:
                chunk_count = len(item) if isinstance(item, list) else 1
                self.log(f'[{label}] 流式进行中，当前分块数 {chunk_count}')
                last_log_time = time.time()

        self._stream_text(current_stream_label, self._stream_cache.get(current_stream_label, ''), finish=True)
        self.clear_stage()
        total_y = ''.join(pair[1] for pair in writer.xy_pairs)
        self.log(f'[{label}] 完成，用时 {time.time() - start_time:.1f}s，输出 {len(total_y)} 字')
        return list(writer.xy_pairs)

    def with_retry(self, label: str, func: Callable[[], str | list | dict], retries: int | None = None):
        retries = self.args.max_retries if retries is None else retries
        infinite_retry = retries <= 0
        last_error = None
        attempt = 0
        while True:
            attempt += 1
            try:
                if attempt > 1:
                    suffix = '（无限重试模式）' if infinite_retry else ''
                    self.log(f'[{label}] 第 {attempt} 次尝试{suffix}')
                return func()
            except Exception as exc:
                last_error = exc
                self.state['last_error'] = f'{label}: {exc}'
                self._save_state()
                self.log(f'[{label}] 失败：{exc}')
                for line in traceback.format_exc().splitlines():
                    self.log(f'[{label}] traceback | {line}')
                if not infinite_retry and attempt >= retries:
                    raise
                sleep_seconds = self.args.retry_backoff_seconds * attempt
                if infinite_retry:
                    sleep_seconds = min(sleep_seconds, max(self.args.retry_backoff_seconds, 300))
                    self.log(f'[{label}] 无限重试模式，{sleep_seconds}s 后继续重试')
                else:
                    self.log(f'[{label}] {sleep_seconds}s 后重试')
                time.sleep(sleep_seconds)
        raise RuntimeError(str(last_error))

    def _series_bible_part_path(self, part_index: int) -> Path:
        return self.series_bible_parts_dir / f'part_{part_index:02d}.md'

    def _compose_series_bible(self, parts: list[str]) -> str:
        clean_parts = [part.strip() for part in parts if (part or '').strip()]
        if not clean_parts:
            return ''
        if clean_parts[0].startswith('# 系列圣经'):
            return '\n\n'.join(clean_parts)
        return '# 系列圣经\n\n' + '\n\n'.join(clean_parts)

    def _build_series_bible_part_prompt(
        self,
        brief: str,
        part_index: int,
        part_title: str,
        part_focus: str,
        previous_parts: list[str],
    ) -> str:
        previous_context = ''
        if previous_parts:
            snippets = []
            for prev_index, prev_text in enumerate(previous_parts, start=1):
                prev_title = SERIES_BIBLE_PART_SPECS[prev_index - 1][0]
                snippets.append(
                    f'【已完成部分 {prev_index}：{prev_title}】\n'
                    f'{truncate_text(prev_text, 900)}'
                )
            previous_context = '\n\n已完成部分摘要（用于保持命名、设定、人物关系一致，不要整段复写）：\n' + '\n\n'.join(snippets)

        return f"""
你正在为一部长篇中文网络小说制作《系列圣经》的分卷稿，本次只生成其中一个部分，而不是整份总稿。

当前任务：
- 当前部分：第 {part_index} 部分《{part_title}》
- 本部分职责：{part_focus}

硬性目标：
- 目标总字数：{self.args.target_chars} 字
- 必须严格服从用户设定，不得偏题、换题材、换文风
- 风格必须适配中文网文连载：强钩子、强冲突、强追读、强悬念
- 本部分只输出当前职责范围内的内容，不要越界包办其他部分
- 必须与已完成部分的书名、术语、人物称谓、势力称谓保持完全一致
- 请使用 Markdown 小标题，首行固定写：## 第{part_index}部分：{part_title}
- 输出信息密度高，避免空话和重复，长度尽量控制在 1400~2200 字

补充要求：
- 如果当前部分需要引用前面已经定下的设定，可以简短承接，但不要大段重复
- 优先给出真正能支撑 200 万字连载的可执行信息，而不是泛泛而谈
- 如果用户设定里有创新点，务必把创新点落实为明确规则、矛盾和长线钩子
{previous_context}

用户设定如下：
{brief}
"""

    def _generate_series_bible_from_parts(self, brief: str) -> str:
        parts: list[str] = []
        for part_index, (part_title, part_focus) in enumerate(SERIES_BIBLE_PART_SPECS, start=1):
            part_path = self._series_bible_part_path(part_index)
            existing_text = read_text(part_path).strip()
            if existing_text:
                parts.append(existing_text)
                continue

            prompt = self._build_series_bible_part_prompt(
                brief=brief,
                part_index=part_index,
                part_title=part_title,
                part_focus=part_focus,
                previous_parts=parts,
            )
            label = f'系列圣经·第{part_index}部分'
            part_text = self.with_retry(
                label,
                lambda prompt=prompt, label=label: self.call_llm(label, prompt, self.planner_model),
            ).strip()
            self.clear_error()
            write_text(part_path, part_text)
            parts.append(part_text)

        bible_text = self._compose_series_bible(parts)
        write_text(self.series_bible_path, bible_text)
        return bible_text

    def ensure_series_bible(self) -> None:
        if self.series_bible_path.exists() and self.series_bible_short_path.exists():
            return

        if self.series_bible_path.exists():
            bible_text = read_text(self.series_bible_path).strip()
        else:
            brief = read_text(self.brief_path)
            bible_text = self._generate_series_bible_from_parts(brief)

        short_prompt = f"""
请把下面这份《系列圣经》压缩成供后续写作调用的短版记忆，不超过 1200 字。
要求保留：核心卖点、主角定位、关键配角作用、世界规则、主线谜团、关系张力、文风提醒。

原文如下：
{bible_text}
"""
        short_text = self.with_retry(
            '圣经短版',
            lambda: self.call_llm('圣经短版', short_prompt, self.summary_model),
        )
        self.clear_error()
        write_text(self.series_bible_short_path, short_text)

        title_match = re.search(r'推荐书名[：:]\s*(.+)', bible_text)
        if title_match:
            self.state['book_title'] = title_match.group(1).strip()
            self._save_state()

    def ensure_volume_plan(self, volume_number: int) -> Path:
        volume_dir = self._volume_dir(volume_number)
        plan_path = volume_dir / 'plan.md'
        if plan_path.exists():
            return plan_path

        chapter_start = (volume_number - 1) * self.args.chapters_per_volume + 1
        chapter_end = chapter_start + self.args.chapters_per_volume - 1
        series_bible_short = truncate_text(read_text(self.series_bible_short_path), 1200)
        story_memory = truncate_text(read_text(self.story_memory_path), 1200)
        recent = truncate_text(self.recent_chapter_summaries(limit=4), 800)
        ending_guidance = self._ending_guidance_text(limit=1000)

        prompt = f"""
请规划第 {volume_number} 卷的卷级路线图。

卷信息：
- 预计章节范围：第 {chapter_start} 章 ~ 第 {chapter_end} 章
- 预计卷数总量：约 {self._estimated_total_volumes()} 卷
- 目标：既要推进主线，也要形成这一卷自己的高潮与卷末大钩子
- 输出尽量控制在 2000~3000 字，聚焦最关键的剧情推进与钩子

必须坚持：
- 严格服从当前作品设定与题材
- 保持中文长篇网文连载节奏
- 兼顾主线推进、人物关系变化、阶段性爽点与悬念
- 本卷必须有完整起承转合与卷末大钩子
- 不要脱离既有设定另起炉灶

【系列圣经短版】
{series_bible_short}

【前情压缩记忆】
{story_memory or '（暂无）'}

【最近章节摘要】
{recent or '（暂无）'}

{ending_guidance}

输出格式：
# 第{volume_number}卷 卷名
## 本卷定位
## 卷目标
## 核心冲突
## 三幕推进
## 关键转折点
## 卷末钩子
## 本卷人物关系推进
## 本卷必须回收/埋下的伏笔
"""
        plan_text = self.with_retry(
            f'第{volume_number}卷规划',
            lambda: self.call_llm(f'第{volume_number}卷规划', prompt, self.planner_model),
        )
        self.clear_error()
        write_text(plan_path, plan_text)
        return plan_path

    def recent_chapter_summaries(self, limit: int = 3) -> str:
        items = self.state.get('completed_chapters', [])[-limit:]
        blocks = []
        for item in items:
            summary = read_text(Path(item['summary_file']))
            if not summary.strip():
                continue
            blocks.append(f"第{item['chapter_number']}章：{summary.strip()}")
        return '\n'.join(blocks)

    def _completion_mode(self) -> str:
        return str(getattr(self.args, 'completion_mode', 'hard_target') or 'hard_target').strip().lower()

    def _effective_min_target_chars(self) -> int:
        min_target = int(getattr(self.args, 'min_target_chars', 0) or 0)
        if min_target > 0:
            return min_target
        target_chars = int(getattr(self.args, 'target_chars', 0) or 0)
        return max(0, target_chars)

    def _effective_max_target_chars(self) -> int:
        max_target = int(getattr(self.args, 'max_target_chars', 0) or 0)
        return max(0, max_target)

    def _in_finale_mode(self) -> bool:
        if self._completion_mode() != 'min_chars_and_story_end':
            return False
        min_target = self._effective_min_target_chars()
        if min_target <= 0:
            return self.state.get('generated_chapters', 0) > 0
        return self.state.get('generated_chars', 0) >= min_target

    def _ending_guidance_text(self, limit: int = 1200) -> str:
        blocks = []

        manual_guidance = read_text(self.ending_guidance_path).strip()
        if manual_guidance:
            blocks.append(
                '【完结要求：高优先级，若与旧卷计划冲突，以此为准】\n'
                + truncate_text(manual_guidance, limit)
            )

        if self._in_finale_mode():
            completion_check = self.state.get('completion_check') or {}
            auto_lines = [
                '【自动收束要求】',
                '- 当前已进入全书终局收束阶段，优先解决主线、终局、尾声、后日谈。',
                '- 不要再新增需要多卷回收的新大坑、新地图、新主反派。',
                '- 允许新信息，但只能服务当前终局回收与情绪收束。',
            ]
            missing = completion_check.get('missing') or []
            if missing:
                auto_lines.append('- 当前仍缺内容：' + '；'.join(str(item) for item in missing[:6]))
            remaining_chapters = int(completion_check.get('remaining_chapters', 0) or 0)
            remaining_chars = int(completion_check.get('remaining_chars', 0) or 0)
            if remaining_chapters > 0:
                auto_lines.append(f'- 当前估计还需约 {remaining_chapters} 章完成自然收尾。')
            if remaining_chars > 0:
                auto_lines.append(f'- 当前估计还需约 {remaining_chars} 字完成自然收尾。')
            next_phase_goal = str(completion_check.get('next_phase_goal', '') or '').strip()
            if next_phase_goal:
                auto_lines.append(f'- 下一阶段目标：{next_phase_goal}')
            blocks.append('\n'.join(auto_lines))

        return '\n\n'.join(blocks).strip()

    def _recent_pending_outlines(self, limit: int = 3) -> str:
        pending = self.state.get('pending_chapters', [])[:limit]
        blocks = []
        for item in pending:
            outline = read_text(Path(item['outline_file']))
            if not outline.strip():
                continue
            blocks.append(
                f"第{item['chapter_number']}章待写大纲：\n{truncate_text(outline, 700)}"
            )
        return '\n\n'.join(blocks)

    def _build_completion_estimate_prompt(self) -> str:
        completed = self.state.get('completed_chapters', [])
        last_draft_tail = ''
        if completed:
            last_draft_tail = tail_text(read_text(Path(completed[-1]['draft_file'])), 1500)

        sections = [
            '请站在中文长篇网文总编的角度，判断这部小说现在是否已经具备“自然完结”的条件。',
            '你的判断标准必须严格，不要为了省字数、省成本或凑整数而硬停。',
            '',
            '完结必须同时满足：',
            '1. 核心主线和最大谜团已经真正落判。',
            '2. 主角的终局胜负、规则级收束或命运落点已经完成。',
            '3. 主要关系线已经给出明确落点。',
            '4. 已经有尾声。',
            '5. 已经有后日谈。',
            '',
            '如果还不够完结，请估算：为了写出“终局 + 尾声 + 后日谈”的完整版本，合理还需要多少章节、多少字。',
            '不要建议继续扩成长篇新阶段，只能给“把这本书完整收住”所需的剩余量。',
            '',
            '输出格式必须严格如下，不要加别的标题：',
            '是否完结：是/否',
            '置信度：0-100',
            '仍缺内容：',
            '- 缺失项1',
            '- 缺失项2',
            '建议还需章节：N',
            '建议还需字数：N',
            '说明：一句到三句，明确为何还不能完结或为何已经完结',
            '下一阶段目标：如果未完结，请给一个最合理的后续收束方向；如果已完结，写“无”',
            '',
            '【系列圣经短版】',
            truncate_text(read_text(self.series_bible_short_path), 1200) or '（暂无）',
            '',
            '【故事记忆】',
            truncate_text(read_text(self.story_memory_path), 1600) or '（暂无）',
            '',
            '【最近章节摘要】',
            truncate_text(self.recent_chapter_summaries(limit=8), 1800) or '（暂无）',
        ]

        pending_outlines = self._recent_pending_outlines(limit=3)
        if pending_outlines:
            sections.extend([
                '',
                '【已规划但未写出的后续大纲】',
                pending_outlines,
            ])

        if last_draft_tail:
            sections.extend([
                '',
                '【最后一章正文结尾】',
                last_draft_tail,
            ])

        ending_guidance = self._ending_guidance_text(limit=1000)
        if ending_guidance:
            sections.extend([
                '',
                ending_guidance,
            ])

        return '\n'.join(sections).strip()

    def _write_completion_report(self, report: dict) -> None:
        lines = [
            f"# 完结评估（{now_str()}）",
            '',
            f"- 是否完结：{'是' if report.get('is_complete') else '否'}",
            f"- 置信度：{report.get('confidence', 0)}",
            f"- 建议还需章节：{report.get('remaining_chapters', 0)}",
            f"- 建议还需字数：{report.get('remaining_chars', 0)}",
            '',
            '## 仍缺内容',
        ]
        missing = report.get('missing') or []
        if missing:
            lines.extend(f'- {item}' for item in missing)
        else:
            lines.append('- 无')
        lines.extend([
            '',
            '## 说明',
            report.get('summary', '') or '（无）',
            '',
            '## 下一阶段目标',
            report.get('next_phase_goal', '') or '无',
            '',
            '## 原始输出',
            report.get('raw_text', '') or '（无）',
        ])
        write_text(self.completion_report_path, '\n'.join(lines).strip() + '\n')

    def evaluate_completion_status(self, force: bool = False) -> dict:
        checked_at_chapter = self.state.get('completion_check', {}).get('checked_at_chapter', -1)
        current_chapter = int(self.state.get('generated_chapters', 0))
        if not force and checked_at_chapter == current_chapter:
            existing = self.state.get('completion_check') or {}
            if existing:
                return existing

        prompt = self._build_completion_estimate_prompt()
        report_text = self.with_retry(
            '完结评估',
            lambda: self.call_llm('完结评估', prompt, self.planner_model),
        )
        self.clear_error()
        report = parse_completion_report(report_text)
        report['checked_at_chapter'] = current_chapter
        self.state['completion_check'] = {
            'checked_at_chapter': current_chapter,
            'is_complete': bool(report.get('is_complete')),
            'confidence': int(report.get('confidence', 0) or 0),
            'missing': list(report.get('missing') or []),
            'remaining_chapters': int(report.get('remaining_chapters', 0) or 0),
            'remaining_chars': int(report.get('remaining_chars', 0) or 0),
            'summary': str(report.get('summary', '') or '').strip(),
            'next_phase_goal': str(report.get('next_phase_goal', '') or '').strip(),
        }
        self._save_state()
        self._write_completion_report(report)
        return report

    def refresh_story_memory(self, force: bool = False) -> None:
        completed = self.state.get('completed_chapters', [])
        if not completed:
            return

        interval = self.args.memory_refresh_interval
        if not force and self.state.get('generated_chapters', 0) - self.state.get('last_memory_refresh_chapter', 0) < interval:
            return

        old_memory = truncate_text(read_text(self.story_memory_path), 1200)
        recent = truncate_text(self.recent_chapter_summaries(limit=interval), 1200)
        series_bible = truncate_text(read_text(self.series_bible_short_path), 1000)
        prompt = f"""
请基于【旧记忆】和【新增章节摘要】，更新一份供后续创作调用的压缩记忆。

要求：
- 总长度不超过 1800 字
- 只保留真正影响后续创作的信息
- 重点维护人物状态、关系变化、世界/势力变化、未回收伏笔、当前主线推进位置
- 风格提示要短，但必须明确当前作品的叙事节奏、冲突类型、人物张力与文风约束

【系列圣经短版】
{series_bible}

【旧记忆】
{old_memory or '（暂无）'}

【新增章节摘要】
{recent}

输出格式：
# 故事至今
## 角色状态
## 世界与势力状态
## 未回收伏笔
## 接下来写作提醒
"""
        memory_text = self.with_retry(
            '更新故事记忆',
            lambda: self.call_llm('更新故事记忆', prompt, self.summary_model),
        )
        self.clear_error()
        write_text(self.story_memory_path, memory_text)
        self.state['last_memory_refresh_chapter'] = self.state.get('generated_chapters', 0)
        self._save_state()

    def plan_next_batch(self) -> None:
        if self.state.get('pending_chapters'):
            return

        next_chapter = self.state['next_chapter_number']
        volume_number, chapter_in_volume = self._chapter_volume_number(next_chapter)
        self.state['current_volume'] = volume_number
        plan_path = self.ensure_volume_plan(volume_number)

        remaining_total = self._remaining_target_chapters()
        remaining_in_volume = self.args.chapters_per_volume - chapter_in_volume + 1
        batch_size = min(self.args.chapters_per_batch, remaining_total, remaining_in_volume)
        if self.args.max_chapters:
            allowed_left = self.args.max_chapters - self.state['generated_chapters']
            batch_size = min(batch_size, allowed_left)
        batch_size = max(1, batch_size)

        chapter_end = next_chapter + batch_size - 1
        context = '\n\n'.join(
            item for item in [
                f'【系列圣经短版】\n{truncate_text(read_text(self.series_bible_short_path), 1200)}',
                f'【卷规划】\n{truncate_text(read_text(plan_path), 1000)}',
                f'【故事记忆】\n{truncate_text(read_text(self.story_memory_path), 1200)}',
                f'【最近章节摘要】\n{truncate_text(self.recent_chapter_summaries(limit=4), 800)}',
                self._ending_guidance_text(limit=1000),
            ] if item.strip()
        )

        finale_extra_rules = ''
        if self._in_finale_mode():
            finale_extra_rules = """
- 当前已进入全书最终收束阶段，必须优先完成终局、尾声、后日谈，不要再扩长线
- 新增信息只能服务于当前终局回收，不能再开启需要多卷回收的新主线
- 如果剩余章节已经不多，必须提前把尾声与后日谈纳入规划
"""

        outline_prompt = f"""
请规划接下来这一批章节的大纲：第 {next_chapter} 章 到 第 {chapter_end} 章。

硬性要求：
- 必须严格承接现有前情，不能重置人物状态
- 题材、世界观、力量体系、冲突逻辑必须服从既有设定
- 每章都要有明确目标、阻力、推进、变化与章末钩子
- 兼顾主线推进、副线拉扯、人物关系变化与阶段性爽点
- 单章必须服务长篇连载，不要写成孤立的无关插曲
- 必须是中文番茄风长篇网文节奏
- 每章都要有实质推进与章末卡点
- 单章定位是“章节大纲”，不是正文，不要写成小说正文
{finale_extra_rules}

输出格式必须严格如下，不要写任何额外解释：
第{next_chapter}章 章节标题
本章大纲内容……

第{next_chapter + 1 if batch_size > 1 else next_chapter + 1}章 章节标题
本章大纲内容……

直到第 {chapter_end} 章。
"""

        def _plan():
            writer = OutlineWriter(
                [('', '')],
                {'summary': context},
                model=self.writer_model,
                sub_model=self.sub_model,
                x_chunk_length=2500,
                y_chunk_length=2500,
                max_thread_num=self.args.max_thread_num,
            )
            pairs = self.run_writer(f'规划章节 {next_chapter}-{chapter_end}', writer, outline_prompt, pair_span=(0, len(writer.xy_pairs)))
            outlines = [pair[1].strip() for pair in pairs if pair[1].strip()]
            if len(outlines) < batch_size:
                raise RuntimeError(f'本批只产出 {len(outlines)} 章大纲，少于预期的 {batch_size} 章。')
            return outlines[:batch_size]

        outlines = self.with_retry(f'规划章节 {next_chapter}-{chapter_end}', _plan)
        self.clear_error()
        pending = self.state.get('pending_chapters', [])
        for offset, outline in enumerate(outlines):
            chapter_number = next_chapter + offset
            volume_number, volume_chapter = self._chapter_volume_number(chapter_number)
            chapter_dir = self._chapter_dir(volume_number, chapter_number)
            outline_path = chapter_dir / 'outline.md'
            plot_path = chapter_dir / 'plot.md'
            draft_path = chapter_dir / 'draft.md'
            summary_path = chapter_dir / 'summary.md'
            write_text(outline_path, outline)
            pending.append({
                'chapter_number': chapter_number,
                'volume_number': volume_number,
                'chapter_in_volume': volume_chapter,
                'title': safe_title(outline),
                'outline_file': str(outline_path),
                'plot_file': str(plot_path),
                'draft_file': str(draft_path),
                'summary_file': str(summary_path),
                'status': 'outlined',
            })

        self.state['pending_chapters'] = pending
        self._save_state()

    def generate_plot(self, chapter: dict) -> str:
        outline_text = read_text(Path(chapter['outline_file']))
        plot_path = Path(chapter['plot_file'])
        if plot_path.exists() and read_text(plot_path).strip():
            return read_text(plot_path)

        context_parts = [
            f'【系列圣经短版】\n{truncate_text(read_text(self.series_bible_short_path), 1000)}',
            f'【当前卷规划】\n{truncate_text(read_text(self.ensure_volume_plan(chapter["volume_number"])), 900)}',
            f'【故事记忆】\n{truncate_text(read_text(self.story_memory_path), 1000)}',
            f'【最近章节摘要】\n{truncate_text(self.recent_chapter_summaries(limit=3), 600)}',
            self._ending_guidance_text(limit=900),
        ]
        context = '\n\n'.join(part for part in context_parts if part.strip())
        finale_plot_rules = ''
        if self._in_finale_mode():
            finale_plot_rules = """
- 当前已进入全书最终收束阶段，本章若承担终局、尾声或后日谈功能，应直接推进，不要继续拖大主线
- 不要新增需要多卷回收的新谜团，只能回收当前主线伏笔并推进角色落点
"""
        prompt = f"""
请把这章大纲扩写成详细剧情梗概。

要求：
- 这是第 {chapter['chapter_number']} 章
- 只输出剧情梗概，不要写成正文
- 节奏快、信息密、冲突密、因果清楚
- 充分体现本书既定题材、世界规则、人物关系与主线矛盾
- 把关键冲突、人物决策、信息揭示、因果变化写清楚
- 兼顾爽点、压迫感、情绪张力与阶段性推进
- 章末必须保留强卡点，为下一章续接
- 目标长度：400~900 字
{finale_plot_rules}
"""

        def _plot():
            writer = PlotWriter(
                [(outline_text, '')],
                {'chapter': context},
                model=self.writer_model,
                sub_model=self.sub_model,
                x_chunk_length=800,
                y_chunk_length=1200,
                max_thread_num=self.args.max_thread_num,
            )
            pairs = self.run_writer(f'第{chapter["chapter_number"]}章剧情', writer, prompt, pair_span=(0, len(writer.xy_pairs)))
            text = ''.join(pair[1] for pair in pairs).strip()
            if len(text) < 150:
                raise RuntimeError('剧情梗概过短。')
            return text

        plot_text = self.with_retry(f'第{chapter["chapter_number"]}章剧情', _plot)
        self.clear_error()
        write_text(plot_path, plot_text)
        chapter['status'] = 'plotted'
        self._save_state()
        return plot_text

    def generate_draft(self, chapter: dict, plot_text: str) -> str:
        draft_path = Path(chapter['draft_file'])
        if draft_path.exists() and read_text(draft_path).strip():
            return read_text(draft_path)

        min_chars = max(1400, int(self.args.chapter_char_target * 0.8))
        target_chars = self.args.chapter_char_target
        max_chars = max(target_chars + 600, 2600)
        finale_draft_rules = ''
        if self._in_finale_mode():
            finale_draft_rules = """
- 当前已进入全书最终收束阶段；如果本章承担终局、尾声或后日谈功能，请按其应有气质直接完成
- 不要再扩展需要多卷回收的新大坑、新地图、新主反派
- 允许保留章末钩子，但钩子必须服务于本书剩余收束，而不是开启新长篇
"""

        def _draft(existing_text: str = '') -> str:
            prompt = f"""
请把这段剧情梗概写成可发布的中文长篇网文正文。

要求：
- 这是第 {chapter['chapter_number']} 章，必须保留并优化章节标题感
- 风格：番茄风、强代入、强画面、强对白、强追读
- 题材、世界观、力量体系、人物关系和冲突类型必须严格服从本书设定
- 重点写清人物目标、阻力、选择、代价、反制与变化
- 兼顾情绪张力、场面张力、信息揭露、人物关系拉扯与章末钩子
- 正文不是梗概，不要用“随后、然后、接着”堆流水账
- 开头三段必须抓人
- 结尾必须卡住
- 目标字数：{target_chars} 字左右，至少 {min_chars} 字，建议不超过 {max_chars} 字
{finale_draft_rules}
"""
            writer = DraftWriter(
                [(plot_text, existing_text)],
                {},
                model=self.writer_model,
                sub_model=self.sub_model,
                x_chunk_length=800,
                y_chunk_length=max_chars,
                max_thread_num=self.args.max_thread_num,
            )
            pairs = self.run_writer(f'第{chapter["chapter_number"]}章正文', writer, prompt, pair_span=(0, len(writer.xy_pairs)))
            return ''.join(pair[1] for pair in pairs).strip()

        draft_text = self.with_retry(f'第{chapter["chapter_number"]}章正文', lambda: _draft(''))
        self.clear_error()
        if len(draft_text) < min_chars:
            expand_prompt = f"""
请在不改变这章主线事件与结尾卡点的前提下，对正文进行扩充和润色。

扩充方向：
- 补强心理描写、动作过程、场景细节、对话博弈、氛围压迫感
- 强化人物关系拉扯、关键信息回收与情绪递进
- 保持网文节奏，不要灌水
- 扩写后至少 {min_chars} 字
"""

            def _expand() -> str:
                writer = DraftWriter(
                    [(plot_text, draft_text)],
                    {},
                    model=self.writer_model,
                    sub_model=self.sub_model,
                    x_chunk_length=800,
                    y_chunk_length=max_chars + 600,
                    max_thread_num=self.args.max_thread_num,
                )
                pairs = self.run_writer(f'第{chapter["chapter_number"]}章扩写', writer, expand_prompt, pair_span=(0, len(writer.xy_pairs)))
                text = ''.join(pair[1] for pair in pairs).strip()
                if len(text) < min_chars:
                    raise RuntimeError('扩写后字数仍不足。')
                return text

            draft_text = self.with_retry(f'第{chapter["chapter_number"]}章扩写', _expand)
            self.clear_error()

        heading = format_chapter_heading(chapter['chapter_number'], chapter['title'])
        draft_body = re.sub(r'^\s*第\s*\d+\s*章[^\n]*', '', draft_text, count=1).strip()
        draft_text = f'{heading}\n\n{draft_body}'.strip()
        write_text(draft_path, draft_text)
        chapter['status'] = 'drafted'
        self._save_state()
        return draft_text

    def summarize_chapter(self, chapter: dict, outline_text: str, plot_text: str, draft_text: str) -> str:
        summary_path = Path(chapter['summary_file'])
        if summary_path.exists() and read_text(summary_path).strip():
            return read_text(summary_path)

        prompt = f"""
请用 80~140 字总结这一章已经真正发生了什么。

要求：
- 点出关键事件
- 点出人物状态变化
- 点出新增伏笔或问题
- 用于后续续写，不要夸张修辞，不要空话

【章节标题与大纲】
{truncate_text(outline_text, 500)}

【剧情梗概】
{truncate_text(plot_text, 800)}

【正文】
{truncate_text(draft_text, 1200)}
"""
        summary_text = self.with_retry(
            f'第{chapter["chapter_number"]}章摘要',
            lambda: self.call_llm(f'第{chapter["chapter_number"]}章摘要', prompt, self.summary_model),
        )
        self.clear_error()
        write_text(summary_path, summary_text.strip())
        return summary_text.strip()

    def finalize_chapter(self, chapter: dict, draft_text: str, summary_text: str) -> None:
        self.state['generated_chars'] += len(draft_text)
        self.state['generated_chapters'] += 1
        self.state['next_chapter_number'] = chapter['chapter_number'] + 1
        self.state['pending_chapters'] = self.state['pending_chapters'][1:]
        self.state['completed_chapters'].append({
            'chapter_number': chapter['chapter_number'],
            'volume_number': chapter['volume_number'],
            'chapter_in_volume': chapter['chapter_in_volume'],
            'title': chapter['title'],
            'draft_file': chapter['draft_file'],
            'summary_file': chapter['summary_file'],
            'chars': len(draft_text),
            'summary_preview': summary_text,
        })
        with self.full_manuscript_path.open('a', encoding='utf-8') as handle:
            handle.write(draft_text.rstrip() + '\n\n')
        self.state['manuscript_last_appended_chapter'] += 1
        self._save_state()
        self.clear_stage()
        self.log(
            f'第{chapter["chapter_number"]}章完成，累计 {self.state["generated_chapters"]} 章 / {self.state["generated_chars"]} 字'
        )

    def should_stop(self) -> bool:
        if self.args.max_chapters and self.state['generated_chapters'] >= self.args.max_chapters:
            return True
        if self._completion_mode() == 'hard_target':
            return self.state['generated_chars'] >= self.args.target_chars

        max_target_chars = self._effective_max_target_chars()
        if max_target_chars and self.state['generated_chars'] >= max_target_chars:
            self.log(f'已达到最大安全字数上限：{max_target_chars}')
            return True

        min_target_chars = self._effective_min_target_chars()
        if min_target_chars and self.state['generated_chars'] < min_target_chars:
            return False

        completion_report = self.evaluate_completion_status()
        if completion_report.get('is_complete'):
            self.log(
                f'完结评估判定已可自然收尾结束：置信度 {completion_report.get("confidence", 0)}'
            )
            return True
        return False

    def run(self) -> None:
        self.state['status'] = 'running'
        self.state['target_chars'] = self.args.target_chars
        self.state['min_target_chars'] = self.args.min_target_chars
        self.state['max_target_chars'] = self.args.max_target_chars
        self.state['completion_mode'] = self.args.completion_mode
        self._save_state()
        self.log('自动长篇模式启动')
        self.log(f'项目目录：{self.project_dir}')
        self.log(
            f'停止模式：{self.args.completion_mode}，硬目标：{self.args.target_chars}，'
            f'最少字数：{self.args.min_target_chars}，最大安全字数：{self.args.max_target_chars}，'
            f'单章目标：{self.args.chapter_char_target}'
        )
        self.log(f'主模型：{self.args.main_model}，副模型：{self.args.sub_model}')

        self.ensure_series_bible()

        if not self.story_memory_path.exists():
            write_text(self.story_memory_path, '# 故事至今\n尚未生成正文。')

        while not self.should_stop():
            self.plan_next_batch()
            chapter = self.state['pending_chapters'][0]
            chapter_number = chapter['chapter_number']
            self.log(f'开始处理第{chapter_number}章：{chapter["title"]}')

            outline_text = read_text(Path(chapter['outline_file']))
            plot_text = self.generate_plot(chapter)
            draft_text = self.generate_draft(chapter, plot_text)
            summary_text = self.summarize_chapter(chapter, outline_text, plot_text, draft_text)
            self.finalize_chapter(chapter, draft_text, summary_text)
            self.refresh_story_memory()

        self.refresh_story_memory(force=True)
        self.state['status'] = 'completed'
        self.state['last_error'] = ''
        self._save_state()
        self.log('自动长篇任务完成')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Long-Novel-GPT 全自动长篇编排器')
    parser.add_argument('--project-dir', default=str(Path('auto_projects') / 'default_project'))
    parser.add_argument('--brief-file', default=str(Path('novel_brief.md')))
    parser.add_argument('--brief-text', default='')
    parser.add_argument('--main-model', default='gpt/gpt-5.4')
    parser.add_argument('--sub-model', default='gpt/gpt-5.4')
    parser.add_argument('--completion-mode', choices=['hard_target', 'min_chars_and_story_end'], default='hard_target')
    parser.add_argument('--target-chars', type=int, default=2_000_000)
    parser.add_argument('--min-target-chars', type=int, default=0)
    parser.add_argument('--max-target-chars', type=int, default=0)
    parser.add_argument('--chapter-char-target', type=int, default=2200)
    parser.add_argument('--chapters-per-volume', type=int, default=30)
    parser.add_argument('--chapters-per-batch', type=int, default=5)
    parser.add_argument('--memory-refresh-interval', type=int, default=5)
    parser.add_argument('--planner-reasoning-effort', default='medium')
    parser.add_argument('--writer-reasoning-effort', default='medium')
    parser.add_argument('--sub-reasoning-effort', default='low')
    parser.add_argument('--summary-reasoning-effort', default='low')
    parser.add_argument('--max-thread-num', type=int, default=1)
    parser.add_argument('--max-retries', type=int, default=0)
    parser.add_argument('--retry-backoff-seconds', type=int, default=15)
    parser.add_argument('--max-chapters', type=int, default=0)
    parser.add_argument('--live-stream', action='store_true')
    parser.add_argument('--evaluate-completion-only', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = AutoNovelRunner(args)
    try:
        if args.evaluate_completion_only:
            report = runner.evaluate_completion_status(force=True)
            payload = json.dumps(report, ensure_ascii=False, indent=2)
            try:
                print(payload)
            except UnicodeEncodeError:
                sys.stdout.buffer.write((payload + '\n').encode('utf-8', errors='replace'))
                sys.stdout.flush()
            return 0
        runner.run()
        return 0
    except KeyboardInterrupt:
        runner.state['status'] = 'paused'
        runner._save_state()
        runner.log('收到中断信号，已保存进度，可直接续跑。')
        return 130
    except Exception as exc:
        runner.state['status'] = 'failed'
        runner.state['last_error'] = str(exc)
        runner._save_state()
        runner.log(f'任务失败：{exc}')
        for line in traceback.format_exc().splitlines():
            runner.log(f'traceback | {line}')
        raise
    finally:
        runner.close()


if __name__ == '__main__':
    raise SystemExit(main())
