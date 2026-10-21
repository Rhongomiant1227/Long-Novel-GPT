import unittest

from core.humanizer import (
    build_humanizer_integrity_review_prompt,
    normalize_humanizer_output,
    validate_humanizer_revision,
)


class HumanizerTest(unittest.TestCase):
    def test_output_drops_heading_and_wrapper(self):
        result = normalize_humanizer_output(
            "```text\n第12章 旧标题\n正文第一段。\n正文第二段。\n```",
            chapter_number=12,
            title="旧标题",
        )
        self.assertEqual(result, "正文第一段。\n正文第二段。")

    def test_revision_protects_named_terms(self):
        report = validate_humanizer_revision(
            "许沉握紧拆线钳。守夜人没有回头。",
            "许沉握紧拆线钳，守夜人仍站在灯下。",
            protected_terms=("许沉", "守夜人"),
        )
        self.assertTrue(report.valid)

    def test_revision_rejects_meta_leak(self):
        report = validate_humanizer_revision(
            "原稿正文。" * 20,
            "作为AI，以下是改写后的正文。" + ("新正文。" * 20),
        )
        self.assertFalse(report.valid)
        self.assertEqual(report.reason, "meta_text_leak")

    def test_integrity_prompt_requires_relationship_preservation(self):
        prompt = build_humanizer_integrity_review_prompt(
            chapter_number=3,
            title="旧站",
            original_body="许沉救下了守夜人。",
            revised_body="许沉救下了守夜人。",
        )
        self.assertIn("人物关系", prompt)
        self.assertIn('"approved": true', prompt)


if __name__ == "__main__":
    unittest.main()
