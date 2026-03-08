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


def clone_model_config(model_config: ModelConfig, **overrides) -> ModelConfig:
    data = dict(model_config)
    for key, value in overrides.items():
        if value in (None, ''):
            continue
        data[key] = value
    return ModelConfig(**data)


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
        self.story_memory_path = self.memory_dir / 'story_memory.md'
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
        )
        self.planner_model = clone_model_config(
            base_main,
            reasoning_effort=args.planner_reasoning_effort,
        )
        self.summary_model = clone_model_config(
            base_sub,
            reasoning_effort=args.summary_reasoning_effort,
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
            brief_text = read_text(source_path)
        elif self.args.brief_text:
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

    def _estimated_total_chapters(self) -> int:
        return max(1, math.ceil(self.args.target_chars / self.args.chapter_char_target))

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
        retries = retries or self.args.max_retries
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                if attempt > 1:
                    self.log(f'[{label}] 第 {attempt} 次尝试')
                return func()
            except Exception as exc:
                last_error = exc
                self.state['last_error'] = f'{label}: {exc}'
                self._save_state()
                self.log(f'[{label}] 失败：{exc}')
                for line in traceback.format_exc().splitlines():
                    self.log(f'[{label}] traceback | {line}')
                if attempt >= retries:
                    raise
                sleep_seconds = self.args.retry_backoff_seconds * attempt
                self.log(f'[{label}] {sleep_seconds}s 后重试')
                time.sleep(sleep_seconds)
        raise RuntimeError(str(last_error))

    def ensure_series_bible(self) -> None:
        if self.series_bible_path.exists() and self.series_bible_short_path.exists():
            return

        brief = read_text(self.brief_path)
        bible_prompt = f"""
请基于下面的设定，产出一份适合中文番茄风科幻长篇网文的《系列圣经》。

硬性目标：
- 目标总字数：{self.args.target_chars} 字
- 题材：科幻网文
- 风格：番茄风、强钩子、强冲突、强追读、强悬念
- 必须适合持续连载，节奏要能撑起超长篇
- 输出必须精炼实用，总长度尽量控制在 6000~8000 字；宁可信息密度高，也不要铺得过散

输出要求：
1. 给出推荐书名，以及 2 个备选书名
2. 核心卖点与一句话宣传语
3. 世界观与科技/灾变规则
4. 主角与主要配角定位、人物弧光、关系张力
5. 主线谜团、阶段性冲突、终局方向
6. 势力版图、关键地点、规则禁忌
7. 长篇节奏策略：如何支撑到 {self.args.target_chars} 字
8. 预计卷级路线图：按 {self._estimated_total_volumes()} 卷左右规划，每卷 3-6 条要点
9. 文风提醒与禁忌清单

设定如下：
{brief}
"""
        bible_text = self.with_retry(
            '系列圣经',
            lambda: self.call_llm('系列圣经', bible_prompt, self.planner_model),
        )
        self.clear_error()
        write_text(self.series_bible_path, bible_text)

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

        prompt = f"""
请规划第 {volume_number} 卷的卷级路线图。

卷信息：
- 预计章节范围：第 {chapter_start} 章 ~ 第 {chapter_end} 章
- 预计卷数总量：约 {self._estimated_total_volumes()} 卷
- 目标：既要推进主线，也要形成这一卷自己的高潮与卷末大钩子
- 输出尽量控制在 2000~3000 字，聚焦最关键的剧情推进与钩子

必须坚持：
- 科幻网文
- 番茄风
- ????????????????????
- ???????????????????????
- ??????????????????????

【系列圣经短版】
{series_bible_short}

【前情压缩记忆】
{story_memory or '（暂无）'}

【最近章节摘要】
{recent or '（暂无）'}

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
- 风格提示要短，但必须明确“番茄风、强钩子、强冲突、双强拉扯”

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

        remaining_total = max(1, math.ceil((self.args.target_chars - self.state['generated_chars']) / self.args.chapter_char_target))
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
            ] if item.strip()
        )

        outline_prompt = f"""
请规划接下来这一批章节的大纲：第 {next_chapter} 章 到 第 {chapter_end} 章。

硬性要求：
- 必须严格承接现有前情，不能重置人物状态
- 题材保持科幻，核心灾变是“静潮”与记忆/人格污染
- ????????????????????????
- ??????????????????????
- ?????????????????????????
- 必须是中文番茄风长篇网文节奏
- 每章都要有实质推进与章末卡点
- 单章定位是“章节大纲”，不是正文，不要写成小说正文

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
        ]
        context = '\n\n'.join(part for part in context_parts if part.strip())
        prompt = f"""
请把这章大纲扩写成详细剧情梗概。

要求：
- 这是第 {chapter['chapter_number']} 章
- 只输出剧情梗概，不要写成正文
- 节奏快、信息密、冲突密、因果清楚
- 充分体现科幻悬疑、意识追凶、记忆污染、心理治疗 AI 的独特性
- ??????????????????
- ???????????????
- 章末必须保留强卡点，为下一章续接
- 目标长度：400~900 字
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

        def _draft(existing_text: str = '') -> str:
            prompt = f"""
请把这段剧情梗概写成可发布的中文长篇网文正文。

要求：
- 这是第 {chapter['chapter_number']} 章，必须保留并优化章节标题感
- 风格：番茄风、强代入、强画面、强对白、强追读
- 题材：科幻悬疑 + 意识/记忆污染 + 心理治疗 AI
- ?????????????????????
- ????????????????????????/???
- ????????????????
- 正文不是梗概，不要用“随后、然后、接着”堆流水账
- 开头三段必须抓人
- 结尾必须卡住
- 目标字数：{target_chars} 字左右，至少 {min_chars} 字，建议不超过 {max_chars} 字
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
- ????????????????
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
        if self.state['generated_chars'] >= self.args.target_chars:
            return True
        if self.args.max_chapters and self.state['generated_chapters'] >= self.args.max_chapters:
            return True
        return False

    def run(self) -> None:
        self.state['status'] = 'running'
        self._save_state()
        self.log('自动长篇模式启动')
        self.log(f'项目目录：{self.project_dir}')
        self.log(f'目标字数：{self.args.target_chars}，单章目标：{self.args.chapter_char_target}')
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
    parser.add_argument('--target-chars', type=int, default=2_000_000)
    parser.add_argument('--chapter-char-target', type=int, default=2200)
    parser.add_argument('--chapters-per-volume', type=int, default=30)
    parser.add_argument('--chapters-per-batch', type=int, default=5)
    parser.add_argument('--memory-refresh-interval', type=int, default=5)
    parser.add_argument('--planner-reasoning-effort', default='high')
    parser.add_argument('--writer-reasoning-effort', default='high')
    parser.add_argument('--sub-reasoning-effort', default='high')
    parser.add_argument('--summary-reasoning-effort', default='high')
    parser.add_argument('--max-thread-num', type=int, default=1)
    parser.add_argument('--max-retries', type=int, default=3)
    parser.add_argument('--retry-backoff-seconds', type=int, default=15)
    parser.add_argument('--max-chapters', type=int, default=0)
    parser.add_argument('--live-stream', action='store_true')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = AutoNovelRunner(args)
    try:
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
