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
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterable

from backend.backend_utils import get_model_config_from_provider_model
from core.draft_writer import DraftWriter
from core.novel_memory_retrieval import NovelMemoryRetrieval
from core.outline_writer import OutlineWriter
from core.plot_writer import PlotWriter
from core.writer_utils import KeyPointMsg
from llm_api import ModelConfig, stream_chat
from llm_api.chat_messages import ChatMessages
from split_full_novel import normalize_heading_line, split_full_novel


PROJECT_TEXT_REPLACEMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    'lychee_prelude': (
        ('\u82b1\u82bd\u8354\u679d', '\u8354\u679d'),
        ('\u82b1\u82bd', '\u8354\u679d'),
    ),
}

PROJECT_ROLE_ONLY_RULES: dict[str, dict[str, Any]] = {
    'watcher_origin_short': {
        'allowed_names': ('神木',),
        'preserved_labels': ('守夜人',),
        'role_labels': ('主管', '风控员', '值班同事', '接线员', '求助者', '审计员', '急救员', '男人', '对方'),
        'fallback_label': '那名同事',
    },
}


DEFAULT_SYSTEM_PROMPT = "你是资深中文网文总编、商业策划和长篇连载统筹。"

MEMORY_RETRIEVAL_ROOM_LABELS = {
    'brief': '立项设定',
    'series_bible': '完整圣经',
    'series_bible_short': '圣经短版',
    'story_memory': '故事记忆',
    'chapter_summary': '章节摘要',
}

ENDING_CRAFT_RESEARCH_SUMMARY = """
【外部收尾经验摘要】
- 读者对故事的最终记忆会被峰值场面与最后收束显著放大；高潮之后若继续重复落锤、反复解释，会快速磨损整本书的尾劲。
- 高评价结尾通常同时满足两个条件：一是“来自前文，因而成立”，二是“在最后一击里略高于预期”，也就是意料之外但情理之中。
- 结尾最忌讳把情绪峰值写成说明会、条款会、总结会；解释可以有，但要服务最后的画面、选择、代价与反照，而不是替代它们。
- 长篇连载收尾要避免同构验证反复出现。更稳的结构通常是：一次真正落锤、一次普通层面的现实验证、一个极短的余韵画面。
- 余味不是靠关键义务悬而不决制造出来的，而是在主线已兑现后，只留下少量让读者自己停留和回想的空间。
- 最后一屏最好是一个可停留的具体画面、动作、选择或反讽，而不是“从此以后制度如何运行”的继续讲解。
""".strip()

CHAPTER_TERMINAL_TAIL_CHARS = frozenset('。！？!?…」』】）》〉）〕”’"\'')
CHAPTER_DANGLING_TAIL_SUFFIXES = (
    '的', '了', '在', '把', '将', '向', '跟', '和', '与', '及', '或', '并', '便', '却',
    '又', '更', '还', '正', '仍', '像', '让', '给', '对', '从', '往', '朝', '到',
    '这', '那', '这时', '此时', '其中',
)
COUNTDOWN_TAIL_RE = re.compile(r'^\d{1,2}:\d{2}(?::\d{2})?$')
PROGRESS_TAIL_KEYWORDS = ('进行中', '进度')


def now_str() -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S')


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_text(path: Path, default: str = '') -> str:
    if not path.exists():
        return default
    return path.read_text(encoding='utf-8')


def project_name_from_path(path: Path | None) -> str:
    if path is None:
        return ''
    parts = path.parts
    lowered = [part.lower() for part in parts]
    if 'auto_projects' not in lowered:
        return ''
    index = lowered.index('auto_projects')
    if index + 1 >= len(parts):
        return ''
    return parts[index + 1]


def lookup_project_override(mapping: dict[str, Any], project_name: str) -> Any:
    if not mapping or not project_name:
        return None
    if project_name in mapping:
        return mapping[project_name]
    best_key = ''
    best_value = None
    for key, value in mapping.items():
        if project_name.startswith(f'{key}_') and len(key) > len(best_key):
            best_key = key
            best_value = value
    return best_value


def sanitize_project_text(text: str, *, project_name: str = '', path: Path | None = None) -> str:
    if not text:
        return text
    target_project = project_name or project_name_from_path(path)
    replacements = lookup_project_override(PROJECT_TEXT_REPLACEMENTS, target_project) or ()
    if not replacements:
        return text
    sanitized = text
    for source, target in replacements:
        sanitized = sanitized.replace(source, target)
    return sanitized


def write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    sanitized = sanitize_project_text(text, path=path)
    tmp_path = path.with_name(f'.{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    try:
        with tmp_path.open('w', encoding='utf-8', newline='') as handle:
            handle.write(sanitized)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def replace_with_retry(tmp_path: Path, target_path: Path, *, attempts: int = 8, base_delay: float = 0.15) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            tmp_path.replace(target_path)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt >= attempts - 1:
                raise
            time.sleep(base_delay * (attempt + 1))
    if last_error is not None:
        raise last_error


def assess_chapter_tail_integrity(body_text: str) -> dict[str, Any]:
    lines = [line.strip() for line in (body_text or '').splitlines() if line.strip()]
    if not lines:
        return {
            'suspicious': True,
            'high_confidence': True,
            'reason': 'empty_body',
            'tail': '',
        }

    tail = lines[-1].strip()
    if not tail:
        return {
            'suspicious': True,
            'high_confidence': True,
            'reason': 'empty_tail',
            'tail': '',
        }

    if COUNTDOWN_TAIL_RE.fullmatch(tail):
        return {
            'suspicious': True,
            'high_confidence': False,
            'reason': 'countdown_tail',
            'tail': tail,
        }

    if '%' in tail and any(keyword in tail for keyword in PROGRESS_TAIL_KEYWORDS):
        return {
            'suspicious': True,
            'high_confidence': False,
            'reason': 'progress_tail',
            'tail': tail,
        }

    if tail.endswith('——') or tail.endswith('—'):
        return {
            'suspicious': False,
            'high_confidence': False,
            'reason': 'cliffhanger_dash_tail',
            'tail': tail,
        }

    if tail[-1] in CHAPTER_TERMINAL_TAIL_CHARS or re.search(r'(?:\.{3,}|…+)$', tail):
        return {
            'suspicious': False,
            'high_confidence': False,
            'reason': 'complete',
            'tail': tail,
        }

    if len(tail) <= 4:
        reason = 'very_short_tail'
    elif re.search(r'[，、：；,:（(【「『《“‘—-]$', tail):
        reason = 'open_clause_tail'
    elif (
        tail.count('“') > tail.count('”')
        or tail.count('‘') > tail.count('’')
        or tail.count('「') > tail.count('」')
        or tail.count('『') > tail.count('』')
    ):
        reason = 'unclosed_quote_tail'
    elif any(tail.endswith(token) for token in CHAPTER_DANGLING_TAIL_SUFFIXES):
        reason = 'dangling_suffix_tail'
    else:
        reason = 'mid_sentence_tail'

    return {
        'suspicious': True,
        'high_confidence': True,
        'reason': reason,
        'tail': tail,
    }


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


def ending_craft_research_text(limit: int = 1200) -> str:
    return truncate_text(ENDING_CRAFT_RESEARCH_SUMMARY, limit)


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
        'conservative_remaining_chapters': 0,
        'conservative_remaining_chars': 0,
        'estimate_note': '',
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


def derive_conservative_completion_estimate(
    report: dict,
    history: list[dict],
    current_chapter: int,
    average_chapter_chars: int,
) -> dict:
    optimistic_chapters = max(0, int(report.get('remaining_chapters', 0) or 0))
    optimistic_chars = max(0, int(report.get('remaining_chars', 0) or 0))
    if report.get('is_complete'):
        return {
            'conservative_remaining_chapters': 0,
            'conservative_remaining_chars': 0,
            'estimate_note': '已判定可自然完结，无需保守余量。',
        }

    missing = [str(item).strip() for item in (report.get('missing') or []) if str(item).strip()]
    major_count = 0
    minor_count = 0
    epilogue_missing = False
    afterstory_missing = False
    for item in missing:
        if '尾声' in item:
            epilogue_missing = True
            continue
        if '后日谈' in item or '后记' in item:
            afterstory_missing = True
            continue
        if any(keyword in item for keyword in (
            '核心', '主线', '谜团', '真相', '源头', '责任链', '终局', '落判',
            '主角', '真名', '原位', '资格', '命运', '胜负', '第一页', '首字',
            '主签', '首签', '规则级',
        )):
            major_count += 1
        else:
            minor_count += 1

    structural_floor = (
        major_count
        + math.ceil(minor_count / 2)
        + int(epilogue_missing)
        + int(afterstory_missing)
    )

    same_estimate_streak = 1 if optimistic_chapters > 0 else 0
    earliest_same_estimate_chapter = current_chapter
    for item in reversed(history):
        if item.get('is_complete'):
            break
        if int(item.get('remaining_chapters', 0) or 0) != optimistic_chapters:
            break
        same_estimate_streak += 1
        earliest_same_estimate_chapter = int(item.get('checked_at_chapter', current_chapter) or current_chapter)

    stagnation_buffer = 0
    if optimistic_chapters > 0 and same_estimate_streak > 1:
        consumed = max(0, current_chapter - earliest_same_estimate_chapter)
        stagnation_buffer = max(same_estimate_streak - 1, math.ceil(consumed / max(optimistic_chapters, 1)))

    conservative_chapters = max(optimistic_chapters, structural_floor, optimistic_chapters + stagnation_buffer)
    if conservative_chapters <= 0:
        conservative_chapters = max(1, structural_floor or 1)

    average_chars = max(1200, int(average_chapter_chars or 0))
    conservative_chars = max(optimistic_chars, conservative_chapters * average_chars)

    reasons = []
    if major_count:
        reasons.append(f'{major_count}项核心终局/真相收束未完成')
    if minor_count:
        reasons.append(f'{minor_count}项关系或配角收束未完成')
    if epilogue_missing:
        reasons.append('尾声尚未写出')
    if afterstory_missing:
        reasons.append('后日谈尚未写出')
    if same_estimate_streak > 1 and optimistic_chapters > 0:
        reasons.append(
            f'“还需{optimistic_chapters}章”的乐观估计已连续{same_estimate_streak}次未下降'
        )
    if not reasons:
        reasons.append('按最近章节体量补足安全收尾余量')

    return {
        'conservative_remaining_chapters': conservative_chapters,
        'conservative_remaining_chars': conservative_chars,
        'estimate_note': '；'.join(reasons),
    }


LONG_SERIES_BIBLE_PART_SPECS = [
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

SHORT_STORY_BIBLE_PART_SPECS = [
    (
        '故事核与最后一屏',
        '本部分只负责：推荐书名/备选书名、核心卖点、一句话梗概、作品气质、主题反差、最后一屏意象。'
        '要求锋利具体，目标 1000~1600 字。',
    ),
    (
        '人物关系与生活残片',
        '本部分只负责：主角、被救者、关键配角、私人亏欠、关系张力、必须保留的生活残片、关键物件/动作。'
        '要求人物先成立，目标 1000~1600 字。',
    ),
    (
        '冲突推进与收束禁忌',
        '本部分只负责：主冲突、关键场景推进、情绪递进、真相落点、结尾收束方式、文风提醒与禁忌清单。'
        '要求服务单篇闭环，目标 1000~1600 字。',
    ),
]


CHAPTER_NUMBER_TOKEN_RE = r'[\d零〇一二两三四五六七八九十百千万IVXLCDMivxlcdmⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+'
MAX_CHAPTER_TITLE_LENGTH = 30
MAX_EXTRACTED_TITLE_LENGTH = 12
CHAPTER_HEADING_PATTERNS = (
    re.compile(
        rf'^\s*第\s*(?P<number>{CHAPTER_NUMBER_TOKEN_RE})\s*章(?:\s+|[：:，,、.．。—-]\s*)?(?P<title>.*)$',
        re.IGNORECASE,
    ),
    re.compile(
        rf'^\s*(?:chapter|chap\.?)\s*(?P<number>{CHAPTER_NUMBER_TOKEN_RE})\b(?:\s+|[：:，,、.．。—-]\s*)?(?P<title>.*)$',
        re.IGNORECASE,
    ),
)
ROMAN_NUMERAL_VALUES = {
    'I': 1,
    'V': 5,
    'X': 10,
    'L': 50,
    'C': 100,
    'D': 500,
    'M': 1000,
}
TITLE_SENTENCE_SPLIT_RE = re.compile(r'[，,。！？；：]')
TITLE_OUTER_PUNCTUATION = ' \t\r\n-—:：，,、.．。;；“”‘’"\'《》()（）[]【】'
TITLE_NOISE_PREFIXES = (
    '本章核心目标是',
    '本章目标是',
    '本章核心任务是',
    '本章任务是',
    '章节标题',
    '章末钩子是',
    '章末钩子',
    '阶段性爽点在于',
    '阶段性爽点',
    '人物关系推进上',
    '人物关系上',
    '推进上',
    '阻力来自',
    '变化在于',
    '已锁定的',
    '刚写入的',
    '刚挂起的',
    '回传的',
    '抢下的',
    '追出的',
    '解出的',
    '暴露出的',
    '发现的',
    '逼出的',
    '逼近的',
    '浮出的',
    '跳出的',
    '卡住的',
    '留下的',
    '完成的',
    '形成的',
    '立下的',
    '吐出的',
    '末尾露出的',
    '得到的',
    '出现的',
    '确认的',
)
GENERIC_TITLE_EXACT = {
    '本章目标',
    '本章大纲',
    '本章大纲内容',
    '本章内容',
    '当前目标',
    '明确目标',
    '章节标题',
    '线索',
    '目标',
    '内容',
}
GENERIC_TITLE_PREFIXES = (
    '本章',
    '当前',
    '阶段',
    '明确',
    '核心',
    '主要',
    '本回',
    '本节',
)


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


def parse_chapter_heading_line(text: str) -> tuple[int | None, str]:
    line = re.sub(r'\s+', ' ', (text or '').strip())
    if not line:
        return None, ''
    for pattern in CHAPTER_HEADING_PATTERNS:
        match = pattern.match(line)
        if not match:
            continue
        chapter_number = parse_chapter_number_token(match.group('number'))
        if chapter_number is None:
            continue
        title = match.group('title') or ''
        title = re.sub(r'\s+', ' ', title).strip(' \t\r\n-—:：，,、.．。')
        return chapter_number, title
    return None, line


def clean_title_fragment(text: str) -> str:
    normalized = re.sub(r'\s+', ' ', text or '').strip()
    return normalized.strip(TITLE_OUTER_PUNCTUATION)


def is_preferred_title_candidate(text: str, *, allow_long: bool = False) -> bool:
    candidate = clean_title_fragment(text)
    if not candidate:
        return False
    if candidate in GENERIC_TITLE_EXACT:
        return False
    if any(candidate.startswith(prefix) for prefix in GENERIC_TITLE_PREFIXES):
        return False
    if candidate.endswith(('目标', '内容')) and len(candidate) <= 6:
        return False
    if not allow_long and len(candidate) > MAX_EXTRACTED_TITLE_LENGTH:
        return False
    return True


def strip_title_noise_prefix(text: str) -> str:
    current = clean_title_fragment(text)
    previous = ''
    while current and current != previous:
        previous = current
        for prefix in TITLE_NOISE_PREFIXES:
            if current.startswith(prefix) and len(current) - len(prefix) >= 2:
                current = clean_title_fragment(current[len(prefix):])
                break
        else:
            generic_prefix = re.match(r'^[^，,。！？；：]{1,8}?的(.+)$', current)
            if generic_prefix and len(generic_prefix.group(1).strip()) >= 2:
                current = clean_title_fragment(generic_prefix.group(1))
                continue
            break
    return current


def extract_quoted_title(text: str) -> str:
    for pattern in (
        r'“([^”]{2,30})”',
        r'"([^"]{2,30})"',
        r'《([^》]{2,30})》',
    ):
        for match in re.finditer(pattern, text or ''):
            candidate = clean_title_fragment(match.group(1))
            if 1 < len(candidate) <= MAX_CHAPTER_TITLE_LENGTH and is_preferred_title_candidate(candidate):
                return candidate
    return ''


def safe_title(text: str) -> str:
    first_line = ''
    for line in text.splitlines():
        line = line.strip()
        if line:
            first_line = line
            break
    if not first_line:
        return '未命名章节'

    _, parsed_title = parse_chapter_heading_line(first_line)
    candidate = clean_title_fragment(parsed_title or first_line)
    if not candidate:
        return '未命名章节'

    sentence_like = (
        len(candidate) > MAX_CHAPTER_TITLE_LENGTH
        or candidate.count('，') >= 1
        or any(mark in candidate for mark in ('。', '；', '：'))
    )
    if sentence_like:
        quoted_title = extract_quoted_title(candidate)
        if quoted_title:
            return quoted_title

    candidate = strip_title_noise_prefix(candidate)
    if (
        candidate
        and len(candidate) <= MAX_CHAPTER_TITLE_LENGTH
        and candidate.count('，') == 0
        and not any(mark in candidate for mark in ('。', '；', '：'))
    ):
        return candidate

    clauses = [
        clean_title_fragment(part)
        for part in TITLE_SENTENCE_SPLIT_RE.split(candidate)
        if clean_title_fragment(part)
    ]
    for clause in clauses:
        quoted_title = extract_quoted_title(clause)
        if quoted_title:
            return quoted_title
        clause = strip_title_noise_prefix(clause)
        clause = re.sub(r'(?:开启时|发生时|当场|之后|以前|之时|当下)$', '', clause).strip()
        clause = clean_title_fragment(clause)
        if 1 < len(clause) <= MAX_CHAPTER_TITLE_LENGTH:
            return clause

    fallback = clean_title_fragment(strip_title_noise_prefix(candidate))
    if not fallback:
        return '未命名章节'
    if len(fallback) > MAX_CHAPTER_TITLE_LENGTH:
        fallback = clean_title_fragment(fallback[:MAX_CHAPTER_TITLE_LENGTH])
    return fallback or '未命名章节'


def normalize_outline_text(chapter_number: int, text: str) -> str:
    source = (text or '').strip()
    if not source:
        raise RuntimeError(f'第{chapter_number}章大纲为空，无法归一化。')

    lines = source.splitlines()
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        raise RuntimeError(f'第{chapter_number}章大纲为空白，无法归一化。')

    first_line = lines[first_index].strip()
    _, parsed_title = parse_chapter_heading_line(first_line)
    title = safe_title(parsed_title or first_line)
    heading = format_chapter_heading(chapter_number, title)

    body = '\n'.join(lines[first_index + 1:]).strip()
    if not body and parsed_title:
        raw_outline = clean_title_fragment(parsed_title)
        if normalize_heading_compare_text(raw_outline) != normalize_heading_compare_text(title):
            body = raw_outline
    elif not body and not parsed_title:
        raw_outline = clean_title_fragment(source)
        if normalize_heading_compare_text(raw_outline) != normalize_heading_compare_text(title):
            body = raw_outline

    if body:
        return f'{heading}\n{body}'.strip()
    return heading


def rewrite_outline_heading(
    chapter_number: int,
    title: str,
    text: str,
) -> str:
    heading = format_chapter_heading(chapter_number, title)
    source = (text or '').strip()
    if not source:
        return heading

    lines = source.splitlines()
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        return heading

    body = '\n'.join(lines[first_index + 1:]).strip()
    if body:
        return f'{heading}\n{body}'.strip()
    return heading


def format_chapter_heading(chapter_number: int, title: str) -> str:
    return f'第{chapter_number}章 {safe_title(title)}'


def iter_title_candidate_fragments(text: str) -> list[str]:
    source = (text or '').strip()
    if not source:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        candidate = clean_title_fragment(raw)
        normalized = normalize_heading_compare_text(candidate)
        if not candidate or not normalized or normalized in seen:
            return
        if not is_preferred_title_candidate(candidate):
            return
        seen.add(normalized)
        candidates.append(candidate)

    for pattern in (
        r'“([^”]{2,30})”',
        r'"([^"]{2,30})"',
        r'《([^》]{2,30})》',
    ):
        for match in re.finditer(pattern, source):
            add(match.group(1))

    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        _, parsed_title = parse_chapter_heading_line(stripped)
        base = parsed_title or stripped
        add(base)
        for part in TITLE_SENTENCE_SPLIT_RE.split(base):
            cleaned = strip_title_noise_prefix(part)
            if cleaned:
                add(cleaned)

    return candidates


def ensure_unique_chapter_title(
    chapter_number: int,
    preferred_title: str,
    existing_titles: Iterable[str],
    source_text: str = '',
) -> str:
    normalized_existing = {
        normalize_heading_compare_text(item)
        for item in (existing_titles or [])
        if normalize_heading_compare_text(item)
    }

    candidate_titles: list[str] = []
    seen_candidates: set[str] = set()

    def add_candidate(raw: str, *, allow_long: bool = False) -> None:
        candidate = safe_title(raw)
        normalized = normalize_heading_compare_text(candidate)
        if not candidate or not normalized or normalized in seen_candidates:
            return
        if not is_preferred_title_candidate(candidate, allow_long=allow_long):
            return
        seen_candidates.add(normalized)
        candidate_titles.append(candidate)

    add_candidate(preferred_title, allow_long=True)
    for fragment in iter_title_candidate_fragments(source_text):
        add_candidate(fragment)

    for candidate in candidate_titles:
        if normalize_heading_compare_text(candidate) not in normalized_existing:
            return candidate

    fallback = candidate_titles[0] if candidate_titles else safe_title(preferred_title or source_text)
    suffix = f'·{chapter_number}'
    available = max(1, MAX_CHAPTER_TITLE_LENGTH - len(suffix))
    fallback = clean_title_fragment(fallback)[:available].strip()
    fallback = clean_title_fragment(fallback) or '未命名章节'
    return f'{fallback}{suffix}'


def extract_json_payload(text: str) -> dict[str, Any]:
    raw = (text or '').strip()
    if not raw:
        raise ValueError('LLM did not return JSON content.')

    fenced_match = re.search(r'```(?:json)?\s*(\{.*\})\s*```', raw, re.DOTALL | re.IGNORECASE)
    if fenced_match:
        raw = fenced_match.group(1).strip()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        brace_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not brace_match:
            raise
        payload = json.loads(brace_match.group(0))

    if not isinstance(payload, dict):
        raise ValueError('JSON response must be an object.')
    return payload


def normalize_heading_compare_text(text: str) -> str:
    normalized = unicodedata.normalize('NFKC', text or '').strip().lower()
    normalized = normalized.strip('#')
    normalized = normalized.replace('《', '').replace('》', '')
    normalized = re.sub(r'[\s:：\-—_]+', '', normalized)
    return normalized


def normalize_chapter_draft_text(
    chapter_number: int,
    title: str,
    text: str,
    heading_mode: str = 'chapter',
) -> str:
    heading_mode = str(heading_mode or 'chapter').strip().lower()
    if heading_mode == 'title_only':
        heading = (title or '').strip() or format_chapter_heading(chapter_number, title)
    else:
        heading = format_chapter_heading(chapter_number, title)
    source = (text or '').strip()
    body = source
    lines = source.splitlines()
    normalized_title = normalize_heading_compare_text(title)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        parsed_number, _ = parse_chapter_heading_line(line)
        if parsed_number is not None:
            body = '\n'.join(lines[index + 1:]).strip()
        elif heading_mode == 'title_only':
            normalized_line = normalize_heading_compare_text(line)
            if normalized_line and normalized_line == normalized_title:
                body = '\n'.join(lines[index + 1:]).strip()
        break
    if not body:
        body = source
    if not body:
        raise RuntimeError(f'第{chapter_number}章正文为空，无法写入。')
    return f'{heading}\n\n{body}'.strip()


def strip_generated_draft_heading(
    chapter_number: int,
    title: str,
    text: str,
) -> str:
    source = (text or '').strip()
    if not source:
        return ''

    body = source
    lines = source.splitlines()
    normalized_title = normalize_heading_compare_text(title)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        parsed_number, _ = parse_chapter_heading_line(line)
        if parsed_number is not None:
            body = '\n'.join(lines[index + 1:]).strip()
        else:
            normalized_line = normalize_heading_compare_text(line)
            if normalized_line and normalized_line == normalized_title:
                body = '\n'.join(lines[index + 1:]).strip()
        break

    return body or source


def summarize_critic_issue(issue: dict[str, Any]) -> str:
    issue_type = str(issue.get('type', 'objective_logic')).strip() or 'objective_logic'
    explanation = str(issue.get('explanation', '')).strip()
    excerpt = str(issue.get('excerpt', '')).strip()
    snippet = explanation or excerpt or '未提供详情'
    snippet = re.sub(r'\s+', ' ', snippet)
    if len(snippet) > 36:
        snippet = snippet[:36] + '...'
    return f'{issue_type}:{snippet}'


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
EXPLICIT_COUNT_CLAIM_RE = re.compile(r'(?P<prefix>[这那]?)(?P<count>[零〇一二三四五六七八九十两百\d]+)个字')


def parse_small_number(text: str) -> int:
    value = (text or '').strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    value = value.replace('〇', '零').replace('两', '二')
    while value.startswith('零'):
        value = value[1:]
    if not value:
        return 0
    if value in CN_DIGIT_MAP:
        return CN_DIGIT_MAP[value]
    if '百' in value:
        head, tail = value.split('百', 1)
        head_value = CN_DIGIT_MAP.get(head, 1 if head == '' else 0)
        return head_value * 100 + parse_small_number(tail)
    if '十' in value:
        head, tail = value.split('十', 1)
        head_value = CN_DIGIT_MAP.get(head, 1 if head == '' else 0)
        tail_value = parse_small_number(tail)
        return head_value * 10 + tail_value
    total = 0
    for char in value:
        if char not in CN_DIGIT_MAP:
            return 0
        total = total * 10 + CN_DIGIT_MAP[char]
    return total


def number_to_chinese(value: int) -> str:
    if value < 0:
        raise ValueError('value must be >= 0')
    if value < 10:
        return '零一二三四五六七八九'[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        if tens == 1:
            prefix = '十'
        else:
            prefix = f'{"零一二三四五六七八九"[tens]}十'
        if ones == 0:
            return prefix
        return prefix + '零一二三四五六七八九'[ones]
    if value < 1000:
        hundreds, rem = divmod(value, 100)
        prefix = f'{"零一二三四五六七八九"[hundreds]}百'
        if rem == 0:
            return prefix
        if rem < 10:
            return prefix + '零' + number_to_chinese(rem)
        return prefix + number_to_chinese(rem)
    return str(value)


def render_number_like(template: str, value: int) -> str:
    template = (template or '').strip()
    if template.isdigit():
        return str(value)
    if value == 2:
        return '两'
    return number_to_chinese(value)


def count_meaningful_chars(text: str) -> int:
    return len(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]', text or ''))


def _line_bounds_for_index(text: str, index: int) -> tuple[int, int]:
    start = text.rfind('\n', 0, index) + 1
    end = text.find('\n', index)
    if end == -1:
        end = len(text)
    return start, end


def _previous_nonempty_line(text: str, before_index: int) -> str:
    cursor = max(0, before_index)
    while cursor > 0:
        line_end = cursor
        line_start = text.rfind('\n', 0, line_end - 1) + 1 if line_end > 0 else 0
        line = text[line_start:line_end].strip()
        if line:
            return line
        cursor = max(0, line_start - 1)
    return ''


def _next_nonempty_line(text: str, after_index: int) -> str:
    cursor = max(0, after_index)
    length = len(text)
    while cursor < length:
        line_end = text.find('\n', cursor)
        if line_end == -1:
            line_end = length
        line = text[cursor:line_end].strip()
        if line:
            return line
        cursor = line_end + 1
    return ''


def _next_nonempty_lines(text: str, after_index: int, limit: int = 2) -> list[str]:
    lines: list[str] = []
    cursor = max(0, after_index)
    length = len(text)
    while cursor < length and len(lines) < limit:
        line_end = text.find('\n', cursor)
        if line_end == -1:
            line_end = length
        line = text[cursor:line_end].strip()
        if line:
            lines.append(line)
        cursor = line_end + 1
    return lines


def _extract_candidate_from_fragment(fragment: str) -> str:
    candidate = re.sub(r'^[\s:：—\-“”"‘’「」『』（）()]+', '', fragment or '').strip()
    if not candidate:
        return ''
    line = candidate.splitlines()[0].strip()
    return line[:80]


def _extract_tail_candidate_from_line(line: str) -> str:
    text = (line or '').strip()
    if not text:
        return ''
    for separator in ('——', '—', ':', '：'):
        if separator in text:
            tail = text.rsplit(separator, 1)[-1].strip()
            if tail:
                return tail
    return text


def _extract_inline_reference_candidate(prefix_text: str) -> str:
    text = (prefix_text or '').strip()
    if not text:
        return ''
    parts = [part.strip() for part in re.split(r'[，,。！？；;：:“”"‘’「」『』（）()\s]+', text) if part.strip()]
    if not parts:
        return ''
    candidate = parts[-1]
    if 0 < count_meaningful_chars(candidate) <= 12:
        return candidate
    return ''


def _extract_previous_reference_candidate(text: str, line_start: int) -> str:
    prev_line = _previous_nonempty_line(text, line_start - 1)
    if not prev_line:
        return ''
    tail = _extract_tail_candidate_from_line(prev_line)
    tail_count = count_meaningful_chars(tail)
    prev_count = count_meaningful_chars(prev_line)
    if tail and tail_count <= 12 and (tail != prev_line or prev_count <= 12):
        return tail
    return ''


def scan_explicit_count_mismatch_issues(text: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for match in EXPLICIT_COUNT_CLAIM_RE.finditer(text or ''):
        expected = parse_small_number(match.group('count'))
        if expected <= 0:
            continue

        line_start, line_end = _line_bounds_for_index(text, match.start())
        current_line = text[line_start:line_end]
        current_line_stripped = current_line.strip()
        line_before_match = current_line[:match.start() - line_start]
        line_after_match = current_line[match.end() - line_start:]
        candidate = ''
        pattern_type = ''

        if re.match(r'^\s*[：:—\-“"‘「『]', line_after_match):
            candidate = _extract_candidate_from_fragment(line_after_match)
            if candidate:
                pattern_type = 'inline_delimited'

        if not candidate:
            stripped_after = line_after_match.strip()
            if stripped_after == '' or re.fullmatch(r'[：:—\-]+', stripped_after):
                next_lines = _next_nonempty_lines(text, line_end + 1, limit=2)
                if next_lines:
                    candidate = next_lines[0]
                    if len(next_lines) > 1 and re.match(r'^(或是|或者|抑或|亦或|还是)', next_lines[1]):
                        candidate = ''
                    elif candidate:
                        pattern_type = 'next_line_delimited'

        if (
            not candidate
            and match.group('prefix') in ('这', '那')
            and re.match(
                r'^\s*(像|一出|一落|一响|一出来|一出口|一出现|一现|一下|一旦|刚|才|便|就|直接|瞬间)',
                line_after_match.strip(),
            )
        ):
            inline_candidate = _extract_inline_reference_candidate(line_before_match)
            previous_candidate = _extract_previous_reference_candidate(text, line_start)
            if inline_candidate:
                candidate = inline_candidate
                pattern_type = 'backref_inline'
            elif previous_candidate:
                candidate = previous_candidate
                pattern_type = 'backref_previous'

        if not candidate:
            continue

        actual = count_meaningful_chars(candidate)
        if actual <= 0 or actual == expected or actual > 24:
            continue

        old_line = current_line_stripped
        line_relative_start = match.start() - line_start
        line_relative_end = match.end() - line_start
        old_claim = current_line[line_relative_start:line_relative_end]
        new_claim = old_claim.replace(match.group('count'), render_number_like(match.group('count'), actual), 1)
        new_line = (current_line[:line_relative_start] + new_claim + current_line[line_relative_end:]).strip()
        if old_line == new_line:
            continue

        issue_key = (old_line, new_line)
        if issue_key in seen:
            continue
        seen.add(issue_key)
        issues.append(
            {
                'type': 'count_mismatch',
                'severity': 'high',
                'source': 'local_rule',
                'pattern_type': pattern_type or 'unknown',
                'auto_fix_safe': pattern_type in {'inline_delimited', 'next_line_delimited'},
                'objective': True,
                'excerpt': old_line,
                'location_hint': current_line_stripped[:80],
                'explanation': f'“{candidate.strip()}”按可见中文/字母数字计数为 {actual} 个字，与前文“{match.group("count")}个字”不符。',
                'fix_instruction': f'将“{match.group("count")}个字”修正为“{render_number_like(match.group("count"), actual)}个字”。',
                'confidence': 100,
                'expected_count': expected,
                'actual_count': actual,
                'candidate_text': candidate.strip(),
                'old_text': old_line,
                'new_text': new_line,
            }
        )

    return issues


def apply_explicit_count_fixes(text: str) -> tuple[str, list[dict[str, Any]]]:
    current = text or ''
    applied: list[dict[str, Any]] = []

    while True:
        issues = scan_explicit_count_mismatch_issues(current)
        if not issues:
            break

        changed = False
        for issue in issues:
            if not bool(issue.get('auto_fix_safe')):
                continue
            old_text = str(issue.get('old_text', '')).strip()
            new_text = str(issue.get('new_text', '')).strip()
            if not old_text or not new_text or old_text == new_text:
                continue
            if old_text not in current:
                continue
            current = current.replace(old_text, new_text, 1)
            applied.append(issue)
            changed = True

        if not changed:
            break

    return current, applied


def apply_replacement_operations(text: str, operations: list[dict[str, Any]]) -> tuple[str, int]:
    current = text
    applied_count = 0
    for operation in operations:
        old_text = str(operation.get('old_text', '')).strip()
        new_text = str(operation.get('new_text', '')).strip()
        if not old_text or old_text == new_text:
            continue
        occurrences = current.count(old_text)
        if occurrences == 0:
            if new_text and new_text in current:
                continue
            raise RuntimeError(f'critic patch old_text not found: {old_text[:60]}')
        if occurrences > 1:
            raise RuntimeError(f'critic patch old_text is not unique: {old_text[:60]}')
        current = current.replace(old_text, new_text, 1)
        applied_count += 1
    return current, applied_count


class AutoNovelRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root = Path(__file__).resolve().parent
        self.project_dir = Path(args.project_dir).resolve()
        self.logs_dir = ensure_dir(self.project_dir / 'logs')
        self.memory_dir = ensure_dir(self.project_dir / 'memory')
        self.volumes_dir = ensure_dir(self.project_dir / 'volumes')
        self.manuscript_dir = ensure_dir(self.project_dir / 'manuscript')
        self.manuscript_chapters_dir = ensure_dir(self.manuscript_dir / 'chapters_txt')
        self.state_path = self.project_dir / 'state.json'
        self.events_path = self.logs_dir / 'events.log'
        self.live_stage_path = self.logs_dir / 'live_stage.json'
        self.brief_path = self.project_dir / 'brief.md'
        self.full_manuscript_path = self.manuscript_dir / 'full_novel.txt'
        self.series_bible_path = self.memory_dir / 'series_bible.md'
        self.series_bible_short_path = self.memory_dir / 'series_bible_short.md'
        self.series_bible_parts_dir = ensure_dir(self.memory_dir / 'series_bible_parts')
        self.story_memory_path = self.memory_dir / 'story_memory.md'
        self.opening_promise_path = self.memory_dir / 'opening_promise.md'
        self.ending_guidance_path = self.memory_dir / 'ending_guidance.md'
        self.auto_ending_guidance_path = self.memory_dir / 'auto_ending_guidance.md'
        self.auto_ending_quality_guidance_path = self.memory_dir / 'auto_ending_quality_guidance.md'
        self.ending_quality_review_path = self.memory_dir / 'ending_quality_review.md'
        self.ending_polish_brief_path = self.memory_dir / 'ending_polish_brief.md'
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
        self.memory_retrieval = NovelMemoryRetrieval(
            self.project_dir,
            wing=self.project_dir.name,
            enabled=not bool(getattr(args, 'disable_memory_retrieval', False)),
            logger=self.logger,
        )
        self._memory_retrieval_needs_backfill = True

        ensure_dir(self.project_dir)
        self._prepare_brief()

        base_main = get_model_config_from_provider_model(args.main_model)
        base_sub = get_model_config_from_provider_model(args.sub_model)
        base_critic = get_model_config_from_provider_model(args.critic_model or args.main_model)
        base_ending_polish = get_model_config_from_provider_model(args.ending_polish_model or args.critic_model or args.main_model)

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
        self.critic_model = clone_model_config(
            base_critic,
            reasoning_effort=args.critic_reasoning_effort,
            **cap_model_output_tokens(base_critic, 8_000),
        )
        self.ending_polish_model = clone_model_config(
            base_ending_polish,
            reasoning_effort=args.ending_polish_reasoning_effort,
            **cap_model_output_tokens(base_ending_polish, 10_000),
        )
        critic_model_name = str(args.critic_model or args.main_model or '').strip().lower()
        critic_interval = int(args.critic_every_chapters or 0)
        critic_max_passes = int(args.critic_max_passes or 0)
        self.critic_enabled = critic_model_name not in {'', '0', 'false', 'off', 'none', 'disable', 'disabled'}
        if critic_interval < 0 or critic_max_passes < 0:
            self.critic_enabled = False

        self.state = self._load_or_init_state()
        self._repair_duplicate_chapter_titles(rewrite_files=True, rebuild_manuscript=True, reason='startup')
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
            replace_with_retry(tmp_path, self.heartbeat_path)
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

    def _sanitize_text(self, text: str) -> str:
        return sanitize_project_text(text or '', project_name=self.project_dir.name)

    def _sanitize_pairs(self, pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        sanitized_pairs: list[tuple[str, str]] = []
        for x_value, y_value in pairs:
            sanitized_pairs.append((x_value, self._sanitize_text(y_value or '')))
        return sanitized_pairs

    def _draft_heading_mode(self) -> str:
        return 'title_only' if bool(getattr(self.args, 'title_only_story', False)) else 'chapter'

    def _project_book_title(self) -> str:
        brief_text = read_text(self.brief_path)
        title_match = re.search(r'《([^》]+)》', brief_text)
        if title_match:
            return title_match.group(1).strip()
        title = str(self.state.get('book_title', '') or '').strip()
        if title:
            return title.strip('《》').strip() or title
        return ''

    def _story_heading_title(self, fallback_title: str) -> str:
        if self._draft_heading_mode() != 'title_only':
            return fallback_title
        book_title = self._project_book_title()
        if book_title:
            return f'《{book_title.strip("《》").strip()}》'
        return fallback_title

    def _existing_story_titles(self) -> set[str]:
        titles: set[str] = set()
        for key in ('completed_chapters', 'pending_chapters'):
            for item in self.state.get(key, []) or []:
                title = str(item.get('title', '') or '').strip()
                if title:
                    titles.add(title)
        return titles

    def _chapter_record_paths(self, chapter: dict[str, Any]) -> dict[str, Path]:
        chapter_number = int(chapter.get('chapter_number', 0) or 0)
        volume_number = int(chapter.get('volume_number', 0) or 0)
        if chapter_number <= 0:
            return {}
        if volume_number <= 0:
            volume_number, _ = self._chapter_volume_number(chapter_number)
        chapter_dir = self.volumes_dir / f'vol_{volume_number:03d}' / 'chapters' / f'ch_{chapter_number:04d}'
        return {
            'outline': Path(chapter.get('outline_file') or chapter_dir / 'outline.md'),
            'plot': Path(chapter.get('plot_file') or chapter_dir / 'plot.md'),
            'draft': Path(chapter.get('draft_file') or chapter_dir / 'draft.md'),
            'summary': Path(chapter.get('summary_file') or chapter_dir / 'summary.md'),
        }

    def _chapter_title_source_text(self, chapter: dict[str, Any], extra_texts: Iterable[str] | None = None) -> str:
        parts: list[str] = []
        current_title = str(chapter.get('title', '') or '').strip()
        if current_title:
            parts.append(current_title)

        for path in self._chapter_record_paths(chapter).values():
            if path.exists():
                text = read_text(path).strip()
                if text:
                    parts.append(text)

        for text in extra_texts or ():
            cleaned = str(text or '').strip()
            if cleaned:
                parts.append(cleaned)

        return '\n\n'.join(parts)

    def _apply_title_to_record_files(self, chapter: dict[str, Any], new_title: str) -> None:
        chapter_number = int(chapter.get('chapter_number', 0) or 0)
        if chapter_number <= 0:
            return

        paths = self._chapter_record_paths(chapter)

        outline_path = paths.get('outline')
        if outline_path and outline_path.exists():
            outline_text = read_text(outline_path)
            if outline_text.strip():
                write_text(
                    outline_path,
                    rewrite_outline_heading(chapter_number, new_title, outline_text),
                )

        draft_path = paths.get('draft')
        if draft_path and draft_path.exists():
            draft_text = read_text(draft_path)
            if draft_text.strip():
                write_text(
                    draft_path,
                    normalize_chapter_draft_text(
                        chapter_number,
                        new_title,
                        draft_text,
                        heading_mode=self._draft_heading_mode(),
                    ),
                )

    def _repair_duplicate_chapter_titles(
        self,
        *,
        rewrite_files: bool,
        rebuild_manuscript: bool,
        reason: str,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for key in ('completed_chapters', 'pending_chapters'):
            bucket = self.state.get(key, []) or []
            for item in bucket:
                if not isinstance(item, dict):
                    continue
                records.append(item)

        records.sort(key=lambda item: int(item.get('chapter_number', 0) or 0))
        existing_titles: list[str] = []
        changes: list[dict[str, Any]] = []

        for chapter in records:
            chapter_number = int(chapter.get('chapter_number', 0) or 0)
            if chapter_number <= 0:
                continue

            current_title = str(chapter.get('title', '') or '').strip()
            source_text = self._chapter_title_source_text(chapter)
            unique_title = ensure_unique_chapter_title(
                chapter_number,
                current_title or source_text,
                existing_titles,
                source_text,
            )
            existing_titles.append(unique_title)

            if unique_title == current_title:
                continue

            chapter['title'] = unique_title
            if rewrite_files:
                self._apply_title_to_record_files(chapter, unique_title)

            changes.append(
                {
                    'chapter_number': chapter_number,
                    'old_title': current_title,
                    'new_title': unique_title,
                }
            )

        if not changes:
            return []

        preview = ', '.join(
            f'第{item["chapter_number"]}章 {item["old_title"] or "（空标题）"} -> {item["new_title"]}'
            for item in changes[:8]
        )
        if len(changes) > 8:
            preview += f' ... 共 {len(changes)} 处'
        self.log(f'[title_guard] {reason} 修复重复章节标题：{preview}')

        if rebuild_manuscript:
            self.rebuild_full_manuscript(export_chapters_txt=True)
        else:
            self._save_state()
        return changes

    def _is_title_only_story(self) -> bool:
        return self._draft_heading_mode() == 'title_only'

    def _series_bible_part_specs(self) -> list[tuple[str, str]]:
        return SHORT_STORY_BIBLE_PART_SPECS if self._is_title_only_story() else LONG_SERIES_BIBLE_PART_SPECS

    def _plan_stage_label(self, volume_number: int) -> str:
        return '单篇路线图' if self._is_title_only_story() else f'第{volume_number}卷规划'

    def _project_role_only_rule(self) -> str:
        config = lookup_project_override(PROJECT_ROLE_ONLY_RULES, self.project_dir.name)
        if not config:
            return ''
        allowed_names = '、'.join(config.get('allowed_names') or ())
        preserved_labels = '、'.join(config.get('preserved_labels') or ())
        role_labels = '、'.join(config.get('role_labels') or ())
        rule = (
            f'除主角“{allowed_names}”外，任何角色都不得使用专有姓名；'
            f'一律使用身份称呼，如“{role_labels}”。'
            '如果需要新增配角，只能新增身份称呼，绝不允许临时命名。'
        )
        if preserved_labels:
            rule += f'允许保留的固定称呼/代号只有“{preserved_labels}”。'
        return rule

    def _project_critic_hard_rules(self) -> str:
        lines: list[str] = []
        config = lookup_project_override(PROJECT_ROLE_ONLY_RULES, self.project_dir.name)
        if config:
            allowed_names = '、'.join(config.get('allowed_names') or ())
            lines.append(
                f'除“{allowed_names}”外，若正文里出现任何其他角色专有姓名，按客观硬伤处理。'
            )
        if self.project_dir.name == 'watcher_origin_short' or self.project_dir.name.startswith('watcher_origin_short_'):
            lines.append('正文中必须真实出现一次完整字样“守夜人”，若全文未出现则按客观硬伤处理。')
            lines.append('“守夜人”必须出现在正文后段的自然叙事句子里，不能只出现在说明、备注或标题中。')
            lines.append('“守夜人”必须至少有一次出现在带引号的对白中，否则按客观硬伤处理。')
            lines.append('正文里第一次出现“守夜人”的位置，必须位于全文最后1200字内，否则按客观硬伤处理。')
            lines.append('从第一次出现“守夜人”的段落开始，到正文结尾，最多只允许保留4个非空段落，否则按客观硬伤处理。')
        if self._draft_heading_mode() == 'title_only':
            book_title = self._project_book_title()
            if book_title:
                lines.append(
                    f'首个非空行必须是《{book_title.strip("《》").strip()}》，若不是则按客观硬伤处理。'
                )
        return '\n'.join(f'- {line}' for line in lines if line)

    def _enforce_project_role_labels(self, label: str, text: str) -> str:
        config = lookup_project_override(PROJECT_ROLE_ONLY_RULES, self.project_dir.name)
        source = (text or '').strip()
        if not config or not source:
            return source

        allowed_names = '、'.join(config.get('allowed_names') or ())
        preserved_labels = '、'.join(config.get('preserved_labels') or ())
        role_labels = '、'.join(config.get('role_labels') or ())
        fallback_label = str(config.get('fallback_label') or '那名角色').strip()
        prompt = f"""
请把下面这段中文小说文本做一次“角色称谓约束校正”。

硬规则：
- 允许保留的专有姓名只有：{allowed_names}
- 允许原样保留的固定称呼/代号只有：{preserved_labels or '（无）'}
- 除此之外，任何人物都不允许保留专有姓名
- 其余人物必须改写为身份称呼，只能优先使用这些称呼：{role_labels}
- 如果原文已经出现允许保留的固定称呼/代号，必须保留，不要删掉，不要改写
- 如果上下文无法精确判断身份，就改成“{fallback_label}”或“对方”之类的无名身份称呼
- 不要新增任何新姓名
- 不要改变剧情、不删减信息、不改写事件顺序、不改标题
- 只输出修订后的完整文本，不要解释

原文如下：
{source}
"""

        def _rewrite() -> str:
            revised = self.call_llm(f'{label}角色称谓校正', prompt, self.critic_model)
            revised = (revised or '').strip()
            if not revised:
                raise RuntimeError(f'{label} 角色称谓校正返回空文本。')
            return revised

        return self.with_retry(f'{label}角色称谓校正', _rewrite)

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
        if '审校' in label or '校审' in label or 'critic' in lower_label:
            return 'critic'
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
            replace_with_retry(tmp_path, self.live_stage_path)
            self._last_live_stage_write_ts = current_time
            self._last_live_stage_payload_key = payload_key
        except Exception as exc:
            self._log_event_only(f'[live_stage] 写入失败：{exc}')

    def _stream_text(self, label: str, full_text: str, reset: bool = False, finish: bool = False) -> None:

        full_text = self._sanitize_text(full_text or '')
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
            state = json.loads(self.state_path.read_text(encoding='utf-8-sig'))
            state.setdefault('min_target_chars', 0)
            state.setdefault('force_finish_chars', 0)
            state.setdefault('max_target_chars', 0)
            state.setdefault('completion_mode', 'hard_target')
            state.setdefault('completion_check', {})
            state.setdefault('last_opening_promise_refresh_chapter', 0)
            state.setdefault('last_ending_guidance_refresh_chapter', 0)
            state.setdefault('last_ending_quality_guidance_refresh_chapter', 0)
            state.setdefault('rewrite_required_from_chapter', 0)
            state.setdefault('ending_quality_check', {})
            state.setdefault('ending_quality_history', [])
            state.setdefault('ending_polish_cycles', 0)
            state.setdefault('ending_polish_last_rewrite_from_chapter', 0)
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
            'force_finish_chars': self.args.force_finish_chars,
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
            'last_opening_promise_refresh_chapter': 0,
            'last_ending_guidance_refresh_chapter': 0,
            'last_ending_quality_guidance_refresh_chapter': 0,
            'rewrite_required_from_chapter': 0,
            'book_title': '',
            'last_error': '',
            'current_stage': '',
            'stage_started_at': '',
            'last_stage_heartbeat_at': '',
            'completion_check': {},
            'ending_quality_check': {},
            'ending_quality_history': [],
            'ending_polish_cycles': 0,
            'ending_polish_last_rewrite_from_chapter': 0,
        }
        self._save_state(state)
        return state

    def _save_state(self, state: dict | None = None) -> None:
        if state is not None:
            self.state = state
        self.state['updated_at'] = now_str()
        tmp_path = self.state_path.with_suffix('.json.tmp')
        tmp_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        replace_with_retry(tmp_path, self.state_path)

    def _invalidate_memory_retrieval(self) -> None:
        self._memory_retrieval_needs_backfill = True

    def _ensure_memory_retrieval_ready(self) -> None:
        if not self.memory_retrieval or not self.memory_retrieval.available:
            return
        if not self._memory_retrieval_needs_backfill:
            return
        stats = self.memory_retrieval.backfill(self.state.get('completed_chapters', []))
        if not stats.get('enabled'):
            return
        self._memory_retrieval_needs_backfill = False
        self.log(
            '[memory_retrieval] 索引已准备：'
            f'更新 {stats.get("synced", 0)}，'
            f'跳过 {stats.get("skipped", 0)}，'
            f'移除 {stats.get("removed", 0)}，'
            f'失败 {stats.get("failed", 0)}'
        )

    def _sync_memory_retrieval_file(
        self,
        room: str,
        path: Path,
        *,
        chapter_number: int = 0,
        source_kind: str = '',
    ) -> None:
        if not self.memory_retrieval or not self.memory_retrieval.available:
            return
        target_path = Path(path)
        text = read_text(target_path).strip()
        self.memory_retrieval.sync_text(
            room=room,
            source_path=target_path,
            text=text,
            chapter_number=chapter_number,
            source_kind=source_kind or room,
        )

    def _build_memory_retrieval_query(
        self,
        stage: str,
        *,
        chapter: dict | None = None,
        outline_text: str = '',
        plot_text: str = '',
    ) -> tuple[str, tuple[str, ...]]:
        story_memory = truncate_text(read_text(self.story_memory_path), 700)
        recent = truncate_text(self.recent_chapter_summaries(limit=4), 900)
        chapter_number = int((chapter or {}).get('chapter_number', 0) or 0)
        chapter_title = str((chapter or {}).get('title', '') or '').strip()
        chapter_label = f'第{chapter_number}章 {chapter_title}'.strip() if chapter_number else ''

        if stage == 'plan':
            query = f"""
为接下来章节规划检索最相关的历史记忆。

当前准备继续写作的位置：第 {self.state.get('next_chapter_number', 1)} 章
最近章节摘要：
{recent or '（暂无）'}

当前故事记忆：
{story_memory or '（暂无）'}
""".strip()
            return truncate_text(query, 1800), ('brief', 'series_bible', 'chapter_summary')

        if stage == 'plot':
            query = f"""
为 {chapter_label or '当前章节'} 的剧情梗概检索最相关的历史记忆。

章节大纲：
{truncate_text(outline_text, 1000) or '（暂无）'}

最近章节摘要：
{recent or '（暂无）'}
""".strip()
            return truncate_text(query, 1800), ('brief', 'series_bible', 'chapter_summary')

        if stage == 'draft':
            query = f"""
为 {chapter_label or '当前章节'} 的正文写作检索最相关的历史记忆。

剧情梗概：
{truncate_text(plot_text, 1000) or '（暂无）'}

最近章节摘要：
{recent or '（暂无）'}
""".strip()
            return truncate_text(query, 1800), ('brief', 'series_bible', 'chapter_summary')

        return '', ()

    def _memory_retrieval_context(
        self,
        stage: str,
        *,
        chapter: dict | None = None,
        outline_text: str = '',
        plot_text: str = '',
    ) -> str:
        if not self.memory_retrieval or not self.memory_retrieval.available:
            return ''
        self._ensure_memory_retrieval_ready()
        query, rooms = self._build_memory_retrieval_query(
            stage,
            chapter=chapter,
            outline_text=outline_text,
            plot_text=plot_text,
        )
        if not query or not rooms:
            return ''
        hits = self.memory_retrieval.search(
            query,
            rooms=rooms,
            n_results=max(1, int(getattr(self.args, 'memory_retrieval_hits', 4) or 4)),
            max_chars=max(400, int(getattr(self.args, 'memory_retrieval_max_chars', 1200) or 1200)),
        )
        if not hits:
            return ''

        blocks = []
        for hit in hits:
            room = str(hit.get('room', '') or '')
            room_label = MEMORY_RETRIEVAL_ROOM_LABELS.get(room, room or '检索记忆')
            source_file = str(hit.get('source_file', '') or '').replace('\\', '/')
            source_label = source_file.split('/')[-1] if source_file else ''
            chapter_number = int(hit.get('chapter_number', 0) or 0)
            header_parts = [room_label]
            if chapter_number:
                header_parts.append(f'第{chapter_number}章')
            if source_label:
                header_parts.append(source_label)
            similarity = hit.get('similarity')
            if isinstance(similarity, (int, float)):
                header_parts.append(f'相似度 {similarity:.3f}')
            snippet = str(hit.get('text', '') or '').strip()
            if not snippet:
                continue
            blocks.append(f"[{' | '.join(header_parts)}]\n{snippet}")

        if not blocks:
            return ''
        return '【历史检索记忆】\n' + '\n\n'.join(blocks)

    def clear_error(self) -> None:
        if self.state.get('last_error'):
            self.state['last_error'] = ''
            self._save_state()

    def _load_cached_text_if_valid(self, path: Path, *, min_chars: int, label: str) -> str:
        if not path.exists():
            return ''
        text = read_text(path).strip()
        if len(text) >= min_chars:
            return text
        if text:
            self.log(f'[{label}] 检测到过短缓存文件，已删除并重新生成：{path}（{len(text)} 字，阈值 {min_chars}）')
        else:
            self.log(f'[{label}] 检测到空缓存文件，已删除并重新生成：{path}')
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return ''

    def _chapter_body_text(self, chapter: dict[str, Any], draft_text: str) -> str:
        chapter_number = int(chapter.get('chapter_number', 0) or 0)
        chapter_title = str(chapter.get('title', '') or '').strip()
        return strip_generated_draft_heading(chapter_number, chapter_title, draft_text).strip()

    def _assess_chapter_draft_integrity(self, chapter: dict[str, Any], draft_text: str) -> dict[str, Any]:
        report = dict(assess_chapter_tail_integrity(self._chapter_body_text(chapter, draft_text)))
        report.update({
            'chapter_number': int(chapter.get('chapter_number', 0) or 0),
            'title': str(chapter.get('title', '') or '').strip(),
            'draft_file': str(chapter.get('draft_file', '') or ''),
        })
        return report

    def _load_cached_draft_if_valid(self, chapter: dict[str, Any], *, min_chars: int, label: str) -> str:
        draft_path = Path(chapter['draft_file'])
        text = self._load_cached_text_if_valid(
            draft_path,
            min_chars=min_chars,
            label=label,
        )
        if not text:
            return ''

        integrity = self._assess_chapter_draft_integrity(chapter, text)
        if not integrity.get('high_confidence'):
            return text

        self.log(
            f'[{label}] 检测到疑似残章缓存，已删除并重新生成：{draft_path}'
            f'（{integrity.get("reason")}，结尾：{integrity.get("tail", "")}）'
        )
        try:
            draft_path.unlink()
        except FileNotFoundError:
            pass
        return ''

    def _repair_chapter_draft_text(
        self,
        chapter: dict[str, Any],
        current_text: str,
        *,
        outline_text: str = '',
        plot_text: str = '',
        previous_summary: str = '',
        next_excerpt: str = '',
        issue_reason: str = '',
        issue_tail: str = '',
        label: str = '',
    ) -> str:
        chapter_number = int(chapter.get('chapter_number', 0) or 0)
        chapter_title = str(chapter.get('title', '') or '').strip()
        stage_label = label or f'第{chapter_number}章结尾修补'
        project_role_rule = self._project_role_only_rule()
        current_body = self._chapter_body_text(chapter, current_text)
        tail_excerpt = tail_text(current_body, 1800)
        prompt = f"""
你正在修补一章已经基本成型、但结尾疑似被截断的中文网文章节。

目标不是重写整章，而是只补出“缺失的结尾续写”，让现有正文可以直接接上去，变成完整可发布章节。

硬要求：
- 只输出“可以直接拼接到现有正文末尾”的续写内容，不要重复已有正文，不要重写标题，不要解释，不要分点。
- 续写优先接住当前最后一句的半截内容，把断掉的动作、对话、画面、信息交代补完整。
- 正常只需补最后一小段到两三小段，控制在必要范围内；不要无端扩成整章重写。
- 不要新增全新角色、全新地点、全新势力、全新规则；只能用本章和相邻章节已建立的元素补齐。
- 允许保留章节悬念，但禁止停在半句话、半个人名、半个动作、半截引号、半截倒计时说明上。
- 如果提供了下一章开头，只能把本章修到能自然衔接，不要抢写下一章已经发生的核心事件。
- 结尾必须落在完整动作、完整对话句、完整画面或完整倒计时/进度提示上。
{f'- {project_role_rule}' if project_role_rule else ''}

【疑似问题】
- 问题类型：{issue_reason or 'bad_ending'}
- 当前尾句：{issue_tail or '（无）'}

【本章标题】
第{chapter_number}章 {chapter_title}

【本章大纲】
{truncate_text(outline_text, 1200) or '（无）'}

【本章剧情】
{truncate_text(plot_text, 1600) or '（无）'}

【上一章摘要】
{truncate_text(previous_summary, 500) or '（无）'}

【下一章开头】
{truncate_text(next_excerpt, 800) or '（无）'}

【当前正文尾段】
{tail_excerpt}
""".strip()

        continuation = self._call_llm_raw(
            stage_label,
            prompt,
            self.ending_polish_model,
            system_prompt='你是资深中文网文章节修补编辑，擅长保留原稿主体，只修断裂处与结尾落点。',
            response_json=False,
            stream_output=True,
        ).strip()
        continuation = self._sanitize_text(continuation)
        if not continuation:
            raise RuntimeError(f'{stage_label} 返回空文本。')

        continuation = strip_generated_draft_heading(
            chapter_number,
            chapter_title,
            continuation,
        ).lstrip()
        revised = current_text.rstrip() + continuation
        revised = normalize_chapter_draft_text(
            chapter_number,
            chapter_title,
            revised,
            heading_mode=self._draft_heading_mode(),
        )
        revised = self._enforce_project_role_labels(stage_label, revised)
        self.clear_error()
        revised = normalize_chapter_draft_text(
            chapter_number,
            chapter_title,
            revised,
            heading_mode=self._draft_heading_mode(),
        )
        if len(revised.strip()) <= len(current_text.strip()):
            raise RuntimeError(f'{stage_label} 未能补出有效续写。')
        repaired_integrity = self._assess_chapter_draft_integrity(chapter, revised)
        if repaired_integrity.get('high_confidence'):
            raise RuntimeError(
                f'{stage_label} 后结尾仍疑似不完整：'
                f'{repaired_integrity.get("reason")} | {repaired_integrity.get("tail", "")}'
            )
        return revised

    def _repair_chapter_draft_if_needed(
        self,
        chapter: dict[str, Any],
        draft_text: str,
        *,
        outline_text: str = '',
        plot_text: str = '',
        previous_summary: str = '',
        next_excerpt: str = '',
        label: str = '',
        force: bool = False,
    ) -> str:
        integrity = self._assess_chapter_draft_integrity(chapter, draft_text)
        if not force and not integrity.get('high_confidence'):
            return draft_text

        stage_label = label or f'第{int(chapter.get("chapter_number", 0) or 0)}章结尾修补'
        self.log(
            f'[{stage_label}] 检测到章节结尾疑似不完整，启动定向修补：'
            f'{integrity.get("reason")} | {integrity.get("tail", "")}'
        )
        return self.with_retry(
            stage_label,
            lambda: self._repair_chapter_draft_text(
                chapter,
                draft_text,
                outline_text=outline_text,
                plot_text=plot_text,
                previous_summary=previous_summary,
                next_excerpt=next_excerpt,
                issue_reason=str(integrity.get('reason', '') or ''),
                issue_tail=str(integrity.get('tail', '') or ''),
                label=stage_label,
            ),
        )

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
            text = self._sanitize_text(read_text(draft_path)).strip()
            if not text:
                self.state['manuscript_last_appended_chapter'] += 1
                continue
            with self.full_manuscript_path.open('a', encoding='utf-8') as handle:
                handle.write(text.rstrip() + '\n\n')
            self.state['manuscript_last_appended_chapter'] += 1
        self._save_state()

    def _completed_manuscript_chapters(self) -> list[dict[str, object]]:
        completed = sorted(
            self.state.get('completed_chapters', []),
            key=lambda item: int(item.get('chapter_number', 0) or 0),
        )
        chapters: list[dict[str, object]] = []
        for item in completed:
            chapter_number = int(item.get('chapter_number', 0) or 0)
            draft_path = Path(item.get('draft_file', ''))
            if not draft_path.exists():
                self.log(f'[manuscript] 跳过缺失章稿：{draft_path}')
                continue
            text = self._sanitize_text(read_text(draft_path)).strip()
            if not text:
                continue
            lines = text.splitlines()
            heading_line = lines[0].strip() if lines else ''
            chapters.append(
                {
                    'chapter_number': chapter_number,
                    'text': text,
                    'heading_line': normalize_heading_line(heading_line, new_number=chapter_number),
                }
            )
        return chapters

    def export_full_novel_chapters_txt(self) -> None:
        for path in self.manuscript_chapters_dir.glob('ch_*.txt'):
            path.unlink()
        manifest_path = self.manuscript_chapters_dir / 'chapters_manifest.json'

        completed_chapters = self._completed_manuscript_chapters()
        if completed_chapters:
            manifest: list[dict[str, object]] = []
            for chapter in completed_chapters:
                chapter_number = int(chapter['chapter_number'])
                chapter_text = str(chapter['text'])
                heading_line = str(chapter['heading_line'])
                write_text(
                    self.manuscript_chapters_dir / f'ch_{chapter_number:04d}.txt',
                    chapter_text.rstrip() + '\n',
                )
                manifest.append(
                    {
                        'source_number': chapter_number,
                        'output_number': chapter_number,
                        'heading_line': heading_line,
                    }
                )
            write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
            )
            return

        if manifest_path.exists():
            manifest_path.unlink()
        if not self.full_manuscript_path.exists():
            return
        text = read_text(self.full_manuscript_path)
        chapters = split_full_novel(text)
        if not chapters:
            return
        manifest: list[dict[str, object]] = []
        for chapter in chapters:
            chapter_number = int(chapter['source_number'])
            chapter_text = str(chapter['text'])
            write_text(
                self.manuscript_chapters_dir / f'ch_{chapter_number:04d}.txt',
                chapter_text.rstrip() + '\n',
            )
            manifest.append(
                {
                    'source_number': chapter_number,
                    'output_number': chapter_number,
                    'heading_line': str(chapter['heading_line']),
                }
            )
        write_text(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
        )

    def rebuild_full_manuscript(self, export_chapters_txt: bool = True) -> None:
        completed = self.state.get('completed_chapters', [])
        chunks = [str(item['text']) for item in self._completed_manuscript_chapters()]

        if chunks:
            write_text(self.full_manuscript_path, '\n\n'.join(chunks).rstrip() + '\n')
        elif self.full_manuscript_path.exists():
            self.full_manuscript_path.unlink()

        self.state['manuscript_last_appended_chapter'] = len(completed)
        self._save_state()
        if export_chapters_txt:
            self.export_full_novel_chapters_txt()

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
        remaining_chars = int(
            completion_check.get('conservative_remaining_chars')
            or completion_check.get('remaining_chars', 0)
            or 0
        )
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
        remaining_chapters = int(
            completion_check.get('conservative_remaining_chapters')
            or completion_check.get('remaining_chapters', 0)
            or 0
        )
        return max(1, remaining_chapters or 1)

    def _estimated_total_chapters(self) -> int:
        return max(1, math.ceil(self._planning_target_chars() / self.args.chapter_char_target))

    def _estimated_total_volumes(self) -> int:
        return max(1, math.ceil(self._estimated_total_chapters() / self.args.chapters_per_volume))

    def _call_llm_raw(
        self,
        label: str,
        user_prompt: str,
        model: ModelConfig,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        *,
        response_json: bool = False,
        stream_output: bool = True,
    ) -> str:
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
        gen = stream_chat(model, messages, response_json=response_json)
        while True:
            try:
                current = next(gen)
            except StopIteration as exc:
                final_messages = exc.value
                break

            if isinstance(current, ChatMessages):
                final_messages = current
                response_text = self._sanitize_text(current.response or '')
                if getattr(current, 'stream_fallback', False) and not fallback_logged:
                    reason = getattr(current, 'stream_fallback_reason', '') or 'stream transport error'
                    self.log(f'[{label}] 流式链路异常，已自动降级为非流式补全：{reason}')
                    fallback_logged = True
                if response_text and stream_output:
                    self._stream_text(label, response_text)
                    self.mark_stage(label)
                elif response_text:
                    self.mark_stage(label)
                if response_text and time.time() - last_log_time >= 8 and len(response_text) != last_length:
                    self.log(f'[{label}] 正在生成，当前约 {len(response_text)} 字')
                    last_log_time = time.time()
                    last_length = len(response_text)

        if not final_messages or not (final_messages.response or '').strip():
            raise RuntimeError(f'{label} 未返回有效内容。')

        response = self._sanitize_text(final_messages.response or '').strip()
        if stream_output:
            self._stream_text(label, response, finish=True)
        self.clear_stage()
        self.log(f'[{label}] 完成，用时 {time.time() - start_time:.1f}s，输出 {len(response)} 字，成本 {final_messages.cost_info}')
        return response

    def call_llm(self, label: str, user_prompt: str, model: ModelConfig, system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> str:
        return self._call_llm_raw(
            label,
            user_prompt,
            model,
            system_prompt=system_prompt,
            response_json=False,
            stream_output=True,
        )

    def call_llm_json(self, label: str, user_prompt: str, model: ModelConfig, system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> dict[str, Any]:
        raw = self._call_llm_raw(
            label,
            user_prompt,
            model,
            system_prompt=system_prompt,
            response_json=True,
            stream_output=False,
        )
        return extract_json_payload(raw)

    def _should_run_critic_for_chapter(self, chapter_number: int, force: bool = False) -> bool:
        if force:
            return True
        if not self.critic_enabled:
            return False
        interval = max(0, int(self.args.critic_every_chapters or 0))
        if interval > 0:
            return chapter_number % interval == 0
        pending = self.state.get('pending_chapters') or []
        if not pending:
            return False
        batch_tail = int(pending[-1].get('chapter_number', 0) or 0)
        return chapter_number == batch_tail

    def _normalize_critic_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_issues = payload.get('issues') or []
        issues: list[dict[str, Any]] = []
        if isinstance(raw_issues, list):
            for raw_issue in raw_issues:
                if not isinstance(raw_issue, dict):
                    continue
                if raw_issue.get('objective', True) is False:
                    continue
                issue_type = str(raw_issue.get('type', 'objective_logic')).strip() or 'objective_logic'
                severity = str(raw_issue.get('severity', 'medium')).strip().lower()
                if severity not in {'high', 'medium', 'low'}:
                    severity = 'medium'
                excerpt = str(raw_issue.get('excerpt', '')).strip()
                explanation = str(raw_issue.get('explanation', '')).strip()
                fix_instruction = str(raw_issue.get('fix_instruction', '')).strip()
                location_hint = str(raw_issue.get('location_hint', '')).strip()
                if not (excerpt or explanation or fix_instruction):
                    continue
                confidence_raw = raw_issue.get('confidence', 0)
                try:
                    confidence = max(0, min(100, int(confidence_raw)))
                except (TypeError, ValueError):
                    confidence = 0
                issues.append(
                    {
                        'type': issue_type,
                        'severity': severity,
                        'excerpt': excerpt,
                        'explanation': explanation,
                        'fix_instruction': fix_instruction,
                        'location_hint': location_hint,
                        'confidence': confidence,
                    }
                )

        needs_fix = bool(payload.get('needs_fix')) or bool(issues)
        return {
            'needs_fix': needs_fix,
            'issues': issues,
            'clean_note': str(payload.get('clean_note', '')).strip(),
        }

    def _normalize_patch_operations(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        raw_operations = payload.get('operations') or []
        operations: list[dict[str, str]] = []
        if not isinstance(raw_operations, list):
            return operations
        for raw_operation in raw_operations:
            if not isinstance(raw_operation, dict):
                continue
            old_text = str(raw_operation.get('old_text', '')).strip()
            new_text = str(raw_operation.get('new_text', '')).strip()
            reason = str(raw_operation.get('reason', '')).strip()
            if not old_text or not new_text or old_text == new_text:
                continue
            operations.append(
                {
                    'old_text': old_text,
                    'new_text': new_text,
                    'reason': reason,
                }
            )
        return operations

    def critic_review_draft(self, chapter: dict, plot_text: str, draft_text: str, pass_index: int = 1) -> dict[str, Any]:
        project_hard_rules = self._project_critic_hard_rules()
        critic_kind = '中文短篇小说' if self._is_title_only_story() else '中文长篇连载'
        prompt = f"""
你是{critic_kind}的“客观事实 critic”。请只检查这章正文中的客观、可验证、无需主观判断的语言逻辑错误。

只允许报告以下类型：
1. 明确计数错误：如“三个字/七个人/两次/第八条”与实际内容不符。
2. 引号内短句与前文声称的字数不符。
3. 同章内人名、称谓、身份、数量、顺序、时序、位置或动作结果发生直接矛盾。
4. 明确的枚举数量、先后关系、左右/上下/内外等空间关系自相矛盾。
5. 其他能从当前章节文本直接证明的硬性逻辑错误。

执行要求：
- 必须尽量穷举全部可证明问题，不要只报最明显的一条。
- 凡是出现“X个字 / X个人 / X次 / 第X条 / 第X页 / 第X签 / X步 / X层 / X息”等显式数量宣称，都要逐一核对。
- 如果同一章里有多处独立问题，要分别列出，不要合并成一句模糊描述。
- 输出前再自检一遍，确认没有遗漏同类显式计数错误。
{project_hard_rules if project_hard_rules else ''}

禁止事项：
- 不要提文风、节奏、爽点、情绪浓度、设定喜好等主观建议。
- 不要把“故意留白”“尚未解释”当成错误。
- 如果不确定，就不要报。

请返回严格 JSON，对象结构如下：
{{
  "needs_fix": true,
  "issues": [
    {{
      "type": "count_mismatch",
      "severity": "high",
      "objective": true,
      "excerpt": "原文中的关键句",
      "location_hint": "可选，位置提示",
      "explanation": "为什么这是客观错误",
      "fix_instruction": "最小修复指令，告诉作者应如何改",
      "confidence": 95
    }}
  ],
  "clean_note": "如果无问题，用一句话说明已通过。"
}}

如果没有客观问题：
- "needs_fix" 必须为 false
- "issues" 必须为空数组

【章节信息】
- 章节号：第 {chapter['chapter_number']} 章
- 标题：{chapter['title']}
- 第 {pass_index} 轮审校

【本章剧情梗概】
{plot_text.strip() or '（无）'}

【本章正文】
{draft_text.strip()}
"""
        payload = self.call_llm_json(
            f'第{chapter["chapter_number"]}章审校第{pass_index}轮',
            prompt,
            self.critic_model,
            system_prompt='你是只抓客观语言逻辑硬伤的严格审校员。',
        )
        return self._normalize_critic_report(payload)

    def critic_rewrite_draft(
        self,
        chapter: dict,
        plot_text: str,
        draft_text: str,
        report: dict[str, Any],
        pass_index: int = 1,
    ) -> str:
        issues_json = json.dumps(report.get('issues', []), ensure_ascii=False, indent=2)
        prompt = f"""
请在不改变本章核心事件、人物立场、世界规则、章节节奏和风格的前提下，对这一章给出“最小必要替换操作”，只修复下面这些已经确认的客观逻辑问题。

硬性要求：
- 不要重写整章，只返回局部替换操作。
- 每个 old_text 必须是原文中真实存在、且足够唯一的原句或短段。
- 每个 new_text 只能做最小修正，不要顺手润色或扩写。
- 如果一个问题只需改一个数字或一句话，就不要输出更大范围。
- 在修这些问题时，顺手把同一处能一并修掉的同类显式计数、引号字数宣称、称谓/时间/位置硬冲突一起修掉，不要把本可同轮解决的问题拆到下一轮。
- 返回严格 JSON：
{{
  "operations": [
    {{
      "old_text": "原文中需要被替换的唯一片段",
      "new_text": "替换后的片段",
      "reason": "一句话说明修了什么"
    }}
  ],
  "note": "可选"
}}
- 如果所有问题都已在现有文本中被修复，返回空 operations。

【本章剧情梗概】
{plot_text.strip() or '（无）'}

【待修复问题】
{issues_json}

【原章节正文】
{draft_text.strip()}
"""
        payload = self.call_llm_json(
            f'第{chapter["chapter_number"]}章修订第{pass_index}轮',
            prompt,
            self.critic_model,
            system_prompt='你是只做局部替换修订的正文修稿编辑。',
        )
        operations = self._normalize_patch_operations(payload)
        if not operations:
            raise RuntimeError(f'第{chapter["chapter_number"]}章 critic 未返回可应用的局部替换操作。')
        patched_text, applied_count = apply_replacement_operations(draft_text, operations)
        if applied_count <= 0:
            raise RuntimeError(f'第{chapter["chapter_number"]}章 critic 局部替换未实际生效。')
        return normalize_chapter_draft_text(
            chapter['chapter_number'],
            chapter['title'],
            patched_text,
            heading_mode=self._draft_heading_mode(),
        )

    def critic_rewrite_full_draft(
        self,
        chapter: dict,
        plot_text: str,
        draft_text: str,
        report: dict[str, Any],
        pass_index: int = 1,
    ) -> str:
        issues_json = json.dumps(report.get('issues', []), ensure_ascii=False, indent=2)
        project_hard_rules = self._project_critic_hard_rules()
        prompt = f"""
你正在做“整章强修复”。

前面的局部替换策略不足以让这一章快速收敛。请在不改变本章核心事件、人物立场、世界规则、关键因果、章节功能和整体风格的前提下，直接输出一版“完整修订后的章节正文”，一次性修复下面这些已确认的客观问题，并主动顺手排查同类硬伤。

硬性要求：
- 直接输出修订后的完整正文，不要 JSON，不要解释，不要批注。
- 优先保留原有段落、句式和叙事节奏，只在必要位置改动。
- 如果局部小修会引发新的矛盾，可以直接重写受影响的整段，但不要把整章写成另一篇。
- 必须一次性修掉以下问题，并主动自检：
  1. 所有“几个字 / 几个人 / 几次 / 第X条 / 第X页 / 第X签 / X步 / X层 / X息”等显式数量宣称与实际一致。
  2. 引号内短句的字数宣称与实际一致。
  3. 同一章内同一事实只保留一种说法，不得再出现身份、称谓、时间、位置、动作结果前后打架。
  4. 若存在项目硬规则，必须一并满足。
- 不要新增新角色姓名、新设定、新支线或额外解释。
- 输出前自行再检查一遍，不要把本轮已知问题拆成下一轮。
{project_hard_rules if project_hard_rules else ''}

【章节信息】
- 章节号：第 {chapter['chapter_number']} 章
- 标题：{chapter['title']}
- 第 {pass_index} 轮强修复

【本章剧情梗概】
{plot_text.strip() or '（无）'}

【已确认问题】
{issues_json}

【当前正文】
{draft_text.strip()}
"""
        revised = self._call_llm_raw(
            f'第{chapter["chapter_number"]}章强修复第{pass_index}轮',
            prompt,
            self.critic_model,
            system_prompt='你是擅长一次性消除客观逻辑硬伤的中文正文强修复编辑。',
            response_json=False,
            stream_output=False,
        ).strip()
        if not revised:
            raise RuntimeError(f'第{chapter["chapter_number"]}章 critic 强修复返回空文本。')
        normalized = normalize_chapter_draft_text(
            chapter['chapter_number'],
            chapter['title'],
            revised,
            heading_mode=self._draft_heading_mode(),
        )
        if len(normalized) < max(400, int(len(draft_text) * 0.55)):
            raise RuntimeError(f'第{chapter["chapter_number"]}章 critic 强修复后文本异常变短。')
        return normalized

    def apply_chapter_critic(self, chapter: dict, plot_text: str, draft_text: str, force: bool = False) -> str:
        chapter_number = int(chapter['chapter_number'])
        current_text = normalize_chapter_draft_text(
            chapter_number,
            chapter['title'],
            draft_text,
            heading_mode=self._draft_heading_mode(),
        )
        if not self._should_run_critic_for_chapter(chapter_number, force=force):
            return current_text

        current_text, local_issues = apply_explicit_count_fixes(current_text)
        if local_issues:
            preview = '；'.join(summarize_critic_issue(issue) for issue in local_issues[:3])
            self.log(f'[第{chapter_number}章critic] 本地规则先修复 {len(local_issues)} 处显式计数字问题：{preview}')

        max_passes = max(0, int(self.args.critic_max_passes or 0))
        repeated_issue_signatures: dict[str, int] = {}
        pass_index = 1
        while True:
            review = self.with_retry(
                f'第{chapter_number}章审校第{pass_index}轮',
                lambda pass_index=pass_index: self.critic_review_draft(
                    chapter,
                    plot_text,
                    current_text,
                    pass_index=pass_index,
                ),
            )
            issues = review.get('issues', [])
            if not review.get('needs_fix') or not issues:
                clean_note = review.get('clean_note') or '未发现客观语言逻辑问题。'
                self.log(f'[第{chapter_number}章critic] 第 {pass_index} 轮通过：{clean_note}')
                return current_text

            issue_signature = json.dumps(
                [
                    {
                        'type': str(issue.get('type', '')).strip(),
                        'excerpt': str(issue.get('excerpt', '')).strip(),
                        'fix_instruction': str(issue.get('fix_instruction', '')).strip(),
                    }
                    for issue in issues
                ],
                ensure_ascii=False,
                sort_keys=True,
            )
            repeated_issue_signatures[issue_signature] = repeated_issue_signatures.get(issue_signature, 0) + 1
            if repeated_issue_signatures[issue_signature] >= 3:
                raise RuntimeError(
                    f'第{chapter_number}章 critic 连续多轮返回同一组问题，判定为无收敛进展，停止自动复检。'
                )

            issue_preview = '；'.join(summarize_critic_issue(issue) for issue in issues[:3])
            self.log(f'[第{chapter_number}章critic] 第 {pass_index} 轮发现 {len(issues)} 个客观问题：{issue_preview}')
            if max_passes > 0 and pass_index >= max_passes:
                raise RuntimeError(
                    f'第{chapter_number}章 critic 在 {max_passes} 轮后仍有 {len(issues)} 个客观问题未解决。'
                )

            issue_types = {str(issue.get('type', '')).strip() for issue in issues}
            use_full_rewrite = (
                len(issues) >= 4
                or pass_index >= 2
                or any(issue_type.startswith('required_') for issue_type in issue_types)
                or any('contradiction' in issue_type for issue_type in issue_types)
            )
            previous_text = current_text
            if use_full_rewrite:
                self.log(
                    f'[第{chapter_number}章critic] 第 {pass_index} 轮升级为整章强修复，'
                    f'避免长时间分轮打补丁。'
                )
                current_text = self.with_retry(
                    f'第{chapter_number}章强修复第{pass_index}轮',
                    lambda pass_index=pass_index, review=review: self.critic_rewrite_full_draft(
                        chapter,
                        plot_text,
                        current_text,
                        review,
                        pass_index=pass_index,
                    ),
                )
            else:
                current_text = self.with_retry(
                    f'第{chapter_number}章修订第{pass_index}轮',
                    lambda pass_index=pass_index, review=review: self.critic_rewrite_draft(
                        chapter,
                        plot_text,
                        current_text,
                        review,
                        pass_index=pass_index,
                    ),
                )
            if current_text == previous_text:
                raise RuntimeError(f'第{chapter_number}章 critic 修订未对正文产生任何变化，停止自动复检。')
            current_text, local_issues = apply_explicit_count_fixes(current_text)
            if local_issues:
                preview = '；'.join(summarize_critic_issue(issue) for issue in local_issues[:3])
                self.log(f'[第{chapter_number}章critic] 第 {pass_index} 轮后本地规则追加修正 {len(local_issues)} 处：{preview}')
            self.log(f'[第{chapter_number}章critic] 第 {pass_index} 轮已完成最小修订，准备复检。')
            pass_index += 1

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
                    # Mapping/json stages are internal program steps; showing raw token text
                    # leaks tool-oriented responses into the live novel stream.
                    if '映射文本' not in current_stream_label:
                        self._stream_text(current_stream_label, self._sanitize_text(merged_text))
                    self.mark_stage(current_stream_label)

            if time.time() - last_log_time >= 8:
                chunk_count = len(item) if isinstance(item, list) else 1
                self.log(f'[{label}] 流式进行中，当前分块数 {chunk_count}')
                last_log_time = time.time()

        self._stream_text(current_stream_label, self._stream_cache.get(current_stream_label, ''), finish=True)
        self.clear_stage()
        sanitized_pairs = self._sanitize_pairs(writer.xy_pairs)
        total_y = ''.join(pair[1] for pair in sanitized_pairs)
        self.log(f'[{label}] 完成，用时 {time.time() - start_time:.1f}s，输出 {len(total_y)} 字')
        return sanitized_pairs

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
        part_specs = self._series_bible_part_specs()
        previous_context = ''
        if previous_parts:
            snippets = []
            for prev_index, prev_text in enumerate(previous_parts, start=1):
                prev_title = part_specs[prev_index - 1][0]
                snippets.append(
                    f'【已完成部分 {prev_index}：{prev_title}】\n'
                    f'{truncate_text(prev_text, 900)}'
                )
            previous_context = '\n\n已完成部分摘要（用于保持命名、设定、人物关系一致，不要整段复写）：\n' + '\n\n'.join(snippets)

        if self._is_title_only_story():
            return f"""
你正在为一篇单篇中文短篇小说制作“短篇工作圣经”的分部分草稿，本次只生成其中一个部分，而不是整份总稿。

当前任务：
- 当前部分：第 {part_index} 部分《{part_title}》
- 本部分职责：{part_focus}

硬性目标：
- 目标总字数：{self.args.target_chars} 字（这是单篇成稿体量参考，不代表要拆成卷或长篇）
- 必须严格服从用户设定，不得偏题、换题材、换文风
- 风格必须适配单篇短篇小说：人物先于设定、情绪穿透优先于连载悬念、结尾要落在最后一屏的余味上
- 本部分只输出当前职责范围内的内容，不要越界包办其他部分
- 必须与已完成部分的书名、术语、人物称谓、意象称谓保持完全一致
- 请使用 Markdown 小标题，首行固定写：## 第{part_index}部分：{part_title}
- 输出信息密度高，避免空话和重复，长度尽量控制在 1000~1800 字

补充要求：
- 所有信息都必须服务于“同一篇内完成建立人物、压上冲突、付出代价、留下最后一屏”
- 禁止使用“卷、长篇路线、连载节奏、阶段性爽点、卷末钩子、后续再展开”等长篇术语
- 优先给出能直接指导单篇成稿的具体信息，而不是泛泛抽象判断
- 如果用户设定里有创新点，务必把创新点落实为具体矛盾、具体物件、具体动作或具体画面
{previous_context}

用户设定如下：
{brief}
"""

        return f"""
你正在为一部{'单篇中文短篇小说' if self._is_title_only_story() else '长篇中文网络小说'}制作《系列圣经》的分卷稿，本次只生成其中一个部分，而不是整份总稿。

当前任务：
- 当前部分：第 {part_index} 部分《{part_title}》
- 本部分职责：{part_focus}

硬性目标：
- 目标总字数：{self.args.target_chars} 字
- 必须严格服从用户设定，不得偏题、换题材、换文风
- 风格必须适配{'单篇短篇小说：人物先于设定、情绪穿透优先于连载悬念、结尾要落在最后一屏的余味上' if self._is_title_only_story() else '中文网文连载：强钩子、强冲突、强追读、强悬念'}
- 本部分只输出当前职责范围内的内容，不要越界包办其他部分
- 必须与已完成部分的书名、术语、人物称谓、势力称谓保持完全一致
- 请使用 Markdown 小标题，首行固定写：## 第{part_index}部分：{part_title}
- 输出信息密度高，避免空话和重复，长度尽量控制在 1400~2200 字

补充要求：
- 如果当前部分需要引用前面已经定下的设定，可以简短承接，但不要大段重复
- 优先给出真正能支撑目标量级连载的可执行信息，而不是泛泛而谈
- 如果用户设定里有创新点，务必把创新点落实为明确规则、矛盾和长线钩子
{previous_context}

用户设定如下：
{brief}
"""

    def _generate_series_bible_from_parts(self, brief: str) -> str:
        part_specs = self._series_bible_part_specs()
        parts: list[str] = []
        for part_index, (part_title, part_focus) in enumerate(part_specs, start=1):
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

        if self._is_title_only_story():
            short_prompt = f"""
请把下面这份“短篇工作圣经”压缩成供后续单篇写作调用的短版记忆，不超过 1200 字。
要求保留：核心卖点、主角定位、被救者/关键配角作用、私人亏欠、生活残片、世界规则、主冲突、最后一屏、文风提醒。
禁止写成“卷级路线”“后续展开计划”。

原文如下：
{bible_text}
"""
        else:
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
        self._sync_memory_retrieval_file('series_bible', self.series_bible_path)
        self._sync_memory_retrieval_file('series_bible_short', self.series_bible_short_path)

        title_match = re.search(r'推荐书名[：:]\s*(.+)', bible_text)
        if title_match:
            self.state['book_title'] = title_match.group(1).strip()
            self._save_state()

    def ensure_volume_plan(self, volume_number: int) -> Path:
        volume_dir = self._volume_dir(volume_number)
        plan_path = volume_dir / 'plan.md'
        if plan_path.exists():
            return plan_path

        plan_label = self._plan_stage_label(volume_number)

        chapter_start = (volume_number - 1) * self.args.chapters_per_volume + 1
        chapter_end = chapter_start + self.args.chapters_per_volume - 1
        series_bible_short = truncate_text(read_text(self.series_bible_short_path), 1200)
        story_memory = truncate_text(read_text(self.story_memory_path), 1200)
        recent = truncate_text(self.recent_chapter_summaries(limit=4), 800)
        ending_guidance = self._ending_guidance_text(limit=1000)

        if self._is_title_only_story():
            prompt = f"""
请规划这篇单篇短故事的整体路线图。

基础信息：
- 唯一正文：第 {chapter_start} 章
- 目标：单篇闭环，人物先成立，情感后穿透，结尾要靠画面收住而不是靠总结点题
- 输出尽量控制在 1600~2600 字，聚焦最关键的戏剧推进与情绪落点

必须坚持：
- 严格服从当前作品设定与题材
- 不要按长篇连载思维拆钩子、拆高潮、拆尾声
- 必须先把人物、私人亏欠、现实代价立起来，再让制度与规则压上去
- 至少给出一条“被救者的真实生活残片”与一条“主角私人亏欠的具体物件/动作”
- 结尾必须是一个能留下余味的最后画面，不要写成制度总结会

【系列圣经短版】
{series_bible_short}

【前情压缩记忆】
{story_memory or '（暂无）'}

【最近章节摘要】
{recent or '（暂无）'}

{ending_guidance}

输出格式：
# 单篇路线图
## 核心人物与私人亏欠
## 主冲突
## 情绪推进
## 关键场景
## 被救者必须具备的生活残片
## 结尾最后一屏
## 必须避免的写法
"""
        else:
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
            plan_label,
            lambda: self.call_llm(plan_label, prompt, self.planner_model),
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

    def _chapter_summary_block(self, items: list[dict[str, Any]], limit: int = 8, *, from_head: bool = False) -> str:
        selected = items[:limit] if from_head else items[-limit:]
        blocks = []
        for item in selected:
            summary = read_text(Path(item.get('summary_file', ''))).strip()
            if not summary:
                continue
            blocks.append(f"第{item['chapter_number']}章：{summary}")
        return '\n'.join(blocks).strip()

    def _chapter_fulltext_block(
        self,
        items: list[dict[str, Any]],
        limit: int,
        *,
        from_head: bool = False,
        per_chapter_limit: int = 3500,
    ) -> str:
        selected = items[:limit] if from_head else items[-limit:]
        blocks = []
        for item in selected:
            draft_path = Path(item.get('draft_file', ''))
            if not draft_path.exists():
                continue
            chapter_number = int(item.get('chapter_number', 0) or 0)
            title = str(item.get('title', '') or '').strip() or format_chapter_heading(chapter_number, '')
            text = truncate_text(read_text(draft_path), per_chapter_limit)
            if not text:
                continue
            blocks.append(f'=== 第{chapter_number}章：{title} ===\n{text}')
        return '\n\n'.join(blocks).strip()

    def _completion_mode(self) -> str:
        return str(getattr(self.args, 'completion_mode', 'hard_target') or 'hard_target').strip().lower()

    def _effective_min_target_chars(self) -> int:
        min_target = int(getattr(self.args, 'min_target_chars', 0) or 0)
        if min_target > 0:
            return min_target
        target_chars = int(getattr(self.args, 'target_chars', 0) or 0)
        return max(0, target_chars)

    def _effective_force_finish_chars(self) -> int:
        force_finish = int(getattr(self.args, 'force_finish_chars', 0) or 0)
        if force_finish > 0:
            return force_finish
        state_force_finish = int(getattr(self, 'state', {}).get('force_finish_chars', 0) or 0)
        if state_force_finish > 0:
            return state_force_finish
        return 0

    def _effective_max_target_chars(self) -> int:
        max_target = int(getattr(self.args, 'max_target_chars', 0) or 0)
        if max_target > 0:
            return max_target
        state_max_target = int(getattr(self, 'state', {}).get('max_target_chars', 0) or 0)
        if state_max_target > 0:
            return state_max_target
        force_finish = self._effective_force_finish_chars()
        if force_finish > 0:
            return math.ceil(force_finish * 1.5)
        return 0

    def _in_finale_mode(self) -> bool:
        if self._completion_mode() != 'min_chars_and_story_end':
            return False
        min_target = self._effective_min_target_chars()
        if min_target <= 0:
            return self.state.get('generated_chapters', 0) > 0
        return self.state.get('generated_chars', 0) >= min_target

    def _in_force_finish_mode(self) -> bool:
        if self._completion_mode() != 'min_chars_and_story_end':
            return False
        force_finish = self._effective_force_finish_chars()
        if force_finish <= 0:
            return False
        return int(self.state.get('generated_chars', 0) or 0) >= force_finish

    def _terminal_finish_runway_chars(self) -> int:
        max_target_chars = self._effective_max_target_chars()
        if max_target_chars <= 0:
            return 0
        recent_average = self._recent_average_chapter_chars()
        return max(180_000, min(320_000, recent_average * 40))

    def _ending_guidance_pressure_runway_chars(self) -> int:
        recent_average = self._recent_average_chapter_chars()
        return max(80_000, min(180_000, recent_average * 18))

    def _remaining_absolute_runway_chars(self) -> int:
        max_target_chars = self._effective_max_target_chars()
        if max_target_chars <= 0:
            return 0
        return max(0, max_target_chars - int(self.state.get('generated_chars', 0) or 0))

    def _in_terminal_finish_mode(self) -> bool:
        if not self._in_force_finish_mode():
            return False
        max_target_chars = self._effective_max_target_chars()
        if max_target_chars <= 0:
            return False
        return self._remaining_absolute_runway_chars() <= self._terminal_finish_runway_chars()

    def _completion_same_remaining_streak(self, remaining_chapters: int) -> int:
        target = max(0, int(remaining_chapters or 0))
        if target <= 0:
            return 0
        streak = 0
        for item in reversed(list(self.state.get('completion_check_history') or [])):
            if item.get('is_complete'):
                break
            if int(item.get('remaining_chapters', 0) or 0) != target:
                break
            streak += 1
        return streak

    def _is_tail_only_completion_missing(self, item: str) -> bool:
        text = str(item or '').strip()
        if not text:
            return True

        hard_block_keywords = (
            '主线', '谜团', '真相', '源头', '责任链', '胜负', '资格', '原位',
            '第一页', '首字', '黑幕', '反派', '翻盘', '身份之谜', '最终解释',
            '命运落点', '规则级', '最大谜团',
        )
        if any(keyword in text for keyword in hard_block_keywords):
            return False

        tail_keywords = (
            '尾声', '后日谈', '后记', '远期', '长期', '时间层', '后世', '多年后',
            '定格', '最后一次', '最终确认', '常态化', '无旧人参与', '旧人参与',
            '旧人彻退', '人已彻退', '普通人', '静态画面', '翻页', '合簿',
            '主栏永空', '页边旧墨', '独立后日谈', '长期落点', '后辈',
        )
        return any(keyword in text for keyword in tail_keywords)

    def _tail_only_completion_stagnation(self, completion_report: dict[str, Any]) -> dict[str, Any]:
        if self._completion_mode() != 'min_chars_and_story_end':
            return {'triggered': False}
        if self._is_title_only_story():
            return {'triggered': False}
        if completion_report.get('is_complete'):
            return {'triggered': False}
        if not self._in_terminal_finish_mode():
            return {'triggered': False}

        remaining_chapters = int(completion_report.get('remaining_chapters', 0) or 0)
        if remaining_chapters != 1:
            return {'triggered': False}

        missing = [str(item).strip() for item in (completion_report.get('missing') or []) if str(item).strip()]
        if not missing:
            return {'triggered': False}
        if not all(self._is_tail_only_completion_missing(item) for item in missing):
            return {'triggered': False}

        streak = self._completion_same_remaining_streak(remaining_chapters)
        if streak < 3:
            return {'triggered': False}

        return {
            'triggered': True,
            'streak': streak,
            'missing': missing,
        }

    def _rewrite_pending_from_chapter(self) -> int:
        rewrite_from = int(self.state.get('rewrite_required_from_chapter', 0) or 0)
        if rewrite_from <= 0:
            return 0
        max_completed = max(
            [int(item.get('chapter_number', 0) or 0) for item in self.state.get('completed_chapters', [])] or [0]
        )
        return rewrite_from if max_completed < rewrite_from else 0

    def _in_ending_guidance_pressure_mode(self) -> bool:
        if self._completion_mode() != 'min_chars_and_story_end':
            return False
        if int(self.state.get('generated_chapters', 0) or 0) <= 0:
            return False
        current_chars = int(self.state.get('generated_chars', 0) or 0)
        pressure_runway = self._ending_guidance_pressure_runway_chars()
        force_finish = self._effective_force_finish_chars()
        if force_finish > 0 and current_chars >= max(0, force_finish - pressure_runway):
            return True
        max_target_chars = self._effective_max_target_chars()
        if max_target_chars > 0 and self._remaining_absolute_runway_chars() <= max(
            pressure_runway,
            self._terminal_finish_runway_chars(),
        ):
            return True
        completion_check = self.state.get('completion_check') or {}
        conservative_remaining_chapters = int(completion_check.get('conservative_remaining_chapters', 0) or 0)
        return self._in_finale_mode() and conservative_remaining_chapters > 0 and conservative_remaining_chapters <= 6

    def _ending_guidance_refresh_interval(self) -> int:
        if self._in_terminal_finish_mode():
            return 1
        if self._in_force_finish_mode():
            return 1
        if self._in_ending_guidance_pressure_mode():
            return 2
        return 0

    def _ending_guidance_mode_label(self) -> str:
        if self._in_terminal_finish_mode():
            return '绝对上限冲刺'
        if self._in_force_finish_mode():
            return '强制收束'
        if self._in_ending_guidance_pressure_mode():
            return '压线预警'
        return '常规'

    def _recent_average_chapter_chars(self, limit: int = 8) -> int:
        completed = self.state.get('completed_chapters', [])
        recent = [
            int(item.get('chars', 0) or 0)
            for item in completed[-limit:]
            if int(item.get('chars', 0) or 0) > 0
        ]
        if not recent:
            return max(1200, int(self.args.chapter_char_target))
        return max(int(self.args.chapter_char_target), math.ceil(sum(recent) / len(recent)))

    def _ending_quality_guidance_refresh_interval(self) -> int:
        if self._in_terminal_finish_mode():
            return 1
        if self._in_force_finish_mode():
            return 1
        if self._in_ending_guidance_pressure_mode():
            return 1
        if self._in_finale_mode():
            completion_check = self.state.get('completion_check') or {}
            conservative_remaining = int(
                completion_check.get('conservative_remaining_chapters')
                or completion_check.get('remaining_chapters', 0)
                or 0
            )
            if 0 < conservative_remaining <= 6:
                return 2
        return 0

    def ensure_opening_promise(self, force: bool = False) -> str:
        completed = self.state.get('completed_chapters', [])
        if not completed:
            return ''

        if not force and self.opening_promise_path.exists():
            return read_text(self.opening_promise_path).strip()

        brief_text = truncate_text(read_text(self.brief_path), 1800)
        series_bible = truncate_text(read_text(self.series_bible_short_path), 1200)
        opening_summaries = self._chapter_summary_block(completed, limit=3, from_head=True)
        opening_fulltext = self._chapter_fulltext_block(completed, limit=3, from_head=True, per_chapter_limit=3200)
        prompt = f"""
请提炼这部长篇小说的“开篇承诺”，供后续终局质量控制使用。

你的任务不是总结全书，而是准确说清：
1. 读者一开始为什么会被抓住。
2. 开篇真正抛出了什么核心问题、关系张力、情绪承诺或意象承诺。
3. 结尾如果只是把事情办完、解释完，会在哪些地方显得虎头蛇尾。
4. 真正有力的终局，必须回应什么，最好以什么方式回应。

要求：
- 只基于设定与开篇材料判断，不要杜撰中后期情节。
- 重点回答“读者最先被什么吸进去”和“结尾必须兑现什么”。
- 允许提炼出一个最值得回响的开篇意象/动作/处境。
- 总长度控制在 500~900 字。

输出格式必须严格如下：
# 开篇承诺
## 读者最先被什么抓住
## 开头提出的核心问题
## 核心情绪 / 关系 / 意象
## 结尾必须兑现什么
## 最容易写砸的收尾方式

【作品设定】
{brief_text or '（无）'}

【系列圣经短版】
{series_bible or '（无）'}

【开篇章节摘要】
{opening_summaries or '（无）'}

【开篇章节正文】
{opening_fulltext or '（无）'}
""".strip()
        opening_promise = self.with_retry(
            '开篇承诺提炼',
            lambda: self.call_llm(
                '开篇承诺提炼',
                prompt,
                self.ending_polish_model,
                system_prompt='你是中文长篇小说的开篇承诺编辑，擅长提炼读者入坑理由与终局兑现义务。',
            ),
        )
        self.clear_error()
        write_text(self.opening_promise_path, opening_promise.strip())
        self.state['last_opening_promise_refresh_chapter'] = int(self.state.get('generated_chapters', 0) or 0)
        self._save_state()
        return opening_promise.strip()

    def _ending_quality_guidance_text(self, limit: int = 1200) -> str:
        blocks = []
        if self._in_finale_mode():
            opening_promise = truncate_text(read_text(self.opening_promise_path), min(limit, 800)).strip()
            if opening_promise:
                blocks.append('【开篇承诺：终局必须回应】\n' + opening_promise)

        auto_quality = truncate_text(read_text(self.auto_ending_quality_guidance_path), limit).strip()
        if auto_quality:
            blocks.append('【自动终局质量指引】\n' + auto_quality)

        polish_brief = truncate_text(read_text(self.ending_polish_brief_path), limit).strip()
        if polish_brief:
            blocks.append('【终局回修要求：本轮写作必须遵守】\n' + polish_brief)

        return '\n\n'.join(blocks).strip()

    def _ending_guidance_text(self, limit: int = 1200) -> str:
        blocks = []

        manual_guidance = read_text(self.ending_guidance_path).strip()
        if manual_guidance:
            blocks.append(
                '【完结要求：高优先级，若与旧卷计划冲突，以此为准】\n'
                + truncate_text(manual_guidance, limit)
            )

        auto_guidance = read_text(self.auto_ending_guidance_path).strip()
        if auto_guidance:
            blocks.append(
                '【自动终局收束指引：依据当前字数压力与剩余跑道生成】\n'
                + truncate_text(auto_guidance, limit)
            )

        quality_guidance = self._ending_quality_guidance_text(limit=limit)
        if quality_guidance:
            blocks.append(quality_guidance)

        if self._in_finale_mode():
            completion_check = self.state.get('completion_check') or {}
            auto_lines = [
                '【自动收束要求】',
                '- 当前已进入全书终局收束阶段，优先解决主线、终局、尾声、后日谈。',
                '- 不要再新增需要多卷回收的新大坑、新地图、新主反派。',
                '- 允许新信息，但只能服务当前终局回收与情绪收束。',
            ]
            if self._in_force_finish_mode():
                auto_lines.extend([
                    '- 当前已超过强制收束阈值，后续章节必须高密度回收，不能再把终局拆成长段新篇章。',
                    '- 如果单章足以完成一个关键终局环节、尾声或后日谈，可以直接完成，不强制为下一章留人工续命钩子。',
                ])
            if self._in_terminal_finish_mode():
                auto_lines.extend([
                    f'- 当前距离最终绝对上限仅剩约 {self._remaining_absolute_runway_chars()} 字，后续已经不是常规收尾，而是最后冲刺。',
                    '- 从现在开始，章节功能只允许是“终局落判 / 命运落点 / 尾声 / 后日谈”之一或其组合。',
                    '- 禁止再引入新的主反派、新证人、新地图、新制度分支、新悬案；若仍有解释空白，必须选择最能闭环的一种解释直接落槌。',
                    '- 每章至少完整关闭一个未解决事项；若一章能够同时关闭多项终局任务，必须合并完成。',
                    '- 若正文终局已经成立，本章或下一章应直接转入尾声；若尾声已成立，下一章应直接进入后日谈并准备全文结束。',
                ])
            missing = completion_check.get('missing') or []
            if missing:
                auto_lines.append('- 当前仍缺内容：' + '；'.join(str(item) for item in missing[:6]))
            remaining_chapters = int(completion_check.get('remaining_chapters', 0) or 0)
            remaining_chars = int(completion_check.get('remaining_chars', 0) or 0)
            conservative_remaining_chapters = int(completion_check.get('conservative_remaining_chapters', 0) or 0)
            conservative_remaining_chars = int(completion_check.get('conservative_remaining_chars', 0) or 0)
            if remaining_chapters > 0:
                auto_lines.append(f'- 当前工作估计还需约 {remaining_chapters} 章完成自然收尾。')
            if remaining_chars > 0:
                auto_lines.append(f'- 当前工作估计还需约 {remaining_chars} 字完成自然收尾。')
            if conservative_remaining_chapters > 0:
                auto_lines.append(
                    f'- 保守估计还需约 {conservative_remaining_chapters} 章，规划时应按这个余量避免机械三章收尾。'
                )
            if conservative_remaining_chars > 0:
                auto_lines.append(f'- 保守估计还需约 {conservative_remaining_chars} 字。')
            estimate_note = str(completion_check.get('estimate_note', '') or '').strip()
            if estimate_note:
                auto_lines.append(f'- 保守估计依据：{estimate_note}')
            next_phase_goal = str(completion_check.get('next_phase_goal', '') or '').strip()
            if next_phase_goal:
                auto_lines.append(f'- 下一阶段目标：{next_phase_goal}')
            blocks.append('\n'.join(auto_lines))

        return '\n\n'.join(blocks).strip()

    def refresh_ending_quality_guidance(self, force: bool = False) -> None:
        completed = self.state.get('completed_chapters', [])
        if not completed or not self._in_finale_mode():
            return
        pending_rewrite_from = self._rewrite_pending_from_chapter()
        if pending_rewrite_from:
            self.log(
                f'[ending_quality] 已进入终局回修待写状态（需从第{pending_rewrite_from}章补回），'
                '暂停刷新终局质量指引，避免对回退后的半成品重复评估。'
            )
            return

        interval = self._ending_quality_guidance_refresh_interval()
        if not force:
            if interval <= 0:
                return
            last_refresh = int(self.state.get('last_ending_quality_guidance_refresh_chapter', 0) or 0)
            current_chapter = int(self.state.get('generated_chapters', 0) or 0)
            if current_chapter - last_refresh < interval:
                return

        completion_check = self.state.get('completion_check') or {}
        if force or not completion_check or int(completion_check.get('checked_at_chapter', -1) or -1) != int(self.state.get('generated_chapters', 0) or 0):
            completion_check = self.evaluate_completion_status(force=force)

        opening_promise = truncate_text(self.ensure_opening_promise(force=False), 1200)
        recent_summaries = truncate_text(self.recent_chapter_summaries(limit=8), 2000)
        last_draft_tail = ''
        if completed:
            last_draft_tail = tail_text(read_text(Path(completed[-1]['draft_file'])), 2200)
        story_memory = truncate_text(read_text(self.story_memory_path), 1800)
        manual_guidance = truncate_text(read_text(self.ending_guidance_path), 1400)
        auto_guidance = truncate_text(read_text(self.auto_ending_guidance_path), 1400)
        ending_research = ending_craft_research_text(1000)

        prompt = f"""
请为这部长篇处于终局阶段的最后少量章节，生成一份“终局质量指引”。

你的目标不是防止没写完，而是防止“只收账、不回响”的虎头蛇尾。

请尤其处理下面这些问题：
1. 结尾必须回应开篇承诺，而不只是把流程办结。
2. 终章/尾声/后日谈需要留下一个能让读者停下来的回响：可以是人物选择、代价、意象回声、价值反照、反讽、迟来的平静，但不能只是总结。
3. 允许留余味，但不能靠不交代关键义务来制造“开放感”。
4. 避免“制度说明会 / 办结通知 / 连续解释 / 重复宣判 / 情绪下坠”。
5. 如果当前结尾已经偏解释化，请明确告诉作者最后1~2章要把什么重新抬起来。
6. 参考外部成熟收尾经验：避免“高潮后反复落锤”“把结尾写成说明会”“用没写完冒充余味”；尽量让最后一屏可停留、可回响。

要求：
- 只面向最后极少量章节，不要写大而空的文学议论。
- 必须落到当前作品已建立的冲突、人物、关系和意象上。
- 必须明确写出“最后一屏/最后一段应给读者什么感觉”。
- 总长度控制在 700~1400 字。

输出格式必须严格如下：
# 终局质量指引
## 开篇承诺回看
## 当前终局最容易失手的地方
## 必须打中的主题回答
## 最后一屏建议
## 禁止的写法
## 下一章质量目标

【开篇承诺】
{opening_promise or '（无）'}

【外部收尾经验摘要】
{ending_research or '（无）'}

【故事记忆】
{story_memory or '（无）'}

【手工完结要求】
{manual_guidance or '（无）'}

【自动终局收束指引】
{auto_guidance or '（无）'}

【最近章节摘要】
{recent_summaries or '（无）'}

【最近正文尾段】
{last_draft_tail or '（无）'}

【当前完结评估】
- 是否完结：{'是' if completion_check.get('is_complete') else '否'}
- 建议还需章节：{int(completion_check.get('remaining_chapters', 0) or 0)}
- 保守估计还需章节：{int(completion_check.get('conservative_remaining_chapters', 0) or 0)}
- 仍缺内容：{'；'.join(str(item) for item in (completion_check.get('missing') or [])) or '无'}
- 下一阶段目标：{str(completion_check.get('next_phase_goal', '') or '').strip() or '无'}
""".strip()

        guidance_text = self.with_retry(
            '终局质量指引',
            lambda: self.call_llm(
                '终局质量指引',
                prompt,
                self.ending_polish_model,
                system_prompt='你是中文长篇小说的终局质量编辑，擅长让结尾拥有与开篇相匹配的抓力、回响和余味。',
            ),
        )
        self.clear_error()
        write_text(self.auto_ending_quality_guidance_path, guidance_text.strip())
        self.state['last_ending_quality_guidance_refresh_chapter'] = int(self.state.get('generated_chapters', 0) or 0)
        self._save_state()
        self.log(
            f'[ending_quality] 已刷新终局质量指引：模式={self._ending_guidance_mode_label()}，'
            f'章节={self.state.get("generated_chapters", 0)}，字数={self.state.get("generated_chars", 0)}'
        )

    def refresh_ending_guidance(self, force: bool = False) -> None:
        completed = self.state.get('completed_chapters', [])
        if not completed:
            return
        pending_rewrite_from = self._rewrite_pending_from_chapter()
        if pending_rewrite_from:
            self.log(
                f'[ending_guidance] 已进入终局回修待写状态（需从第{pending_rewrite_from}章补回），'
                '暂停刷新完结评估/自动收束指引，避免把回退后的中间稿误判成最终状态。'
            )
            return

        interval = self._ending_guidance_refresh_interval()
        if not force:
            if interval <= 0:
                return
            last_refresh = int(self.state.get('last_ending_guidance_refresh_chapter', 0) or 0)
            current_chapter = int(self.state.get('generated_chapters', 0) or 0)
            if current_chapter - last_refresh < interval:
                return

        completion_check = self.state.get('completion_check') or {}
        if force or not completion_check or int(completion_check.get('checked_at_chapter', -1) or -1) != int(self.state.get('generated_chapters', 0) or 0):
            completion_check = self.evaluate_completion_status(force=force)

        recent_summaries = truncate_text(self.recent_chapter_summaries(limit=10), 2400)
        last_draft_tail = ''
        if completed:
            last_draft_tail = truncate_text(read_text(Path(completed[-1]['draft_file'])), 2200)
        pending_outlines = truncate_text(self._recent_pending_outlines(limit=3), 1800)
        story_memory = truncate_text(read_text(self.story_memory_path), 1800)
        series_bible = truncate_text(read_text(self.series_bible_short_path), 1200)
        manual_guidance = truncate_text(read_text(self.ending_guidance_path), 1600)
        ending_research = ending_craft_research_text(1000)

        prompt = f"""
请为这部长篇生成一份“自动终局收束指引”，供接下来极少量章节的规划与正文直接遵循。

你的目标不是继续扩写，而是：
1. 在不烂尾的前提下，随着字数压力上升，尽快自然收束。
2. 明确哪些终局任务必须优先回收，哪些可以合并，哪些绝对不能再开。
3. 让后续章节在剩余跑道内完成“终局 / 尾声 / 后日谈”。
4. 参考外部成熟收尾经验：不要在高潮后长时间磨损尾劲；落锤后最多保留一次现实验证和一个极短余韵，不要把尾声写成连续说明会。

当前模式：{self._ending_guidance_mode_label()}

硬要求：
- 只面向“接下来很少的章节”给指引，不要写长线规划。
- 不要复述圣经大词，要落到当前尾段实际还缺什么。
- 若当前已进入强制收束或绝对上限冲刺，必须主动压缩，不允许再建议宽松展开。
- 必须明确列出“禁止新增”的内容。
- 必须明确给出“下一章最优先完成什么”。
- 总长度控制在 700~1400 字。

输出格式必须严格如下：
# 自动终局收束指引
## 当前状态判断
## 必须立即回收的事项
- 条目
## 可合并完成的事项
- 条目
## 接下来1-3章建议
- 条目
## 禁止新增
- 条目
## 下一章最高优先级
- 条目

【系列圣经短版】
{series_bible or '（无）'}

【外部收尾经验摘要】
{ending_research or '（无）'}

【故事记忆】
{story_memory or '（无）'}

【手工完结要求】
{manual_guidance or '（无）'}

【最近章节摘要】
{recent_summaries or '（无）'}

【最近正文尾段】
{last_draft_tail or '（无）'}

【已规划未写大纲】
{pending_outlines or '（无）'}

【当前完结评估】
- 是否完结：{'是' if completion_check.get('is_complete') else '否'}
- 建议还需章节：{int(completion_check.get('remaining_chapters', 0) or 0)}
- 建议还需字数：{int(completion_check.get('remaining_chars', 0) or 0)}
- 保守估计还需章节：{int(completion_check.get('conservative_remaining_chapters', 0) or 0)}
- 保守估计还需字数：{int(completion_check.get('conservative_remaining_chars', 0) or 0)}
- 仍缺内容：{'；'.join(str(item) for item in (completion_check.get('missing') or [])) or '无'}
- 保守估计依据：{str(completion_check.get('estimate_note', '') or '').strip() or '无'}
- 下一阶段目标：{str(completion_check.get('next_phase_goal', '') or '').strip() or '无'}

【字数压力】
- 当前总字数：{int(self.state.get('generated_chars', 0) or 0)}
- 强制收束阈值：{self._effective_force_finish_chars()}
- 最终绝对上限：{self._effective_max_target_chars()}
- 剩余绝对跑道：{self._remaining_absolute_runway_chars()}
- 压线预警跑道：{self._ending_guidance_pressure_runway_chars()}
""".strip()

        guidance_text = self.with_retry(
            '自动终局收束指引',
            lambda: self.call_llm('自动终局收束指引', prompt, self.planner_model),
        )
        self.clear_error()
        write_text(self.auto_ending_guidance_path, guidance_text.strip())
        self.state['last_ending_guidance_refresh_chapter'] = int(self.state.get('generated_chapters', 0) or 0)
        self._save_state()
        self.log(
            f'[ending_guidance] 已刷新自动终局收束指引：模式={self._ending_guidance_mode_label()}，'
            f'章节={self.state.get("generated_chapters", 0)}，字数={self.state.get("generated_chars", 0)}'
        )

    def _build_ending_quality_prompt(self) -> str:
        completed = list(self.state.get('completed_chapters', []) or [])
        if not completed:
            raise RuntimeError('尚无已完成章节，无法评估终局质量。')

        current_chapter = int(self.state.get('generated_chapters', 0) or 0)
        repeated_polish_mode = (
            int(self.state.get('ending_polish_cycles', 0) or 0) > 0
            or self._rewrite_pending_from_chapter() > 0
            or int(self.state.get('ending_polish_last_rewrite_from_chapter', 0) or 0) > 0
        )
        opening_promise = truncate_text(self.ensure_opening_promise(force=False), 1200 if repeated_polish_mode else 1400)
        brief_text = truncate_text(read_text(self.brief_path), 1500 if repeated_polish_mode else 1800)
        series_bible = truncate_text(read_text(self.series_bible_short_path), 1200 if repeated_polish_mode else 1400)
        story_memory = truncate_text(read_text(self.story_memory_path), 1800 if repeated_polish_mode else 2200)
        completion_report = truncate_text(read_text(self.completion_report_path), 1800 if repeated_polish_mode else 2200)
        ending_guidance = truncate_text(self._ending_guidance_text(limit=1200), 1400 if repeated_polish_mode else 1800)
        ending_research = ending_craft_research_text(700 if repeated_polish_mode else 1000)
        opening_summaries = self._chapter_summary_block(completed, limit=2 if repeated_polish_mode else 3, from_head=True)
        opening_fulltext = self._chapter_fulltext_block(
            completed,
            limit=2 if repeated_polish_mode else 3,
            from_head=True,
            per_chapter_limit=2400 if repeated_polish_mode else 3200,
        )
        tail_summaries = self._chapter_summary_block(completed, limit=10 if repeated_polish_mode else 14, from_head=False)
        tail_fulltext = self._chapter_fulltext_block(
            completed,
            limit=8 if repeated_polish_mode else 12,
            from_head=False,
            per_chapter_limit=3200 if repeated_polish_mode else 4200,
        )
        full_novel_tail = truncate_text(read_text(self.full_manuscript_path), 12000 if repeated_polish_mode else 18000)
        book_title = self._project_book_title()
        story_kind = '单篇短篇小说' if self._is_title_only_story() else '长篇小说'
        editor_kind = '中文短篇小说的终稿编辑' if self._is_title_only_story() else '中文长篇连载的终局质量编辑'
        last_rewrite_from = int(self.state.get('ending_polish_last_rewrite_from_chapter', 0) or 0)
        quality_extra_rule = (
            '对单篇短篇，不要机械要求“尾声”“后日谈”作为独立段落；只要单篇内部已经完成回响与收束，就可以判定为通过。'
            if self._is_title_only_story()
            else '开放式必须是“有意悬置的思考”，不是关键义务未兑现。'
        )

        return f"""
请评估这部{story_kind}“当前结尾版本”的终局质量。重点不是它有没有写完，而是它是否真正配得上自己的开篇抓力。

你是{editor_kind}。请优先判断：
1. 开篇承诺、开头立起的核心问题/关系/意象，是否在结尾得到回应、升格、反照或有力的反讽。
2. 结尾是否只是“把事情办完”，还是留下了一个能让读者停下来的 final image / final choice / final irony / final question。
3. 结尾是否过度解释、制度说明化、总结会化、办结化，导致情绪与思想火力下降。
4. 是否存在虎头蛇尾：开头极抓人，结尾却只剩收账、复述、总结。
5. 允许留余味，但不能靠没写完制造余味；{quality_extra_rule}
6. 参考外部成熟收尾经验：高潮后的重复落锤、解释化尾声、同构案例反复验证、尾声长于必要幅度，都应显著扣分。
7. 商用品质达标即可通过，不要把“还能更好”误判成“必须回修”。

通过/回修判定硬规则：
- 若综合质量分 >= 85、回响分 >= 90、余味/思考分 >= 85，且不存在关键义务缺失、关键冲突未闭环、人物命运未落点、最后一屏失效等硬伤，默认应判 `quality_pass=true`、`needs_polish=false`。
- 只有当“不回修会明显损害读者完成感”时，才允许判 `needs_polish=true`。
- 如果只是还能更凝练、更高级、更有文学感，但现稿已经达到可发布、可合书、可让读者停下来的水准，不要要求回修。

回修规则：
- 如果无需回修，`rewrite_from_chapter` 必须为 0。
- 如果只需小修最后一章，也必须写出最后一章章节号。
- 如果问题集中在最后两三章的气质和火力，而不是更早布局，就不要把起点乱提早。
- 不要为了追求“文学感”破坏已经完成的主线闭环与人物落点。
- 若需要回修，优先使用作品早已建立的冲突、关系、意象和代价来形成回响，不要再新开设定。
- 最近一次已执行的终局回修起点：{last_rewrite_from or 0}。如果本次仍想把回修起点继续前推到更早章节，必须在 `evidence` 里给出至少 2 条落在更早章节的直接证据，并在 `rewrite_scope_reason` 里明确说明“为什么仅改更晚章节无法补救”。
- 不允许因为“整体气质更完整”“更有余味”“更像一体”这类泛化表述，就把回修范围从最后一章直接扩到更早数章；没有硬证据时，必须坚持最小必要范围。

请返回严格 JSON：
{{
  "project_name": "{self.project_dir.name}",
  "book_title": "{book_title}",
  "quality_pass": true,
  "needs_polish": false,
  "quality_score": 0,
  "resonance_score": 0,
  "thought_provoking_score": 0,
  "confidence": 0,
  "rewrite_from_chapter": 0,
  "diagnosis": ["问题1", "问题2"],
  "strengths": ["优点1", "优点2"],
  "rewrite_goals": ["若要回修，应优先做什么"],
  "must_keep": ["回修时绝对不能丢掉的已完成成果"],
  "final_image_target": "如果需要加强，最后一屏应该朝什么感觉收",
  "editor_summary": "一段专业结论",
  "polish_brief_markdown": "# 终局打磨要求\\n## 必须保留\\n- ...\\n## 必须加强\\n- ...\\n## 禁止写法\\n- ...\\n## 最后1-2章目标\\n- ..."
}}

【作品设定】
{brief_text or '（无）'}

【开篇承诺】
{opening_promise or '（无）'}

【外部收尾经验摘要】
{ending_research or '（无）'}

【系列圣经短版】
{series_bible or '（无）'}

【故事记忆】
{story_memory or '（无）'}

【当前完结评估】
{completion_report or '（无）'}

【当前终局指引】
{ending_guidance or '（无）'}

【开篇章节摘要】
{opening_summaries or '（无）'}

【开篇章节正文】
{opening_fulltext or '（无）'}

【尾段章节摘要】
{tail_summaries or '（无）'}

【最后若干章正文】
{tail_fulltext or '（无）'}

【全文结尾长摘】
{full_novel_tail or '（无）'}

【当前章节号】
{current_chapter}
""".strip()

    def _normalize_ending_quality_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        current_chapter = int(self.state.get('generated_chapters', 0) or 0)

        def normalize_score(value: Any) -> int:
            try:
                return max(0, min(100, int(value or 0)))
            except (TypeError, ValueError):
                return 0

        def normalize_lines(key: str) -> list[str]:
            return [str(item).strip() for item in (payload.get(key) or []) if str(item).strip()]

        needs_polish = bool(payload.get('needs_polish'))
        quality_pass = bool(payload.get('quality_pass')) and not needs_polish
        rewrite_from_chapter = int(payload.get('rewrite_from_chapter', 0) or 0)
        if needs_polish and rewrite_from_chapter <= 0:
            rewrite_from_chapter = current_chapter
        if rewrite_from_chapter > current_chapter:
            rewrite_from_chapter = current_chapter
        if not needs_polish:
            rewrite_from_chapter = 0

        polish_brief = str(payload.get('polish_brief_markdown', '') or '').strip()
        if needs_polish and not polish_brief:
            rewrite_goals = normalize_lines('rewrite_goals')
            must_keep = normalize_lines('must_keep')
            final_image_target = str(payload.get('final_image_target', '') or '').strip()
            lines = ['# 终局打磨要求', '## 必须保留']
            if must_keep:
                lines.extend(f'- {item}' for item in must_keep)
            else:
                lines.append('- 保留现有主线闭环、人物落点和已完成的必要后果。')
            lines.extend(['', '## 必须加强'])
            if rewrite_goals:
                lines.extend(f'- {item}' for item in rewrite_goals)
            else:
                lines.append('- 让最后一两章更有主题反照、人物代价与读后余味。')
            lines.extend(['', '## 禁止写法'])
            lines.extend([
                '- 禁止把最后几章写成制度说明会、办结通知或重复总结。',
                '- 禁止新开设定、新开黑幕、新加大反派来硬拉火力。',
                '- 禁止破坏现有主线闭环与人物最终命运落点。',
            ])
            lines.extend(['', '## 最后1-2章目标'])
            lines.append(f'- {final_image_target or "让最后一屏回应开篇承诺，并留下明确余味，而不是只剩解释。"}')
            polish_brief = '\n'.join(lines).strip()

        return {
            'checked_at_chapter': current_chapter,
            'project_name': str(payload.get('project_name') or self.project_dir.name).strip(),
            'book_title': str(payload.get('book_title') or self._project_book_title()).strip(),
            'quality_pass': quality_pass,
            'needs_polish': needs_polish,
            'quality_score': normalize_score(payload.get('quality_score')),
            'resonance_score': normalize_score(payload.get('resonance_score')),
            'thought_provoking_score': normalize_score(payload.get('thought_provoking_score')),
            'confidence': normalize_score(payload.get('confidence')),
            'rewrite_from_chapter': rewrite_from_chapter,
            'diagnosis': normalize_lines('diagnosis'),
            'strengths': normalize_lines('strengths'),
            'rewrite_goals': normalize_lines('rewrite_goals'),
            'must_keep': normalize_lines('must_keep'),
            'final_image_target': str(payload.get('final_image_target', '') or '').strip(),
            'editor_summary': str(payload.get('editor_summary', '') or '').strip(),
            'polish_brief_markdown': polish_brief,
        }

    def _apply_ending_quality_scope_guard(self, report: dict[str, Any]) -> dict[str, Any]:
        if not report.get('needs_polish'):
            return report

        proposed = int(report.get('rewrite_from_chapter', 0) or 0)
        if proposed <= 0:
            return report

        previous = int(self.state.get('ending_polish_last_rewrite_from_chapter', 0) or 0)
        if previous <= 0 or proposed >= previous:
            return report

        quality_score = int(report.get('quality_score', 0) or 0)
        resonance_score = int(report.get('resonance_score', 0) or 0)
        thought_score = int(report.get('thought_provoking_score', 0) or 0)
        diagnosis_blob = ' '.join(
            [
                *[str(item).strip() for item in (report.get('diagnosis') or []) if str(item).strip()],
                *[str(item).strip() for item in (report.get('rewrite_goals') or []) if str(item).strip()],
                str(report.get('rewrite_scope_reason', '') or '').strip(),
                str(report.get('editor_summary', '') or '').strip(),
            ]
        )
        scope_keywords = (
            '铺垫', '布局', '前置', '前文', '前面', '更早', '无法只改', '不能只改',
            '仅改最后', '只改最后', '前几章', '倒数', '起势', '承接失衡',
        )
        evidence_chapters = sorted(
            {
                int(item.get('chapter', 0) or 0)
                for item in (report.get('evidence') or [])
                if isinstance(item, dict) and int(item.get('chapter', 0) or 0) > 0
            }
        )
        earlier_evidence = [chapter for chapter in evidence_chapters if chapter < previous]
        strong_scope_reason = any(keyword in diagnosis_blob for keyword in scope_keywords)
        severe_gap = quality_score < 78 or resonance_score < 82 or thought_score < 80

        max_extra_backward = 0
        if strong_scope_reason and earlier_evidence:
            max_extra_backward = 1
        if severe_gap and strong_scope_reason and len(earlier_evidence) >= 2:
            max_extra_backward = 2

        guarded_rewrite_from = max(proposed, previous - max_extra_backward)
        if guarded_rewrite_from == proposed:
            return report

        note = (
            f'系统护栏：上一轮终局回修起点是第{previous}章。'
            f'本轮缺少足够证据支持直接扩大到第{proposed}章，已将回修范围收敛到第{guarded_rewrite_from}章。'
        )
        report['rewrite_from_chapter'] = guarded_rewrite_from

        diagnosis = [str(item).strip() for item in (report.get('diagnosis') or []) if str(item).strip()]
        diagnosis.append(note)
        report['diagnosis'] = diagnosis[:8]

        rewrite_goals = [str(item).strip() for item in (report.get('rewrite_goals') or []) if str(item).strip()]
        if max_extra_backward <= 0:
            rewrite_goals.insert(0, f'本轮先在第{guarded_rewrite_from}章内完成最小必要强修，不要再无证据地继续前推回修范围。')
        else:
            rewrite_goals.insert(0, f'本轮最多只允许把回修范围前推到第{guarded_rewrite_from}章，先验证这一章是否足够解决尾段问题。')
        deduped_goals: list[str] = []
        seen_goals: set[str] = set()
        for item in rewrite_goals:
            if item and item not in seen_goals:
                seen_goals.add(item)
                deduped_goals.append(item)
        report['rewrite_goals'] = deduped_goals[:8]

        scope_reason = str(report.get('rewrite_scope_reason', '') or '').strip()
        report['rewrite_scope_reason'] = ((scope_reason + '\n') if scope_reason else '') + note

        polish_brief = str(report.get('polish_brief_markdown', '') or '').strip()
        if polish_brief:
            report['polish_brief_markdown'] = polish_brief + f'\n- {note}'

        return report

    def _write_ending_quality_review(self, report: dict[str, Any]) -> None:
        lines = [
            f"# 终局质量评估（{now_str()}）",
            '',
            f"- 项目：{report.get('project_name', self.project_dir.name)}",
            f"- 书名：{report.get('book_title') or '（未记录）'}",
            f"- 是否通过：{'是' if report.get('quality_pass') else '否'}",
            f"- 是否需要打磨回修：{'是' if report.get('needs_polish') else '否'}",
            f"- 综合质量分：{report.get('quality_score', 0)}",
            f"- 回响分：{report.get('resonance_score', 0)}",
            f"- 余味/思考分：{report.get('thought_provoking_score', 0)}",
            f"- 置信度：{report.get('confidence', 0)}",
            f"- 建议回修起点：{report.get('rewrite_from_chapter', 0)}",
            '',
            '## 主要判断',
        ]
        diagnosis = report.get('diagnosis') or []
        if diagnosis:
            lines.extend(f'- {item}' for item in diagnosis)
        else:
            lines.append('- 无')
        lines.extend(['', '## 已有优点'])
        strengths = report.get('strengths') or []
        if strengths:
            lines.extend(f'- {item}' for item in strengths)
        else:
            lines.append('- 无')
        lines.extend(['', '## 回修时必须保留'])
        must_keep = report.get('must_keep') or []
        if must_keep:
            lines.extend(f'- {item}' for item in must_keep)
        else:
            lines.append('- 无')
        lines.extend(['', '## 若需回修，优先目标'])
        goals = report.get('rewrite_goals') or []
        if goals:
            lines.extend(f'- {item}' for item in goals)
        else:
            lines.append('- 无')
        lines.extend([
            '',
            '## 最后一屏目标',
            report.get('final_image_target') or '（无）',
            '',
            '## 编辑结论',
            report.get('editor_summary') or '（无）',
            '',
            '## 自动回修要求',
            report.get('polish_brief_markdown') or '（无）',
            '',
            '## 原始 JSON',
            '```json',
            json.dumps(report, ensure_ascii=False, indent=2),
            '```',
        ])
        write_text(self.ending_quality_review_path, '\n'.join(lines).strip() + '\n')

    def evaluate_ending_quality(self, force: bool = False) -> dict[str, Any]:
        checked_at_chapter = int((self.state.get('ending_quality_check') or {}).get('checked_at_chapter', -1) or -1)
        current_chapter = int(self.state.get('generated_chapters', 0) or 0)
        if not force and checked_at_chapter == current_chapter:
            existing = self.state.get('ending_quality_check') or {}
            if existing:
                return existing

        prompt = self._build_ending_quality_prompt()
        payload = self.with_retry(
            '终局质量评估',
            lambda: self.call_llm_json(
                '终局质量评估',
                prompt,
                self.ending_polish_model,
                system_prompt='你是中文长篇连载的终局质量编辑，擅长判断结尾是否真正回应开篇、留下余味，并指出最小必要回修范围。',
            ),
        )
        self.clear_error()
        report = self._normalize_ending_quality_report(payload)
        report = self._apply_ending_quality_scope_guard(report)
        self._write_ending_quality_review(report)

        history = list(self.state.get('ending_quality_history') or [])
        history = [
            item for item in history
            if int(item.get('checked_at_chapter', -1) or -1) != current_chapter
        ]
        history.append({
            'checked_at_chapter': current_chapter,
            'quality_pass': bool(report.get('quality_pass')),
            'needs_polish': bool(report.get('needs_polish')),
            'quality_score': int(report.get('quality_score', 0) or 0),
            'rewrite_from_chapter': int(report.get('rewrite_from_chapter', 0) or 0),
        })
        self.state['ending_quality_history'] = history[-20:]
        self.state['ending_quality_check'] = report
        if report.get('quality_pass'):
            self.state['ending_polish_cycles'] = 0
            self.state['ending_polish_last_rewrite_from_chapter'] = 0
            if self.ending_polish_brief_path.exists():
                self.ending_polish_brief_path.unlink()
        self._save_state()
        return report

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
            '如果当前只剩“后日谈/更远时间层定格/旧人彻退后的常态化确认”这类纯尾声任务，且最近几章已经在反复做同类尾段变体，不要机械继续判“还差一章”；此时应直接判定“是否完结：是”，把最后的回响强弱交给终局质量打磨阶段处理。',
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
        sections.insert(
            12,
            '不要机械套用“一章揭晓、一章执行、一章尾声”的固定三段式模板；如果仍有多项独立收束任务，请把这些任务量折算进章节余量。',
        )
        if self._is_title_only_story():
            sections = [
                '请站在中文短篇小说编辑的角度，判断这篇单篇故事现在是否已经具备“完整闭环且值得收住”的条件。',
                '你的判断标准必须严格，不要为了省字数、省成本或凑整数而硬停，也不要把长篇连载的尾声/后日谈模板硬套到短篇上。',
                '',
                '对单篇短篇来说，完结必须同时满足：',
                '1. 主冲突已经真正闭环，读者不会把关键问题理解成“作者还没写到”。',
                '2. 主角的关键选择、代价、命运落点或立场变化已经落地。',
                '3. 被救者、关键对照人物或核心关系至少已有一个清晰落点，而不是只停在功能说明。',
                '4. 结尾已经收在具体画面、动作、残响、反照或反讽上，而不是主题总结。',
                '5. 允许留白，但关键义务不能伪装成留白；缺失的关键信息不能靠“余味”糊过去。',
                '',
                '如果还不够完整，请估算：为了把这篇单篇故事补到真正成立，还需要多少“回修量”。',
                '这里的“建议还需章节”只允许写 0 或 1：0 代表现在就能收住，1 代表还需要整体回修这一篇，而不是新增连载下一章。',
                '“建议还需字数”指为了把这一篇补到成立，大致还需要补写或重写的字数。',
                '',
                '输出格式必须严格如下，不要加别的标题：',
                '是否完结：是/否',
                '置信度：0-100',
                '仍缺内容：',
                '- 缺失项1',
                '- 缺失项2',
                '建议还需章节：N',
                '建议还需字数：N',
                '说明：一句到三句，明确为什么这篇现在还不能收住，或为什么已经可以收住',
                '下一阶段目标：如果未完结，请给一个最合理的整篇回修方向；如果已完结，写“无”',
                '',
                '额外提醒：不要机械要求“尾声”“后日谈”；如果正文已经在单篇内部完成了回响与余味，就视为满足。',
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
        if self._in_force_finish_mode():
            sections.insert(
                13,
                '当前文本已经超过强制收束阈值，后续估算必须按“高压缩完结”处理，不要继续按长篇常规节奏外扩。',
            )
        if self._in_terminal_finish_mode():
            sections.insert(
                14,
                f'当前距离最终绝对上限仅剩约 {self._remaining_absolute_runway_chars()} 字，后续估算必须保证在这段字数内完成全文终局、尾声与后日谈；若常规写法装不下，必须主动合并任务并压缩完结。',
            )

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
        lines.insert(6, f"- 保守估计还需章节：{report.get('conservative_remaining_chapters', 0)}")
        lines.insert(7, f"- 保守估计还需字数：{report.get('conservative_remaining_chars', 0)}")
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
            '## 保守估计依据',
            report.get('estimate_note', '') or '（无）',
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
        history = list(self.state.get('completion_check_history') or [])
        report_text = self.with_retry(
            '完结评估',
            lambda: self.call_llm('完结评估', prompt, self.planner_model),
        )
        self.clear_error()
        report = parse_completion_report(report_text)
        report.update(
            derive_conservative_completion_estimate(
                report=report,
                history=history,
                current_chapter=current_chapter,
                average_chapter_chars=self._recent_average_chapter_chars(),
            )
        )
        report['checked_at_chapter'] = current_chapter
        history = [
            item for item in history
            if int(item.get('checked_at_chapter', -1) or -1) != current_chapter
        ]
        history.append({
            'checked_at_chapter': current_chapter,
            'is_complete': bool(report.get('is_complete')),
            'remaining_chapters': int(report.get('remaining_chapters', 0) or 0),
            'remaining_chars': int(report.get('remaining_chars', 0) or 0),
            'conservative_remaining_chapters': int(report.get('conservative_remaining_chapters', 0) or 0),
            'conservative_remaining_chars': int(report.get('conservative_remaining_chars', 0) or 0),
        })
        self.state['completion_check_history'] = history[-20:]
        self.state['completion_check'] = {
            'checked_at_chapter': current_chapter,
            'is_complete': bool(report.get('is_complete')),
            'confidence': int(report.get('confidence', 0) or 0),
            'missing': list(report.get('missing') or []),
            'remaining_chapters': int(report.get('remaining_chapters', 0) or 0),
            'remaining_chars': int(report.get('remaining_chars', 0) or 0),
            'conservative_remaining_chapters': int(report.get('conservative_remaining_chapters', 0) or 0),
            'conservative_remaining_chars': int(report.get('conservative_remaining_chars', 0) or 0),
            'estimate_note': str(report.get('estimate_note', '') or '').strip(),
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
        self._sync_memory_retrieval_file('story_memory', self.story_memory_path)
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
        if self._in_force_finish_mode():
            batch_size = min(batch_size, 1)
        elif self._in_finale_mode():
            batch_size = min(batch_size, 2)
        batch_size = max(1, batch_size)

        chapter_end = next_chapter + batch_size - 1
        retrieval_context = self._memory_retrieval_context('plan')
        context = '\n\n'.join(
            item for item in [
                f'【系列圣经短版】\n{truncate_text(read_text(self.series_bible_short_path), 1200)}',
                f'【卷规划】\n{truncate_text(read_text(plan_path), 1000)}',
                f'【故事记忆】\n{truncate_text(read_text(self.story_memory_path), 1200)}',
                retrieval_context,
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
        if self._in_force_finish_mode():
            finale_extra_rules += """
- 当前已超过强制收束阈值，必须把剩余主线压缩到极少章节内完成
- 不允许再设计“本来还能再写几十章”的终局分叉，必须尽快闭合责任链、主角终局、关系落点、尾声与后日谈
- 如果一章足以完成一个关键终局环节，可以直接完成，不强制保留下一章钩子
"""
        if self._in_terminal_finish_mode():
            finale_extra_rules += f"""
- 当前已进入最终绝对上限前的最后冲刺区，剩余可用字数仅约 {self._remaining_absolute_runway_chars()} 字
- 后续章节只允许承担“终局落判 / 命运落点 / 尾声 / 后日谈”之一或其组合，不再允许任何过渡型拖延章节
- 禁止引入新角色职责、新证词支线、新地点、新制度分支；如存在空白，直接采用最能闭环的解释落判
- 每章必须至少完整关闭一个未解事项；如果一章能够关闭两项以上，就必须合并，不得拆章续命
- 如果本章已经适合成为正文终章、尾声章或后日谈章，就直接写到位，不得再为后续保留人工悬念
"""

        chapter_shape_rule = '- 每章都要有明确目标、阻力、推进、变化与章末钩子'
        if self._in_finale_mode():
            chapter_shape_rule = '- 每章都要有明确目标、阻力、推进与变化；若承担终局/尾声/后日谈功能，可直接完成，不强制章末钩子'
        if self._in_force_finish_mode():
            chapter_shape_rule = '- 每章都要明确承担剩余终局任务之一，并高密度推进；不允许为了续章而空转或强行保留章末钩子'
        if self._in_terminal_finish_mode():
            chapter_shape_rule = '- 每章只能承担终局落判、人物命运落点、尾声、后日谈之一或其组合；必须直接关闭未解事项，不得铺垫任何新枝线'
        chapter_end_rule = '- 每章都要有实质推进与章末卡点'
        if self._in_finale_mode():
            chapter_end_rule = '- 每章都要有实质推进；若本章承担终局、尾声或后日谈功能，可以直接完成该功能，不强制章末卡点'
        if self._in_force_finish_mode():
            chapter_end_rule = '- 每章都要高密度推进终局收束；若本章已经适合直接完成终局/尾声/后日谈，则直接完成，不得为了续章再留人工卡点'
        if self._in_terminal_finish_mode():
            chapter_end_rule = '- 每章都必须显著减少全书剩余未解事项；若本章已适合成为正文终章、尾声章或后日谈章，必须直接收束到位，不得拖延'
        if self._draft_heading_mode() == 'title_only':
            chapter_shape_rule = '- 这是单篇短故事的唯一正文规划，必须单篇闭环，不能按连载思维拆悬念、拆回收、拆情绪落点'
            chapter_end_rule = '- 结尾允许留余味，但必须完成主冲突闭环，不得保留“下一章再解决”的连载式卡点'
        batch_scope_rule = '- 单章必须服务长篇连载，不要写成孤立的无关插曲'
        batch_style_rule = '- 必须是中文番茄风长篇网文节奏'
        if self._is_title_only_story():
            batch_scope_rule = '- 这是单篇短故事，不要按长篇连载拆任务；必须把人物、冲突、代价和最后一屏放在同一篇内完成'
            batch_style_rule = '- 必须是可一口气读完的中文短篇叙事：具体人物、具体处境、具体动作，避免流程图式推进'

        outline_prompt = f"""
请规划接下来这一批章节的大纲：第 {next_chapter} 章 到 第 {chapter_end} 章。

硬性要求：
- 必须严格承接现有前情，不能重置人物状态
- 题材、世界观、力量体系、冲突逻辑必须服从既有设定
- {chapter_shape_rule.lstrip('- ').strip()}
- 兼顾主线推进、副线拉扯、人物关系变化与阶段性爽点
- {batch_scope_rule.lstrip('- ').strip()}
- {batch_style_rule.lstrip('- ').strip()}
- 章节编号必须使用阿拉伯数字格式“第1章”，禁止写成“第一章”“第Ⅰ章”“Chapter 1”
- 每章第一行只允许写“第N章 标题”；标题必须是短标题，建议 4-12 字，最多 {MAX_CHAPTER_TITLE_LENGTH} 字，禁止把剧情推进句、人物关系说明、章末钩子写进标题行
- 新章节标题必须全书唯一；禁止与任何已完成章节或本批次其他章节重名，若语义接近也要主动换一种叫法
- {chapter_end_rule.lstrip('- ').strip()}
- 单章定位是“章节大纲”，不是正文，不要写成小说正文
{finale_extra_rules}

输出格式必须严格如下，不要写任何额外解释：
第{next_chapter}章 章节标题
本章大纲内容……

第{next_chapter + 1 if batch_size > 1 else next_chapter + 1}章 章节标题
本章大纲内容……

直到第 {chapter_end} 章。
"""
        project_role_rule = self._project_role_only_rule()
        if project_role_rule:
            outline_prompt += f"\n补充硬规则：\n- {project_role_rule}\n"

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
        existing_titles = self._existing_story_titles()
        for offset, outline in enumerate(outlines):
            chapter_number = next_chapter + offset
            volume_number, volume_chapter = self._chapter_volume_number(chapter_number)
            chapter_dir = self._chapter_dir(volume_number, chapter_number)
            outline_path = chapter_dir / 'outline.md'
            plot_path = chapter_dir / 'plot.md'
            draft_path = chapter_dir / 'draft.md'
            summary_path = chapter_dir / 'summary.md'
            normalized_outline = normalize_outline_text(chapter_number, outline)
            write_text(outline_path, normalized_outline)
            story_heading_title = self._story_heading_title(safe_title(normalized_outline))
            story_heading_title = ensure_unique_chapter_title(
                chapter_number,
                story_heading_title,
                existing_titles,
                normalized_outline,
            )
            existing_titles.add(story_heading_title)
            pending.append({
                'chapter_number': chapter_number,
                'volume_number': volume_number,
                'chapter_in_volume': volume_chapter,
                'title': story_heading_title,
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
        cached_plot = self._load_cached_text_if_valid(
            plot_path,
            min_chars=150,
            label=f'第{chapter["chapter_number"]}章剧情',
        )
        if cached_plot:
            return cached_plot

        retrieval_context = self._memory_retrieval_context(
            'plot',
            chapter=chapter,
            outline_text=outline_text,
        )
        context_parts = [
            f'【系列圣经短版】\n{truncate_text(read_text(self.series_bible_short_path), 1000)}',
            f'【当前卷规划】\n{truncate_text(read_text(self.ensure_volume_plan(chapter["volume_number"])), 900)}',
            f'【故事记忆】\n{truncate_text(read_text(self.story_memory_path), 1000)}',
            retrieval_context,
            f'【最近章节摘要】\n{truncate_text(self.recent_chapter_summaries(limit=3), 600)}',
            self._ending_guidance_text(limit=900),
        ]
        context = '\n\n'.join(part for part in context_parts if part.strip())
        finale_plot_rules = ''
        if self._in_finale_mode():
            finale_plot_rules = """
- 当前已进入全书最终收束阶段，本章若承担终局、尾声或后日谈功能，应直接推进，不要继续拖大主线
- 不要新增需要多卷回收的新谜团，只能回收当前主线伏笔并推进角色落点
- 若本章已经接近正文终章/尾声/后日谈，必须安排一个能回应开篇承诺的具体落点、选择或意象，不能只写程序性收束
"""
        if self._in_force_finish_mode():
            finale_plot_rules += """
- 当前已超过强制收束阈值，本章必须高密度推进剩余终局任务，不允许再把终局拆成松散长段
- 章末如需保留交接点，也只能服务于本书剩余极少章节的收束，不能制造新的大悬念
"""
        if self._in_terminal_finish_mode():
            finale_plot_rules += """
- 当前已进入最终绝对上限前的最后冲刺，本章只允许写“终局落判 / 命运落点 / 尾声 / 后日谈”之一或其组合
- 禁止引入任何新的大谜团、新反派、新证人、新地点；如存在未说明空白，直接选取最能闭环的解释落槌
- 本章必须至少实质性关闭一项未解事项；若能在一章内同时关闭多项，就必须合并完成
"""
        plot_ending_rule = '- 章末必须保留强卡点，为下一章续接'
        if self._in_finale_mode():
            plot_ending_rule = '- 章末可以保留轻交接点，但应优先服务终局收束，不强制强卡点'
        if self._in_force_finish_mode():
            plot_ending_rule = '- 若本章已适合直接完成一个终局环节、尾声或后日谈，可直接收住，不得为了续章制造强卡点'
        if self._in_terminal_finish_mode():
            plot_ending_rule = '- 若本章适合直接写成正文终章、尾声章或后日谈章，则直接收束，不得为了续章保留任何人工悬念'
        if self._draft_heading_mode() == 'title_only':
            plot_ending_rule = '- 这是单篇短故事的唯一剧情梗概，结尾允许留余味，但必须完成主冲突闭环，不能保留下一章钩子'
        prompt = f"""
请把这章大纲扩写成详细剧情梗概。

要求：
- 这是第 {chapter['chapter_number']} 章
- 只输出剧情梗概，不要写成正文
- 节奏快、信息密、冲突密、因果清楚
- 充分体现本书既定题材、世界规则、人物关系与主线矛盾
- 把关键冲突、人物决策、信息揭示、因果变化写清楚
- 兼顾爽点、压迫感、情绪张力与阶段性推进
- {plot_ending_rule.lstrip('- ').strip()}
- 目标长度：400~900 字
{finale_plot_rules}
"""
        if self._is_title_only_story():
            prompt += """

单篇短故事补充要求：
- 不要把剧情梗概写成“功能清单”或“制度说明流程图”。
- 必须明确：主角的私人亏欠是什么，被救者身上哪 2~3 个生活碎片最扎人。
- 至少写出一个配角在关键场景中的个人反应或私心，而不是只给立场标签。
- 抢救成功后，尾段要尽量短；不要预先把主题总结句写进梗概里。
"""
        project_role_rule = self._project_role_only_rule()
        if project_role_rule:
            prompt += f"\n补充硬规则：\n- {project_role_rule}\n"

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
        plot_text = self._enforce_project_role_labels(f'第{chapter["chapter_number"]}章剧情', plot_text)
        self.clear_error()
        write_text(plot_path, plot_text)
        chapter['status'] = 'plotted'
        self._save_state()
        return plot_text

    def _title_only_story_segment_specs(self, target_chars: int) -> list[dict[str, str]]:
        if target_chars <= 12000:
            return [
                {
                    'name': '起势段',
                    'goal': '迅速立住主角处境、关键关系和引爆事件，把读者拉进故事。',
                    'ending': '本段结尾必须把人物真正推入后果，不要提前收束全篇。',
                },
                {
                    'name': '收束段',
                    'goal': '承接前段完成冲突闭环、代价落地和最后一屏，不要留成下一章。',
                    'ending': '本段必须完成单篇闭环，最后收在具体画面、动作或残响上。',
                },
            ]
        return [
            {
                'name': '起势段',
                'goal': '迅速立住主角处境、关键关系和引爆事件，把读者拉进故事。',
                'ending': '本段结尾必须把人物推入不可回避的压力，不要提前解释结局。',
            },
            {
                'name': '承压段',
                'goal': '把冲突真正顶到最疼的位置，写清选择、反制和代价，避免原地打转。',
                'ending': '本段结尾应把人物逼到必须落判的位置，为最终收束腾出空间。',
            },
            {
                'name': '收束段',
                'goal': '完成主冲突闭环、代价兑现和最后一屏，不要把结尾写成主题说明会。',
                'ending': '本段必须完成单篇闭环，最后收在具体画面、动作或残响上。',
            },
        ]

    def _generate_title_only_segmented_draft(
        self,
        chapter: dict,
        plot_text: str,
        min_chars: int,
        target_chars: int,
        max_chars: int,
        finale_draft_rules: str,
        memory_context: str = '',
    ) -> str:
        specs = self._title_only_story_segment_specs(target_chars)
        weights = [1] * len(specs)
        total_weight = sum(weights)
        base_target = max(target_chars, min_chars)
        allocated_targets: list[int] = []
        running_total = 0
        for index, _spec in enumerate(specs):
            if index == len(specs) - 1:
                segment_target = max(2200, base_target - running_total)
            else:
                segment_target = max(2200, int(base_target * weights[index] / total_weight))
                running_total += segment_target
            allocated_targets.append(segment_target)

        segments: list[str] = []
        for index, spec in enumerate(specs):
            segment_target = allocated_targets[index]
            segment_min = max(1800, int(segment_target * 0.72))
            segment_max = max(segment_target + 1200, int(segment_target * 1.28))
            previous_text = '\n\n'.join(item for item in segments if item.strip()).strip()
            previous_tail = tail_text(previous_text, 5000) if previous_text else ''
            is_final_segment = index == len(specs) - 1
            segment_label = f'第{chapter["chapter_number"]}章正文-{spec["name"]}'

            prompt = f"""
你正在分段创作一篇单篇中文短篇小说的正文。本次只写其中一个连续段落区间，而不是重写全篇。

硬性要求：
- 全程只输出中文小说正文，不要输出英文，不要输出分析，不要输出提纲，不要输出注释。
- 不要重复前一段已经写出的内容；已完成正文只作为续写上下文参考。
- 不要补写作品标题，不要写“第1章”“第一章”“Chapter 1”等章节编号。
- 你现在只负责从上一段停住的地方继续往下写，不要回顾式总结前文。
- 题材、人物、关系、冲突和结局义务必须严格服从当前这份剧情梗概。
- 风格必须是可发布的中文短篇小说正文：人物真实感、情绪穿透力和画面推进优先，避免流程图腔、设定讲解腔和空泛抒情。
- 被救者/被伤害者不能只是功能位，必须继续让读者看见其作为活人的细节。
- 配角不能只负责传递信息，至少保留一个人的个人反应、私心或处境折角。
- 这是一篇单篇短篇小说，不允许“未完待续”式结尾。

本段职责：
- 段名：{spec["name"]}
- 核心目标：{spec["goal"]}
- 结尾要求：{spec["ending"]}
- 本段目标字数：{segment_target} 字左右，至少 {segment_min} 字，建议不超过 {segment_max} 字。

额外要求：
- 开头承接必须顺滑，像同一篇小说自然往下流动，而不是另起一章。
- 不要把已经发生的事再讲一遍；新增内容必须带来新的动作、对话、判断、代价或情绪变化。
- 若不是最后一段，不要提前把全篇主题总结完，不要抢跑结局。
- 若是最后一段，必须完成主冲突闭环、人物代价和最后一屏；最后一段不要替读者下结论，要收在具体画面、动作或残响上。
{finale_draft_rules}
""".strip()
            role_only_rule = self._project_role_only_rule()
            if role_only_rule:
                prompt += f"\n\n补充硬规则：\n- {role_only_rule}\n"
            if memory_context:
                prompt += f"\n\n{memory_context}\n"
            if previous_tail:
                prompt += "\n你会收到前文尾段作为上下文，只允许顺着它继续，不得回头改写。\n"
            if is_final_segment:
                prompt += "\n这是最后一段，必须真正收住，不能再把关键义务推给不存在的下一段。\n"
            else:
                prompt += "\n这不是最后一段，本段结尾只允许形成强推进，不允许提前写成全篇总结或尾声。\n"

            def _write_segment() -> str:
                writer = DraftWriter(
                    [(plot_text, '')],
                    {'context_y': previous_tail},
                    model=self.writer_model,
                    sub_model=self.sub_model,
                    x_chunk_length=800,
                    y_chunk_length=segment_max,
                    max_thread_num=self.args.max_thread_num,
                )
                pairs = self.run_writer(segment_label, writer, prompt, pair_span=(0, len(writer.xy_pairs)))
                text = ''.join(pair[1] for pair in pairs).strip()
                text = strip_generated_draft_heading(
                    chapter['chapter_number'],
                    chapter['title'],
                    text,
                ).strip()
                if len(text) < segment_min:
                    raise RuntimeError(f'{spec["name"]}字数不足。')
                return text

            segment_text = self.with_retry(segment_label, _write_segment)
            self.clear_error()
            segments.append(segment_text)

        draft_text = '\n\n'.join(item.strip() for item in segments if item.strip()).strip()
        if len(draft_text) < min_chars:
            raise RuntimeError(
                f'分段生成后正文仍不足最低字数要求：当前 {len(draft_text)}，至少需要 {min_chars}。'
            )
        return draft_text

    def generate_draft(self, chapter: dict, plot_text: str) -> str:
        draft_path = Path(chapter['draft_file'])
        cached_draft = self._load_cached_draft_if_valid(
            chapter,
            min_chars=200,
            label=f'第{chapter["chapter_number"]}章正文',
        )
        if cached_draft:
            return cached_draft

        min_chars = max(1400, int(self.args.chapter_char_target * 0.8))
        target_chars = self.args.chapter_char_target
        max_chars = max(target_chars + 600, 2600)
        if self._is_title_only_story() and not self._in_terminal_finish_mode():
            # 短篇以项目最小字数为准，避免“分段写完后又被整篇扩写”这一不稳定路径反复触发。
            min_chars = max(6000, int(self.args.min_target_chars or 0))
            target_chars = max(target_chars, min_chars)
            max_chars = max(max_chars, target_chars + 1200)
        if self._in_terminal_finish_mode():
            min_chars = 1200
            target_chars = min(target_chars, 1800)
            max_chars = min(max_chars, 2200)
        prompt_min_chars = min_chars
        prompt_target_chars = target_chars
        prompt_max_chars = max_chars
        expand_y_chunk_length = max_chars + 600
        if self._is_title_only_story() and not self._in_terminal_finish_mode():
            # 单篇短篇先保证正文能稳定落地，避免首轮一次性请求过长导致上游 524。
            prompt_target_chars = min(target_chars, 9000)
            prompt_min_chars = min(min_chars, max(7000, int(prompt_target_chars * 0.82)))
            prompt_max_chars = min(max_chars, max(prompt_target_chars + 1200, 10800))
            expand_y_chunk_length = min(max_chars + 600, max(min_chars + 1800, 12000))
        finale_draft_rules = ''
        if self._in_finale_mode():
            finale_draft_rules = """
- 当前已进入全书最终收束阶段；如果本章承担终局、尾声或后日谈功能，请按其应有气质直接完成
- 不要再扩展需要多卷回收的新大坑、新地图、新主反派
- 允许保留章末钩子，但钩子必须服务于本书剩余收束，而不是开启新长篇
- 若本章承担终章、尾声或后日谈功能，最后两段必须留下主题反照、人物代价、关系回响或意象回声，不要只剩总结与说明
"""
        if self._in_force_finish_mode():
            finale_draft_rules += """
- 当前已超过强制收束阈值，本章必须尽量完成关键终局任务，不要再把剩余收束拖成宽松长段
- 如果本章已经适合直接完成一个终局环节、尾声或后日谈，就直接完成，不强制留出下一章的人工卡口
"""
        if self._in_terminal_finish_mode():
            finale_draft_rules += """
- 当前已进入最终绝对上限前的最后冲刺，本章只允许承担“终局落判 / 命运落点 / 尾声 / 后日谈”之一或其组合
- 禁止新增任何需要后续再解释的新设定、新势力、新证词、新地点；如有未说明空白，直接用最稳的解释收束
- 本章必须至少写实一个终局落点；如果已经具备全文完结条件，就直接把本章写成正文终章、尾声章或后日谈章
"""
        draft_memory_context = self._memory_retrieval_context(
            'draft',
            chapter=chapter,
            plot_text=plot_text,
        )

        def _draft(existing_text: str = '') -> str:
            draft_tension_rule = '- 兼顾情绪张力、场面张力、信息揭露、人物关系拉扯与章末钩子'
            if self._in_finale_mode():
                draft_tension_rule = '- 兼顾情绪张力、场面张力、信息揭露与人物关系落点；若本章承担终局/尾声/后日谈功能，不强制章末钩子'
            if self._in_force_finish_mode():
                draft_tension_rule = '- 兼顾情绪张力、信息揭露、人物关系落点与终局完成度；不允许为了续章而额外制造章末钩子'
            if self._in_terminal_finish_mode():
                draft_tension_rule = '- 只保留对终局有直接作用的情绪张力、信息揭露与人物落点；每段内容都必须服务于结案、落命、尾声或后日谈'
            draft_ending_rule = '- 结尾必须卡住'
            if self._in_finale_mode():
                draft_ending_rule = '- 结尾可以保留轻微余波，但不强制卡住；若本章承担终局、尾声或后日谈功能，应允许自然收束'
            if self._in_force_finish_mode():
                draft_ending_rule = '- 结尾必须优先服务全书收束；如果本章已经适合自然收住，就直接收住，不得为了续章强行卡住'
            if self._in_terminal_finish_mode():
                draft_ending_rule = '- 结尾必须朝全文结束直接推进；若本章已适合成为正文终章、尾声章或后日谈章，就直接写到位，不得再留新的程序性悬念'
            title_line_rule = f'- 最终正文标题行必须使用阿拉伯数字格式“第{chapter["chapter_number"]}章 标题”，禁止写成“第一章”“第Ⅰ章”“Chapter 1”'
            chapter_role_rule = f'- 这是第 {chapter["chapter_number"]} 章，必须保留并优化章节标题感'
            if self._draft_heading_mode() == 'title_only':
                title_line_rule = f'- 这是单篇短故事，最终正文开头只保留作品标题“{chapter["title"]}”，禁止出现“第1章”“第一章”“第Ⅰ章”“Chapter 1”等章节编号'
                chapter_role_rule = '- 这是单篇短故事的唯一正文，不要写成连载章节'
            style_rule = '- 风格：番茄风、强代入、强画面、强对白、强追读'
            if self._is_title_only_story():
                style_rule = '- 风格：短篇叙事优先，人物真实感与情绪穿透高于连载腔；不要写成设定讲解、流程图或爽文复盘'
            prompt = f"""
请把这段剧情梗概写成{('可发布的中文短篇小说正文' if self._is_title_only_story() else '可发布的中文长篇网文正文')}。

要求：
- {chapter_role_rule.lstrip('- ').strip()}
- {title_line_rule.lstrip('- ').strip()}
- {style_rule.lstrip('- ').strip()}
- 题材、世界观、力量体系、人物关系和冲突类型必须严格服从本书设定
- 重点写清人物目标、阻力、选择、代价、反制与变化
- {draft_tension_rule.lstrip('- ').strip()}
- 正文不是梗概，不要用“随后、然后、接着”堆流水账
- 开头三段必须抓人
- {draft_ending_rule.lstrip('- ').strip()}
- 若本篇是单篇短故事，则必须单篇完整闭环，允许余味，不允许“未完待续”式结尾
- 目标字数：{target_chars} 字左右，至少 {min_chars} 字，建议不超过 {max_chars} 字
{finale_draft_rules}
"""
            if self._is_title_only_story():
                prompt += """

单篇短故事额外硬要求：
- 被救者不能只是“案例”或“高危样本”，必须尽快让读者看见他作为一个活人的细节。
- 配角不能只承担观点功能；至少让主管、值班同事、接线员其中两人带出个人处境或性格折角。
- 抢救成功后的审计与处分段落必须克制，绝不能盖过前面“人差点死掉”的疼感。
- 最后一段不要总结主题，不要替读者下结论，要收在一个具体画面、动作或残响上。
"""
            if self._is_title_only_story():
                prompt += "\n- 这是短篇初稿阶段，先把事件闭环、情绪落点和最后一屏画面写稳，不要为了冲字数把单次生成拉得过满。\n"
            prompt = prompt.replace(
                f'目标字数：{target_chars} 字左右，至少 {min_chars} 字，建议不超过 {max_chars} 字',
                f'目标字数：{prompt_target_chars} 字左右，至少 {prompt_min_chars} 字，建议不超过 {prompt_max_chars} 字',
            )
            role_only_rule = self._project_role_only_rule()
            if role_only_rule:
                prompt += f"\n补充硬规则：\n- {role_only_rule}\n"
            if draft_memory_context:
                prompt += f"\n\n{draft_memory_context}\n"
            writer = DraftWriter(
                [(plot_text, existing_text)],
                {},
                model=self.writer_model,
                sub_model=self.sub_model,
                x_chunk_length=800,
                y_chunk_length=prompt_max_chars,
                max_thread_num=self.args.max_thread_num,
            )
            pairs = self.run_writer(f'第{chapter["chapter_number"]}章正文', writer, prompt, pair_span=(0, len(writer.xy_pairs)))
            return ''.join(pair[1] for pair in pairs).strip()

        if self._is_title_only_story() and not self._in_terminal_finish_mode():
            draft_text = self.with_retry(
                f'第{chapter["chapter_number"]}章正文分段生成',
                lambda: self._generate_title_only_segmented_draft(
                    chapter=chapter,
                    plot_text=plot_text,
                    min_chars=min_chars,
                    target_chars=target_chars,
                    max_chars=max_chars,
                    finale_draft_rules=finale_draft_rules,
                    memory_context=draft_memory_context,
                ),
            )
        else:
            draft_text = self.with_retry(f'第{chapter["chapter_number"]}章正文', lambda: _draft(''))
        self.clear_error()
        if len(draft_text) < min_chars:
            expand_goal_line = '请在不改变这章主线事件与结尾卡点的前提下，对正文进行扩充和润色。'
            if self._in_finale_mode():
                expand_goal_line = '请在不改变这章主线事件与收束功能的前提下，对正文进行扩充和润色。'
            expand_prompt = f"""
{expand_goal_line}

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
                    y_chunk_length=expand_y_chunk_length,
                    max_thread_num=self.args.max_thread_num,
                )
                pairs = self.run_writer(f'第{chapter["chapter_number"]}章扩写', writer, expand_prompt, pair_span=(0, len(writer.xy_pairs)))
                text = ''.join(pair[1] for pair in pairs).strip()
                if len(text) < min_chars:
                    raise RuntimeError('扩写后字数仍不足。')
                return text

            draft_text = self.with_retry(f'第{chapter["chapter_number"]}章扩写', _expand)
            self.clear_error()

        if not self._is_title_only_story():
            draft_text = self._enforce_project_role_labels(f'第{chapter["chapter_number"]}章正文', draft_text)
            self.clear_error()
        draft_text = normalize_chapter_draft_text(
            chapter['chapter_number'],
            chapter['title'],
            draft_text,
            heading_mode=self._draft_heading_mode(),
        )
        draft_text = self.apply_chapter_critic(chapter, plot_text, draft_text)
        draft_text = self._enforce_project_role_labels(f'第{chapter["chapter_number"]}章正文定稿', draft_text)
        self.clear_error()
        draft_text = normalize_chapter_draft_text(
            chapter['chapter_number'],
            chapter['title'],
            draft_text,
            heading_mode=self._draft_heading_mode(),
        )
        outline_text = read_text(Path(chapter['outline_file']))
        previous_summary = ''
        completed = list(self.state.get('completed_chapters', []) or [])
        if completed:
            previous_summary = read_text(Path(completed[-1].get('summary_file', ''))).strip()
        draft_text = self._repair_chapter_draft_if_needed(
            chapter,
            draft_text,
            outline_text=outline_text,
            plot_text=plot_text,
            previous_summary=previous_summary,
            label=f'第{chapter["chapter_number"]}章结尾修补',
        )
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
        self._sync_memory_retrieval_file(
            'chapter_summary',
            summary_path,
            chapter_number=int(chapter.get('chapter_number', 0) or 0),
            source_kind='summary',
        )
        return summary_text.strip()

    def finalize_chapter(self, chapter: dict, draft_text: str, summary_text: str) -> None:
        draft_text = self._sanitize_text(draft_text)
        summary_text = self._sanitize_text(summary_text)
        integrity = self._assess_chapter_draft_integrity(chapter, draft_text)
        if integrity.get('high_confidence'):
            raise RuntimeError(
                f'第{chapter["chapter_number"]}章在入库前仍疑似残章：'
                f'{integrity.get("reason")} | {integrity.get("tail", "")}'
            )
        existing_titles = [
            str(item.get('title', '') or '').strip()
            for item in self.state.get('completed_chapters', [])
            if str(item.get('title', '') or '').strip()
        ]
        source_text = self._chapter_title_source_text(
            chapter,
            extra_texts=(draft_text, summary_text),
        )
        unique_title = ensure_unique_chapter_title(
            int(chapter.get('chapter_number', 0) or 0),
            str(chapter.get('title', '') or '').strip() or source_text,
            existing_titles,
            source_text,
        )
        if unique_title != str(chapter.get('title', '') or '').strip():
            old_title = str(chapter.get('title', '') or '').strip()
            chapter['title'] = unique_title
            draft_text = normalize_chapter_draft_text(
                chapter['chapter_number'],
                unique_title,
                draft_text,
                heading_mode=self._draft_heading_mode(),
            )
            self._apply_title_to_record_files(chapter, unique_title)
            self.log(
                f'[title_guard] 第{chapter["chapter_number"]}章标题去重：'
                f'{old_title or "（空标题）"} -> {unique_title}'
            )
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
        rewrite_required_from = int(self.state.get('rewrite_required_from_chapter', 0) or 0)
        if rewrite_required_from > 0 and int(chapter['chapter_number']) >= rewrite_required_from:
            self.state['rewrite_required_from_chapter'] = 0
        with self.full_manuscript_path.open('a', encoding='utf-8') as handle:
            handle.write(draft_text.rstrip() + '\n\n')
        self.state['manuscript_last_appended_chapter'] += 1
        self._save_state()
        self.clear_stage()
        self.log(
            f'第{chapter["chapter_number"]}章完成，累计 {self.state["generated_chapters"]} 章 / {self.state["generated_chars"]} 字'
        )

    def _delete_future_content_for_rewrite(self, rewrite_from_chapter: int) -> None:
        target_volume, _ = self._chapter_volume_number(rewrite_from_chapter)
        for volume_dir in sorted(self.volumes_dir.glob('vol_*')):
            try:
                volume_number = int(volume_dir.name.split('_')[-1])
            except ValueError:
                continue
            if volume_number < target_volume:
                continue
            plan_path = volume_dir / 'plan.md'
            if plan_path.exists():
                plan_path.unlink()
            if volume_number > target_volume:
                shutil.rmtree(volume_dir, ignore_errors=True)
                continue
            chapters_dir = volume_dir / 'chapters'
            if not chapters_dir.exists():
                continue
            for chapter_dir in sorted(chapters_dir.glob('ch_*')):
                try:
                    chapter_number = int(chapter_dir.name.split('_')[-1])
                except ValueError:
                    continue
                if chapter_number >= rewrite_from_chapter:
                    shutil.rmtree(chapter_dir, ignore_errors=True)
        self._invalidate_memory_retrieval()

    def _reset_story_memory_for_rewrite(self, rewrite_from_chapter: int, reason_lines: list[str]) -> None:
        lines = [
            '# 故事至今',
            '## 当前状态',
            f'- 本项目已从第{rewrite_from_chapter}章开始进入终局回修模式。',
            f'- 第{rewrite_from_chapter}章及之后的旧正文、旧摘要、旧完结判断一律作废。',
            '- 当前有效前情以保留正文、系列圣经、最近章节摘要与开篇承诺为准。',
            '',
            '## 回修提醒',
            '- 下一次续写前，应先对照开篇承诺与当前终局义务，避免只做程序性收束。',
            '- 后续规划不得引用旧终局中的重复宣判、重复尾声、时间倒退、过度解释或办结式段落。',
        ]
        if reason_lines:
            lines.extend(['', '## 本轮回修重点'])
            lines.extend(f'- {line}' for line in reason_lines if line.strip())
        write_text(self.story_memory_path, '\n'.join(lines).strip() + '\n')

    def rewind_from_chapter(self, rewrite_from_chapter: int, *, reason_lines: list[str] | None = None, polish_brief: str = '') -> None:
        rewrite_from_chapter = int(rewrite_from_chapter or 0)
        if rewrite_from_chapter <= 0:
            raise ValueError('rewrite_from_chapter must be greater than 0')

        completed = list(self.state.get('completed_chapters') or [])
        if rewrite_from_chapter == 1:
            if not completed:
                raise ValueError('当前没有已完成正文可供回修。')
            retained: list[dict[str, Any]] = []
        else:
            retained = [item for item in completed if int(item.get('chapter_number', 0) or 0) < rewrite_from_chapter]
        if rewrite_from_chapter > 1 and len(retained) == len(completed):
            raise ValueError(f'第{rewrite_from_chapter}章及之后没有已完成正文可供回修。')

        self._delete_future_content_for_rewrite(rewrite_from_chapter)
        for path in (
            self.completion_report_path,
            self.auto_ending_guidance_path,
            self.auto_ending_quality_guidance_path,
            self.ending_quality_review_path,
        ):
            if path.exists():
                path.unlink()

        if polish_brief.strip():
            write_text(self.ending_polish_brief_path, polish_brief.strip())
        elif self.ending_polish_brief_path.exists():
            self.ending_polish_brief_path.unlink()

        self._reset_story_memory_for_rewrite(rewrite_from_chapter, reason_lines or [])

        recalculated_completed: list[dict[str, Any]] = []
        generated_chars = 0
        for item in retained:
            chapter_number = int(item.get('chapter_number', 0) or 0)
            draft_file = str(item.get('draft_file', '') or '')
            summary_file = str(item.get('summary_file', '') or '')
            draft_text = read_text(Path(draft_file))
            summary_text = read_text(Path(summary_file)).strip()
            chars = len(draft_text.strip())
            generated_chars += chars
            volume_number, chapter_in_volume = self._chapter_volume_number(chapter_number)
            recalculated_completed.append({
                'chapter_number': chapter_number,
                'volume_number': int(item.get('volume_number', 0) or volume_number),
                'chapter_in_volume': int(item.get('chapter_in_volume', 0) or chapter_in_volume),
                'title': str(item.get('title', '') or '').strip(),
                'draft_file': draft_file,
                'summary_file': summary_file,
                'chars': chars,
                'summary_preview': summary_text or str(item.get('summary_preview', '') or '').strip(),
            })

        target_volume, _ = self._chapter_volume_number(rewrite_from_chapter)
        self.state['status'] = 'running'
        self.state['generated_chars'] = generated_chars
        self.state['generated_chapters'] = len(recalculated_completed)
        self.state['next_chapter_number'] = rewrite_from_chapter
        self.state['current_volume'] = target_volume
        self.state['pending_chapters'] = []
        self.state['completed_chapters'] = recalculated_completed
        self.state['manuscript_last_appended_chapter'] = 0
        self.state['last_memory_refresh_chapter'] = 0
        self.state['last_ending_guidance_refresh_chapter'] = 0
        self.state['last_ending_quality_guidance_refresh_chapter'] = 0
        self.state['rewrite_required_from_chapter'] = rewrite_from_chapter
        self.state['ending_polish_last_rewrite_from_chapter'] = rewrite_from_chapter
        self.state['last_error'] = ''
        self.state['completion_check'] = {}
        self.state['completion_check_history'] = []
        self.state['ending_quality_check'] = {}
        self.state['ending_quality_history'] = []
        self.rebuild_full_manuscript(export_chapters_txt=True)
        self.log(
            f'[ending_quality] 已从第{rewrite_from_chapter}章回退；旧稿在该章及之后的完结/指引结论全部作废，'
            '后续评估只针对回修后的新稿面。'
        )

    def _recalculate_completed_state_from_disk(self) -> None:
        completed = sorted(
            self.state.get('completed_chapters', []),
            key=lambda item: int(item.get('chapter_number', 0) or 0),
        )
        recalculated_completed: list[dict[str, Any]] = []
        generated_chars = 0
        max_chapter_number = 0
        for item in completed:
            chapter_number = int(item.get('chapter_number', 0) or 0)
            max_chapter_number = max(max_chapter_number, chapter_number)
            draft_file = str(item.get('draft_file', '') or '')
            summary_file = str(item.get('summary_file', '') or '')
            draft_text = read_text(Path(draft_file))
            summary_text = read_text(Path(summary_file)).strip()
            chars = len(draft_text.strip())
            generated_chars += chars
            volume_number, chapter_in_volume = self._chapter_volume_number(chapter_number)
            recalculated_completed.append({
                'chapter_number': chapter_number,
                'volume_number': int(item.get('volume_number', 0) or volume_number),
                'chapter_in_volume': int(item.get('chapter_in_volume', 0) or chapter_in_volume),
                'title': str(item.get('title', '') or '').strip(),
                'draft_file': draft_file,
                'summary_file': summary_file,
                'chars': chars,
                'summary_preview': summary_text or str(item.get('summary_preview', '') or '').strip(),
            })

        self.state['completed_chapters'] = recalculated_completed
        self.state['generated_chapters'] = len(recalculated_completed)
        self.state['generated_chars'] = generated_chars
        self.state['next_chapter_number'] = (max_chapter_number + 1) if max_chapter_number else 1
        self.state['manuscript_last_appended_chapter'] = len(recalculated_completed)
        self._invalidate_memory_retrieval()

    def scan_completed_chapter_integrity(self) -> dict[str, Any]:
        completed = sorted(
            self.state.get('completed_chapters', []),
            key=lambda item: int(item.get('chapter_number', 0) or 0),
        )
        issues: list[dict[str, Any]] = []
        summary = {
            'project': self.project_dir.name,
            'chapters_checked': len(completed),
            'missing_draft': 0,
            'empty_draft': 0,
            'missing_summary': 0,
            'empty_summary': 0,
            'bad_ending_high': 0,
            'bad_ending_low': 0,
        }

        for item in completed:
            chapter_number = int(item.get('chapter_number', 0) or 0)
            draft_path = Path(str(item.get('draft_file', '') or ''))
            summary_path = Path(str(item.get('summary_file', '') or ''))

            if not draft_path.exists():
                summary['missing_draft'] += 1
                issues.append({
                    'project': self.project_dir.name,
                    'chapter': chapter_number,
                    'title': str(item.get('title', '') or '').strip(),
                    'issue': 'missing_draft',
                    'path': str(draft_path),
                    'confidence': 'high',
                })
                continue

            draft_text = read_text(draft_path)
            if not draft_text.strip():
                summary['empty_draft'] += 1
                issues.append({
                    'project': self.project_dir.name,
                    'chapter': chapter_number,
                    'title': str(item.get('title', '') or '').strip(),
                    'issue': 'empty_draft',
                    'path': str(draft_path),
                    'confidence': 'high',
                })
                continue

            if not summary_path.exists():
                summary['missing_summary'] += 1
                issues.append({
                    'project': self.project_dir.name,
                    'chapter': chapter_number,
                    'title': str(item.get('title', '') or '').strip(),
                    'issue': 'missing_summary',
                    'path': str(summary_path),
                    'confidence': 'high',
                })
            elif not read_text(summary_path).strip():
                summary['empty_summary'] += 1
                issues.append({
                    'project': self.project_dir.name,
                    'chapter': chapter_number,
                    'title': str(item.get('title', '') or '').strip(),
                    'issue': 'empty_summary',
                    'path': str(summary_path),
                    'confidence': 'high',
                })

            chapter_ref = {
                'chapter_number': chapter_number,
                'title': str(item.get('title', '') or '').strip(),
                'draft_file': str(draft_path),
            }
            integrity = self._assess_chapter_draft_integrity(chapter_ref, draft_text)
            if not integrity.get('suspicious'):
                continue

            confidence = 'high' if integrity.get('high_confidence') else 'low'
            if confidence == 'high':
                summary['bad_ending_high'] += 1
            else:
                summary['bad_ending_low'] += 1
            issues.append({
                'project': self.project_dir.name,
                'chapter': chapter_number,
                'title': str(item.get('title', '') or '').strip(),
                'issue': 'bad_ending',
                'reason': str(integrity.get('reason', '') or ''),
                'tail': str(integrity.get('tail', '') or ''),
                'path': str(draft_path),
                'confidence': confidence,
            })

        return {
            'summary': summary,
            'issues': issues,
        }

    def repair_incomplete_completed_chapters(
        self,
        *,
        include_low_confidence: bool = False,
        limit: int = 0,
    ) -> dict[str, Any]:
        scan_report = self.scan_completed_chapter_integrity()
        issues = [
            item for item in scan_report.get('issues', [])
            if item.get('issue') == 'bad_ending'
            and (include_low_confidence or item.get('confidence') == 'high')
        ]
        if limit > 0:
            issues = issues[:limit]

        completed = sorted(
            self.state.get('completed_chapters', []),
            key=lambda item: int(item.get('chapter_number', 0) or 0),
        )
        chapter_map = {
            int(item.get('chapter_number', 0) or 0): item
            for item in completed
        }
        original_status = str(self.state.get('status', '') or '')
        repaired: list[dict[str, Any]] = []

        for index, issue in enumerate(issues):
            chapter_number = int(issue.get('chapter', 0) or 0)
            chapter = chapter_map.get(chapter_number)
            if not chapter:
                continue

            chapter_index = next(
                (
                    idx for idx, item in enumerate(completed)
                    if int(item.get('chapter_number', 0) or 0) == chapter_number
                ),
                -1,
            )
            if chapter_index < 0:
                continue

            draft_path = Path(str(chapter.get('draft_file', '') or ''))
            summary_path = Path(str(chapter.get('summary_file', '') or ''))
            current_text = read_text(draft_path).strip()
            if not current_text:
                continue

            chapter_paths = self._chapter_record_paths(chapter)
            outline_text = read_text(chapter_paths.get('outline', Path())) if chapter_paths.get('outline') else ''
            plot_text = read_text(chapter_paths.get('plot', Path())) if chapter_paths.get('plot') else ''
            previous_summary = ''
            if chapter_index > 0:
                previous_summary = read_text(Path(str(completed[chapter_index - 1].get('summary_file', '') or ''))).strip()
            next_excerpt = ''
            if chapter_index + 1 < len(completed):
                next_chapter = completed[chapter_index + 1]
                next_text = self._chapter_body_text(next_chapter, read_text(Path(str(next_chapter.get('draft_file', '') or ''))))
                next_excerpt = next_text[:1000].strip()

            revised = self._repair_chapter_draft_if_needed(
                chapter,
                current_text,
                outline_text=outline_text,
                plot_text=plot_text,
                previous_summary=previous_summary,
                next_excerpt=next_excerpt,
                label=f'第{chapter_number}章历史残章修补',
                force=True,
            )
            write_text(draft_path, revised)
            if summary_path.exists():
                summary_path.unlink()
            summary_text = self.summarize_chapter(
                {
                    'chapter_number': chapter_number,
                    'title': str(chapter.get('title', '') or '').strip(),
                    'summary_file': str(summary_path),
                },
                outline_text,
                plot_text,
                revised,
            )
            repaired.append({
                'chapter': chapter_number,
                'title': str(chapter.get('title', '') or '').strip(),
                'path': str(draft_path),
                'summary_file': str(summary_path),
                'old_tail': str(issue.get('tail', '') or ''),
                'new_tail': str(self._assess_chapter_draft_integrity(chapter, revised).get('tail', '') or ''),
                'confidence': str(issue.get('confidence', '') or ''),
                'summary_preview': summary_text.strip(),
            })
            self.log(
                f'[chapter_integrity] 已修补第{chapter_number}章，'
                f'进度 {index + 1}/{len(issues)}'
            )

        if repaired:
            self._recalculate_completed_state_from_disk()
            self.rebuild_full_manuscript(export_chapters_txt=True)
            self.state['status'] = original_status or 'initialized'
            self.state['last_error'] = ''
            self._save_state()

        remaining_report = self.scan_completed_chapter_integrity()
        return {
            'project': self.project_dir.name,
            'requested_repairs': len(issues),
            'repaired': repaired,
            'remaining': remaining_report,
        }

    def _polish_title_only_story_in_place(
        self,
        *,
        cycle_index: int,
        reason_lines: list[str],
        polish_brief: str,
        stage_label: str,
    ) -> None:
        if not self._is_title_only_story():
            raise RuntimeError('只有短篇单篇模式才能使用定向终稿修补。')

        completed = sorted(
            self.state.get('completed_chapters', []),
            key=lambda item: int(item.get('chapter_number', 0) or 0),
        )
        if not completed:
            raise RuntimeError('当前没有可供定向修补的已完成正文。')

        chapter = completed[-1]
        chapter_number = int(chapter.get('chapter_number', 0) or 0)
        chapter_title = str(chapter.get('title', '') or '').strip()
        draft_path = Path(str(chapter.get('draft_file', '') or ''))
        summary_path = Path(str(chapter.get('summary_file', '') or ''))
        current_text = read_text(draft_path).strip()
        if not current_text:
            raise RuntimeError(f'第{chapter_number}章当前正文为空，无法定向修补。')

        if polish_brief.strip():
            write_text(self.ending_polish_brief_path, polish_brief.strip())
        elif self.ending_polish_brief_path.exists():
            self.ending_polish_brief_path.unlink()

        reason_block = '\n'.join(f'- {line}' for line in reason_lines if line.strip()) or '- 让现有单篇真正收住。'
        guidance_text = self._ending_guidance_text(limit=1800)
        quality_guidance_text = self._ending_quality_guidance_text(limit=1800)
        project_role_rule = self._project_role_only_rule()
        prompt = f"""
你正在做一篇单篇中文短篇小说的“终稿定向修补”。

目标不是推翻重写，而是在保留现有故事主体、人物关系、场景顺序、核心事件、主要文风和绝大多数正文的前提下，
把当前版本补到真正可收住。

硬要求：
- 直接输出修订后的完整正文，不要解释，不要分点，不要附注。
- 优先保留现有文本的 85% 以上内容，尤其不要重写开头。
- 优先修后半段，尤其是最后三分之一；如果前文已经成立，不要为了“更顺”重开新叙事链。
- 严禁新增第二章、附录、后记、作者说明、设定说明。
- 严禁新增新专有姓名、额外世界观、额外大事件、额外配角线。
- 只能用现有故事已经建立的人物、代价、动作、场景和物件来补齐闭环。
- 结尾必须落在具体动作、回声或静止画面上，不能停在主题总结句上。
{f'- {project_role_rule}' if project_role_rule else ''}

【本轮修补目标】
{reason_block}

【本轮回修要求】
{polish_brief.strip() or '（无）'}

【当前终局指引】
{guidance_text or '（无）'}

【当前终局质量指引】
{quality_guidance_text or '（无）'}

【当前正文】
{current_text}
"""

        revised = self._call_llm_raw(
            f'{stage_label}第{cycle_index}轮',
            prompt,
            self.ending_polish_model,
            system_prompt='你是只做定向终稿修补的中文短篇小说编辑，擅长保留主体、只补关键闭环。',
            response_json=False,
            stream_output=False,
        ).strip()
        revised = self._sanitize_text(revised)
        if not revised:
            raise RuntimeError('短篇定向终稿修补返回空文本。')

        revised = normalize_chapter_draft_text(
            chapter_number,
            chapter_title,
            revised,
            heading_mode=self._draft_heading_mode(),
        )
        revised = self._enforce_project_role_labels(f'第{chapter_number}章终稿定向修补', revised)
        revised = normalize_chapter_draft_text(
            chapter_number,
            chapter_title,
            revised,
            heading_mode=self._draft_heading_mode(),
        )
        if revised.strip() == current_text.strip():
            raise RuntimeError('短篇定向终稿修补未产生有效改动。')
        if len(revised) < max(400, int(len(current_text) * 0.75)):
            raise RuntimeError('短篇定向终稿修补后文本异常变短。')

        write_text(draft_path, revised)
        write_text(summary_path, '')
        summary_text = self.summarize_chapter(
            {
                'chapter_number': chapter_number,
                'title': chapter_title,
                'summary_file': str(summary_path),
            },
            '',
            '',
            revised,
        )
        write_text(summary_path, summary_text.strip())
        self._recalculate_completed_state_from_disk()
        self.rebuild_full_manuscript(export_chapters_txt=True)
        self.state['status'] = 'running'
        self.state['last_error'] = ''
        self.state['completion_check'] = {}
        self.state['ending_quality_check'] = {}
        self._save_state()
        self.log(
            f'[title_only_polish] 第 {cycle_index} 轮已对第{chapter_number}章执行定向终稿修补；'
            '不再整篇回退，接下来直接重评当前成稿。'
        )

    def maybe_polish_ending_before_stop(self) -> bool:
        if self._completion_mode() != 'min_chars_and_story_end':
            return True
        report = self.evaluate_ending_quality()
        if not report.get('needs_polish'):
            self.log(
                f'终局质量评估通过：综合 {report.get("quality_score", 0)} / 回响 {report.get("resonance_score", 0)} / 余味 {report.get("thought_provoking_score", 0)}'
            )
            return True

        rewrite_from_chapter = int(report.get('rewrite_from_chapter', 0) or 0)
        if rewrite_from_chapter <= 0:
            raise RuntimeError('终局质量评估要求回修，但未返回有效章节起点。')

        cycles = int(self.state.get('ending_polish_cycles', 0) or 0) + 1
        max_cycles = max(1, int(getattr(self.args, 'ending_polish_max_cycles', 2) or 2))
        self.state['ending_polish_cycles'] = cycles
        self._save_state()
        if cycles > max_cycles:
            raise RuntimeError(
                f'终局质量自动回修已达到上限 {max_cycles} 轮，最近一次建议从第{rewrite_from_chapter}章回修，请人工检查。'
            )

        reason_lines = [str(item).strip() for item in (report.get('rewrite_goals') or []) if str(item).strip()]
        if report.get('final_image_target'):
            reason_lines.append(f'最后一屏目标：{report.get("final_image_target")}')
        if self._is_title_only_story():
            self.log(
                f'终局质量评估未通过：综合 {report.get("quality_score", 0)} / 回响 {report.get("resonance_score", 0)} / 余味 {report.get("thought_provoking_score", 0)}；'
                f'第 {cycles} 轮将对当前短篇成稿做定向终稿修补。'
            )
            self._polish_title_only_story_in_place(
                cycle_index=cycles,
                reason_lines=reason_lines,
                polish_brief=str(report.get('polish_brief_markdown', '') or '').strip(),
                stage_label='短篇终局质量定向修补',
            )
            return self.should_stop()
        self.log(
            f'终局质量评估未通过：综合 {report.get("quality_score", 0)} / 回响 {report.get("resonance_score", 0)} / 余味 {report.get("thought_provoking_score", 0)}；'
            f'第 {cycles} 轮自动回修将从第{rewrite_from_chapter}章开始。'
        )
        self.log(
            '说明：上一轮“完结评估=是”只代表旧稿在主线闭环层面已可结束；'
            '本轮未通过的是终局质量要求。回退后若再次出现“未完结”，指向的是新的回修底稿，并不与上一轮旧稿结论冲突。'
        )
        self.rewind_from_chapter(
            rewrite_from_chapter,
            reason_lines=reason_lines,
            polish_brief=str(report.get('polish_brief_markdown', '') or '').strip(),
        )
        return False

    def maybe_rewrite_title_only_story_before_stop(self, completion_report: dict[str, Any]) -> bool:
        if not self._is_title_only_story():
            return True

        cycles = int(self.state.get('ending_polish_cycles', 0) or 0) + 1
        max_cycles = max(1, int(getattr(self.args, 'ending_polish_max_cycles', 2) or 2))
        self.state['ending_polish_cycles'] = cycles
        self._save_state()
        if cycles > max_cycles:
            raise RuntimeError(
                f'单篇终稿自动回修已达到上限 {max_cycles} 轮，当前稿仍未达到可收住标准，请人工检查。'
            )

        missing = [str(item).strip() for item in (completion_report.get('missing') or []) if str(item).strip()]
        summary = str(completion_report.get('summary', '') or '').strip()
        next_phase_goal = str(completion_report.get('next_phase_goal', '') or '').strip()
        guidance_text = self._ending_guidance_text(limit=1600)

        reason_lines: list[str] = []
        if summary:
            reason_lines.append(summary)
        reason_lines.extend(missing[:6])
        if next_phase_goal:
            reason_lines.append(f'下一阶段目标：{next_phase_goal}')

        polish_lines = [
            '# 单篇终稿回修要求',
            '## 当前判定',
            f'- 当前版本尚未达到“单篇完整闭环且值得收住”的标准。',
        ]
        if summary:
            polish_lines.append(f'- {summary}')
        polish_lines.extend([
            '',
            '## 必须补齐',
        ])
        if missing:
            polish_lines.extend(f'- {item}' for item in missing)
        else:
            polish_lines.append('- 让整篇在单章内部真正收住，而不是停在说明和总结句上。')
        polish_lines.extend([
            '',
            '## 必须遵守',
            '- 这是单篇短故事的整章重写，不允许新增第二章来接尾巴。',
            '- 必须把主冲突、代价、人物认领和最后一屏全部压回这一篇内部完成。',
            '- 结尾必须落在具体动作、回声或静止画面上，不能用主题总结句收束。',
            '- 不允许把“缺少的关键闭环”伪装成开放式余味。',
        ])
        if guidance_text:
            polish_lines.extend([
                '',
                '## 现有高优先级终局指引',
                guidance_text,
            ])
        polish_brief = '\n'.join(polish_lines).strip()

        self.log(
            f'单篇完结评估未通过：置信度 {completion_report.get("confidence", 0)}；'
            f'第 {cycles} 轮将对当前短篇成稿做定向终稿修补。'
        )
        self._polish_title_only_story_in_place(
            cycle_index=cycles,
            reason_lines=reason_lines,
            polish_brief=polish_brief,
            stage_label='短篇完结定向修补',
        )
        return self.should_stop()

    def should_stop(self) -> bool:
        if self._rewrite_pending_from_chapter():
            return False
        if self._completion_mode() == 'hard_target':
            if self.args.max_chapters and self.state['generated_chapters'] >= self.args.max_chapters:
                return True
            return self.state['generated_chars'] >= self.args.target_chars

        max_chapters_reached = bool(self.args.max_chapters and self.state['generated_chapters'] >= self.args.max_chapters)

        max_target_chars = self._effective_max_target_chars()
        if max_target_chars and self.state['generated_chars'] >= max_target_chars:
            self.log(f'已达到最终绝对安全字数上限：{max_target_chars}')
            return True

        min_target_chars = self._effective_min_target_chars()
        if min_target_chars and self.state['generated_chars'] < min_target_chars:
            return False

        completion_report = self.evaluate_completion_status()
        if completion_report.get('is_complete'):
            if not self.maybe_polish_ending_before_stop():
                return False
            self.log(
                f'完结评估判定已可自然收尾结束：置信度 {completion_report.get("confidence", 0)}'
            )
            return True

        tail_stagnation = self._tail_only_completion_stagnation(completion_report)
        if tail_stagnation.get('triggered'):
            missing = '；'.join(str(item) for item in (tail_stagnation.get('missing') or []))
            self.log(
                f'完结评估尾段停滞保护触发：连续 {tail_stagnation.get("streak", 0)} 章仍判定“还需1章”，'
                f'且缺口仅剩尾声/后日谈类收束（{missing}）；停止新增章节，转入终局质量打磨。'
            )
            if not self.maybe_polish_ending_before_stop():
                return False
            self.log(
                f'尾段停滞保护判定当前版本已可结束：置信度 {completion_report.get("confidence", 0)}'
            )
            return True

        if max_chapters_reached:
            if self._is_title_only_story():
                return self.maybe_rewrite_title_only_story_before_stop(completion_report)
            return True
        return False

    def run(self) -> None:
        self.state['status'] = 'running'
        self.state['target_chars'] = self.args.target_chars
        self.state['min_target_chars'] = self.args.min_target_chars
        self.state['force_finish_chars'] = self._effective_force_finish_chars()
        self.state['max_target_chars'] = self._effective_max_target_chars()
        self.state['completion_mode'] = self.args.completion_mode
        self._save_state()
        self.log('自动短篇模式启动' if self._is_title_only_story() else '自动长篇模式启动')
        self.log(f'项目目录：{self.project_dir}')
        self.log(
            f'停止模式：{self.args.completion_mode}，硬目标：{self.args.target_chars}，'
            f'最少字数：{self.args.min_target_chars}，强制收束阈值：{self._effective_force_finish_chars()}，'
            f'最终绝对上限：{self._effective_max_target_chars()}，'
            f'单章目标：{self.args.chapter_char_target}，'
            f'正文标题模式：{self._draft_heading_mode()}'
        )
        self.log(f'主模型：{self.args.main_model}，副模型：{self.args.sub_model}')
        critic_model_name = self.args.critic_model or self.args.main_model
        if not self.critic_enabled:
            critic_status = '关闭'
        elif int(self.args.critic_every_chapters or 0) > 0:
            critic_status = f'每 {self.args.critic_every_chapters} 章'
        else:
            critic_status = f'按批次尾章触发（当前每批 {self.args.chapters_per_batch} 章）'
        critic_pass_limit = '不限轮数（无进展自动停止）' if int(self.args.critic_max_passes or 0) <= 0 else str(self.args.critic_max_passes)
        self.log(
            f'critic：{critic_status}，模型：{critic_model_name}，'
            f'推理：{self.args.critic_reasoning_effort}，最大复检轮数：{critic_pass_limit}'
        )
        ending_polish_model_name = self.args.ending_polish_model or critic_model_name
        self.log(
            f'ending_quality：模型：{ending_polish_model_name}，推理：{self.args.ending_polish_reasoning_effort}，'
            f'自动回修上限：{self.args.ending_polish_max_cycles} 轮'
        )

        self.ensure_series_bible()

        if not self.story_memory_path.exists():
            write_text(self.story_memory_path, '# 故事至今\n尚未生成正文。')
            self._sync_memory_retrieval_file('story_memory', self.story_memory_path)
        self._ensure_memory_retrieval_ready()

        while not self.should_stop():
            self.refresh_ending_guidance()
            self.refresh_ending_quality_guidance()
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
            self.refresh_ending_guidance()
            self.refresh_ending_quality_guidance()

        self.refresh_story_memory(force=True)
        self.refresh_ending_guidance(force=True)
        self.refresh_ending_quality_guidance(force=True)
        self.export_full_novel_chapters_txt()
        self.state['status'] = 'completed'
        self.state['last_error'] = ''
        self._save_state()
        self.log('自动长篇任务完成')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Long-Novel-GPT 全自动长篇编排器')
    parser.add_argument('--project-dir', default=str(Path('auto_projects') / 'default_project'))
    parser.add_argument('--brief-file', default=str(Path('novel_brief.md')))
    parser.add_argument('--brief-text', default='')
    parser.add_argument('--main-model', default='sub2api/gpt-5.4')
    parser.add_argument('--sub-model', default='sub2api/gpt-5.4')
    parser.add_argument('--completion-mode', choices=['hard_target', 'min_chars_and_story_end'], default='hard_target')
    parser.add_argument('--target-chars', type=int, default=2_000_000)
    parser.add_argument('--min-target-chars', type=int, default=0)
    parser.add_argument('--force-finish-chars', type=int, default=0)
    parser.add_argument('--max-target-chars', type=int, default=0)
    parser.add_argument('--chapter-char-target', type=int, default=2200)
    parser.add_argument('--chapters-per-volume', type=int, default=30)
    parser.add_argument('--chapters-per-batch', type=int, default=5)
    parser.add_argument('--memory-refresh-interval', type=int, default=5)
    parser.add_argument('--disable-memory-retrieval', action='store_true')
    parser.add_argument('--memory-retrieval-hits', type=int, default=4)
    parser.add_argument('--memory-retrieval-max-chars', type=int, default=1200)
    parser.add_argument('--planner-reasoning-effort', default='high')
    parser.add_argument('--writer-reasoning-effort', default='high')
    parser.add_argument('--sub-reasoning-effort', default='medium')
    parser.add_argument('--summary-reasoning-effort', default='medium')
    parser.add_argument('--critic-model', default='')
    parser.add_argument('--critic-every-chapters', type=int, default=0)
    parser.add_argument('--critic-reasoning-effort', default='xhigh')
    parser.add_argument('--critic-max-passes', type=int, default=0)
    parser.add_argument('--ending-polish-model', default='')
    parser.add_argument('--ending-polish-reasoning-effort', default='xhigh')
    parser.add_argument('--ending-polish-max-cycles', type=int, default=2)
    parser.add_argument('--max-thread-num', type=int, default=1)
    parser.add_argument('--max-retries', type=int, default=0)
    parser.add_argument('--retry-backoff-seconds', type=int, default=15)
    parser.add_argument('--max-chapters', type=int, default=0)
    parser.add_argument('--title-only-story', action='store_true')
    parser.add_argument('--live-stream', action='store_true')
    parser.add_argument('--evaluate-completion-only', action='store_true')
    parser.add_argument('--scan-chapter-integrity-only', action='store_true')
    parser.add_argument('--repair-incomplete-chapters', action='store_true')
    parser.add_argument('--repair-include-low-confidence', action='store_true')
    parser.add_argument('--repair-limit', type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = AutoNovelRunner(args)
    try:
        if runner.state.get('status') == 'manual_review_required':
            runner.log('项目当前已标记为 manual_review_required；需先人工回退或重置终局回修状态后再续跑。')
            return 3
        if args.evaluate_completion_only:
            report = runner.evaluate_completion_status(force=True)
            payload = json.dumps(report, ensure_ascii=False, indent=2)
            try:
                print(payload)
            except UnicodeEncodeError:
                sys.stdout.buffer.write((payload + '\n').encode('utf-8', errors='replace'))
                sys.stdout.flush()
            return 0
        if args.scan_chapter_integrity_only:
            report = runner.scan_completed_chapter_integrity()
            payload = json.dumps(report, ensure_ascii=False, indent=2)
            try:
                print(payload)
            except UnicodeEncodeError:
                sys.stdout.buffer.write((payload + '\n').encode('utf-8', errors='replace'))
                sys.stdout.flush()
            return 0
        if args.repair_incomplete_chapters:
            report = runner.repair_incomplete_completed_chapters(
                include_low_confidence=bool(args.repair_include_low_confidence),
                limit=int(args.repair_limit or 0),
            )
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
        error_text = str(exc)
        manual_review_required_markers = (
            '终局质量自动回修已达到上限',
            '单篇终稿自动回修已达到上限',
        )
        requires_manual_review = any(marker in error_text for marker in manual_review_required_markers)
        runner.state['status'] = 'manual_review_required' if requires_manual_review else 'failed'
        runner.state['last_error'] = error_text
        runner._save_state()
        runner.log(f'任务失败：{exc}')
        for line in traceback.format_exc().splitlines():
            runner.log(f'traceback | {line}')
        if requires_manual_review:
            runner.log('已标记为 manual_review_required，等待人工处理，不再自动续跑。')
            return 3
        raise
    finally:
        runner.close()


if __name__ == '__main__':
    raise SystemExit(main())
