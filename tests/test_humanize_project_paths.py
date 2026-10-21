import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "humanize_project.py"
SPEC = importlib.util.spec_from_file_location("humanize_project", SCRIPT_PATH)
assert SPEC and SPEC.loader
humanize_project = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(humanize_project)


class HumanizeProjectPathTest(unittest.TestCase):
    def test_context_skips_empty_optional_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            draft = Path(directory) / "draft.md"
            draft.write_text("第1章 测试\n正文。", encoding="utf-8")

            outline, plot = humanize_project.chapter_context(
                {"draft_file": str(draft), "outline_file": "", "plot_file": ""},
                "",
            )

        self.assertEqual(outline, "")
        self.assertEqual(plot, "")


if __name__ == "__main__":
    unittest.main()
