from __future__ import annotations

import argparse
import json
import re
import unicodedata
from pathlib import Path


CHAPTER_NUMBER_TOKEN_RE = r'[\d零〇一二两三四五六七八九十百千万IVXLCDMivxlcdmⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+'
CHAPTER_HEADING_RE = re.compile(
    rf'(?mi)^(?:第\s*(?P<cn>{CHAPTER_NUMBER_TOKEN_RE})\s*章|(?:chapter|chap\.?)\s*(?P<en>{CHAPTER_NUMBER_TOKEN_RE})\b)[^\n]*$'
)
CHAPTER_HEADING_LINE_RE = re.compile(
    rf'^\s*(?:第\s*(?P<cn>{CHAPTER_NUMBER_TOKEN_RE})\s*章|(?:chapter|chap\.?)\s*(?P<en>{CHAPTER_NUMBER_TOKEN_RE})\b)(?:\s+|[：:，,、.．。—-]\s*)?(?P<title>.*)$',
    re.IGNORECASE,
)
CN_DIGIT_MAP = {
    '零': 0,
    '〇': 0,
    '一': 1,
    '二': 2,
    '两': 2,
    '三': 3,
    '四': 4,
    '五': 5,
    '六': 6,
    '七': 7,
    '八': 8,
    '九': 9,
}
ROMAN_NUMERAL_VALUES = {
    'I': 1,
    'V': 5,
    'X': 10,
    'L': 50,
    'C': 100,
    'D': 500,
    'M': 1000,
}


def parse_chinese_number_token(text: str) -> int | None:
    value = (text or '').strip().replace('〇', '零').replace('两', '二')
    if not value:
        return None
    allowed = set(CN_DIGIT_MAP) | {'十', '百', '千', '万'}
    if any(char not in allowed for char in value):
        return None
    if all(char in CN_DIGIT_MAP for char in value):
        total = 0
        for char in value:
            total = total * 10 + CN_DIGIT_MAP[char]
        return total

    unit_map = {'十': 10, '百': 100, '千': 1000, '万': 10000}
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in CN_DIGIT_MAP:
            number = CN_DIGIT_MAP[char]
            continue
        unit = unit_map[char]
        if unit == 10000:
            section = section + number
            total += (section or 1) * unit
            section = 0
            number = 0
            continue
        if number == 0:
            number = 1
        section += number * unit
        number = 0
    return total + section + number


def parse_roman_number_token(text: str) -> int | None:
    value = unicodedata.normalize('NFKC', (text or '').strip()).upper()
    if not value:
        return None
    if any(char not in ROMAN_NUMERAL_VALUES for char in value):
        return None
    total = 0
    previous = 0
    for char in reversed(value):
        current = ROMAN_NUMERAL_VALUES[char]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total or None


def parse_chapter_number_token(text: str) -> int | None:
    value = unicodedata.normalize('NFKC', (text or '').strip())
    if not value:
        return None
    if value.isdigit():
        return int(value)
    chinese_value = parse_chinese_number_token(value)
    if chinese_value is not None:
        return chinese_value
    return parse_roman_number_token(value)


def parse_heading_line(line: str) -> tuple[int | None, str]:
    match = CHAPTER_HEADING_LINE_RE.match((line or '').strip())
    if not match:
        return None, ''
    chapter_number = parse_chapter_number_token(match.group('cn') or match.group('en') or '')
    if chapter_number is None:
        return None, ''
    title = re.sub(r'\s+', ' ', match.group('title') or '').strip(' \t\r\n-—:：，,、.．。')
    return chapter_number, title


def normalize_heading_line(line: str, new_number: int | None = None) -> str:
    chapter_number, title = parse_heading_line(line)
    if chapter_number is None:
        return line
    number = chapter_number if new_number is None else new_number
    clean_title = title or '未命名章节'
    return f'第{number}章 {clean_title}'


def split_full_novel(text: str) -> list[dict[str, object]]:
    matches = list(CHAPTER_HEADING_RE.finditer(text))
    chapters: list[dict[str, object]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chapter_text = text[start:end].strip()
        if not chapter_text:
            continue
        heading_line = chapter_text.splitlines()[0]
        source_number, _ = parse_heading_line(heading_line)
        if source_number is None:
            continue
        chapters.append(
            {
                "source_number": source_number,
                "text": chapter_text,
                "heading_line": normalize_heading_line(heading_line),
            }
        )
    return chapters


def rewrite_heading_number(chapter_text: str, new_number: int) -> str:
    lines = chapter_text.splitlines()
    if not lines:
        return chapter_text
    lines[0] = normalize_heading_line(lines[0], new_number=new_number)
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
                "heading_line": normalize_heading_line(str(chapter["heading_line"]), new_number=output_number),
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
        chapter_text = rewrite_heading_number(str(chapter["text"]), chapter_number)
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
