import os
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auto_novel import (
    MAX_CHAPTER_TITLE_LENGTH,
    chapter_title_quality_issues,
    ensure_unique_chapter_title,
    is_reader_facing_chapter_title,
    normalize_chapter_draft_text,
    normalize_outline_text,
    parse_chapter_heading_line,
    safe_title,
)


class ChapterTitleNormalizationTest(unittest.TestCase):
    def test_outline_sentence_is_rewritten_to_short_heading(self):
        source = (
            '第38章 形成的有效活体确认记录，回砸当前复核入口，逼流程承认'
            '“先认活体，再走代偿”属于自相矛盾。\n'
            '章末钩子：复核没有取消，而是被上调为“联席核验”。\n'
        )
        normalized = normalize_outline_text(43, source)
        first_line = normalized.splitlines()[0]
        chapter_number, title = parse_chapter_heading_line(first_line)

        self.assertEqual(chapter_number, 43)
        self.assertTrue(title)
        self.assertLessEqual(len(title), MAX_CHAPTER_TITLE_LENGTH)
        self.assertNotIn('，', title)
        self.assertIn('章末钩子', normalized)

    def test_safe_title_extracts_short_title_from_sentence_like_outline(self):
        source = (
            '第43章 形成的有效活体确认记录，回砸当前复核入口，逼流程承认'
            '“先认活体，再走代偿”属于自相矛盾。'
        )
        title = safe_title(source)
        self.assertTrue(title)
        self.assertLessEqual(len(title), MAX_CHAPTER_TITLE_LENGTH)
        self.assertNotIn('。', title)

    def test_plain_overlong_title_is_limited_to_ten_chars(self):
        title = safe_title('第7章 谁来决定这座城记住什么')

        self.assertLessEqual(len(title), 10)
        self.assertLessEqual(len(title), MAX_CHAPTER_TITLE_LENGTH)

    def test_draft_without_heading_gets_standard_heading(self):
        draft = (
            '形成的有效活体确认记录，回砸当前复核入口，逼流程承认“先认活体，再走代偿”属于自相矛盾。\n'
            '第二段正文。\n'
        )
        normalized = normalize_chapter_draft_text(43, safe_title(draft), draft)
        lines = [line for line in normalized.splitlines() if line.strip()]
        chapter_number, title = parse_chapter_heading_line(lines[0])

        self.assertEqual(chapter_number, 43)
        self.assertTrue(title)
        self.assertLessEqual(len(title), MAX_CHAPTER_TITLE_LENGTH)
        self.assertEqual(lines[1], '形成的有效活体确认记录，回砸当前复核入口，逼流程承认“先认活体，再走代偿”属于自相矛盾。')

    def test_duplicate_title_gets_unique_fallback(self):
        title = ensure_unique_chapter_title(
            45,
            '缺位见证',
            {'缺位见证', '第二见证席'},
            '第45章 缺位见证\n第二见证席亮起的那一瞬，听证厅里所有人的脸色都变了。\n',
        )
        self.assertNotEqual(title, '缺位见证')
        self.assertNotEqual(title, '第二见证席')
        self.assertLessEqual(len(title), MAX_CHAPTER_TITLE_LENGTH)
        self.assertNotRegex(title, r'[0-9·]')
        self.assertTrue(is_reader_facing_chapter_title(title))

    def test_machine_style_process_title_is_rejected(self):
        self.assertTrue(chapter_title_quality_issues('执行期'))
        self.assertTrue(chapter_title_quality_issues('低耗存续验证'))
        self.assertTrue(chapter_title_quality_issues('尾声'))

    def test_reader_facing_title_can_use_a_concrete_conflict(self):
        self.assertTrue(is_reader_facing_chapter_title('他们还留了把钥匙'))
        self.assertTrue(is_reader_facing_chapter_title('协议还在杀人'))


if __name__ == '__main__':
    unittest.main()
