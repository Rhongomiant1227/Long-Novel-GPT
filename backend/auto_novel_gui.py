from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

from config import DEFAULT_MAIN_MODEL, DEFAULT_SUB_MODEL


auto_novel_gui_bp = Blueprint('auto_novel_gui', __name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTO_PROJECTS_DIR = REPO_ROOT / 'auto_projects'
PROJECT_CONFIG_PATH = 'project_config.json'
DEFAULT_PROJECTS_ROOT = str(AUTO_PROJECTS_DIR)
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
MAX_CONTENT_CHARS = 600_000
MAX_LOG_CHARS = 40_000

DEFAULT_AUTO_CONFIG = {
    'target_chars': 2_000_000,
    'chapter_char_target': 2200,
    'chapters_per_volume': 30,
    'chapters_per_batch': 5,
    'memory_refresh_interval': 5,
    'main_model': DEFAULT_MAIN_MODEL,
    'sub_model': DEFAULT_SUB_MODEL,
    'planner_reasoning_effort': 'high',
    'writer_reasoning_effort': 'high',
    'sub_reasoning_effort': 'medium',
    'summary_reasoning_effort': 'medium',
    'max_thread_num': 1,
    'max_retries': 0,
    'retry_backoff_seconds': 15,
    'max_chapters': 0,
    'stall_timeout_seconds': 480,
    'restart_delay_seconds': 15,
    'heartbeat_interval_seconds': 30,
    'runner_heartbeat_grace_seconds': 90,
    'max_stage_runtime_seconds': 1800,
}


def now_str() -> str:
    return time.strftime(TIMESTAMP_FORMAT)


def ensure_auto_projects_dir() -> Path:
    AUTO_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return AUTO_PROJECTS_DIR


def read_json(path: Path, default: dict | list | None = None):
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {} if default is None else default


def write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + '.tmp')
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp_path.replace(path)


def read_text(path: Path, default: str = '') -> str:
    if not path.exists():
        return default
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return path.read_text(encoding='utf-8', errors='replace')


def tail_text(path: Path, max_chars: int = MAX_LOG_CHARS) -> str:
    text = read_text(path)
    if len(text) <= max_chars:
        return text
    return f'...(仅显示末尾 {max_chars} 字)\n\n{text[-max_chars:]}'


def truncate_text(text: str, max_chars: int = MAX_CONTENT_CHARS) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    head = max_chars // 2
    tail = max_chars - head
    clipped = f'{text[:head]}\n\n...(内容过长，已截断)...\n\n{text[-tail:]}'
    return clipped, True


def parse_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, TIMESTAMP_FORMAT))
    except Exception:
        return None


def age_seconds(value: str) -> int | None:
    timestamp = parse_timestamp(value)
    if timestamp is None:
        return None
    return max(0, int(time.time() - timestamp))


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == 'nt':
        process_handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process_handle:
            return False
        ctypes.windll.kernel32.CloseHandle(process_handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def sanitize_project_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', '_', (name or '').strip())
    cleaned = cleaned.strip(' .')
    cleaned = re.sub(r'\s+', '_', cleaned)
    return cleaned or f'project_{int(time.time())}'


def get_project_dir(project_id: str) -> Path:
    ensure_auto_projects_dir()
    if not project_id or '/' in project_id or '\\' in project_id or project_id in {'.', '..'}:
        raise FileNotFoundError('无效项目')
    path = (AUTO_PROJECTS_DIR / project_id).resolve()
    if path.parent != AUTO_PROJECTS_DIR.resolve() or not path.exists():
        raise FileNotFoundError('项目不存在')
    return path


def get_project_path_for_create(project_name: str) -> Path:
    ensure_auto_projects_dir()
    return (AUTO_PROJECTS_DIR / sanitize_project_name(project_name)).resolve()


def detect_file_type(path: str) -> str:
    normalized = path.replace('\\', '/').lower()
    basename = Path(normalized).name
    if basename == 'brief.md':
        return 'brief'
    if basename == 'full_novel.txt':
        return 'manuscript'
    if basename == 'series_bible.md':
        return 'series_bible'
    if basename == 'series_bible_short.md':
        return 'series_bible_short'
    if basename == 'story_memory.md':
        return 'memory'
    if basename == 'state.json':
        return 'state'
    if basename == 'project_config.json':
        return 'config'
    if basename == 'plan.md':
        return 'plan'
    if basename == 'outline.md':
        return 'outline'
    if basename == 'plot.md':
        return 'plot'
    if basename == 'draft.md':
        return 'draft'
    if basename == 'summary.md':
        return 'summary'
    if basename == 'live_stage.json':
        return 'live'
    if basename == 'runner.log':
        return 'runner_log'
    if basename == 'watchdog.log':
        return 'watchdog_log'
    if basename.endswith('.log'):
        return 'log'
    if basename.endswith('.json'):
        return 'json'
    return 'text'


def pretty_file_label(path: str, chapter_number: int | None = None) -> str:
    file_type = detect_file_type(path)
    if file_type == 'brief':
        return '作品设定'
    if file_type == 'manuscript':
        return '全书正文'
    if file_type == 'series_bible':
        return '系列圣经'
    if file_type == 'series_bible_short':
        return '系列圣经（短）'
    if file_type == 'memory':
        return '剧情记忆'
    if file_type == 'state':
        return '运行状态'
    if file_type == 'config':
        return '项目配置'
    if file_type == 'plan':
        return '卷计划'
    if file_type == 'outline':
        return f'第{chapter_number}章 章节' if chapter_number else '章节'
    if file_type == 'plot':
        return f'第{chapter_number}章 剧情' if chapter_number else '剧情'
    if file_type == 'draft':
        return f'第{chapter_number}章 正文' if chapter_number else '正文'
    if file_type == 'summary':
        return f'第{chapter_number}章 摘要' if chapter_number else '摘要'
    if file_type == 'live':
        return '实时快照'
    if file_type == 'runner_log':
        return 'Runner 日志'
    if file_type == 'watchdog_log':
        return 'Watchdog 日志'
    return Path(path).name


def read_watchdog_status(project_dir: Path) -> dict:
    lock_data = read_json(project_dir / 'logs' / 'watchdog.instance.json', {})
    pid = int(lock_data.get('pid', 0) or 0)
    running = bool(pid and is_pid_running(pid))
    return {
        'pid': pid,
        'running': running,
        'started_at': lock_data.get('started_at', ''),
    }


def load_project_config(project_dir: Path) -> dict:
    config_path = project_dir / PROJECT_CONFIG_PATH
    config = dict(DEFAULT_AUTO_CONFIG)
    saved = read_json(config_path, {})
    state = read_json(project_dir / 'state.json', {})
    for key in DEFAULT_AUTO_CONFIG:
        if key in state and state.get(key) not in (None, ''):
            config[key] = state.get(key)
        if key in saved and saved.get(key) not in (None, ''):
            config[key] = saved.get(key)
    config['display_name'] = saved.get('display_name') or state.get('book_title') or project_dir.name
    config['updated_at'] = saved.get('updated_at') or state.get('updated_at') or now_str()
    config['created_at'] = saved.get('created_at') or state.get('created_at') or now_str()
    return config


def save_project_config(project_dir: Path, config: dict) -> dict:
    merged = dict(DEFAULT_AUTO_CONFIG)
    merged.update({key: value for key, value in config.items() if value not in (None, '')})
    existing = read_json(project_dir / PROJECT_CONFIG_PATH, {})
    merged['display_name'] = config.get('display_name') or existing.get('display_name') or project_dir.name
    merged['created_at'] = existing.get('created_at') or now_str()
    merged['updated_at'] = now_str()
    write_json(project_dir / PROJECT_CONFIG_PATH, merged)
    return merged


def load_project_snapshot(project_dir: Path) -> dict:
    state = read_json(project_dir / 'state.json', {})
    heartbeat = read_json(project_dir / 'logs' / 'runner_heartbeat.json', {})
    live_stage = read_json(project_dir / 'logs' / 'live_stage.json', {})
    watchdog = read_watchdog_status(project_dir)
    config = load_project_config(project_dir)

    heartbeat_pid = int(heartbeat.get('pid', 0) or 0)
    heartbeat_age = age_seconds(str(heartbeat.get('at', '')))
    stage_runtime = age_seconds(str(heartbeat.get('stage_started_at', '')) or str(state.get('stage_started_at', '')))
    current_stage = str(state.get('current_stage') or heartbeat.get('current_stage') or live_stage.get('label') or '')
    status = str(state.get('status', 'not_started') or 'not_started')
    last_error = str(state.get('last_error', '') or '')
    generated_chars = int(state.get('generated_chars', 0) or 0)
    target_chars = int(config.get('target_chars', DEFAULT_AUTO_CONFIG['target_chars']) or DEFAULT_AUTO_CONFIG['target_chars'])
    progress = round((generated_chars / target_chars) * 100, 3) if target_chars else 0
    grace_seconds = int(config.get('runner_heartbeat_grace_seconds', 90) or 90)
    runner_alive = bool(heartbeat_pid and is_pid_running(heartbeat_pid))
    running = watchdog['running'] or runner_alive
    if not running and status == 'running' and heartbeat_age is not None and heartbeat_age <= grace_seconds * 2:
        running = True

    return {
        'id': project_dir.name,
        'name': project_dir.name,
        'display_name': state.get('book_title') or config.get('display_name') or project_dir.name,
        'path': str(project_dir),
        'status': status,
        'running': running,
        'watchdog_pid': watchdog['pid'],
        'runner_pid': heartbeat_pid,
        'watchdog_started_at': watchdog['started_at'],
        'heartbeat_at': heartbeat.get('at', ''),
        'heartbeat_age_seconds': heartbeat_age,
        'stage_runtime_seconds': stage_runtime,
        'current_stage': current_stage,
        'stage_started_at': state.get('stage_started_at') or heartbeat.get('stage_started_at', ''),
        'generated_chapters': int(state.get('generated_chapters', 0) or 0),
        'generated_chars': generated_chars,
        'next_chapter_number': int(state.get('next_chapter_number', 1) or 1),
        'current_volume': int(state.get('current_volume', 1) or 1),
        'target_chars': target_chars,
        'progress_percent': progress,
        'book_title': state.get('book_title', ''),
        'last_error': last_error,
        'updated_at': state.get('updated_at') or config.get('updated_at') or now_str(),
        'created_at': state.get('created_at') or config.get('created_at') or now_str(),
        'stalled': bool(running and heartbeat_age is not None and heartbeat_age > grace_seconds * 2),
        'live_stage': live_stage,
        'config': config,
    }


def project_sort_key(project: dict):
    updated = parse_timestamp(project.get('updated_at', '')) or 0
    return (0 if project.get('running') else 1, -updated, project.get('name', ''))


def list_projects() -> list[dict]:
    ensure_auto_projects_dir()
    projects = [load_project_snapshot(path) for path in AUTO_PROJECTS_DIR.iterdir() if path.is_dir()]
    projects.sort(key=project_sort_key)
    return projects


def chapter_title_map(project_dir: Path) -> dict[int, str]:
    state = read_json(project_dir / 'state.json', {})
    mapping: dict[int, str] = {}
    for item in state.get('completed_chapters', []) or []:
        chapter_number = int(item.get('chapter_number', 0) or 0)
        title = str(item.get('title', '') or '').strip()
        if chapter_number:
            mapping[chapter_number] = title
    return mapping


def build_project_tree(project_dir: Path) -> list[dict]:
    chapter_titles = chapter_title_map(project_dir)
    tree: list[dict] = []

    def add_group(label: str, type_name: str, children: list[dict]) -> None:
        if children:
            tree.append({'label': label, 'type': type_name, 'kind': 'group', 'children': children})

    overview_children = []
    for relative_path in ['brief.md', 'manuscript/full_novel.txt', 'state.json', PROJECT_CONFIG_PATH]:
        full_path = project_dir / relative_path
        if full_path.exists():
            overview_children.append({
                'label': pretty_file_label(relative_path),
                'path': relative_path,
                'type': detect_file_type(relative_path),
                'kind': 'file',
            })
    add_group('项目概览', 'overview', overview_children)

    memory_children = []
    for relative_path in ['memory/series_bible.md', 'memory/series_bible_short.md', 'memory/story_memory.md']:
        full_path = project_dir / relative_path
        if full_path.exists():
            memory_children.append({
                'label': pretty_file_label(relative_path),
                'path': relative_path,
                'type': detect_file_type(relative_path),
                'kind': 'file',
            })
    add_group('记忆设定', 'memory_group', memory_children)

    volume_nodes = []
    for volume_dir in sorted((project_dir / 'volumes').glob('vol_*')):
        if not volume_dir.is_dir():
            continue
        volume_number_match = re.search(r'vol_(\d+)', volume_dir.name)
        volume_number = int(volume_number_match.group(1)) if volume_number_match else 0
        volume_children = []
        plan_path = volume_dir / 'plan.md'
        if plan_path.exists():
            volume_children.append({
                'label': '卷计划',
                'path': str(plan_path.relative_to(project_dir)).replace('\\', '/'),
                'type': 'plan',
                'kind': 'file',
            })

        chapter_groups = []
        chapters_dir = volume_dir / 'chapters'
        for chapter_dir in sorted(chapters_dir.glob('ch_*')) if chapters_dir.exists() else []:
            if not chapter_dir.is_dir():
                continue
            chapter_match = re.search(r'ch_(\d+)', chapter_dir.name)
            chapter_number = int(chapter_match.group(1)) if chapter_match else 0
            title_suffix = chapter_titles.get(chapter_number, '').strip()
            title_text = f' · {title_suffix}' if title_suffix else ''
            chapter_children = []
            for filename in ['draft.md', 'plot.md', 'outline.md', 'summary.md']:
                file_path = chapter_dir / filename
                if not file_path.exists():
                    continue
                relative_path = str(file_path.relative_to(project_dir)).replace('\\', '/')
                chapter_children.append({
                    'label': pretty_file_label(relative_path, chapter_number),
                    'path': relative_path,
                    'type': detect_file_type(relative_path),
                    'kind': 'file',
                    'chapter_number': chapter_number,
                })
            if chapter_children:
                chapter_groups.append({
                    'label': f'第{chapter_number}章{title_text}',
                    'type': 'chapter',
                    'kind': 'chapter',
                    'chapter_number': chapter_number,
                    'children': chapter_children,
                })

        volume_children.extend(chapter_groups)
        if volume_children:
            volume_nodes.append({
                'label': f'第{volume_number}卷',
                'type': 'volume',
                'kind': 'volume',
                'volume_number': volume_number,
                'children': volume_children,
            })
    add_group('卷与章节', 'volumes', volume_nodes)

    log_children = []
    for relative_path in [
        'logs/live_stage.json',
        'logs/runner.log',
        'logs/watchdog.log',
        'logs/events.log',
        'logs/console.out.log',
        'logs/runner_heartbeat.json',
    ]:
        full_path = project_dir / relative_path
        if full_path.exists():
            log_children.append({
                'label': pretty_file_label(relative_path),
                'path': relative_path,
                'type': detect_file_type(relative_path),
                'kind': 'file',
            })
    add_group('日志诊断', 'logs', log_children)

    return tree


def resolve_project_file(project_dir: Path, relative_path: str) -> Path:
    if not relative_path:
        raise FileNotFoundError('未指定文件路径')
    normalized = relative_path.replace('\\', '/').lstrip('/')
    candidate = (project_dir / normalized).resolve()
    if candidate != project_dir.resolve() and project_dir.resolve() not in candidate.parents:
        raise FileNotFoundError('非法文件路径')
    if not candidate.exists() or not candidate.is_file():
        raise FileNotFoundError('文件不存在')
    return candidate


def read_project_file(project_dir: Path, relative_path: str) -> dict:
    path = resolve_project_file(project_dir, relative_path)
    file_type = detect_file_type(relative_path)
    if file_type.endswith('log') or file_type in {'live', 'log'}:
        content = tail_text(path)
        truncated = len(read_text(path)) > len(content)
    else:
        raw_text = read_text(path)
        if path.suffix.lower() == '.json':
            try:
                raw_text = json.dumps(json.loads(raw_text), ensure_ascii=False, indent=2)
            except Exception:
                pass
        content, truncated = truncate_text(raw_text)
    return {
        'path': str(path.relative_to(project_dir)).replace('\\', '/'),
        'label': pretty_file_label(relative_path),
        'type': file_type,
        'size': path.stat().st_size,
        'updated_at': time.strftime(TIMESTAMP_FORMAT, time.localtime(path.stat().st_mtime)),
        'content': content,
        'truncated': truncated,
    }


def build_start_command(project_dir: Path, config: dict) -> list[str]:
    python_exe = Path(sys.executable).resolve()
    return [
        str(python_exe),
        '-u',
        str(REPO_ROOT / 'watch_auto_novel_visible.py'),
        '--repo-root', str(REPO_ROOT),
        '--python-exe', str(python_exe),
        '--script-path', str(REPO_ROOT / 'auto_novel.py'),
        '--project-dir', str(project_dir),
        '--brief-file', str(project_dir / 'brief.md'),
        '--target-chars', str(int(config['target_chars'])),
        '--chapter-char-target', str(int(config['chapter_char_target'])),
        '--chapters-per-volume', str(int(config['chapters_per_volume'])),
        '--chapters-per-batch', str(int(config['chapters_per_batch'])),
        '--memory-refresh-interval', str(int(config['memory_refresh_interval'])),
        '--main-model', str(config['main_model']),
        '--sub-model', str(config['sub_model']),
        '--planner-reasoning-effort', str(config['planner_reasoning_effort']),
        '--writer-reasoning-effort', str(config['writer_reasoning_effort']),
        '--sub-reasoning-effort', str(config['sub_reasoning_effort']),
        '--summary-reasoning-effort', str(config['summary_reasoning_effort']),
        '--max-thread-num', str(int(config['max_thread_num'])),
        '--max-retries', str(int(config['max_retries'])),
        '--retry-backoff-seconds', str(int(config['retry_backoff_seconds'])),
        '--stall-timeout-seconds', str(int(config['stall_timeout_seconds'])),
        '--restart-delay-seconds', str(int(config['restart_delay_seconds'])),
        '--heartbeat-interval-seconds', str(int(config['heartbeat_interval_seconds'])),
        '--runner-heartbeat-grace-seconds', str(int(config['runner_heartbeat_grace_seconds'])),
        '--max-stage-runtime-seconds', str(int(config['max_stage_runtime_seconds'])),
    ] + (['--max-chapters', str(int(config['max_chapters']))] if int(config.get('max_chapters', 0) or 0) > 0 else [])


def spawn_project(project_dir: Path, config: dict) -> dict:
    command = build_start_command(project_dir, config)
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        creationflags=creationflags,
        close_fds=True,
    )
    return {'spawned': True, 'pid': process.pid, 'command': command}


def stop_project_process(project_dir: Path) -> dict:
    watchdog = read_watchdog_status(project_dir)
    heartbeat = read_json(project_dir / 'logs' / 'runner_heartbeat.json', {})
    killed_pid = 0
    for pid in [int(watchdog.get('pid', 0) or 0), int(heartbeat.get('pid', 0) or 0)]:
        if not pid or not is_pid_running(pid):
            continue
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, text=True)
        killed_pid = pid
        break
    return {'stopped': bool(killed_pid), 'pid': killed_pid}


def project_response(project_dir: Path) -> dict:
    snapshot = load_project_snapshot(project_dir)
    snapshot['tree'] = build_project_tree(project_dir)
    return snapshot


def parse_project_request(data: dict) -> dict:
    return {
        'display_name': str(data.get('project_name') or data.get('display_name') or '').strip() or '未命名项目',
        'target_chars': int(data.get('target_chars') or DEFAULT_AUTO_CONFIG['target_chars']),
        'chapter_char_target': int(data.get('chapter_char_target') or DEFAULT_AUTO_CONFIG['chapter_char_target']),
        'chapters_per_volume': int(data.get('chapters_per_volume') or DEFAULT_AUTO_CONFIG['chapters_per_volume']),
        'chapters_per_batch': int(data.get('chapters_per_batch') or DEFAULT_AUTO_CONFIG['chapters_per_batch']),
        'memory_refresh_interval': int(data.get('memory_refresh_interval') or DEFAULT_AUTO_CONFIG['memory_refresh_interval']),
        'main_model': str(data.get('main_model') or DEFAULT_AUTO_CONFIG['main_model']),
        'sub_model': str(data.get('sub_model') or DEFAULT_AUTO_CONFIG['sub_model']),
        'planner_reasoning_effort': str(data.get('planner_reasoning_effort') or DEFAULT_AUTO_CONFIG['planner_reasoning_effort']),
        'writer_reasoning_effort': str(data.get('writer_reasoning_effort') or DEFAULT_AUTO_CONFIG['writer_reasoning_effort']),
        'sub_reasoning_effort': str(data.get('sub_reasoning_effort') or DEFAULT_AUTO_CONFIG['sub_reasoning_effort']),
        'summary_reasoning_effort': str(data.get('summary_reasoning_effort') or DEFAULT_AUTO_CONFIG['summary_reasoning_effort']),
        'max_thread_num': int(data.get('max_thread_num') or DEFAULT_AUTO_CONFIG['max_thread_num']),
        'max_retries': int(data.get('max_retries') or DEFAULT_AUTO_CONFIG['max_retries']),
        'retry_backoff_seconds': int(data.get('retry_backoff_seconds') or DEFAULT_AUTO_CONFIG['retry_backoff_seconds']),
        'max_chapters': int(data.get('max_chapters') or DEFAULT_AUTO_CONFIG['max_chapters']),
        'stall_timeout_seconds': int(data.get('stall_timeout_seconds') or DEFAULT_AUTO_CONFIG['stall_timeout_seconds']),
        'restart_delay_seconds': int(data.get('restart_delay_seconds') or DEFAULT_AUTO_CONFIG['restart_delay_seconds']),
        'heartbeat_interval_seconds': int(data.get('heartbeat_interval_seconds') or DEFAULT_AUTO_CONFIG['heartbeat_interval_seconds']),
        'runner_heartbeat_grace_seconds': int(data.get('runner_heartbeat_grace_seconds') or DEFAULT_AUTO_CONFIG['runner_heartbeat_grace_seconds']),
        'max_stage_runtime_seconds': int(data.get('max_stage_runtime_seconds') or DEFAULT_AUTO_CONFIG['max_stage_runtime_seconds']),
    }


@auto_novel_gui_bp.route('/auto_novel/projects', methods=['GET'])
def get_auto_novel_projects():
    return jsonify({
        'projects': list_projects(),
        'defaults': dict(DEFAULT_AUTO_CONFIG),
        'projects_root': DEFAULT_PROJECTS_ROOT,
    })


@auto_novel_gui_bp.route('/auto_novel/projects', methods=['POST'])
def create_auto_novel_project():
    data = request.get_json(silent=True) or {}
    brief_text = str(data.get('brief_text') or '').strip()
    if not brief_text:
        return jsonify({'error': 'brief_text 不能为空'}), 400

    project_name = str(data.get('project_name') or '').strip() or '未命名项目'
    project_dir = get_project_path_for_create(project_name)
    if project_dir.exists():
        return jsonify({'error': f'项目已存在：{project_dir.name}'}), 409

    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / 'logs').mkdir(parents=True, exist_ok=True)
    (project_dir / 'memory').mkdir(parents=True, exist_ok=True)
    (project_dir / 'volumes').mkdir(parents=True, exist_ok=True)
    (project_dir / 'manuscript').mkdir(parents=True, exist_ok=True)
    (project_dir / 'brief.md').write_text(brief_text, encoding='utf-8')

    config = save_project_config(project_dir, parse_project_request(data))
    auto_start = bool(data.get('auto_start', True))
    started = None
    if auto_start:
        started = spawn_project(project_dir, config)

    payload = project_response(project_dir)
    payload['started'] = started
    return jsonify(payload), 201


@auto_novel_gui_bp.route('/auto_novel/projects/<project_id>/continue', methods=['POST'])
def continue_auto_novel_project(project_id: str):
    try:
        project_dir = get_project_dir(project_id)
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404

    snapshot = load_project_snapshot(project_dir)
    if snapshot['running']:
        snapshot['started'] = {'spawned': False, 'reason': 'already_running'}
        return jsonify(snapshot)

    data = request.get_json(silent=True) or {}
    config = save_project_config(project_dir, parse_project_request({**snapshot['config'], **data}))
    started = spawn_project(project_dir, config)
    payload = project_response(project_dir)
    payload['started'] = started
    return jsonify(payload)


@auto_novel_gui_bp.route('/auto_novel/projects/<project_id>/stop', methods=['POST'])
def stop_auto_novel_project(project_id: str):
    try:
        project_dir = get_project_dir(project_id)
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404

    result = stop_project_process(project_dir)
    payload = project_response(project_dir)
    payload['stop_result'] = result
    return jsonify(payload)


@auto_novel_gui_bp.route('/auto_novel/projects/<project_id>/status', methods=['GET'])
def get_auto_novel_project_status(project_id: str):
    try:
        project_dir = get_project_dir(project_id)
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404

    payload = load_project_snapshot(project_dir)
    payload['runner_log_tail'] = tail_text(project_dir / 'logs' / 'runner.log')
    payload['watchdog_log_tail'] = tail_text(project_dir / 'logs' / 'watchdog.log')
    payload['events_tail'] = tail_text(project_dir / 'logs' / 'events.log')
    return jsonify(payload)


@auto_novel_gui_bp.route('/auto_novel/projects/<project_id>/tree', methods=['GET'])
def get_auto_novel_project_tree(project_id: str):
    try:
        project_dir = get_project_dir(project_id)
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404
    return jsonify({'tree': build_project_tree(project_dir)})


@auto_novel_gui_bp.route('/auto_novel/projects/<project_id>/content', methods=['GET'])
def get_auto_novel_project_content(project_id: str):
    try:
        project_dir = get_project_dir(project_id)
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404

    relative_path = str(request.args.get('path') or '').strip()
    if not relative_path:
        return jsonify({'error': '缺少 path 参数'}), 400
    try:
        return jsonify(read_project_file(project_dir, relative_path))
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404


@auto_novel_gui_bp.route('/auto_novel/projects/<project_id>/live', methods=['GET'])
def get_auto_novel_project_live(project_id: str):
    try:
        project_dir = get_project_dir(project_id)
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404

    snapshot = load_project_snapshot(project_dir)
    return jsonify({
        'project': snapshot,
        'live_stage': read_json(project_dir / 'logs' / 'live_stage.json', {}),
        'runner_log_tail': tail_text(project_dir / 'logs' / 'runner.log'),
        'watchdog_log_tail': tail_text(project_dir / 'logs' / 'watchdog.log'),
        'events_tail': tail_text(project_dir / 'logs' / 'events.log'),
    })
