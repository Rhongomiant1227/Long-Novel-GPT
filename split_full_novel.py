from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


CHAPTER_HEADING_RE = re.compile(r"(?m)^第\s*(\d+)\s*章[^\n]*$")


def split_full_novel(text: str) -> list[dict[str, object]]:
    matches = list(CHAPTER_HEADING_RE.finditer(text))
    chapters: list[dict[str, object]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        source_number = int(match.group(1))
        chapter_text = text[start:end].strip()
        if chapter_text:
            heading_line = chapter_text.splitlines()[0]
            chapters.append(
                {
                    "source_number": source_number,
                    "text": chapter_text,
                    "heading_line": heading_line,
                }
            )
    return chapters


def rewrite_heading_number(chapter_text: str, new_number: int) -> str:
    lines = chapter_text.splitlines()
    if not lines:
        return chapter_text
    lines[0] = CHAPTER_HEADING_RE.sub(
        lambda match: lines[0].replace(match.group(1), str(new_number), 1),
        lines[0],
        count=1,
    )
    return "\n".join(lines)


def clean_output_dir(output_dir: Path) -> None:
    for path in output_dir.glob("ch_*.txt"):
        path.unlink()


def build_manifest(chapters: list[dict[str, object]], renumber_sequentially: bool) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for index, chapter in enumerate(chapters, start=1):
        source_number = int(chapter["source_number"])
        output_number = index if renumber_sequentially else source_number
        manifest.append(
            {
                "source_number": source_number,
                "output_number": output_number,
                "heading_line": str(chapter["heading_line"]),
            }
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Split full_novel.txt into one .txt per chapter.")
    parser.add_argument("input_file", help="Path to full_novel.txt")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Directory for split chapter txt files. Defaults to <input parent>/chapters_txt",
    )
    parser.add_argument(
        "--renumber-sequentially",
        action="store_true",
        help="Rewrite chapter numbers to 1..N in output files and headings.",
    )
    parser.add_argument(
        "--clean-output",
        action="store_true",
        help="Delete existing ch_*.txt files in the output directory before writing.",
    )
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="Write chapters_manifest.json with source/output chapter numbers.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_file).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    output_dir = Path(args.output_dir).resolve() if args.output_dir else input_path.parent / "chapters_txt"
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.clean_output:
        clean_output_dir(output_dir)

    text = input_path.read_text(encoding="utf-8")
    chapters = split_full_novel(text)
    if not chapters:
        raise RuntimeError("No chapter headings matched. Expected lines like '第123章 标题'.")

    for index, chapter in enumerate(chapters, start=1):
        source_number = int(chapter["source_number"])
        chapter_number = index if args.renumber_sequentially else source_number
        chapter_text = str(chapter["text"])
        if args.renumber_sequentially:
            chapter_text = rewrite_heading_number(chapter_text, chapter_number)
        output_path = output_dir / f"ch_{chapter_number:04d}.txt"
        output_path.write_text(chapter_text.rstrip() + "\n", encoding="utf-8")

    if args.write_manifest:
        manifest_path = output_dir / "chapters_manifest.json"
        manifest_path.write_text(
            json.dumps(
                build_manifest(chapters, renumber_sequentially=args.renumber_sequentially),
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    print(f"Split {len(chapters)} chapters to: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
