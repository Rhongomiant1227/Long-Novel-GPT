"""Expose the classic editor's YAML books through the workstation's document tree."""

import re
from functools import lru_cache
from pathlib import Path

import yaml

from split_full_novel import parse_heading_line


ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / 'frontend' / 'data' / 'example_novels'
LAST_IMPORT = ROOT / 'tmp.yaml'


def book_sources():
    for path in sorted(SAMPLES.glob('*.yaml')):
        yield 'example:' + path.stem, path, 'example'
    if LAST_IMPORT.is_file():
        yield 'import:latest', LAST_IMPORT, 'import'


def chunk_texts(section):
    return (chunk[1] for chunk in section.get('chunks', [])
            if isinstance(chunk, list) and len(chunk) >= 2 and isinstance(chunk[1], str))


@lru_cache(maxsize=16)
def read_book(book_id, path, source, modified_ns, size):
    with path.open(encoding='utf-8-sig') as handle:
        data = yaml.load(handle, Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
    if not isinstance(data, dict) or not isinstance(data.get('outline'), dict):
        raise ValueError('作品资料尚未准备好')
    outline = data['outline']
    plot = data.get('plot') or {}
    draft = data.get('draft') or {}
    outline_text = outline.get('context') or ''
    description = data.get('description') or ''
    name = path.stem
    if source == 'import':
        title = re.match(r'\s*《([^》\n]+)》', outline_text)
        name = data.get('title') or (title.group(1) if title else '最近导入的小说')

    documents = {}
    overview = []
    chapters = []
    total_chars = 0

    def add_file(nodes, key, label, kind, value):
        value = value.replace('\r\n', '\n').strip()
        if value:
            file = {'kind': 'file', 'path': key, 'label': label, 'type': kind, 'characters': len(value)}
            nodes.append(file)
            documents[key] = {**file, 'content': value, 'truncated': False}
        return len(value)

    add_file(overview, 'brief', '作品简介', 'brief', description)
    add_file(overview, 'outline', '全书大纲', 'outline', outline_text)
    chapter_outlines = {}
    for value in chunk_texts(outline):
        heading = value.strip().partition('\n')[0].strip()
        if heading:
            chapter_outlines[heading] = value
    headings = dict.fromkeys([*chapter_outlines, *plot, *draft])
    for index, heading in enumerate(headings, 1):
        number, title = parse_heading_line(heading)
        number = number or index
        files = []
        prefix = f'chapters/{index}/'
        total_chars += add_file(files, prefix + 'draft', '正文', 'draft', ''.join(chunk_texts(draft.get(heading, {}))))
        plot_section = plot.get(heading, {})
        add_file(files, prefix + 'outline', '章节大纲', 'outline', chapter_outlines.get(heading) or plot_section.get('context') or '')
        add_file(files, prefix + 'plot', '剧情梗概', 'plot', ''.join(chunk_texts(plot_section)))
        if files:
            chapters.append({'kind': 'chapter', 'type': 'chapter', 'chapter_number': number,
                             'title': title or heading, 'label': heading, 'children': files})
    chapters.sort(key=lambda chapter: chapter['chapter_number'])
    tree = [{'type': 'overview', 'kind': 'group', 'label': '作品资料', 'children': overview}]
    if chapters:
        tree.append({'type': 'volumes', 'kind': 'group', 'label': '章节', 'children': [
            {'type': 'volume', 'kind': 'volume', 'volume_number': 1, 'label': '章节目录', 'children': chapters},
        ]})
    summary = {
        'id': book_id, 'source': source, 'display_name': name, 'description': description,
        'generated_chapters': len(chapters), 'generated_chars': total_chars,
    }
    return summary, tree, documents


def library_book(book_id):
    for key, path, source in book_sources():
        if key == book_id:
            stat = path.stat()
            try:
                return read_book(key, path, source, stat.st_mtime_ns, stat.st_size)
            except yaml.YAMLError as exc:
                raise ValueError('作品资料正在更新，请稍后重试') from exc
    raise FileNotFoundError('作品不存在')


def list_library_books():
    books = []
    for key, path, source in book_sources():
        try:
            stat = path.stat()
            summary, _, _ = read_book(key, path, source, stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError, yaml.YAMLError):
            # The classic import can be rewriting its snapshot while the shelf is opened.
            continue
        books.append(summary)
    return books
