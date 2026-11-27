import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from auto_novel import AutoNovelRunner
from core.project_overrides import load_project_overrides
from core.webnovel_craft import (
    draft_craft_guidance,
    outline_craft_guidance,
    plot_craft_guidance,
    supported_continuity_alerts,
)


class WebnovelCraftTests(unittest.TestCase):
    def test_plot_contract_focuses_on_goal_leverage_causality_and_earned_hooks(self):
        guidance = plot_craft_guidance()
        for expected in ("人物眼下要什么", "筹码", "局面怎样改变", "凭空追加危险"):
            self.assertIn(expected, guidance)

    def test_draft_contract_supports_flirtation_without_fixed_formula(self):
        guidance = draft_craft_guidance()
        self.assertIn("暧昧", guidance)
        self.assertIn("改变彼此的判断或局势", guidance)
        self.assertIn("不套固定段长", guidance)

    def test_opening_guidance_is_not_injected_into_later_batches(self):
        self.assertIn("前三章", outline_craft_guidance(1, 3))
        self.assertNotIn("前三章", outline_craft_guidance(51, 55))
        self.assertIn("到期伏笔", outline_craft_guidance(51, 55))

    def test_continuity_alert_requires_exact_quotes_from_both_inputs(self):
        sources = {"chapter_4": "掌柜昨夜亲手把钥匙还了回去。"}
        draft = "掌柜从未见过那把钥匙。"
        alert = {
            "source_id": "chapter_4",
            "source_excerpt": "掌柜昨夜亲手把钥匙还了回去。",
            "draft_excerpt": draft,
            "explanation": "知情状态冲突",
        }
        invalid = [
            {**alert, "source_id": "chapter_3"},
            {**alert, "source_excerpt": "掌柜昨晚归还了钥匙"},
            {**alert, "draft_excerpt": "掌柜没见过钥匙"},
            {**alert, "draft_excerpt": "钥匙"},
        ]
        self.assertEqual(supported_continuity_alerts([*invalid, alert, alert], sources, draft), [alert])

    def test_historical_review_only_reads_earlier_drafts(self):
        runner = AutoNovelRunner.__new__(AutoNovelRunner)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner.brief_path = root / "brief.md"
            runner.brief_path.write_text("Known rules.", encoding="utf-8")
            runner.state = {"completed_chapters": []}
            for number in range(1, 6):
                draft = root / f"{number}.md"
                draft.write_text(f"Body {number}", encoding="utf-8")
                runner.state["completed_chapters"].append(
                    {"chapter_number": number, "draft_file": str(draft)}
                )
            self.assertEqual(runner._continuity_review_sources(4), {
                "brief": "Known rules.", "chapter_3": "Body 3", "chapter_2": "Body 2",
            })

    def test_continuity_only_findings_do_not_trigger_critic_rewrites(self):
        runner = AutoNovelRunner.__new__(AutoNovelRunner)
        report = runner._normalize_critic_report({
            "needs_fix": False,
            "issues": [{"type": "cross_chapter", "excerpt": "Some quote"}],
            "continuity_alerts": [{"source_id": "chapter_1"}],
        })
        self.assertEqual(report["issues"], [])

    def test_cli_can_restore_baseline_guidance(self):
        runner = AutoNovelRunner.__new__(AutoNovelRunner)
        runner.args = SimpleNamespace()
        self.assertTrue(runner._uses_webnovel_skill())
        runner.args.disable_webnovel_skill = True
        self.assertFalse(runner._uses_webnovel_skill())

    def test_project_overrides_default_to_empty_when_machine_file_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = os.path.join(temp_dir, "missing.json")
            with patch.dict(os.environ, {"LONG_NOVEL_PROJECT_OVERRIDES": missing}):
                self.assertEqual(load_project_overrides(), {})


if __name__ == "__main__":
    unittest.main()
