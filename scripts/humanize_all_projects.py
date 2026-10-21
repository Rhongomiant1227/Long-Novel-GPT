"""Run the humanizer over every local project not already uploaded to Fanqie.

Projects run independently with a bounded pool. Each project remains
chapter-serial and retries its current failed chapter without skipping.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXCLUDED = {"lychee_prelude", "inscribed_immortal", "crowd_blackbox"}
BOOK_TITLE_RE = re.compile(r"《([^》]+)》")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def book_title(project_dir: Path) -> str:
    for name in ("brief.md", "README.md", "series_bible.md"):
        path = project_dir / name
        if not path.exists():
            continue
        match = BOOK_TITLE_RE.search(path.read_text(encoding="utf-8-sig", errors="ignore"))
        if match:
            return match.group(1).strip()
    return ""


def legacy_state_path(project_dir: Path) -> Path | None:
    state_path = project_dir / "state.json"
    if state_path.exists():
        return state_path
    chapter_dir = project_dir / "manuscript" / "chapters_txt"
    chapter_files = sorted(chapter_dir.glob("ch_*.txt"))
    if not chapter_files:
        return None
    adapter_path = REPO_ROOT / ".run" / "humanizer_legacy_states" / f"{project_dir.name}.json"
    if not adapter_path.exists():
        completed = []
        for chapter_file in chapter_files:
            match = re.match(r"^ch_(\d+)\.txt$", chapter_file.name, re.IGNORECASE)
            if not match:
                continue
            number = int(match.group(1))
            lines = chapter_file.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
            heading = lines[0].strip() if lines else ""
            heading_match = re.match(r"^\s*第\s*(\d+)\s*章(?:\s+|：|:|-)?(.*)$", heading)
            title = heading_match.group(2).strip() if heading_match else heading
            completed.append(
                {
                    "chapter_number": number,
                    "title": title,
                    "draft_file": str(chapter_file.resolve()),
                    "summary_file": "",
                }
            )
        adapter_path.parent.mkdir(parents=True, exist_ok=True)
        adapter_path.write_text(
            json.dumps(
                {
                    "project": project_dir.name,
                    "legacy_adapter": True,
                    "completed_chapters": completed,
                    "humanized_chapters": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return adapter_path


def candidate_projects(excluded: set[str]) -> list[Path]:
    projects: list[Path] = []
    for path in sorted((REPO_ROOT / "auto_projects").iterdir()):
        if not path.is_dir() or path.name in excluded:
            continue
        state_path = legacy_state_path(path)
        state = load_json(state_path, {}) if state_path else {}
        chapters = state.get("completed_chapters") if isinstance(state, dict) else None
        if isinstance(chapters, list) and chapters:
            projects.append(path)
    return projects


class Logger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def write(self, text: str) -> None:
        line = str(text).rstrip("\r\n")
        with self.lock:
            print(line, flush=True)
            self.handle.write(line + "\n")

    def close(self) -> None:
        with self.lock:
            self.handle.close()


def latest_report(project_dir: Path) -> dict[str, Any]:
    reports = sorted(
        project_dir.glob("humanizer_runs/*/report.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return load_json(reports[0], {}) if reports else {}


def run_project(
    project_dir: Path,
    *,
    model: str,
    chunk_chars: int,
    retry_interval_seconds: int,
    state_file: Path,
    logger: Logger,
) -> dict[str, Any]:
    logger.write(f"===== PROJECT START: {project_dir.name} / {book_title(project_dir)} =====")
    command = [
        sys.executable,
        "-u",
        str(REPO_ROOT / "scripts" / "humanize_project.py"),
        "--project-dir",
        str(project_dir),
        "--state-file",
        str(state_file),
        "--mode",
        "rewrite",
        "--model",
        model,
        "--apply",
        "--skip-humanized",
        "--chunk-chars",
        str(chunk_chars),
        "--retry-interval-seconds",
        str(retry_interval_seconds),
        "--max-api-retries",
        "0",
        "--stop-on-failure",
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["FAST_BASE_URL"] = "https://fast.sbbbbbbbbb.xyz/v1"

    attempt = 0
    while True:
        attempt += 1
        logger.write(
            f"===== PROJECT ATTEMPT {attempt}: {project_dir.name} / {book_title(project_dir)} ====="
        )
        process = subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            logger.write(f"[{project_dir.name}] {line}")
        exit_code = process.wait()
        report = latest_report(project_dir)
        report["process_exit_code"] = exit_code
        failed = int(report.get("failed_chapters", 0) or 0)
        logger.write(
            f"===== PROJECT ATTEMPT END: {project_dir.name}; exit={exit_code}; "
            f"applied={report.get('changed_chapters', 0)}; failed={failed} ====="
        )
        if exit_code == 0 and failed == 0:
            return report

        error = ""
        for item in reversed(report.get("results") or []):
            if item.get("status") == "failed":
                error = str(item.get("error") or "").strip()
                break
        logger.write(
            f"[RETRY SAME CHAPTER] {project_dir.name}; "
            f"waiting {retry_interval_seconds}s before restart; {error or 'child process failed'}"
        )
        time.sleep(max(1, retry_interval_seconds))


def run_project_worker(
    project_dir: Path,
    *,
    model: str,
    chunk_chars: int,
    retry_interval_seconds: int,
    logger: Logger,
) -> dict[str, Any]:
    """Run one project independently; failures remain isolated to this project."""
    state_file = legacy_state_path(project_dir) or project_dir / "state.json"
    return run_project(
        project_dir,
        model=model,
        chunk_chars=chunk_chars,
        retry_interval_seconds=retry_interval_seconds,
        state_file=state_file,
        logger=logger,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Humanize all non-uploaded local novel projects.")
    parser.add_argument("--model", default="fast/grok-4.6")
    parser.add_argument("--chunk-chars", type=int, default=7000)
    parser.add_argument("--retry-interval-seconds", type=int, default=1800)
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=4,
        help="maximum number of projects making API requests concurrently",
    )
    parser.add_argument("--log-file", default="")
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()

    excluded = set(DEFAULT_EXCLUDED)
    excluded.update(str(item).strip() for item in args.exclude if str(item).strip())
    log_path = Path(args.log_file) if args.log_file else REPO_ROOT / ".run" / (
        f"humanizer_all_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    logger = Logger(log_path.resolve())
    queue_state_path = REPO_ROOT / ".run" / "humanizer_all_queue_state.json"
    queue_state = load_json(queue_state_path, {})
    projects = candidate_projects(excluded)
    logger.write(f"Humanizer queue started at {datetime.now().isoformat(timespec='seconds')}")
    logger.write(f"Model: {args.model}")
    logger.write(f"Retry interval for transient API errors: {args.retry_interval_seconds}s")
    logger.write(f"Excluded uploaded projects: {', '.join(sorted(excluded))}")
    logger.write(f"Projects with completed chapters: {len(projects)}")
    max_concurrency = max(1, min(len(projects) or 1, int(args.max_concurrency)))
    logger.write(
        f"Processing mode: up to {max_concurrency} projects concurrently; "
        "each project remains chapter-serial"
    )
    logger.write("Failure policy: stop at the failed chapter; never skip; retry same chapter after the interval")

    queue_state.update(
        {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "model": args.model,
            "retry_interval_seconds": args.retry_interval_seconds,
            "max_concurrency": max_concurrency,
            "excluded": sorted(excluded),
            "projects": [path.name for path in projects],
            "status": "running",
            "completed_projects": [],
            "active_projects": [],
        }
    )
    queue_state_path.parent.mkdir(parents=True, exist_ok=True)
    queue_state_path.write_text(json.dumps(queue_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    failed_projects = 0
    completed_projects: list[str] = []
    try:
        logger.write(
            f"Submitting all {len(projects)} projects; API concurrency limit={max_concurrency}"
        )
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="humanizer-project",
        ) as executor:
            future_map = {
                executor.submit(
                    run_project_worker,
                    project_dir,
                    model=args.model,
                    chunk_chars=args.chunk_chars,
                    retry_interval_seconds=args.retry_interval_seconds,
                    logger=logger,
                ): project_dir
                for project_dir in projects
            }
            queue_state["active_projects"] = [
                project_dir.name for project_dir in projects[:max_concurrency]
            ]
            queue_state["queued_projects"] = [
                project_dir.name for project_dir in projects[max_concurrency:]
            ]
            queue_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            queue_state_path.write_text(
                json.dumps(queue_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            for index, future in enumerate(
                concurrent.futures.as_completed(future_map),
                start=1,
            ):
                project_dir = future_map[future]
                try:
                    report = future.result()
                except Exception as exc:
                    failed_projects += 1
                    report = {
                        "project": project_dir.name,
                        "process_exit_code": 1,
                        "error": str(exc),
                    }
                    logger.write(
                        f"[PROJECT UNHANDLED FAILURE] {project_dir.name}: {exc}"
                    )
                else:
                    if (
                        int(report.get("failed_chapters", 0) or 0) > 0
                        or int(report.get("process_exit_code", 0) or 0) != 0
                    ):
                        failed_projects += 1

                completed_projects.append(project_dir.name)
                active_names = [
                    item
                    for item in queue_state.get("active_projects", [])
                    if item != project_dir.name
                ]
                queued_names = list(queue_state.get("queued_projects", []))
                if queued_names:
                    replacement = queued_names.pop(0)
                    active_names.append(replacement)
                    logger.write(
                        f"[SLOT AVAILABLE] {project_dir.name} finished; "
                        f"next queued project is {replacement}"
                    )
                queue_state["active_projects"] = active_names
                queue_state["queued_projects"] = queued_names
                queue_state["completed_projects"] = completed_projects
                queue_state["last_project"] = project_dir.name
                queue_state["last_report"] = report
                queue_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                queue_state_path.write_text(
                    json.dumps(queue_state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                logger.write(
                    f"[PROJECT COMPLETE {index}/{len(projects)}] {project_dir.name}; "
                    f"active={len(active_names)} queued={len(queued_names)}"
                )
        queue_state["status"] = "completed"
    except KeyboardInterrupt:
        logger.write(
            "Humanizer queue interrupted by user; applied chapters remain backed up and resumable."
        )
        queue_state["status"] = "interrupted"
        queue_state["interrupted_at"] = datetime.now().isoformat(timespec="seconds")
        queue_state_path.write_text(
            json.dumps(queue_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 130
    finally:
        queue_state["failed_projects"] = failed_projects
        queue_state["completed_projects"] = completed_projects
        queue_state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        queue_state_path.write_text(
            json.dumps(queue_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.write(
            f"Humanizer queue finished; failed_projects={failed_projects}; log={log_path.resolve()}"
        )
        logger.close()
    return 1 if failed_projects else 0


if __name__ == "__main__":
    raise SystemExit(main())
