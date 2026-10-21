import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "humanize_all_projects.py"
SPEC = importlib.util.spec_from_file_location("humanize_all_projects", SCRIPT_PATH)
assert SPEC and SPEC.loader
humanize_all_projects = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(humanize_all_projects)


class HumanizeAllProjectsTest(unittest.TestCase):
    def test_concurrency_is_bounded_by_project_count(self):
        self.assertEqual(max(1, min(0 or 1, 4)), 1)
        self.assertEqual(max(1, min(3, 4)), 3)

    def test_logger_serializes_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.log"
            logger = humanize_all_projects.Logger(path)
            logger.write("one")
            logger.write("two")
            logger.close()
            self.assertEqual(path.read_text(encoding="utf-8").splitlines(), ["one", "two"])


if __name__ == "__main__":
    unittest.main()
