from __future__ import annotations

import argparse
import codecs
import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path


WATCHDOG_LOG_PATH: Path | None = None
CHILD_OUTPUT_LOG_PATH: Path | None = None
INSTANCE_LOCK_PATH: Path | None = None
CONSOLE_OUTPUT_AVAILABLE = True
CONSOLE_OUTPUT_ERROR_LOGGED = False


def now_str() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


def _append_text(path: Path | None, text: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(text)


def _write_console(text: str) -> None:
    global CONSOLE_OUTPUT_AVAILABLE, CONSOLE_OUTPUT_ERROR_LOGGED
    if not CONSOLE_OUTPUT_AVAILABLE or not text:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (OSError, ValueError, UnicodeEncodeError) as exc:
        CONSOLE_OUTPUT_AVAILABLE = False
        if not CONSOLE_OUTPUT_ERROR_LOGGED:
            CONSOLE_OUTPUT_ERROR_LOGGED = True
            _append_text(WATCHDOG_LOG_PATH, f'{now_str()} | [watchdog] console output disabled: {exc}\n')


def log(message: str) -> None:
    line = f'{now_str()} | [watchdog] {message}'
    _write_console(line + '\n')
    _append_text(WATCHDOG_LOG_PATH, line + '\n')


def tee_child_output(text: str) -> None:
    _write_console(text)
    _append_text(CHILD_OUTPUT_LOG_PATH, text)


def parse_timestamp(value: str) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return None


def read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_state_snapshot(state_path: Path) -> dict:
    data = read_json_file(state_path)
    return {
        'status': str(data.get('status', '')),
        'generated_chapters': int(data.get('generated_chapters', 0) or 0),
        'generated_chars': int(data.get('generated_chars', 0) or 0),
        'next_chapter_number': int(data.get('next_chapter_number', 0) or 0),
        'current_stage': str(data.get('current_stage', '')),
        'last_error': str(data.get('last_error', '')),
    }


def read_runner_heartbeat(heartbeat_path: Path) -> dict:
    data = read_json_file(heartbeat_path)
    return {
        'at': str(data.get('at', '')),
        'current_stage': str(data.get('current_stage', '')),
        'stage_started_at': str(data.get('stage_started_at', '')),
    }


def get_signal_age_seconds(timestamp_text: str) -> float | None:
    timestamp = parse_timestamp(timestamp_text)
    if timestamp is None:
        return None
    return max(0.0, time.time() - timestamp)


def is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    process_handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process_handle:
        return False
    ctypes.windll.kernel32.CloseHandle(process_handle)
    return True


def claim_instance(lock_path: Path) -> tuple[bool, int | None]:
    data = read_json_file(lock_path)
    existing_pid = int(data.get('pid', 0) or 0)
    if existing_pid and existing_pid != os.getpid() and is_pid_running(existing_pid):
        return False, existing_pid

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_payload = {
        'pid': os.getpid(),
        'started_at': now_str(),
        'script': str(Path(__file__).resolve()),
    }
    lock_path.write_text(json.dumps(lock_payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return True, None


def release_instance(lock_path: Path | None) -> None:
    if lock_path is None or not lock_path.exists():
        return
    data = read_json_file(lock_path)
    if int(data.get('pid', 0) or 0) != os.getpid():
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


def build_child_command(args: argparse.Namespace) -> list[str]:
    command = [
        str(args.python_exe),
        '-u',
        str(args.script_path),
        '--project-dir', str(args.project_dir),
        '--brief-file', str(args.brief_file),
        '--completion-mode', args.completion_mode,
        '--target-chars', str(args.target_chars),
        '--min-target-chars', str(args.min_target_chars),
        '--force-finish-chars', str(args.force_finish_chars),
        '--max-target-chars', str(args.max_target_chars),
        '--chapter-char-target', str(args.chapter_char_target),
        '--chapters-per-volume', str(args.chapters_per_volume),
        '--chapters-per-batch', str(args.chapters_per_batch),
        '--memory-refresh-interval', str(args.memory_refresh_interval),
        '--main-model', args.main_model,
        '--sub-model', args.sub_model,
        '--planner-reasoning-effort', args.planner_reasoning_effort,
        '--writer-reasoning-effort', args.writer_reasoning_effort,
        '--sub-reasoning-effort', args.sub_reasoning_effort,
        '--summary-reasoning-effort', args.summary_reasoning_effort,
        '--max-thread-num', str(args.max_thread_num),
        '--max-retries', str(args.max_retries),
        '--retry-backoff-seconds', str(args.retry_backoff_seconds),
        '--live-stream',
    ]
    if args.max_chapters:
        command.extend(['--max-chapters', str(args.max_chapters)])
    return command


class ReaderThread(threading.Thread):
    def __init__(self, pipe, output_queue: queue.Queue[str]):
        super().__init__(daemon=True)
        self.pipe = pipe
        self.output_queue = output_queue

    def run(self) -> None:
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                chunk = self.pipe.read(1024)
                if not chunk:
                    remaining = decoder.decode(b'', final=True)
                    if remaining:
                        self.output_queue.put(remaining)
                    break
                text = decoder.decode(chunk)
                if text:
                    self.output_queue.put(text)
        except Exception as exc:
            self.output_queue.put('\n' + now_str() + f' | [watchdog] reader error: {exc}\n')
        finally:
            self.output_queue.put('__READER_EOF__')


def terminate_process(process: subprocess.Popen[bytes], grace_seconds: int = 10) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except Exception:
        return
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.2)
    try:
        process.kill()
    except Exception:
        pass


def run_once(args: argparse.Namespace, state_path: Path) -> bool:
    command = build_child_command(args)
    log('starting child runner')
    log(f"command: {' '.join(command)}")

    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'

    process = subprocess.Popen(
        command,
        cwd=args.repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    assert process.stdout is not None
    log(f'child pid={process.pid}')

    output_queue: queue.Queue[str] = queue.Queue()
    reader = ReaderThread(process.stdout, output_queue)
    reader.start()

    last_output_time = time.time()
    last_child_output_time = last_output_time
    next_heartbeat_at = last_output_time + args.heartbeat_interval_seconds
    eof_seen = False

    try:
        while True:
            try:
                item = output_queue.get(timeout=1.0)
            except queue.Empty:
                item = None

            if item is not None:
                if item == '__READER_EOF__':
                    eof_seen = True
                else:
                    tee_child_output(item)
                    last_output_time = time.time()
                    last_child_output_time = last_output_time
                    next_heartbeat_at = last_output_time + args.heartbeat_interval_seconds

            if process.poll() is not None and eof_seen and output_queue.empty():
                break

            if process.poll() is None:
                now = time.time()
                idle_seconds = now - last_child_output_time
                state_snapshot = None
                runner_heartbeat = None
                runner_heartbeat_age = None
                stage_runtime = None
                stage_name = '-'

                if now >= next_heartbeat_at or idle_seconds >= args.stall_timeout_seconds:
                    state_snapshot = read_state_snapshot(state_path)
                    runner_heartbeat = read_runner_heartbeat(args.runner_heartbeat_path)
                    stage_name = runner_heartbeat['current_stage'] or state_snapshot['current_stage'] or '-'
                    runner_heartbeat_age = get_signal_age_seconds(runner_heartbeat['at'])
                    stage_runtime = get_signal_age_seconds(runner_heartbeat['stage_started_at'])

                if now >= next_heartbeat_at:
                    status = state_snapshot['status'] if state_snapshot is not None else 'unknown'
                    chapters = state_snapshot['generated_chapters'] if state_snapshot is not None else 0
                    chars_count = state_snapshot['generated_chars'] if state_snapshot is not None else 0
                    next_chapter = state_snapshot['next_chapter_number'] if state_snapshot is not None else 0
                    heartbeat_age_text = f'{int(runner_heartbeat_age)}s' if runner_heartbeat_age is not None else 'unknown'
                    stage_runtime_text = f'{int(stage_runtime)}s' if stage_runtime is not None else 'unknown'
                    log(
                        f'still waiting; idle={int(idle_seconds)}s, runner_heartbeat={heartbeat_age_text}, '
                        f'stage_runtime={stage_runtime_text}, stage={stage_name}, status={status or "unknown"}, '
                        f'chapters={chapters}, chars={chars_count}, next={next_chapter}'
                    )
                    next_heartbeat_at = now + args.heartbeat_interval_seconds

                if idle_seconds >= args.stall_timeout_seconds:
                    stage_runtime_allowed = (
                        args.max_stage_runtime_seconds <= 0
                        or stage_runtime is None
                        or stage_runtime <= args.max_stage_runtime_seconds
                    )
                    hard_silence_hit = (
                        args.max_silent_seconds > 0
                        and idle_seconds >= args.max_silent_seconds
                    )
                    if (
                        runner_heartbeat_age is not None
                        and runner_heartbeat_age <= args.runner_heartbeat_grace_seconds
                        and stage_runtime_allowed
                        and not hard_silence_hit
                    ):
                        log(
                            f'no child output for {int(idle_seconds)}s, but runner heartbeat is fresh '
                            f'({int(runner_heartbeat_age)}s) at stage={stage_name}; keep waiting'
                        )
                        last_output_time = now
                        next_heartbeat_at = now + args.heartbeat_interval_seconds
                        continue

                    status = state_snapshot['status'] if state_snapshot is not None else 'unknown'
                    chapters = state_snapshot['generated_chapters'] if state_snapshot is not None else 0
                    chars_count = state_snapshot['generated_chars'] if state_snapshot is not None else 0
                    next_chapter = state_snapshot['next_chapter_number'] if state_snapshot is not None else 0
                    last_error = state_snapshot['last_error'] if state_snapshot is not None else ''
                    reason = 'max silent ceiling reached' if hard_silence_hit else 'stall timeout exceeded'
                    log(
                        f'no output for {int(idle_seconds)}s; terminating child [{reason}] '
                        f'(stage={stage_name}, status={status or "unknown"}, chapters={chapters}, chars={chars_count}, '
                        f'next={next_chapter}, last_error={last_error or "-"})'
                    )
                    terminate_process(process)
                    break
    except KeyboardInterrupt:
        log('received Ctrl+C, stopping child runner')
        terminate_process(process)
        raise
    finally:
        try:
            process.stdout.close()
        except Exception:
            pass

    exit_code = process.poll()
    state_snapshot = read_state_snapshot(state_path)
    log(
        f'child exit_code={exit_code}, status={state_snapshot["status"] or "unknown"}, '
        f'chapters={state_snapshot["generated_chapters"]}, chars={state_snapshot["generated_chars"]}, '
        f'next={state_snapshot["next_chapter_number"]}'
    )
    return state_snapshot['status'] == 'completed'


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='Visible watchdog runner for auto_novel.py')
    parser.add_argument('--repo-root', default=str(repo_root))
    parser.add_argument('--python-exe', default=str(repo_root / '.venv' / 'Scripts' / 'python.exe'))
    parser.add_argument('--script-path', default='auto_novel.py')
    parser.add_argument('--project-dir', default=str(repo_root / 'auto_projects' / 'default_project'))
    parser.add_argument('--brief-file', default=str(repo_root / 'novel_brief.md'))
    parser.add_argument('--completion-mode', choices=['hard_target', 'min_chars_and_story_end'], default='hard_target')
    parser.add_argument('--target-chars', type=int, default=2_000_000)
    parser.add_argument('--min-target-chars', type=int, default=0)
    parser.add_argument('--force-finish-chars', type=int, default=0)
    parser.add_argument('--max-target-chars', type=int, default=0)
    parser.add_argument('--chapter-char-target', type=int, default=2200)
    parser.add_argument('--chapters-per-volume', type=int, default=30)
    parser.add_argument('--chapters-per-batch', type=int, default=5)
    parser.add_argument('--memory-refresh-interval', type=int, default=5)
    parser.add_argument('--main-model', default='gpt/gpt-5.4')
    parser.add_argument('--sub-model', default='gpt/gpt-5.4')
    parser.add_argument('--planner-reasoning-effort', default='medium')
    parser.add_argument('--writer-reasoning-effort', default='medium')
    parser.add_argument('--sub-reasoning-effort', default='low')
    parser.add_argument('--summary-reasoning-effort', default='low')
    parser.add_argument('--max-thread-num', type=int, default=1)
    parser.add_argument('--max-retries', type=int, default=0)
    parser.add_argument('--retry-backoff-seconds', type=int, default=15)
    parser.add_argument('--max-chapters', type=int, default=0)
    parser.add_argument('--stall-timeout-seconds', type=int, default=480)
    parser.add_argument('--restart-delay-seconds', type=int, default=15)
    parser.add_argument('--heartbeat-interval-seconds', type=int, default=30)
    parser.add_argument('--runner-heartbeat-grace-seconds', type=int, default=90)
    parser.add_argument('--max-stage-runtime-seconds', type=int, default=0)
    parser.add_argument('--max-silent-seconds', type=int, default=900)
    args = parser.parse_args()
    args.repo_root = Path(args.repo_root).resolve()
    args.python_exe = Path(args.python_exe).resolve()
    args.script_path = Path(args.script_path).resolve() if Path(args.script_path).is_absolute() else Path(args.script_path)
    args.project_dir = Path(args.project_dir).resolve()
    args.brief_file = Path(args.brief_file).resolve()
    args.watchdog_log_path = args.project_dir / 'logs' / 'watchdog.log'
    args.child_output_log_path = args.project_dir / 'logs' / 'console.out.log'
    args.runner_heartbeat_path = args.project_dir / 'logs' / 'runner_heartbeat.json'
    args.instance_lock_path = args.project_dir / 'logs' / 'watchdog.instance.json'
    return args


def main() -> int:
    global WATCHDOG_LOG_PATH, CHILD_OUTPUT_LOG_PATH, INSTANCE_LOCK_PATH
    args = parse_args()
    state_path = args.project_dir / 'state.json'
    WATCHDOG_LOG_PATH = args.watchdog_log_path
    CHILD_OUTPUT_LOG_PATH = args.child_output_log_path
    INSTANCE_LOCK_PATH = args.instance_lock_path

    if not args.python_exe.exists():
        python_on_path = shutil.which('python')
        if python_on_path:
            args.python_exe = Path(python_on_path).resolve()
            print(f'Python virtualenv not found, fallback to PATH python: {args.python_exe}')
        else:
            print(f'Python virtualenv not found: {args.python_exe}', file=sys.stderr)
            return 1

    claimed, existing_pid = claim_instance(args.instance_lock_path)
    if not claimed:
        log(f'another watchdog is already running (pid={existing_pid}), exiting')
        return 2

    try:
        while True:
            completed = run_once(args, state_path)
            if completed:
                log('project completed, watchdog exits')
                return 0
            log(f'restarting after {args.restart_delay_seconds}s')
            time.sleep(args.restart_delay_seconds)
    except KeyboardInterrupt:
        log('watchdog interrupted by user')
        return 130
    except Exception:
        log('watchdog crashed with unexpected exception:')
        for line in traceback.format_exc().splitlines():
            log(line)
        return 1
    finally:
        release_instance(args.instance_lock_path)


if __name__ == '__main__':
    raise SystemExit(main())
