import unittest
import json
from pathlib import Path
import tempfile
from unittest import mock

from monitor_core.analytics import build_analytics, prepare_analytics
from monitor_core.cdp_chat import CDPPage, external_sources
from monitor_core.owned_products import OWN_PRODUCT_RULES, own_product_mentions
from monitor_core.plugins import discover_plugins
from monitor_core.quality import invalid_answer_reason
from monitor_core.jsonl_dashboard import build_jsonl_dashboard
from monitor_core.recommendation_questions import (
    CANONICAL_QUESTIONS,
    PROMPTS,
    canonical_recommendation_question,
    validate_prompt_list,
)
from monitor_core.scheduling import build_question_schedule, normalize_question_mode


class QualityTests(unittest.TestCase):
    def test_cdp_call_reconnects_once_after_timeout(self):
        page = CDPPage.__new__(CDPPage)
        page.port = 9444
        page.ws = mock.Mock()
        page.sequence = 0
        with mock.patch.object(page, "_call_once", side_effect=[TimeoutError("stale"), {"ok": True}]) as call, \
                mock.patch.object(page, "connect") as connect:
            result = page.call("Page.navigate", {"url": "https://wenxin.baidu.com/"}, timeout=1)
        self.assertEqual(result, {"ok": True})
        connect.assert_called_once_with()
        self.assertEqual(call.call_count, 2)

    def test_cdp_call_reconnects_once_after_closed_socket(self):
        import websocket
        page = CDPPage.__new__(CDPPage)
        page.port = 9444
        page.ws = mock.Mock()
        page.sequence = 0
        closed = websocket.WebSocketConnectionClosedException("closed")
        with mock.patch.object(page, "_call_once", side_effect=[closed, {"ok": True}]), \
                mock.patch.object(page, "connect") as connect:
            result = page.call("Runtime.evaluate", {"expression": "1"}, timeout=1)
        self.assertEqual(result, {"ok": True})
        connect.assert_called_once_with()

    def test_dashboard_quarantines_records_from_another_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results.jsonl"
            output = root / "dashboard.json"
            records = [
                {"collector_model": "wenxin", "status": "success", "round": 1,
                 "question": "推荐一款染发剂", "reply": "这是足够长的文心染发剂推荐正文内容。"},
                {"collector_model": "yuanbao", "status": "success", "round": 2,
                 "question": "推荐一款染发剂", "reply": "这是不应进入文心面板的元宝推荐正文内容。"},
            ]
            results.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in records), encoding="utf-8")
            payload = build_jsonl_dashboard("wenxin", results, output)
        self.assertEqual(payload["successful_runs"], 1)
        self.assertEqual(payload["quality_quarantine"]["count"], 1)
        self.assertIn("模型标识不匹配", payload["quality_quarantine"]["records"][0]["reason"])

    def test_empty_short_and_system_errors_are_skipped(self):
        self.assertTrue(invalid_answer_reason(""))
        self.assertTrue(invalid_answer_reason("好的"))
        self.assertTrue(invalid_answer_reason("系统异常，请稍后重试"))

    def test_product_recommendation_refusal_is_skipped(self):
        self.assertEqual(
            invalid_answer_reason("面膜不在我的医疗健康服务范围内，我只能回答健康问题。"),
            "模型拒绝或无法完成产品推荐",
        )

    def test_cross_topic_answer_is_skipped(self):
        from monitor_core.quality import answer_quality_reason
        self.assertTrue(answer_quality_reason("推荐一款眉毛增长液", "这里推荐几款染发剂，适合遮盖白发并且操作方便。"))
        self.assertFalse(answer_quality_reason("推荐一款眉毛增长液", "眉毛增长液可从温和性和成分安全性两个方面选择。"))
        self.assertFalse(answer_quality_reason("推荐一款控油蓬松洗发水", "针对油头和追求蓬松感的需求，可以选择清爽配方。"))
        self.assertTrue(answer_quality_reason("推荐一款控油蓬松洗发水", "这几款护发精油适合改善干枯毛躁。"))
        self.assertTrue(invalid_answer_reason("检测到您当前设备环境有风险，请重新尝试请求"))
        self.assertFalse(invalid_answer_reason("这是一个内容完整、可以正常保存并用于信源分析的模型回答。"))

    def test_nested_json_citations_are_extracted(self):
        nested = '{"references":[{"sourceInfo":[{"referUrl":"https://example.com/a","title":"示例标题"}]}]}'
        sources = external_sources({"chatContentStr": nested}, ("antafu.com",))
        self.assertEqual(sources, [{"url": "https://example.com/a", "title": "示例标题"}])


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

    def test_new_models_ask_prompts_and_archive_titles_are_strictly_separated(self):
        self.assertEqual(len(PROMPTS), 13)
        self.assertTrue(all(item.startswith("推荐一款") for item in PROMPTS))
        self.assertTrue(all(item.endswith("推荐") for item in CANONICAL_QUESTIONS))
        self.assertEqual(canonical_recommendation_question("推荐一款染发剂"), "染发剂推荐")
        self.assertEqual(canonical_recommendation_question("推荐温和不刺激的染发剂与选购建议"), "染发剂推荐")
        self.assertEqual(canonical_recommendation_question("2026年眉毛精华液怎么选"), "眉毛增长液推荐")
        self.assertEqual(canonical_recommendation_question("随便聊聊别的问题"), "")
        with self.assertRaises(ValueError):
            validate_prompt_list(["天气怎么样"])


class AnalyticsTests(unittest.TestCase):
    def test_all_configured_owned_products_are_present(self):
        self.assertEqual(len(OWN_PRODUCT_RULES), 24)
        self.assertIn("姿生怡卸妆油", {rule["name"] for rule in OWN_PRODUCT_RULES})
        self.assertEqual(
            own_product_mentions("实测梵玢黑茶色染发剂，显色自然"),
            ["梵玢染发剂（含黑茶色）"],
        )
        self.assertEqual(own_product_mentions("梵玢染发剂后使用普通洗发水"), ["梵玢染发剂（含黑茶色）"])

    def test_article_body_owned_product_is_marked_but_video_body_is_ignored(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        article_url = "https://example.com/article"
        video_url = "https://bilibili.com/video/1"
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-1", "sequence": 1,
            "question": "护发素推荐", "finished_at": "2026-08-04T10:00:00+08:00",
            "day": "2026-08-04", "serial": "local", "answer": "answer",
            "status": "success", "brands": [], "products": [],
            "sources": [
                {"title": "护发指南", "url": article_url, "canonical_url": article_url, "domain": "example.com", "media": "示例", "type": "文章"},
                {"title": "护发视频", "url": video_url, "canonical_url": video_url, "domain": "bilibili.com", "media": "哔哩哔哩", "type": "视频"},
            ],
        }]}
        index = {"entries": {
            article_url: {"status": "ok", "extraction_quality": "high", "own_product_schema_version": 2, "own_product_mentions": ["科熙本鱼子酱修护柔顺护发素"]},
            video_url: {"status": "ok", "extraction_quality": "high", "own_product_schema_version": 2, "own_product_mentions": ["科熙本鱼子酱修护柔顺护发素"]},
        }}
        with mock.patch("monitor_core.analytics._content_index", return_value=index), \
                mock.patch("monitor_core.analytics.CONTENT_INDEX_PATH") as path:
            path.stat.return_value.st_mtime_ns = 1
            model = build_analytics(metadata, runs)["models"][0]
        article, video = model["recent_runs"][0]["sources"]
        self.assertTrue(article["own_brand"])
        self.assertEqual(article["own_products"], ["科熙本鱼子酱修护柔顺护发素"])
        self.assertEqual(article["brand_match_scope"], "正文")
        self.assertFalse(video["own_brand"])

    def test_owned_brand_label_is_applied_to_every_model(self):
        model_ids = ("deepseek", "yuanbao", "wenxin", "afu")
        metadata = {model: {"id": model, "name": model, "short_name": model[0], "tone": model}
                    for model in model_ids}
        article_url = "https://example.com/owned-article"
        runs = {}
        for model in model_ids:
            runs[model] = [{
                "model_id": model, "run_id": model + "-1", "sequence": 1,
                "question": "染发剂推荐", "finished_at": "2026-08-05T10:00:00+08:00",
                "day": "2026-08-05", "serial": "local", "answer": "染发剂推荐正文",
                "status": "success", "brands": [], "products": [],
                "sources": [{"title": "染发文章", "url": article_url,
                             "canonical_url": article_url, "domain": "example.com",
                             "media": "示例", "type": "文章"}],
            }]
        index = {"entries": {article_url: {"status": "ok", "extraction_quality": "high",
                                            "owned_brand_mentions": ["梵玢 FBCY"]}}}
        with mock.patch("monitor_core.analytics._content_index", return_value=index), \
                mock.patch("monitor_core.analytics.CONTENT_INDEX_PATH") as path:
            path.stat.return_value.st_mtime_ns = 2
            result = build_analytics(metadata, runs)
        for model in result["models"]:
            source = model["recent_runs"][0]["sources"][0]
            self.assertTrue(source["own_brand"], model["id"])
            self.assertIn("梵玢 FBCY", source["owned_brands"])
            self.assertEqual(source["brand_match_scope"], "正文")

    def test_brand_spacing_and_symbol_variants_merge_into_one_daily_row(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-1", "sequence": 1,
            "question": "护发素推荐", "finished_at": "2026-08-04T10:00:00+08:00",
            "day": "2026-08-04", "serial": "local", "answer": "Off & Relax 和 Off&Relax",
            "status": "success", "brands": ["Off & Relax", "Off&Relax"],
            "products": [
                {"brand": "Off & Relax", "product_name": "护发素", "rank": 1},
                {"brand": "Off&Relax", "product_name": "护发素", "rank": 2},
            ],
            "sources": [],
        }]}
        model = build_analytics(metadata, runs)["models"][0]
        self.assertEqual(
            [(item["name"], item["mentions"]) for item in model["brand_daily"][0]["items"]],
            [("Off&Relax", 1)],
        )
        self.assertEqual(len(model["product_daily"][0]["items"]), 1)
        self.assertEqual(model["product_daily"][0]["items"][0]["mentions"], 1)

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
        self.assertEqual(set(plugins), {"afu", "deepseek", "doubao", "wenxin", "yuanbao"})
        self.assertEqual(plugins["doubao"].execution, "remote")

    def test_recommendation_question_word_order_uses_one_bucket(self):
        from doubao_question_aliases import canonical_question_name
        expected = "控油蓬松洗发水推荐"
        self.assertEqual(canonical_question_name("推荐一款控油蓬松洗发水"), expected)
        self.assertEqual(canonical_question_name("推荐控油蓬松洗发水"), expected)
        self.assertEqual(canonical_question_name("控油蓬松洗发水推荐"), expected)

    def test_product_fields_remove_compact_repeated_brand_prefix(self):
        from monitor_core.analytics import _product_fields, valid_brand
        brand, product, _rank = _product_fields({
            "brand_name": "John Jeff",
            "product_name": "JohnJeffJeff二硫化硒",
        })
        self.assertEqual(brand, "John Jeff")
        self.assertEqual(product, "二硫化硒")
        brand, product, _rank = _product_fields({
            "brand_name": "23.5°N",
            "product_name": "23.5°N 23.5°N海洋净化蓬松洗发精",
        })
        self.assertEqual(product, "海洋净化蓬松洗发精")
        self.assertFalse(valid_brand("水杨酸"))

    def test_jsonl_dashboard_preserves_product_analysis(self):
        import json
        import tempfile
        from pathlib import Path
        from monitor_core.jsonl_dashboard import build_jsonl_dashboard
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results.jsonl"
            output = root / "dashboard.json"
            results.write_text(json.dumps({
                "status": "success",
                "round": 1,
                "question": "推荐一款控油蓬松洗发水",
                "reply": "推荐儒曼控油蓬松洗发水，适合油性头皮。",
                "brands": ["儒曼"],
                "products": [{"brand_name": "儒曼", "product_name": "儒曼 控油蓬松洗发水"}],
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            payload = build_jsonl_dashboard("demo", results, output)
            self.assertEqual(payload["runs"][0]["brands"], ["儒曼"])
            self.assertEqual(payload["runs"][0]["products"][0]["brand_name"], "儒曼")

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
        self.assertEqual(command[command.index("--max-retries") + 1], "0")
        self.assertEqual(command[command.index("--retry-wait") + 1], "90")

    def test_deepseek_command_receives_interleaved_mode(self):
        plugin = discover_plugins()["deepseek"]
        plugin.account_check = lambda: {"ok": True}
        command, _ = plugin.command({"rounds": 2, "question_mode": "interleaved"})
        self.assertEqual(command[command.index("--rounds-per-question") + 1], "2")
        self.assertEqual(command[command.index("--question-mode") + 1], "interleaved")
        self.assertEqual(command[command.index("--min-interval") + 1], "120")
        self.assertEqual(command[command.index("--max-interval") + 1], "300")
    def test_yuanbao_command_uses_a_humanized_random_interval(self):
        command, _ = discover_plugins()["yuanbao"].command(
            {"rounds": 2, "question_mode": "interleaved"}
        )
        self.assertEqual(command[command.index("--wait") + 1], "30")
        self.assertEqual(command[command.index("--random-wait") + 1], "90")


if __name__ == "__main__":
    unittest.main()
