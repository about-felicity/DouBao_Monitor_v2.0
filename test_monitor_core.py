import unittest

from monitor_core.analytics import build_analytics
from monitor_core.plugins import discover_plugins
from monitor_core.quality import invalid_answer_reason
from monitor_core.scheduling import build_question_schedule, normalize_question_mode


class QualityTests(unittest.TestCase):
    def test_empty_short_and_system_errors_are_skipped(self):
        self.assertTrue(invalid_answer_reason(""))
        self.assertTrue(invalid_answer_reason("好的"))
        self.assertTrue(invalid_answer_reason("系统异常，请稍后重试"))
        self.assertFalse(invalid_answer_reason("这是一个内容完整、可以正常保存并用于信源分析的模型回答。"))


class SchedulingTests(unittest.TestCase):
    def test_interleaved_asks_each_question_once_per_round(self):
        self.assertEqual(
            build_question_schedule(["A", "B"], 3, "interleaved"),
            ["A", "B", "A", "B", "A", "B"],
        )

    def test_sequential_finishes_one_question_before_the_next(self):
        self.assertEqual(
            build_question_schedule(["A", "B"], 3, "sequential"),
            ["A", "A", "A", "B", "B", "B"],
        )

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_question_mode("unknown")


class AnalyticsTests(unittest.TestCase):
    def test_models_are_discovered_from_isolated_plugin_directories(self):
        plugins = discover_plugins()
        self.assertEqual(set(plugins), {"doubao", "yuanbao", "deepseek"})
        self.assertEqual(plugins["doubao"].execution, "remote")

    def test_question_date_filter_and_source_types_are_isolated(self):
        metadata = {"demo": {"id": "demo", "name": "演示", "short_name": "演", "tone": "demo"}}
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-1", "sequence": 1,
            "question": "问题 A", "finished_at": "2026-08-03T10:00:00+08:00",
            "day": "2026-08-03", "serial": "local", "answer": "回答", "status": "success",
            "sources": [
                {"title": "文章标题关键词", "url": "https://example.com/a", "canonical_url": "https://example.com/a", "domain": "example.com", "media": "示例网", "type": "文章"},
                {"title": "视频标题关键词", "url": "https://bilibili.com/v/1", "canonical_url": "https://bilibili.com/v/1", "domain": "bilibili.com", "media": "哔哩哔哩", "type": "视频"},
            ],
        }]}
        result = build_analytics(metadata, runs, question="问题 A", date="2026-08-03")
        model = result["models"][0]
        self.assertEqual(model["runs"], 1)
        self.assertEqual(len(model["top_articles"]), 1)
        self.assertEqual(len(model["top_videos"]), 1)


class PluginCommandTests(unittest.TestCase):
    def test_yuanbao_command_receives_per_question_rounds_and_mode(self):
        plugin = discover_plugins()["yuanbao"]
        command, _ = plugin.command({"rounds": 3, "question_mode": "sequential"})
        self.assertIn("--rounds-per-question", command)
        self.assertEqual(command[command.index("--rounds-per-question") + 1], "3")
        self.assertEqual(command[command.index("--mode") + 1], "sequential")

    def test_deepseek_command_receives_interleaved_mode(self):
        plugin = discover_plugins()["deepseek"]
        plugin.account_check = lambda: {"ok": True}
        command, _ = plugin.command({"rounds": 2, "question_mode": "interleaved"})
        self.assertEqual(command[command.index("--rounds-per-question") + 1], "2")
        self.assertEqual(command[command.index("--question-mode") + 1], "interleaved")


if __name__ == "__main__":
    unittest.main()
