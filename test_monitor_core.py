import unittest

from monitor_core.analytics import build_analytics, prepare_analytics
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
    def test_selected_model_filter_options_do_not_leak_other_model_dates(self):
        metadata = {
            "a": {"id": "a", "name": "A", "short_name": "A", "tone": "a"},
            "b": {"id": "b", "name": "B", "short_name": "B", "tone": "b"},
        }
        def run(model, day, question):
            return {
                "model_id": model, "run_id": f"{model}-{day}", "sequence": 1,
                "question": question, "finished_at": day, "day": day,
                "serial": "local", "answer": "answer", "status": "success",
                "brands": [], "products": [], "sources": [],
            }
        runs = {
            "a": [run("a", "2026-08-04", "Question A")],
            "b": [run("b", "2026-07-31", "Question B")],
        }
        result = build_analytics(metadata, runs, model="a")
        self.assertEqual(result["dates"], ["2026-08-04"])
        self.assertEqual(result["questions"], ["Question A"])

    def test_prepared_snapshot_can_be_reused_across_filters(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-1", "sequence": 1,
            "question": "Question A", "finished_at": "2026-08-03T10:00:00+08:00",
            "day": "2026-08-03", "serial": "local", "answer": "Brand answer",
            "status": "success", "brands": ["Brand"], "products": [], "sources": [],
        }]}
        prepared = prepare_analytics(runs)
        result = build_analytics(
            metadata, runs, model="demo", question="Question A",
            date="2026-08-03", prepared=prepared,
        )
        self.assertEqual(result["models"][0]["runs"], 1)

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

    def test_daily_brand_product_rates_ranks_and_owned_source_marks(self):
        metadata = {"demo": {"id": "demo", "name": "演示", "short_name": "演", "tone": "demo"}}
        runs = {"demo": [
            {"model_id": "demo", "run_id": "d1", "sequence": 1, "question": "染发剂推荐", "finished_at": "2026-08-02T10:00:00+08:00", "day": "2026-08-02", "serial": "local", "answer": "推荐梵玢染发剂", "status": "success", "brands": ["梵玢"], "products": [{"brand": "梵玢", "product_name": "染发剂", "rank": 2}], "sources": [{"title": "梵玢染发剂实测视频", "url": "https://bilibili.com/v/owned", "canonical_url": "https://bilibili.com/v/owned", "domain": "bilibili.com", "media": "哔哩哔哩", "type": "视频"}]},
            {"model_id": "demo", "run_id": "d2", "sequence": 2, "question": "染发剂推荐", "finished_at": "2026-08-02T11:00:00+08:00", "day": "2026-08-02", "serial": "local", "answer": "其他品牌", "status": "success", "brands": [], "products": [], "sources": []},
        ]}
        model = build_analytics(metadata, runs)["models"][0]
        self.assertEqual(model["brand_daily"][0]["items"][0]["mention_rate"], 50.0)
        self.assertEqual(model["product_daily"][0]["items"][0]["rank"], 1)
        self.assertTrue(model["top_videos"][0]["own_brand"])
        self.assertEqual(model["source_brand_daily"][0]["owned_source_rate"], 100.0)


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
        self.assertEqual(command[command.index("--min-interval") + 1], "60")
        self.assertEqual(command[command.index("--max-interval") + 1], "600")

    def test_yuanbao_command_uses_a_humanized_random_interval(self):
        command, _ = discover_plugins()["yuanbao"].command(
            {"rounds": 2, "question_mode": "interleaved"}
        )
        self.assertEqual(command[command.index("--wait") + 1], "30")
        self.assertEqual(command[command.index("--random-wait") + 1], "90")


if __name__ == "__main__":
    unittest.main()
