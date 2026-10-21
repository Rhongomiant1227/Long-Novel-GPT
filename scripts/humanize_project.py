"""Review or safely rewrite all completed drafts in a novel project.

The tool processes one chapter at a time. It never changes chapter titles or
numbers, creates a per-run backup before applying edits, and rebuilds the
upload-facing manuscript files only after all requested chapters finish.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from auto_novel import (
    PROJECT_ROLE_ONLY_RULES,
    assess_chapter_tail_integrity,
    extract_json_payload,
    normalize_chapter_draft_text,
    read_text,
    strip_generated_draft_heading,
    write_text,
)
from backend.backend_utils import get_model_config_from_provider_model
from core.humanizer import (
    build_humanizer_integrity_review_prompt,
    build_humanizer_prompt,
    build_humanizer_review_prompt,
    normalize_humanizer_output,
    validate_humanizer_revision,
)
from llm_api import stream_chat
from split_full_novel import normalize_heading_line


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def is_retryable_provider_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            " 429",
            " 500",
            " 502",
            " 503",
            " 504",
            "status_code=429",
            "status_code=500",
            "status_code=502",
            "status_code=503",
            "status_code=504",
            "rate limit",
            "too many requests",
            "temporarily unavailable",
            "service unavailable",
            "timed out",
            "timeout",
        )
    )


def call_model(
    model,
    *,
    system_prompt: str,
    user_prompt: str,
    retry_interval_seconds: int = 1800,
    max_retries: int = 0,
) -> str:
    messages = [
        {"role": "system", "content": system_prompt.strip()},
        {"role": "user", "content": user_prompt.strip()},
    ]
    attempt = 0
    while True:
        try:
            final = None
            for current in stream_chat(model, messages):
                final = current
            text = str(getattr(final, "response", "") or "").strip()
            if not text:
                raise RuntimeError("model returned empty content")
            return text
        except Exception as exc:
            if not is_retryable_provider_error(exc):
                raise
            if max_retries > 0 and attempt >= max_retries:
                raise
            attempt += 1
            print(
                f"[API retry {attempt}] transient provider error; "
                f"waiting {retry_interval_seconds}s before retry",
                flush=True,
            )
            time.sleep(max(1, retry_interval_seconds))


def optional_file_path(value: Any) -> Path | None:
    """Return an existing regular file for an optional state.json field."""
    raw_path = str(value or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    return path if path.is_file() else None


def chapter_context(item: dict[str, Any], previous_summary: str) -> tuple[str, str]:
    draft_path = optional_file_path(item.get("draft_file"))
    outline_path = optional_file_path(item.get("outline_file"))
    plot_path = optional_file_path(item.get("plot_file"))
    if outline_path is None and draft_path is not None:
        outline_path = draft_path.with_name("outline.md")
    if plot_path is None and draft_path is not None:
        plot_path = draft_path.with_name("plot.md")
    outline = read_text(outline_path).strip() if outline_path and outline_path.is_file() else ""
    plot = read_text(plot_path).strip() if plot_path and plot_path.is_file() else ""
    return outline, plot


def project_protected_terms(project_dir: Path) -> tuple[str, ...]:
    rule = PROJECT_ROLE_ONLY_RULES.get(project_dir.name, {})
    terms: list[str] = []
    for key in ("allowed_names", "preserved_labels", "role_labels"):
        terms.extend(str(value).strip() for value in rule.get(key, ()) if str(value).strip())
    return tuple(dict.fromkeys(terms))


def split_body_for_humanizer(body: str, target_chars: int) -> list[str]:
    """Split unusually long chapters at paragraph boundaries before rewriting."""
    text = str(body or "").strip()
    target = max(1000, int(target_chars or 7000))
    if len(text) <= target:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if current:
            chunks.append("\n\n".join(current).strip())
            current = []
            current_chars = 0

    for paragraph in text.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > target:
            flush()
            for start in range(0, len(paragraph), target):
                chunks.append(paragraph[start : start + target].strip())
            continue
        added = len(paragraph) + (2 if current else 0)
        if current and current_chars + added > target:
            flush()
        current.append(paragraph)
        current_chars += len(paragraph) + (2 if len(current) > 1 else 0)
    flush()
    return [chunk for chunk in chunks if chunk]


def semantic_review(
    model,
    *,
    chapter_number: int,
    title: str,
    source_body: str,
    revised_body: str,
    outline: str,
    plot: str,
    previous_summary: str,
    retry_interval_seconds: int,
    max_retries: int,
) -> dict[str, Any]:
    raw = call_model(
        model,
        system_prompt="你是小说连续性审校员，只返回合法 JSON；任何事实或关系变化都必须拒绝。",
        user_prompt=build_humanizer_integrity_review_prompt(
            chapter_number=chapter_number,
            title=title,
            original_body=source_body,
            revised_body=revised_body,
            outline=outline,
            plot=plot,
            previous_summary=previous_summary,
        ),
        retry_interval_seconds=retry_interval_seconds,
        max_retries=max_retries,
    )
    report = extract_json_payload(raw)
    if report.get("approved") is not True:
        violations = list(report.get("violations") or [])[:8]
        detail = "; ".join(str(item) for item in violations) or "semantic review rejected revision"
        raise RuntimeError(f"semantic continuity review failed: {detail}")
    return {
        "semantic_review": "passed",
        "semantic_summary": str(report.get("summary") or "").strip()[:240],
    }


def rebuild_manuscript(project_dir: Path, state: dict[str, Any]) -> None:
    manuscript_dir = project_dir / "manuscript"
    chapters_dir = manuscript_dir / "chapters_txt"
    full_path = manuscript_dir / "full_novel.txt"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    for old_path in chapters_dir.glob("ch_*.txt"):
        old_path.unlink()

    chapters: list[dict[str, Any]] = []
    for item in sorted(
        state.get("completed_chapters", []),
        key=lambda value: int(value.get("chapter_number", 0) or 0),
    ):
        number = int(item.get("chapter_number", 0) or 0)
        draft_path = optional_file_path(item.get("draft_file"))
        if number <= 0 or draft_path is None:
            continue
        text = read_text(draft_path).strip()
        if not text:
            continue
        heading = normalize_heading_line(text.splitlines()[0], new_number=number)
        chapters.append({"number": number, "text": text, "heading": heading})

    write_text(full_path, "\n\n".join(item["text"] for item in chapters).rstrip() + "\n")
    manifest = []
    for item in chapters:
        number = int(item["number"])
        write_text(chapters_dir / f"ch_{number:04d}.txt", str(item["text"]).rstrip() + "\n")
        manifest.append(
            {
                "source_number": number,
                "output_number": number,
                "heading_line": item["heading"],
            }
        )
    write_text(chapters_dir / "chapters_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    state["manuscript_last_appended_chapter"] = len(chapters)


def run(args: argparse.Namespace) -> dict[str, Any]:
    project_dir = Path(args.project_dir).resolve()
    state_path = Path(args.state_file).resolve() if args.state_file else project_dir / "state.json"
    state = load_json(state_path, {})
    if not state:
        raise FileNotFoundError(f"state.json not found: {state_path}")

    chapters = sorted(
        [item for item in state.get("completed_chapters", []) if isinstance(item, dict)],
        key=lambda item: int(item.get("chapter_number", 0) or 0),
    )
    if args.skip_humanized:
        completed_numbers = {
            int(value)
            for value in state.get("humanized_chapters", [])
            if str(value).isdigit()
        }
        chapters = [
            item
            for item in chapters
            if int(item.get("chapter_number", 0) or 0) not in completed_numbers
        ]
    if args.from_chapter:
        chapters = [item for item in chapters if int(item.get("chapter_number", 0) or 0) >= args.from_chapter]
    if args.to_chapter:
        chapters = [item for item in chapters if int(item.get("chapter_number", 0) or 0) <= args.to_chapter]
    if args.limit:
        chapters = chapters[: args.limit]
    if not chapters:
        return {"project": project_dir.name, "status": "skipped", "reason": "no_matching_completed_chapters"}

    model = get_model_config_from_provider_model(args.model)
    protected_terms = project_protected_terms(project_dir)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    report_dir = Path(args.report_dir or project_dir / "humanizer_runs" / run_id).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir = report_dir / "candidates"
    if args.mode == "rewrite":
        candidate_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = report_dir / "backup"
    if args.apply:
        backup_dir.mkdir(parents=True, exist_ok=True)

    previous_summary = ""
    results: list[dict[str, Any]] = []
    changed = 0
    for index, item in enumerate(chapters, start=1):
        chapter_number = int(item.get("chapter_number", 0) or 0)
        title = str(item.get("title", "") or "").strip()
        draft_path = optional_file_path(item.get("draft_file"))
        summary_path = optional_file_path(item.get("summary_file"))
        if summary_path:
            previous_summary = read_text(summary_path).strip()
        if draft_path is None:
            results.append({"chapter_number": chapter_number, "status": "skipped", "reason": "draft_missing"})
            continue

        source = read_text(draft_path)
        body = strip_generated_draft_heading(chapter_number, title, source).strip()
        if not body:
            results.append({"chapter_number": chapter_number, "status": "skipped", "reason": "empty_body"})
            continue
        outline, plot = chapter_context(item, previous_summary)

        result: dict[str, Any] = {
            "chapter_number": chapter_number,
            "title": title,
            "source_chars": len(body),
        }
        print(f"[{index}/{len(chapters)}] chapter {chapter_number}: {title}", flush=True)
        try:
            if args.mode == "review":
                raw = call_model(
                    model,
                    system_prompt="你是中文网文语言质量审校员，只返回合法 JSON，不改剧情，不讨论AI检测。",
                    user_prompt=build_humanizer_review_prompt(
                        chapter_number=chapter_number,
                        title=title,
                        body=body,
                        outline=outline,
                        plot=plot,
                    ),
                    retry_interval_seconds=args.retry_interval_seconds,
                    max_retries=args.max_api_retries,
                )
                report = extract_json_payload(raw)
                result.update(
                    {
                        "status": "reviewed",
                        "quality_score": report.get("quality_score", 0),
                        "should_rewrite": bool(report.get("should_rewrite")),
                        "issues": list(report.get("issues") or [])[:8],
                        "rewrite_focus": list(report.get("rewrite_focus") or [])[:3],
                    }
                )
            else:
                chunks = split_body_for_humanizer(body, args.chunk_chars)
                revised_chunks: list[str] = []
                for chunk_index, chunk in enumerate(chunks, start=1):
                    raw = call_model(
                        model,
                        system_prompt="你是中文网文质量编辑，只做语言层改写，不能改变剧情事实和章节边界。",
                        user_prompt=build_humanizer_prompt(
                            chapter_number=chapter_number,
                            title=title,
                            body=chunk,
                            outline=outline,
                            plot=plot,
                            previous_summary=previous_summary,
                            protected_terms=protected_terms,
                            chunk_label=f"（第{chunk_index}/{len(chunks)}段）" if len(chunks) > 1 else "",
                        ),
                        retry_interval_seconds=args.retry_interval_seconds,
                        max_retries=args.max_api_retries,
                    )
                    revised_chunk = normalize_humanizer_output(
                        raw,
                        chapter_number=chapter_number,
                        title=title,
                    )
                    chunk_validation = validate_humanizer_revision(
                        chunk,
                        revised_chunk,
                        protected_terms=protected_terms,
                    )
                    if not chunk_validation.valid:
                        raise RuntimeError(
                            f"chunk {chunk_index} validation failed: {chunk_validation.reason}"
                        )
                    revised_chunks.append(revised_chunk)
                revised_body = "\n\n".join(item for item in revised_chunks if item).strip()
                validation = validate_humanizer_revision(
                    body,
                    revised_body,
                    protected_terms=protected_terms,
                )
                if not validation.valid:
                    raise RuntimeError(f"validation failed: {validation.reason}")
                continuity = semantic_review(
                    model,
                    chapter_number=chapter_number,
                    title=title,
                    source_body=body,
                    revised_body=revised_body,
                    outline=outline,
                    plot=plot,
                    previous_summary=previous_summary,
                    retry_interval_seconds=args.retry_interval_seconds,
                    max_retries=args.max_api_retries,
                )
                revised = normalize_chapter_draft_text(chapter_number, title, revised_body)
                integrity = assess_chapter_tail_integrity(revised_body)
                if integrity.get("high_confidence"):
                    raise RuntimeError(f"tail integrity failed: {integrity.get('reason')}")
                result.update(
                    {
                        "status": "validated",
                        "chunks": len(chunks),
                        "revised_chars": len(revised_body),
                        "tail_integrity": integrity.get("reason"),
                        **continuity,
                    }
                )
                candidate_path = candidate_dir / f"ch_{chapter_number:04d}.draft.md"
                write_text(candidate_path, revised)
                result["candidate_file"] = str(candidate_path)
                if args.apply:
                    backup_path = backup_dir / f"ch_{chapter_number:04d}.draft.md"
                    shutil.copy2(draft_path, backup_path)
                    write_text(draft_path, revised)
                    result["status"] = "applied"
                    changed += 1
                    state["humanized_chapters"] = sorted(
                        {
                            *[
                                int(value)
                                for value in state.get("humanized_chapters", [])
                                if str(value).isdigit()
                            ],
                            chapter_number,
                        }
                    )
                    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        except Exception as exc:
            result.update({"status": "failed", "error": str(exc)})
        results.append(result)
        if summary_path:
            previous_summary = read_text(summary_path).strip()
        if result.get("status") == "failed" and args.stop_on_failure:
            print(
                f"[STOP] chapter {chapter_number} failed; refusing to skip it",
                flush=True,
            )
            break

    if args.apply and changed:
        rebuild_manuscript(project_dir, state)
        history = list(state.get("humanizer_history") or [])
        history.append(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode": "whole_project_rewrite",
                "model": args.model,
                "run_id": run_id,
                "changed_chapters": changed,
                "report_dir": str(report_dir),
            }
        )
        state["humanizer_history"] = history[-100:]
        state["humanized_chapters"] = sorted(
            {
                *[int(value) for value in state.get("humanized_chapters", []) if str(value).isdigit()],
                *[int(item["chapter_number"]) for item in results if item.get("status") == "applied"],
            }
        )
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    payload = {
        "project": project_dir.name,
        "mode": args.mode,
        "apply": bool(args.apply),
        "model": args.model,
        "requested_chapters": len(chapters),
        "changed_chapters": changed,
        "failed_chapters": sum(1 for item in results if item.get("status") == "failed"),
        "report_dir": str(report_dir),
        "results": results,
    }
    write_text(report_dir / "report.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review or humanize completed novel chapters.")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--state-file", default="")
    parser.add_argument("--mode", choices=("review", "rewrite"), default="review")
    parser.add_argument("--model", default="fast/grok-4.6")
    parser.add_argument("--from-chapter", type=int, default=0)
    parser.add_argument("--to-chapter", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="stop at the first failed chapter so the outer queue can retry it",
    )
    parser.add_argument("--skip-humanized", action="store_true")
    parser.add_argument("--chunk-chars", type=int, default=7000)
    parser.add_argument("--retry-interval-seconds", type=int, default=1800)
    parser.add_argument(
        "--max-api-retries",
        type=int,
        default=0,
        help="0 means retry transient provider errors forever",
    )
    parser.add_argument("--report-dir", default="")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    parsed_args = parse_args()
    result_payload = run(parsed_args)
    print(json.dumps(result_payload, ensure_ascii=False, indent=2))
    if parsed_args.stop_on_failure and int(result_payload.get("failed_chapters", 0) or 0) > 0:
        raise SystemExit(1)
