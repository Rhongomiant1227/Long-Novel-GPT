import os
import sys
import unittest


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auto_novel import (
    clean_generated_draft_body,
    clean_live_draft_stream_text,
    normalize_chapter_draft_text,
)
from core.writer_utils import split_text_into_chunks


class LocalDraftCleanupTest(unittest.TestCase):
    def test_normalize_chapter_draft_text_strips_code_fences_and_body_labels(self):
        source = (
            "第1章 神经灼痕\n\n"
            "```【正文】贫民区的夜气沉重而黏滞。治安无人机低空巡航。```\n"
            "```【正文】贫民区的夜气沉重而黏滞。治安无人机低空巡航。```\n"
        )

        normalized = normalize_chapter_draft_text(1, "神经灼痕", source)

        self.assertTrue(normalized.startswith("第1章 神经灼痕\n\n"))
        self.assertNotIn("```", normalized)
        self.assertNotIn("【正文】", normalized)
        self.assertEqual(normalized.count("贫民区的夜气沉重而黏滞。"), 1)

    def test_clean_generated_draft_body_dedupes_repeated_paragraphs(self):
        repeated = "陆关推开诊所的门，夜风扑到脸上。他没有回头，只把芯片攥得更紧。"
        source = (
            f"{repeated}\n\n"
            f"{repeated}\n\n"
            "岚医生隔着昏黄灯光看着他，最后只说了一句，别死。"
        )

        cleaned = clean_generated_draft_body(2, "医疗暗网", source)

        self.assertEqual(cleaned.count(repeated), 1)
        self.assertIn("岚医生隔着昏黄灯光看着他", cleaned)

    def test_clean_live_draft_stream_text_removes_code_fence_noise(self):
        source = (
            "```2026-06-12 12:13:25 | [第5章正文] 流式进行中，当前分块数 1\n\n"
            "【正文】陆关抬眼看向深网神龛。\n```"
        )

        cleaned = clean_live_draft_stream_text(source)

        self.assertNotIn("```", cleaned)
        self.assertNotIn("【正文】", cleaned)
        self.assertIn("陆关抬眼看向深网神龛。", cleaned)

    def test_split_text_into_chunks_hard_splits_long_unpunctuated_text(self):
        text = "甲" * 120
        chunks = split_text_into_chunks(
            text,
            max_chunk_size=40,
            min_chunk_n=3,
            min_chunk_size=5,
            max_chunk_n=20,
        )

        self.assertGreaterEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= 40 for chunk in chunks))
        self.assertEqual("".join(chunks), text)


if __name__ == "__main__":
    unittest.main()
