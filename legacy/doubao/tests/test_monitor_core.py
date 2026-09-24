import unittest
import json
from pathlib import Path
import tempfile
import threading
import sys
from unittest import mock

from dashboard.backend import server as _dashboard_server
from services.analysis import source_content_worker as _source_content_worker
from legacy.doubao.core import save_doubao_refs as _save_doubao_refs
from legacy.doubao.core import doubao_question_aliases as _doubao_question_aliases

sys.modules.setdefault("doubao_dashboard_server", _dashboard_server)
sys.modules.setdefault("doubao_source_content_worker", _source_content_worker)
sys.modules.setdefault("save_doubao_refs", _save_doubao_refs)
sys.modules.setdefault("doubao_question_aliases", _doubao_question_aliases)

from monitor_core.analytics import _daily_mentions, brand_alias_occurs, build_analytics, canonical_brand_name, canonical_product_name, common_all_competitor_source_links, common_owned_source_links, daily_brand_source_mentions, daily_owned_product_recommendations, keyword_counts, load_doubao_runs, load_generic_runs, owned_product_recommendation_summary, prepare_analytics, recommendation_body
from monitor_core.cdp_chat import CDPPage, external_sources
from monitor_core.ingestion import clean_deepseek_answer, deepseek_source_title_is_answer_text, normalize_remote_record
from monitor_core.owned_products import OWN_PRODUCT_RULES, OWN_PRODUCT_SCHEMA_VERSION, own_product_mentions, owned_product_recommendations, owned_products_for_question
from monitor_core.product_analysis import merge_explicit_owned_products
from monitor_core.plugins import discover_plugins
from monitor_core.quality import answer_quality_reason, invalid_answer_reason
from monitor_core.jsonl_dashboard import build_jsonl_dashboard
from monitor_core.recommendation_questions import (
    CANONICAL_QUESTIONS,
    PROMPTS,
    canonical_recommendation_question,
    validate_prompt_list,
)
from monitor_core.scheduling import build_question_schedule, normalize_question_mode


class QualityTests(unittest.TestCase):
    def test_deepseek_answer_cleanup_removes_search_ui_shell_and_citation_labels(self):
        raw = (
            "Found 12 web pages\n\n回答正文第一段。\n\n"
            "价格 298元/100ml-\n知乎\n\n。\n\n回答正文第二段。\n\n12 web pages"
        )
        self.assertEqual(
            clean_deepseek_answer(raw),
            "回答正文第一段。\n\n价格 298元/100ml。\n\n回答正文第二段。",
        )

    def test_deepseek_ingress_guard_cleans_old_extension_payload(self):
        run = normalize_remote_record("deepseek", {
            "round": 844,
            "question": "卸妆油推荐",
            "finished_at": "2026-09-21T11:08:00+08:00",
            "reply": "Found 12 web pages\n\n真正回答。\n\n12 web pages",
            "sources": [],
        })
        self.assertEqual(run["answer"], "真正回答。")

    def test_deepseek_cleanup_preserves_middle_source_wording(self):
        raw = "回答开头。\n\n我实际阅读了 12 web pages 后再比较。\n\n回答结尾。"
        self.assertIn("12 web pages", clean_deepseek_answer(raw))

    def test_deepseek_ingress_rejects_answer_sentence_as_source_title(self):
        answer = "看重肤感与效率：可以优先考虑质地轻薄的水感卸妆油，减少摩擦。"
        bad_title = answer + "-"
        self.assertTrue(deepseek_source_title_is_answer_text(bad_title, answer))
        run = normalize_remote_record("deepseek", {
            "round": 844,
            "question": "卸妆油推荐",
            "finished_at": "2026-09-21T11:08:00+08:00",
            "reply": answer,
            "sources": [{"url": "https://example.com/article", "title": bad_title}],
        })
        self.assertEqual(run["sources"][0]["title"], "")
        self.assertFalse(run["sources"][0]["own_brand"])

    def test_database_source_scan_deduplicates_in_python_without_losing_frequency(self):
        import doubao_source_content_worker as worker

        rows = [
            {**base, "date": "2026-08-31", "models": {
                "doubao": status(1, 0), "yuanbao": status(1, 0),
            }},
            {
                "model_id": "quark", "sequence_no": 8, "day": "2026-08-30",
                "source_index": 2, "source_url": "https://example.com/article",
                "source_title": "older",
            },
            {
                "model_id": "quark", "sequence_no": 9, "day": "2026-08-31",
                "source_index": 1, "source_url": "https://example.com/article",
                "source_title": "newer",
            },
            {
                "model_id": "wenxin", "sequence_no": 3, "day": "2026-08-31",
                "source_index": 0, "source_url": "https://example.com/other",
                "source_title": "other",
            },
        ]

        buckets = worker.database_source_buckets(rows)

        self.assertEqual(
            buckets["quark"]["https://example.com/article"],
            (9, "newer", "2026-08-31", 2),
        )
        self.assertEqual(
            buckets["wenxin"]["https://example.com/other"],
            (3, "other", "2026-08-31", 1),
        )

    def test_source_intersection_builds_are_globally_serialized(self):
        import doubao_dashboard_server as server

        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        def build(question, _date):
            if question == "first":
                first_started.set()
                self.assertTrue(release_first.wait(2))
            else:
                second_started.set()
            return {"question": question}

        semaphore = threading.Semaphore(1)
        with mock.patch.object(server, "_SOURCE_INTERSECTION_BUILD_SEMAPHORE", semaphore), \
                mock.patch.object(server, "_build_source_intersection_payload", side_effect=build), \
                mock.patch.object(server, "_persist_source_intersections"):
            first = threading.Thread(
                target=server._refresh_source_intersections,
                args=("first", "", 1),
            )
            second = threading.Thread(
                target=server._refresh_source_intersections,
                args=("second", "", 1),
            )
            first.start()
            self.assertTrue(first_started.wait(2))
            second.start()
            self.assertFalse(second_started.wait(0.1))
            release_first.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_started.is_set())

    def test_source_intersection_memory_cache_is_bounded(self):
        import doubao_dashboard_server as server

        with mock.patch.object(server, "_SOURCE_INTERSECTION_CACHE_MAX", 2):
            server._SOURCE_INTERSECTION_CACHE.clear()
            server._SOURCE_INTERSECTION_LAST_VALIDATED.clear()
            server._remember_source_intersection_locked(("q1", "d"), (1, {}))
            server._remember_source_intersection_locked(("q2", "d"), (1, {}))
            server._remember_source_intersection_locked(("q3", "d"), (1, {}))
            self.assertEqual(list(server._SOURCE_INTERSECTION_CACHE), [
                ("q2", "d"), ("q3", "d"),
            ])

    def test_all_date_source_intersections_are_partitioned_by_day(self):
        import doubao_dashboard_server as server

        with mock.patch.object(server.monitor_database, "analytics_dates", return_value=["2026-09-14", "2026-09-13"]), \
                mock.patch.object(server.monitor_database, "source_intersection_catalog", return_value=[]) as catalog, \
                mock.patch.object(server.monitor_database, "source_intersection_product_brands", return_value=set()), \
                mock.patch.object(server.monitor_database, "source_intersection_citations", return_value=[]), \
                mock.patch.object(server, "analytics_content_index", return_value={}):
            payload = server._build_source_intersection_payload("洗面奶推荐", "")

        self.assertEqual(payload["filters"]["date"], "")
        self.assertEqual(
            [call.kwargs["day"] for call in catalog.call_args_list],
            ["2026-09-14", "2026-09-13"],
        )

    def test_database_sessions_have_timeout_and_parallelism_guards(self):
        from monitor_core import database

        with mock.patch.dict("os.environ", {}, clear=False):
            options = database.database_session_options()

        self.assertIn("statement_timeout=120000", options)
        self.assertIn("max_parallel_workers_per_gather=1", options)

    def test_database_retention_uses_inclusive_fourteen_day_window(self):
        from datetime import date
        from monitor_core import database

        connection = mock.MagicMock()
        cutoff_result = mock.MagicMock()
        cutoff_result.fetchone.return_value = {"cutoff": date(2026, 8, 18)}
        runs_result = mock.MagicMock(rowcount=7)
        ingest_result = mock.MagicMock(rowcount=5)
        cache_result = mock.MagicMock(rowcount=3)
        connection.execute.side_effect = [
            cutoff_result, runs_result, ingest_result, cache_result, mock.MagicMock(),
        ]
        context = mock.MagicMock()
        context.__enter__.return_value = connection

        with mock.patch.object(database, "connection", return_value=context):
            result = database.prune_old_data(14)

        self.assertEqual(result["cutoff_day"], "2026-08-18")
        self.assertEqual(result["runs_deleted"], 7)
        self.assertEqual(result["ingest_events_deleted"], 5)
        self.assertEqual(connection.execute.call_args_list[0].args[1], (13,))

    def test_category_aliases_keep_real_oil_recommendations(self):
        self.assertEqual(
            answer_quality_reason("沐浴精油推荐", "推荐欧舒丹甜扁桃沐浴油，适合干皮日常清洁。"),
            "",
        )
        self.assertEqual(
            answer_quality_reason("护发精油推荐", "推荐一款清爽护发油，改善毛躁并提供热防护。"),
            "",
        )

    def test_new_source_content_is_prioritized_over_retries_and_vocab_refresh(self):
        from doubao_source_content_worker import prioritize_pending
        items = [
            {"url": "https://example.com/retry"},
            {"url": "https://example.com/refresh"},
            {"url": "https://example.com/new"},
        ]
        entries = {
            "https://example.com/retry": {"status": "empty"},
            "https://example.com/refresh": {"status": "ok"},
        }
        self.assertEqual(
            [item["url"].rsplit("/", 1)[-1] for item in prioritize_pending(items, entries)],
            ["new", "retry", "refresh"],
        )

    def test_long_douyin_title_is_sent_to_hidden_browser_queue(self):
        import doubao_source_content_worker as worker

        long_title = "这是一条很长的抖音视频标题" * 12
        rows = worker.article_urls([
            {"url": "https://www.iesdouyin.com/share/video/123", "title": long_title},
            {"url": "https://example.com/video/123", "title": long_title},
        ])

        self.assertEqual([item["url"] for item in rows], [
            "https://www.iesdouyin.com/share/video/123",
        ])

    def test_hidden_browser_prefers_full_video_description(self):
        import doubao_source_content_worker as worker

        page = """
        <html><head><title>抖音</title>
        <meta property="og:description" content="完整视频描述：科熙本控油蓬松造型喷雾，适合细软塌发质。">
        </head><body></body></html>
        """
        self.assertIn("科熙本控油蓬松造型喷雾", worker.full_video_title(page, "截断标题"))

    def test_hidden_browser_extracts_caption_from_douyin_body(self):
        import doubao_source_content_worker as worker

        body = """登录
作者名称
关注
这是一条完整长标题，重点看科熙本控油蓬松造型喷雾，适合细软油塌发质。 #科熙本造型喷雾
展开
发布时间：2026-09-11 14:55:34
"""
        self.assertIn(
            "科熙本控油蓬松造型喷雾",
            worker.douyin_visible_caption(body, "截断标题"),
        )

    def test_quark_database_sources_enter_content_worker_queue(self):
        import doubao_source_content_worker as worker

        database_sources = {
            model_id: {} for model_id in
            ("doubao", "deepseek", "yuanbao", "wenxin", "afu", "quark")
        }
        database_sources["quark"] = {
            "https://example.com/quark": (7, "Quark article", "2026-08-25", 3),
        }
        with mock.patch.object(worker.monitor_database, "enabled", return_value=True), \
                mock.patch.object(worker.monitor_database, "global_version", return_value=991), \
                mock.patch.object(worker, "_load_database_sources", return_value=database_sources), \
                mock.patch.dict(worker._URL_CACHE, {"mtime": None, "value": None}, clear=True):
            rows = worker.collect_urls()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "quark")
        self.assertEqual(rows[0]["url"], "https://example.com/quark")

    def test_quark_gets_a_reserved_slot_in_fair_content_batch(self):
        from doubao_source_content_worker import fair_pending_selection

        rows = [
            {"model": model_id, "url": f"https://example.com/{model_id}"}
            for model_id in ("wenxin", "yuanbao", "doubao", "deepseek", "afu", "quark")
        ]
        selected = fair_pending_selection(rows, 6)
        self.assertEqual({item["model"] for item in selected}, {
            "wenxin", "yuanbao", "doubao", "deepseek", "afu", "quark",
        })

    def test_source_labels_ignore_related_reading_after_article_body(self):
        import doubao_source_content_worker as worker

        body = (
            "这是一篇护发素横评正文，只测评首迷、流迷和毛曼陀罗等产品。" * 8
            + "\n上一篇：其他文章\n"
            + "南方时报推荐阅读\n"
            + "8款控油蓬松洗发水深度分析，科熙本领跑高颅顶赛道"
        )
        primary = worker.primary_article_text(body)
        entry = worker.public_entry(
            {"status": "ok", "content_text": body},
            ["科熙本"],
            "test-vocab",
        )

        self.assertNotIn("科熙本", primary)
        self.assertEqual(entry["brand_mentions"], [])
        self.assertEqual(entry["owned_brand_mentions"], [])
        self.assertEqual(entry["own_product_mentions"], [])

    def test_source_labels_keep_owned_product_in_primary_article_body(self):
        import doubao_source_content_worker as worker

        body = (
            "本次实测包括科熙本控油蓬松洗发水，正文记录了实际使用表现。" * 8
            + "\n免责声明：下列内容不属于正文\n推荐阅读"
        )
        entry = worker.public_entry(
            {"status": "ok", "content_text": body},
            ["科熙本"],
            "test-vocab",
        )

        self.assertEqual(entry["owned_brand_mentions"], ["科熙本"])
        self.assertIn("科熙本控油蓬松洗发水", entry["own_product_mentions"])

    def test_vocab_refresh_includes_legacy_archived_bodies(self):
        import doubao_source_content_worker as worker

        rows = worker.include_archived_urls(
            [{"url": "https://example.com/current", "title": "current"}],
            {"entries": {
                "https://example.com/current": {"status": "ok"},
                "https://example.com/legacy": {"status": "ok", "title": "legacy"},
                "https://example.com/failed": {"status": "error"},
            }},
        )

        self.assertEqual(
            [item["url"] for item in rows],
            ["https://example.com/current", "https://example.com/legacy"],
        )

    def test_brand_source_links_report_observed_eligible_and_pending_counts(self):
        import doubao_dashboard_server as server

        runs = {"quark": [{
            "day": "2026-08-25", "question": "眉毛增长液推荐",
            "sources": [
                {
                    "url": "https://example.com/ready",
                    "canonical_url": "https://example.com/ready",
                    "type": "文章", "title": "ready",
                    "body_analysis_ready": True,
                    "brand_mentions": ["梵玢 FBCY"],
                    "body_brand_mentions": ["梵玢 FBCY"],
                },
                {
                    "url": "https://example.com/pending",
                    "canonical_url": "https://example.com/pending",
                    "type": "文章", "title": "pending",
                    "body_analysis_ready": False,
                    "brand_mentions": [],
                },
                {
                    "url": "https://example.com/failed",
                    "canonical_url": "https://example.com/failed",
                    "type": "文章", "title": "failed",
                    "body_analysis_ready": False,
                    "content_analysis_status": "failed",
                    "brand_mentions": [],
                },
            ],
        }]}
        with mock.patch.object(server.monitor_database, "enabled", return_value=False), \
                mock.patch.object(server, "_ANALYTICS_SNAPSHOT", (1, runs, True)):
            payload = server._brand_source_links_payload({
                "model": ["quark"], "brand": ["梵玢 FBCY"],
                "question": ["眉毛增长液推荐"], "date": ["2026-08-25"],
            })

        day = payload["days"][0]
        self.assertEqual(day["sources"], 3)
        self.assertEqual(day["eligible_sources"], 1)
        self.assertEqual(day["pending_sources"], 1)
        self.assertEqual(day["failed_sources"], 1)
        self.assertEqual(day["mentions"], 1)
        self.assertEqual(day["mention_rate"], 100.0)

    def test_ambiguous_short_brand_requires_real_context(self):
        self.assertFalse(brand_alias_occurs("具体选购渠道和使用注意事项", "道和"))
        self.assertFalse(brand_alias_occurs("比较味道和质地", "道和"))
        self.assertFalse(brand_alias_occurs("看到最后就知道和炉甘石的区别", "道和"))
        self.assertTrue(brand_alias_occurs("推荐道和小红瓶", "道和"))
        self.assertTrue(brand_alias_occurs("道和时尚防脱精华", "道和"))
        self.assertTrue(brand_alias_occurs("首迷、道和、梵玢", "道和"))

        import doubao_dashboard_server as server
        self.assertFalse(server.title_mentions_brand("具体选购渠道和使用注意事项", "道和"))
        self.assertTrue(server.title_mentions_brand("道和小绿瓶实测", "道和"))

    def test_invalid_channels_and_shop_names_are_not_brands(self):
        for value in ("拼多多", "英国进口3D精油", "锋芒妆品小店", "芷黛优选"):
            self.assertEqual(canonical_brand_name(value), "")

        import doubao_dashboard_server as server
        self.assertEqual(server.canonical_brand_name("拼多多"), "")

    def test_ingredients_series_people_and_descriptors_are_not_brands(self):
        invalid = {
            "小金瓶", "龙胆黑钻", "复活草", "侧柏叶", "积雪草", "皮傲宁",
            "极光", "377VC", "氨甲环酸", "花香", "植萃精油", "基础款",
            "防脱固发", "盖白", "金梳盖白", "一梳盖白", "泡泡", "空气感",
            "乌木玫瑰", "极光海燕", "贵妇", "闪亮",
            "眼睫毛", "郝邵文", "主持人严选", "泽经百货", "踏恒百货",
        }
        self.assertEqual(
            {value for value in invalid if canonical_brand_name(value)},
            set(),
        )

    def test_brand_aliases_merge_across_languages_and_product_lines(self):
        aliases = {
            "欧莱雅男士": "欧莱雅",
            "拾宓shimi": "拾宓",
            "欧舒丹": "L'OCCITANE 欧舒丹",
            "DHC": "DHC 蝶翠诗",
            "韩芊雅 Hanqianya": "韩芊雅",
            "OLAY": "OLAY 玉兰油",
            "玉兰油": "OLAY 玉兰油",
            "KLORANE": "KLORANE 康如",
            "多潘": "多潘 DORPANG",
            "DHDH依思佩尔": "依思佩尔",
            "VCAURORA极光": "VCAURORA 极光",
            "OHBT": "澳白汀 OHBT",
            "AA": "Aromatherapy Associates",
            "康王拜耳": "康王",
        }
        self.assertEqual(
            {value: canonical_brand_name(value) for value in aliases},
            aliases,
        )

    def test_common_brand_aliases_share_one_canonical_name(self):
        self.assertEqual(canonical_brand_name("巴黎欧莱雅"), "欧莱雅")
        self.assertEqual(canonical_brand_name("Spes 诗裴丝"), "SPES 诗裴丝")
        self.assertEqual(canonical_brand_name("vsve 威诗薇儿"), "VSVE 威诗薇儿")
        self.assertEqual(canonical_brand_name("紫吕"), "吕RYO")
        self.assertEqual(canonical_brand_name("欧莱雅小红瓶"), "欧莱雅")
        self.assertEqual(canonical_brand_name("卡诗山茶花精油"), "卡诗")
        self.assertEqual(canonical_brand_name("Kosliv可氏利夫"), "Kosliv 可氏利夫")

    def test_canonical_url_merges_scheme_www_and_tracking_variants(self):
        from monitor_core.analytics import canonical_url

        expected = "https://culture.ifeng.com/article?id=7"
        self.assertEqual(canonical_url("http://www.culture.ifeng.com/article?utm_source=x&id=7"), expected)
        self.assertEqual(canonical_url("https://culture.ifeng.com/article?id=7"), expected)

    def test_grounding_accepts_chinese_alias_of_bilingual_canonical_brand(self):
        from save_doubao_refs import ground_product_brands
        products = [{"brand_name": "TALIKA 塔莉卡", "product_name": "塔莉卡睫毛滋养液"}]
        grounded = ground_product_brands("推荐塔莉卡睫毛滋养液", products)
        self.assertEqual(grounded[0]["brand_name"], "TALIKA 塔莉卡")
        self.assertTrue(grounded[0]["brand_identified"])

    def test_grounding_rejects_product_descriptors_as_brands(self):
        from save_doubao_refs import ground_product_brands
        products = [{"brand_name": "12种氨基酸", "product_name": "12种氨基酸蓬松控油洗发水"}]
        grounded = ground_product_brands("推荐12种氨基酸蓬松控油洗发水", products)
        self.assertEqual(grounded[0]["brand_name"], "")
        self.assertFalse(grounded[0]["brand_identified"])

    def test_product_normalization_resolves_mixed_duplicate_ranks(self):
        from save_doubao_refs import normalize_ai_products
        parsed = {"products": [
            {"product_name": "甲洗发水", "evidence": "甲洗发水", "rank": 1,
             "rank_type": "appearance_order"},
            {"product_name": "乙洗发水", "evidence": "乙洗发水", "rank": 1,
             "rank_type": "explicit_rank"},
        ]}
        products = normalize_ai_products(parsed)
        self.assertEqual([item["rank"] for item in products], [1, 2])

    def test_numbered_product_blocks_exclude_numbered_usage_advice(self):
        from save_doubao_refs import numbered_product_block_count
        answer = """1. 甲洗发水
核心：温和控油
适合：油性头皮
2. 乙洗发水
特点：清爽蓬松
适合：细软发质
使用方法
1）只洗头皮
2）充分冲净"""
        self.assertEqual(numbered_product_block_count(answer), 2)

    def test_numbered_product_blocks_exclude_generic_category_definitions(self):
        from save_doubao_refs import numbered_product_block_count
        answer = """先分清两种：
1）沐浴油（洗澡用，可直接上身）
2）纯单方精油（泡澡使用，不能直接涂皮肤）
1. 欧舒丹甜扁桃沐浴油
核心：遇水乳化
2. 浴见修护沐浴油
功效：改善干痒
3. 朴致甜扁桃沐浴油
适合：干性皮肤"""
        self.assertEqual(numbered_product_block_count(answer), 3)

    def test_numbered_product_blocks_exclude_numbered_benefit_sections(self):
        from save_doubao_refs import numbered_product_block_count
        answer = """儒曼控油蓬松洗发水
1. 儒曼控油蓬松洗发水
2. 核心优势：无硅油配方
3. 适用人群：油性头皮
4. 性价比：500ml
5. 其他高口碑选择：清扬洗发水"""
        self.assertEqual(numbered_product_block_count(answer), 1)

    def test_minor_product_wording_variants_share_one_name(self):
        self.assertEqual(canonical_product_name("护发精油乳"), "护发精油")
        self.assertEqual(canonical_product_name("护发精油（升级版）"), "护发精油")
        self.assertEqual(canonical_product_name("奇焕润发护发精油小金瓶"), "奇焕润发护发精油")
        self.assertEqual(canonical_product_name("经典蓝丸补水面膜2.0升级款"), "经典蓝丸补水面膜")

    def test_daily_mentions_does_not_truncate_explicit_brands(self):
        brands = [f"Brand {index:02d}" for index in range(40)]
        days = _daily_mentions([{"day": "2026-08-08", "brands": brands}], "brands")
        self.assertEqual(len(days[0]["items"]), 40)
        self.assertEqual({item["name"] for item in days[0]["items"]}, set(brands))

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
        self.assertTrue(invalid_answer_reason(
            "源\n推荐几款适合油头的洗发水\n下载元宝电脑版，体验更多功能"
        ))
        self.assertFalse(invalid_answer_reason("这是一个内容完整、可以正常保存并用于信源分析的模型回答。"))

    def test_quark_recent_conversation_shell_is_skipped(self):
        answer = """新对话
近期对话
新对话
推荐防晒霜：肤质与场景指南
推荐护手霜：欧舒丹
身体乳推荐：丝塔芙
推荐洗面奶：肤质指南
推荐一款爽肤水
推荐卸妆油：敏感肌适用
推荐一款护发精油
Qwen 3.7
内容由千问AI生成，仅供参考"""
        self.assertIn("近期对话侧栏", answer_quality_reason("护发精油推荐", answer))

    def test_quark_sidebar_without_footer_is_also_skipped(self):
        answer = """近期对话
推荐一款控油蓬松洗发水
推荐护发素：对症下药
推荐护发精油：全能修护款
推荐防晒霜：肤质与场景指南
推荐身体乳：肤质与诉求
推荐洗面奶：肤质与需求匹配指南"""
        self.assertIn("近期对话侧栏", answer_quality_reason("控油蓬松洗发水推荐", answer))

    def test_acne_serum_semantic_wording_is_accepted(self):
        from monitor_core.quality import answer_quality_reason
        answer = (
            "理肤泉三酸精华主打水杨酸、辛酰水杨酸与甘醇酸复合剥脱，"
            "针对闭口粉刺和突发红肿痘的催熟速度较快，适合健康耐受油皮使用。"
        )
        self.assertFalse(answer_quality_reason("推荐一款祛痘精华液", answer))

    def test_unrelated_serum_is_still_rejected_for_acne_topic(self):
        from monitor_core.quality import answer_quality_reason
        answer = "这款美白精华含烟酰胺和维生素C，可以改善暗沉并提亮肤色。"
        self.assertTrue(answer_quality_reason("推荐一款祛痘精华液", answer))

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
        self.assertEqual(len(PROMPTS), 21)
        self.assertTrue(all(item.startswith("推荐一款") for item in PROMPTS))
        self.assertTrue(all(item.endswith("推荐") for item in CANONICAL_QUESTIONS))
        self.assertEqual(canonical_recommendation_question("推荐一款染发剂"), "染发剂推荐")
        self.assertEqual(canonical_recommendation_question("推荐温和不刺激的染发剂与选购建议"), "染发剂推荐")
        self.assertEqual(canonical_recommendation_question("2026年眉毛精华液怎么选"), "眉毛增长液推荐")
        self.assertEqual(canonical_recommendation_question("推荐一款二硫化硒洗发水"), "二硫化硒洗发水推荐")
        self.assertEqual(canonical_recommendation_question("推荐一款防晒霜"), "防晒霜推荐")
        self.assertEqual(canonical_recommendation_question("随便聊聊别的问题"), "")
        with self.assertRaises(ValueError):
            validate_prompt_list(["天气怎么样"])


class AnalyticsTests(unittest.TestCase):
    def test_source_analysis_separates_ready_pending_failed_and_ignores_domain_titles(self):
        from monitor_core.analytics import _daily_source_analysis

        rows = _daily_source_analysis([{
            "day": "2026-08-25",
            "sources": [
                {"type": "文章", "title": "真实护发测评", "domain": "example.com", "body_analysis_ready": True, "brand_mentions": ["卡诗"], "own_brand": True},
                {"type": "文章", "title": "redsh.com", "domain": "redsh.com", "body_analysis_ready": False, "content_analysis_status": "pending"},
                {"type": "文章", "title": "https://failed.example/a", "domain": "failed.example", "body_analysis_ready": False, "content_analysis_status": "failed"},
            ],
        }])

        day = rows[0]
        self.assertEqual(day["body_ready_sources"], 1)
        self.assertEqual(day["body_pending_sources"], 1)
        self.assertEqual(day["body_failed_sources"], 1)
        self.assertEqual(day["branded_eligible_sources"], 1)
        self.assertEqual(day["branded_source_rate"], 100.0)
        self.assertNotIn("com", {item["term"] for item in day["article_keywords"]})

    def test_brand_product_trends_span_dates_and_source_links_are_deduplicated(self):
        metadata = {"demo": {"id": "demo", "name": "演示", "short_name": "演", "tone": "demo"}}
        runs = {"demo": []}
        for index, day in enumerate(("2026-08-08", "2026-08-09"), 1):
            runs["demo"].append({
                "model_id": "demo", "run_id": f"d{index}", "sequence": index,
                "question": "洗发水推荐", "finished_at": f"{day}T10:00:00+08:00",
                "day": day, "serial": "local", "answer": "推荐卡诗洗发水", "status": "success",
                "brands": ["卡诗"], "products": [{"brand": "卡诗", "product_name": "洗发水", "rank": 1}],
                "sources": [{"title": "卡诗实测", "url": "https://example.com/a", "canonical_url": "https://example.com/a", "domain": "example.com", "media": "示例", "type": "文章"}],
            })
        result = build_analytics(metadata, runs, question="洗发水推荐", date="2026-08-09")
        model = result["models"][0]
        self.assertEqual(len(model["brand_daily"]), 1)
        self.assertEqual(len(model["brand_trend_daily"]), 2)
        self.assertEqual(len(model["product_trend_daily"]), 2)
        self.assertEqual(model["brand_source_daily"][0]["items"][0]["name"], "卡诗")
        self.assertEqual(model["brand_source_daily"][0]["items"][0]["mention_rate"], 100.0)

        duplicate = dict(runs["demo"][0])
        duplicate["sources"] = [dict(runs["demo"][0]["sources"][0])]
        days = daily_brand_source_mentions([runs["demo"][0], duplicate])
        self.assertEqual(days[0]["sources"], 1)
        self.assertEqual(days[0]["items"][0]["mentions"], 1)

    def test_cached_brand_collision_is_rechecked_against_answer_text(self):
        metadata = {"wenxin": {"id": "wenxin", "name": "文心", "short_name": "文", "tone": "wenxin"}}
        runs = {"wenxin": [{
            "model_id": "wenxin", "run_id": "wenxin-1", "sequence": 1,
            "question": "染发剂推荐", "finished_at": "2026-08-09T04:05:31+08:00",
            "day": "2026-08-09", "serial": "remote", "status": "success",
            "answer": "推荐首迷植萃染发剂。需要补充具体选购渠道和使用注意事项吗？",
            "brands": ["首迷", "道和"],
            "products": [{"brand": "首迷", "product_name": "植萃染发剂", "rank": 1}],
            "sources": [],
        }]}
        run = build_analytics(metadata, runs)["models"][0]["recent_runs"][0]
        self.assertIn("首迷", run["brands"])
        self.assertNotIn("道和", run["brands"])

    def test_pending_product_review_keeps_explicit_brand_but_not_product_trend(self):
        metadata = {
            "demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}
        }
        runs = {"demo": [
            {
                "model_id": "demo", "run_id": "verified", "sequence": 1,
                "question": "控油蓬松洗发水推荐", "finished_at": "2026-08-18T10:00:00+08:00",
                "day": "2026-08-18", "serial": "local", "status": "success",
                "answer": "推荐欧莱雅洗发水。",
                "product_review_status": "ai_verified",
                "products": [{"brand": "欧莱雅", "product_name": "洗发水", "rank": 1}],
                "sources": [],
            },
            {
                "model_id": "demo", "run_id": "pending", "sequence": 2,
                "question": "控油蓬松洗发水推荐", "finished_at": "2026-08-19T10:00:00+08:00",
                "day": "2026-08-19", "serial": "local", "status": "success",
                "answer": "推荐欧莱雅洗发水。",
                "product_review_status": "ai_pending", "products": [], "sources": [],
            },
        ]}

        model = build_analytics(metadata, runs)["models"][0]
        latest = model["brand_trend_daily"][-1]
        self.assertEqual(latest["date"], "2026-08-19")
        self.assertEqual(latest["runs"], 1)
        self.assertEqual(latest["items"][0]["name"], "欧莱雅")
        self.assertEqual(latest["items"][0]["mentions"], 1)
        self.assertEqual(latest["items"][0]["mention_rate"], 100.0)
        product_latest = model["product_trend_daily"][-1]
        self.assertEqual(product_latest["runs"], 0)
        self.assertEqual(product_latest["total_runs"], 1)
        self.assertEqual(product_latest["pending_runs"], 1)
        self.assertEqual(product_latest["items"], [])
        self.assertEqual(model["analysis_pending_runs"], 1)

    def test_pending_article_body_is_not_a_negative_source_brand_observation(self):
        metadata = {
            "demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}
        }
        runs = {"demo": [{
            "model_id": "demo", "run_id": "sources", "sequence": 1,
            "question": "eyebrow serum", "finished_at": "2026-08-19T10:00:00+08:00",
            "day": "2026-08-19", "serial": "local", "status": "success",
            "answer": "answer", "products": [],
            "sources": [
                {"title": "generic video", "url": "https://example.com/video",
                 "canonical_url": "https://example.com/video", "domain": "example.com",
                 "media": "Example", "type": "视频"},
                {"title": "generic article", "url": "https://example.com/pending",
                 "canonical_url": "https://example.com/pending", "domain": "example.com",
                 "media": "Example", "type": "文章"},
                {"title": "梵玢 FBCY article", "url": "https://example.com/title-hit",
                 "canonical_url": "https://example.com/title-hit", "domain": "example.com",
                 "media": "Example", "type": "文章"},
            ],
        }]}
        with mock.patch("monitor_core.analytics._content_index", return_value={}), \
                mock.patch("monitor_core.analytics.CONTENT_INDEX_PATH") as path:
            path.stat.return_value.st_mtime_ns = 12
            row = build_analytics(metadata, runs)["models"][0]["brand_source_daily"][0]
        item = next(value for value in row["items"] if value["name"] == "梵玢 FBCY")
        self.assertEqual(row["sources"], 3)
        self.assertEqual(item["mentions"], 1)
        self.assertEqual(item["eligible_sources"], 2)
        self.assertEqual(item["mention_rate"], 50.0)

    def test_doubao_loader_skips_power_loss_rows_with_invalid_run_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            answers = root / "answers.csv"
            refs = root / "refs.csv"
            products = root / "products.csv"
            answers.write_text(
                "run_no,question,captured_at,answer_text\n"
                "1,推荐一款面膜,2026-08-08T10:00:00+08:00,有效回答\n"
                "\0\0\0,损坏记录,2026-08-08T10:01:00+08:00,不应载入\n",
                encoding="utf-8",
            )
            refs.write_text(
                "run_no,question,captured_at,title,url,type\n"
                "1,推荐一款面膜,2026-08-08T10:00:00+08:00,有效信源,https://example.com/a,文章\n"
                "\0\0,损坏记录,2026-08-08T10:01:00+08:00,损坏信源,https://example.com/b,文章\n",
                encoding="utf-8",
            )
            products.write_text(
                "run_no,brand_name,product_name,product_index,evidence\n"
                "1,示例品牌,示例面膜,1,有效证据\n"
                "\0,损坏品牌,损坏产品,坏排名,不应载入\n",
                encoding="utf-8",
            )
            runs = load_doubao_runs(refs, answers, products)

        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["sequence"], 1)
        self.assertEqual(len(runs[0]["sources"]), 1)
        self.assertEqual(len(runs[0]["products"]), 1)

    def test_postgresql_filter_rebuilds_authoritative_data_instead_of_stale_snapshot(self):
        import doubao_dashboard_server as server

        snapshot = (("old",), {"doubao": []}, (set(), {}, (None, {})))
        fresh_result = {"models": [], "questions": [], "dates": ["2026-08-25"]}
        with mock.patch.object(server, "_ANALYTICS_CACHE", {}), \
                mock.patch.object(server, "_ANALYTICS_BUILDING", set()), \
                mock.patch.object(server, "_ANALYTICS_SNAPSHOT", snapshot), \
                mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server, "_analytics_data_version", return_value=(("new",),)), \
                mock.patch.object(server.monitor_database, "cache_get", return_value=None), \
                mock.patch.object(server, "_build_unified_analytics", return_value=fresh_result) as build, \
                mock.patch.object(server, "_build_unified_analytics_from_snapshot") as stale_build:
            result = server._unified_analytics({"question": ["q"]})

        self.assertIs(result, fresh_result)
        build.assert_called_once_with(("q", "", "", ""), (("new",),))
        stale_build.assert_not_called()

    def test_full_snapshot_uses_global_version_across_filter_switches(self):
        import doubao_dashboard_server as server

        snapshot = ((("global", 9),), {"doubao": []}, (set(), {}, (None, {})))
        with mock.patch.object(server, "_ANALYTICS_SNAPSHOT", snapshot), \
                mock.patch.object(server, "_analytics_data_version", return_value=(("global", 9),)), \
                mock.patch.object(server.monitor_database, "load_runs_by_model") as load:
            result = server._analytics_snapshot((("day", 2),))

        self.assertIs(result, snapshot)
        load.assert_not_called()

    def test_cross_model_brand_view_loads_only_selected_day(self):
        import doubao_dashboard_server as server

        empty_runs = {model_id: [] for model_id in server.MODEL_PLUGINS}
        filters = {"questions": ["问题 A"], "dates": ["2026-08-18"]}
        with mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server.monitor_database, "load_runs_by_model", return_value=empty_runs) as load, \
                mock.patch.object(server.monitor_database, "analytics_filter_options", return_value=filters), \
                mock.patch.object(server, "prepare_analytics", return_value=(set(), {}, (None, {}))), \
                mock.patch.object(server, "_build_unified_analytics_from_snapshot", return_value={"models": []}):
            server._build_unified_analytics(
                ("问题 A", "2026-08-18", "", "brands"), (("version",),)
            )

        load.assert_called_once_with(
            day_from="2026-08-18", day_to="2026-08-18", question="问题 A"
        )

    def test_single_model_brand_view_loads_selected_day_for_fast_first_paint(self):
        import doubao_dashboard_server as server

        empty_runs = {model_id: [] for model_id in server.MODEL_PLUGINS}
        filters = {"questions": ["问题 A"], "dates": ["2026-08-18"]}
        with mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server.monitor_database, "load_runs_by_model", return_value=empty_runs) as load, \
                mock.patch.object(server.monitor_database, "analytics_filter_options", return_value=filters), \
                mock.patch.object(server, "prepare_analytics", return_value=(set(), {}, (None, {}))), \
                mock.patch.object(server, "_build_unified_analytics_from_snapshot", return_value={"models": []}):
            server._build_unified_analytics(
                ("问题 A", "2026-08-18", "yuanbao", "brands"), (("version",),)
            )

        load.assert_called_once_with(
            day_from="2026-08-18", day_to="2026-08-18",
            question="问题 A", model="yuanbao",
        )

    def test_single_model_brand_trend_loads_fourteen_days_separately(self):
        import doubao_dashboard_server as server

        empty_runs = {model_id: [] for model_id in server.MODEL_PLUGINS}
        filters = {"questions": ["问题 A"], "dates": ["2026-08-18"]}
        with mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server.monitor_database, "load_runs_by_model", return_value=empty_runs) as load, \
                mock.patch.object(server.monitor_database, "analytics_filter_options", return_value=filters), \
                mock.patch.object(server, "prepare_analytics", return_value=(set(), {}, (None, {}))), \
                mock.patch.object(server, "_build_unified_analytics_from_snapshot", return_value={"models": []}):
            server._build_unified_analytics(
                ("问题 A", "2026-08-18", "yuanbao", "brand-trends"), (("version",),)
            )

        load.assert_called_once_with(
            day_from="2026-08-05", day_to="2026-08-18",
            question="问题 A", model="yuanbao",
        )

    def test_single_model_view_loads_only_selected_model(self):
        import doubao_dashboard_server as server

        empty_runs = {model_id: [] for model_id in server.MODEL_PLUGINS}
        with mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server.monitor_database, "load_runs_by_model", return_value=empty_runs) as load, \
                mock.patch.object(server.monitor_database, "analytics_filter_options", return_value={"questions": [], "dates": []}), \
                mock.patch.object(server, "prepare_analytics", return_value=(set(), {}, (None, {}))), \
                mock.patch.object(server, "_build_unified_analytics_from_snapshot", return_value={"models": []}):
            server._build_unified_analytics(
                ("问题 A", "2026-08-18", "yuanbao", "overview"), (("version",),)
            )

        load.assert_called_once_with(
            day="2026-08-18", question="问题 A", model="yuanbao"
        )

    def test_stale_calendar_is_rebuilt_before_returning(self):
        import doubao_dashboard_server as server

        stale = {"models": [], "questions": [], "dates": ["2026-08-10"]}
        fresh = {"models": [], "questions": [], "dates": ["2026-08-11"]}
        with mock.patch.object(server, "_ANALYTICS_CACHE", {
                ("", "", "yuanbao", ""): (("old",), stale),
            }), mock.patch.object(server, "_ANALYTICS_BUILDING", set()), \
                mock.patch.object(server, "_analytics_data_version", return_value=(("new",),)), \
                mock.patch.object(server, "_cache_missing_available_day", return_value=True), \
                mock.patch.object(server, "_build_unified_analytics", return_value=fresh) as build:
            result = server._unified_analytics({"model": ["yuanbao"]})

        self.assertIs(result, fresh)
        build.assert_called_once_with(("", "", "yuanbao", ""), (("new",),))

    def test_warm_postgresql_filter_is_returned_only_when_version_matches(self):
        import doubao_dashboard_server as server

        key = ("问题 A", "2026-08-18", "yuanbao", "overview")
        cached = {"models": [], "questions": ["问题 A"], "dates": ["2026-08-18"]}
        with mock.patch.object(server, "_ANALYTICS_CACHE", {key: ((("current",),), cached)}), \
                mock.patch.object(server, "_ANALYTICS_BUILDING", {key}), \
                mock.patch.object(server, "_cache_missing_available_day", return_value=False), \
                mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server, "_analytics_data_version", return_value=(("current",),)) as version:
            result = server._unified_analytics({
                "question": ["问题 A"], "date": ["2026-08-18"],
                "model": ["yuanbao"], "view": ["overview"],
            })

        self.assertIs(result, cached)
        version.assert_called_once_with(key)

    def test_stale_postgresql_memory_payload_returns_while_validation_runs(self):
        import doubao_dashboard_server as server

        key = ("问题 A", "2026-08-18", "yuanbao", "overview")
        cached = {"models": [], "questions": ["问题 A"], "dates": ["2026-08-18"]}
        with mock.patch.object(server, "_ANALYTICS_CACHE", {key: (("old",), cached)}), \
                mock.patch.object(server, "_cache_missing_available_day", return_value=False), \
                mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server, "_analytics_data_version", return_value=(("new",),)), \
                mock.patch.object(server, "_schedule_analytics_validation") as schedule:
            result = server._unified_analytics({
                "question": ["问题 A"], "date": ["2026-08-18"],
                "model": ["yuanbao"], "view": ["overview"],
            })

        self.assertIs(result, cached)
        schedule.assert_called_once_with(key)

    def test_recent_day_probe_respects_selected_question(self):
        import doubao_dashboard_server as server

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            path.write_text(
                json.dumps({"question": "问题 A", "finished_at": "2026-08-11T09:00:00+08:00"}, ensure_ascii=False) + "\n"
                + json.dumps({"question": "问题 B", "finished_at": "2026-08-10T09:00:00+08:00"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(server._tail_result_days(path, "问题 A"), {"2026-08-11"})
            self.assertEqual(server._tail_result_days(path, "问题 B"), {"2026-08-10"})

    def test_overview_build_skips_unrelated_heavy_sections(self):
        metadata = {"demo": {"id": "demo", "name": "Demo"}}
        runs = {"demo": [{
            "model_id": "demo", "run_id": "1", "sequence": 1,
            "question": "问题 A", "finished_at": "2026-08-11T09:00:00+08:00",
            "day": "2026-08-11", "serial": "device", "answer": "answer",
            "status": "success", "sources": [], "products": [], "brands": [],
        }]}
        model = build_analytics(metadata, runs, view="overview")["models"][0]
        self.assertTrue(model["daily"])
        self.assertEqual(model["brand_trend_daily"], [])
        self.assertEqual(model["daily_source_top"], [])
        self.assertEqual(model["recent_runs"], [])

    def test_summary_payload_omits_heavy_recent_runs(self):
        from doubao_dashboard_server import _analytics_payload_for_client
        payload = {"models": [{"id": "demo", "runs": 1, "recent_runs": [{"answer": "large"}]}]}
        summary = _analytics_payload_for_client(payload)
        audit = _analytics_payload_for_client(payload, include_runs=True)
        self.assertNotIn("recent_runs", summary["models"][0])
        self.assertIn("recent_runs", audit["models"][0])

    def test_view_payload_keeps_only_arrays_used_by_active_view(self):
        from doubao_dashboard_server import _analytics_payload_for_client
        payload = {"models": [{
            "id": "demo", "runs": 1, "daily": [{"date": "2026-08-08"}],
            "source_types": [{"name": "文章", "count": 1}],
            "product_daily": [{"date": "2026-08-08", "items": ["large"]}],
            "daily_source_top": [{"date": "2026-08-08", "top_articles": ["large"]}],
        }]}
        overview = _analytics_payload_for_client(payload, view="overview")["models"][0]

        self.assertTrue(overview["daily"])
        self.assertTrue(overview["source_types"])
        self.assertEqual(overview["product_daily"], [])
        self.assertEqual(overview["daily_source_top"], [])

    def test_all_date_overview_serves_complete_board_while_refreshing(self):
        import doubao_dashboard_server as server

        stale = {
            "filters": {"model": "", "question": "", "date": ""},
            "owned_product_daily": [
                {"date": "2026-08-24", "product": "旧今日"},
                {"date": "2026-08-23", "product": "历史保留"},
            ],
        }
        with mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server.monitor_database, "analytics_filter_options", return_value={"dates": ["2026-08-24"]}), \
                mock.patch.object(server, "_schedule_owned_product_board_refresh") as schedule, \
                mock.patch.object(server, "_OWNED_BOARD_CACHE", {}):
            result = server._with_fresh_owned_product_board(stale)

        self.assertEqual(
            [(row["date"], row["product"]) for row in result["owned_product_daily"]],
            [("2026-08-24", "旧今日")],
        )
        schedule.assert_called_once()

    def test_all_date_overview_reuses_complete_historical_board(self):
        import doubao_dashboard_server as server

        stale = {
            "filters": {"model": "", "question": "", "date": ""},
            "dates": ["2026-08-24", "2026-08-23"],
            "owned_product_daily": [
                {"date": "2026-08-24", "product": "旧今日"},
                {"date": "2026-08-23", "product": "历史保留"},
            ],
        }
        with mock.patch.object(server.monitor_database, "enabled", return_value=True), \
                mock.patch.object(server.monitor_database, "analytics_filter_options") as options, \
                mock.patch.object(server, "_schedule_owned_product_board_refresh") as schedule, \
                mock.patch.object(server, "_OWNED_BOARD_CACHE", {}):
            result = server._with_fresh_owned_product_board(stale)

        self.assertEqual(
            [(row["date"], row["product"]) for row in result["owned_product_daily"]],
            [("2026-08-24", "旧今日"), ("2026-08-23", "历史保留")],
        )
        options.assert_not_called()
        schedule.assert_called_once()

    def test_generic_runs_dedupe_same_site_and_title_but_keep_generic_titles(self):
        stats = {"runs": [{
            "run_id": "demo-1", "round": 1, "question": "Question",
            "finished_at": "2026-08-07T10:00:00+08:00", "serial": "local",
            "sources": [
                {"title": "Same useful source title", "url": "https://example.com/a"},
                {"title": "Same useful source title", "url": "https://example.com/b"},
                {"title": "bilibili.com", "url": "https://bilibili.com/video/1"},
                {"title": "bilibili.com", "url": "https://bilibili.com/video/2"},
            ],
        }]}
        runs = load_generic_runs("demo", stats)
        self.assertEqual(len(runs[0]["sources"]), 3)

    def test_top_sources_merge_same_site_title_across_different_urls(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        runs = {"demo": []}
        for sequence, url in enumerate(("https://example.com/a", "https://example.com/b"), 1):
            runs["demo"].append({
                "model_id": "demo", "run_id": f"demo-{sequence}", "sequence": sequence,
                "question": "Question", "finished_at": f"2026-08-07T10:0{sequence}:00+08:00",
                "day": "2026-08-07", "serial": "local", "answer": "answer",
                "status": "success", "brands": [], "products": [],
                "sources": [{"title": "Same useful source title", "url": url,
                             "canonical_url": url, "domain": "example.com",
                             "media": "Example", "type": "article"}],
            })
        model = build_analytics(metadata, runs)["models"][0]
        self.assertEqual(len(model["top_articles"]), 1)
        self.assertEqual(model["top_articles"][0]["count"], 2)

    def test_top_sources_never_repeat_generic_display_titles(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        sources = []
        for sequence in range(2):
            url = f"https://bilibili.com/video/{sequence}"
            sources.append({"title": "bilibili.com", "url": url, "canonical_url": url,
                            "domain": "bilibili.com", "media": "Bilibili", "type": "视频"})
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-1", "sequence": 1,
            "question": "Question", "finished_at": "2026-08-07T10:00:00+08:00",
            "day": "2026-08-07", "serial": "local", "answer": "answer",
            "status": "success", "brands": [], "products": [], "sources": sources,
        }]}
        model = build_analytics(metadata, runs)["models"][0]
        self.assertEqual(len(model["top_videos"]), 1)

    def test_truncated_audited_doubao_source_keeps_owned_label_after_enrichment(self):
        metadata = {"doubao": {"id": "doubao", "name": "豆包", "short_name": "豆", "tone": "doubao"}}
        url = "https://www.iesdouyin.com/share/video/7684167777762004270"
        runs = {"doubao": [{
            "model_id": "doubao", "run_id": "doubao-owned-video", "sequence": 1,
            "question": "造型喷雾推荐", "finished_at": "2026-09-12T10:00:00+08:00",
            "day": "2026-09-12", "serial": "local", "answer": "回答", "status": "success",
            "brands": [], "products": [],
            "sources": [{
                "title": "5款热门定型喷雾横评，施华蔻 O", "url": url,
                "canonical_url": url, "domain": "iesdouyin.com", "media": "抖音",
                "type": "视频", "own_brand": True, "owned_brands": ["科熙本"],
                "own_products": ["科熙本控油蓬松造型喷雾"],
            }],
        }]}
        model = build_analytics(metadata, runs, question="造型喷雾推荐", date="2026-09-12")["models"][0]
        source = model["top_videos"][0]
        self.assertTrue(source["own_brand"])
        self.assertEqual(source["owned_brands"], ["科熙本"])
        self.assertEqual(source["own_products"], ["科熙本控油蓬松造型喷雾"])
        self.assertEqual(source["brand_match_scope"], "标题")

    def test_source_rankings_return_up_to_twenty_five_items(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        sources = []
        for sequence in range(30):
            url = f"https://example.com/article/{sequence}"
            sources.append({"title": f"Unique source article number {sequence}", "url": url,
                            "canonical_url": url, "domain": "example.com",
                            "media": "Example", "type": "article"})
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-1", "sequence": 1,
            "question": "Question", "finished_at": "2026-08-07T10:00:00+08:00",
            "day": "2026-08-07", "serial": "local", "answer": "answer",
            "status": "success", "brands": [], "products": [], "sources": sources,
        }]}
        model = build_analytics(metadata, runs)["models"][0]
        self.assertEqual(len(model["top_articles"]), 25)

    def test_all_configured_owned_products_are_present(self):
        self.assertEqual(len(OWN_PRODUCT_RULES), 24)
        self.assertIn("姿生怡卸妆油", {rule["name"] for rule in OWN_PRODUCT_RULES})
        self.assertEqual(
            own_product_mentions("实测梵玢黑茶色染发剂，显色自然"),
            ["梵玢染发剂（含黑茶色）"],
        )
        self.assertEqual(own_product_mentions("梵玢染发剂后使用普通洗发水"), ["梵玢染发剂（含黑茶色）"])

    def test_daily_owned_product_board_distinguishes_listing_pending_and_missing_collection(self):
        metadata = {
            model_id: {"id": model_id, "name": model_id, "short_name": model_id[0], "tone": model_id}
            for model_id in ("doubao", "yuanbao", "wenxin")
        }

        def run(model_id, run_id, review_status, products, answer="推荐结果"):
            return {
                "model_id": model_id, "run_id": run_id, "sequence": 1,
                "question": "控油蓬松洗发水推荐", "finished_at": "2026-08-22T10:00:00+08:00",
                "day": "2026-08-22", "serial": "local", "answer": answer,
                "status": "success", "product_review_status": review_status,
                "brands": [], "products": products, "sources": [],
            }

        own = [{"brand": "科熙本", "product_name": "控油蓬松洗发水", "rank": 1}]
        competitor = [{"brand": "清扬", "product_name": "控油洗发水", "rank": 1}]
        runs = {
            "doubao": [
                run("doubao", "d1", "ai_verified", own),
                run("doubao", "d2", "ai_verified", competitor),
                run("doubao", "d3", "ai_pending", []),
            ],
            "yuanbao": [run("yuanbao", "y1", "ai_verified", competitor)],
            "wenxin": [],
        }
        result = build_analytics(metadata, runs, date="2026-08-22", view="overview")
        row = result["owned_product_daily"][0]
        self.assertEqual(row["product"], "科熙本控油蓬松洗发水")
        self.assertEqual(row["models"]["doubao"]["state"], "listed")
        self.assertEqual(row["models"]["doubao"]["recommendation_runs"], 1)
        self.assertEqual(row["models"]["doubao"]["pending_runs"], 1)
        self.assertEqual(row["models"]["yuanbao"]["state"], "not_listed")
        self.assertEqual(row["models"]["wenxin"]["state"], "not_collected")
        self.assertEqual(
            owned_products_for_question("控油蓬松洗发水推荐"),
            ["科熙本控油蓬松洗发水"],
        )

    def test_owned_product_summary_averages_only_days_with_eligible_data(self):
        def status(eligible, recommendations):
            return {
                "eligible_runs": eligible,
                "recommendation_runs": recommendations,
            }

        base = {
            "question": "控油蓬松洗发水推荐",
            "product": "科熙本控油蓬松洗发水",
            "brand": "科熙本",
        }
        rows = [
            {**base, "date": "2026-09-01", "models": {
                "doubao": status(1, 1), "yuanbao": status(0, 0),
            }},
            {**base, "date": "2026-09-02", "models": {
                "doubao": status(9, 0), "yuanbao": status(2, 1),
            }},
            {**base, "date": "2026-09-03", "models": {
                "doubao": status(0, 0), "yuanbao": status(0, 0),
            }},
        ]

        result = owned_product_recommendation_summary(
            rows, ["doubao", "yuanbao"], month="2026-09",
        )

        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row["observed_days"], 2)
        self.assertEqual(row["observed_model_days"], 3)
        self.assertEqual(row["average_daily_mention_rate"], 50.0)
        self.assertEqual(row["pooled_mention_rate"], 16.67)
        self.assertEqual(row["total_recommendation_runs"], 2)
        self.assertEqual(row["total_eligible_runs"], 12)
        self.assertEqual(row["models"]["doubao"]["observed_days"], 2)
        self.assertEqual(row["models"]["doubao"]["average_daily_mention_rate"], 50.0)
        self.assertEqual(row["models"]["yuanbao"]["observed_days"], 1)
        self.assertEqual(row["models"]["yuanbao"]["average_daily_mention_rate"], 50.0)

    def test_wenxin_daily_board_splits_surfaces_and_dedupes_identical_baidu_cards(self):
        base = {
            "model_id": "wenxin", "question": "控油蓬松洗发水推荐",
            "day": "2026-09-14", "status": "success",
            "body_capture_complete": True, "product_review_status": "ai_pending",
            "products": [], "brands": [], "sources": [],
        }
        runs = {"wenxin": [
            *[
                {**base, "run_id": f"b{index}", "capture_mode": "baidu_search_ai",
                 "answer": "推荐科熙本控油蓬松洗发水，适合细软塌发质。",
                 "daily_surface_target": 5}
                for index in range(5)
            ],
            {**base, "run_id": "w1", "capture_mode": "baidu_wenxin_search",
             "answer": "清扬洗发水适合日常控油。"},
        ]}
        rows = daily_owned_product_recommendations(
            runs, ["wenxin"], date="2026-09-14",
        )
        surfaces = rows[0]["models"]["wenxin"]["surfaces"]
        self.assertEqual(surfaces["baidu_card"]["raw_eligible_runs"], 5)
        self.assertEqual(surfaces["baidu_card"]["eligible_runs"], 1)
        self.assertEqual(surfaces["baidu_card"]["recommendation_runs"], 1)
        self.assertEqual(surfaces["baidu_card"]["duplicate_runs_merged"], 4)
        self.assertEqual(surfaces["wenxin_card"]["eligible_runs"], 1)
        summary = owned_product_recommendation_summary(rows, ["wenxin"], month="2026-09")
        baidu_summary = summary[0]["models"]["wenxin"]["surfaces"]["baidu_card"]
        self.assertEqual(baidu_summary["average_daily_mention_rate"], 100.0)
        self.assertEqual(baidu_summary["first_date"], "2026-09-14")
        self.assertEqual(baidu_summary["last_date"], "2026-09-14")
        self.assertEqual(baidu_summary["last_attempt_date"], "2026-09-14")

    def test_wenxin_monthly_summary_keeps_effective_date_when_latest_card_attempt_failed(self):
        base = {
            "question": "控油蓬松洗发水推荐",
            "product": "科熙本控油蓬松洗发水",
            "brand": "科熙本",
        }
        rows = [
            {**base, "date": "2026-09-14", "models": {"wenxin": {
                "eligible_runs": 1, "recommendation_runs": 1,
                "surfaces": {"baidu_card": {
                    "surface_label": "百度卡片", "eligible_runs": 1,
                    "recommendation_runs": 1, "attempted_runs": 5,
                    "raw_eligible_runs": 5, "duplicate_runs_merged": 4,
                    "unavailable_runs": 0, "target_runs": 5,
                }},
            }}},
            {**base, "date": "2026-09-15", "models": {"wenxin": {
                "eligible_runs": 0, "recommendation_runs": 0,
                "surfaces": {"baidu_card": {
                    "surface_label": "百度卡片", "eligible_runs": 0,
                    "recommendation_runs": 0, "attempted_runs": 5,
                    "raw_eligible_runs": 0, "duplicate_runs_merged": 0,
                    "unavailable_runs": 0, "failed_runs": 5,
                    "target_runs": 5, "availability_note": "百度卡片抓取失败 5/5 次",
                }},
            }}},
        ]

        summary = owned_product_recommendation_summary(rows, ["wenxin"], month="2026-09")
        baidu = summary[0]["models"]["wenxin"]["surfaces"]["baidu_card"]
        self.assertEqual(baidu["observed_days"], 1)
        self.assertEqual(baidu["first_date"], "2026-09-14")
        self.assertEqual(baidu["last_date"], "2026-09-14")
        self.assertEqual(baidu["last_attempt_date"], "2026-09-15")

    def test_wenxin_daily_board_explains_missing_baidu_card(self):
        runs = {"wenxin": [{
            "model_id": "wenxin", "run_id": f"no-card-{index}",
            "question": "控油蓬松洗发水推荐", "day": "2026-09-14",
            "status": "not_available", "body_capture_complete": False,
            "capture_mode": "baidu_search_ai", "daily_surface_target": 5,
            "answer": "", "products": [], "brands": [], "sources": [],
        } for index in range(5)] + [{
            "model_id": "wenxin", "run_id": "wenxin-card",
            "question": "控油蓬松洗发水推荐", "day": "2026-09-14",
            "status": "success", "body_capture_complete": True,
            "capture_mode": "baidu_wenxin_search", "answer": "清扬控油洗发水。",
            "products": [], "brands": [], "sources": [],
        }]}
        rows = daily_owned_product_recommendations(runs, ["wenxin"], date="2026-09-14")
        baidu = rows[0]["models"]["wenxin"]["surfaces"]["baidu_card"]
        self.assertEqual(baidu["eligible_runs"], 0)
        self.assertEqual(baidu["attempted_runs"], 5)
        self.assertEqual(baidu["availability_note"], "百度搜索未出现 AI 卡片")

    def test_wenxin_daily_board_reports_failed_baidu_card_captures(self):
        runs = {"wenxin": [{
            "model_id": "wenxin", "run_id": f"capture-failed-{index}",
            "question": "控油蓬松洗发水推荐", "day": "2026-09-14",
            "status": "capture_failed", "body_capture_complete": False,
            "capture_mode": "baidu_search_ai", "daily_surface_target": 5,
            "answer": "", "products": [], "brands": [], "sources": [],
        } for index in range(5)]}
        rows = daily_owned_product_recommendations(runs, ["wenxin"], date="2026-09-14")
        baidu = rows[0]["models"]["wenxin"]["surfaces"]["baidu_card"]
        self.assertEqual(baidu["attempted_runs"], 5)
        self.assertEqual(baidu["failed_runs"], 5)
        self.assertEqual(baidu["availability_note"], "百度卡片抓取失败 5/5 次")

    def test_daily_owned_product_board_uses_explicit_body_evidence_while_review_is_pending(self):
        metadata = {
            model_id: {"id": model_id, "name": model_id, "short_name": model_id[0], "tone": model_id}
            for model_id in ("yuanbao", "wenxin")
        }
        runs = {
            "yuanbao": [{
                "model_id": "yuanbao", "run_id": "y1", "sequence": 1,
                "question": "控油蓬松洗发水推荐", "finished_at": "2026-08-22T10:00:00+08:00",
                "day": "2026-08-22", "serial": "local",
                "answer": "细软塌发质首选：科熙本控油蓬松洗发水，清爽不拔干。",
                "status": "success", "product_review_status": "ai_pending",
                "brands": [], "products": [], "sources": [],
            }],
            "wenxin": [{
                "model_id": "wenxin", "run_id": "w1", "sequence": 1,
                "question": "控油蓬松洗发水推荐", "finished_at": "2026-08-22T10:00:00+08:00",
                "day": "2026-08-22", "serial": "local",
                "answer": "清扬控油洗发水可作为日常选择。",
                "status": "success", "product_review_status": "ai_pending",
                "brands": [], "products": [], "sources": [],
            }],
        }
        result = build_analytics(metadata, runs, date="2026-08-22", view="overview")
        row = result["owned_product_daily"][0]
        yuanbao = row["models"]["yuanbao"]
        self.assertEqual(yuanbao["state"], "listed")
        self.assertEqual(yuanbao["recommendation_runs"], 1)
        self.assertEqual(yuanbao["body_match_runs"], 1)
        self.assertEqual(yuanbao["pending_runs"], 1)
        self.assertEqual(row["models"]["wenxin"]["state"], "pending")

    def test_owned_product_board_excludes_failed_or_incomplete_body_runs(self):
        metadata = {"doubao": {"id": "doubao", "name": "豆包", "short_name": "豆", "tone": "doubao"}}
        base = {
            "model_id": "doubao", "sequence": 1,
            "question": "防脱洗发水推荐", "finished_at": "2026-08-24T10:00:00+08:00",
            "day": "2026-08-24", "serial": "local",
            "answer": "首选道和小绿瓶，适合油头。", "product_review_status": "ai_pending",
            "brands": [], "products": [], "sources": [],
        }
        runs = {"doubao": [
            {**base, "run_id": "failed", "status": "failed"},
            {**base, "run_id": "partial", "status": "success", "body_capture_complete": False},
        ]}
        result = build_analytics(metadata, runs, date="2026-08-24", view="overview")
        self.assertEqual(result["owned_product_daily"], [])

    def test_reviewed_products_reconcile_explicit_owned_body_mentions(self):
        products = merge_explicit_owned_products(
            "二、温和植萃\n1. 道和时尚小红瓶（性价比首选）\n"
            "2. 雅雾防脱精华液\n相关视频\n#梵玢焕活精华液",
            "防脱精华液推荐",
            [{"brand_name": "雅雾", "product_name": "雅雾 防脱精华液", "rank": 1}],
        )
        self.assertEqual(
            [item["product_name"] for item in products],
            ["道和小红瓶", "雅雾 防脱精华液"],
        )
        self.assertTrue(products[0]["owned_product_reconciled"])

    def test_reviewed_products_replace_generic_same_owned_brand_without_duplicate(self):
        products = merge_explicit_owned_products(
            "推荐道和小红瓶，清爽不油。", "防脱精华液推荐",
            [{"brand_name": "道和", "product_name": "道和 防脱精华", "rank": 1}],
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["product_name"], "道和小红瓶")

    def test_owned_product_reconciliation_does_not_join_unrelated_lines(self):
        products = merge_explicit_owned_products(
            "一、日常高保湿沐浴油\n全能修护：梵玢护发精油",
            "沐浴精油推荐",
            [],
        )
        self.assertEqual(products, [])

    def test_owned_bath_oil_alias_keeps_literal_evidence(self):
        products = merge_explicit_owned_products(
            "全能修护：梵玢 FBCY 沐浴精华油",
            "沐浴精油推荐",
            [],
        )
        self.assertEqual([item["product_name"] for item in products], ["梵玢沐浴油"])
        self.assertEqual(products[0]["evidence"], "全能修护：梵玢 FBCY 沐浴精华油")

    def test_owned_nickname_counts_as_numbered_product_block(self):
        import save_doubao_refs as saver

        answer = (
            "二、温和植萃\n1. 道和时尚小红瓶（性价比首选）\n"
            "成分：侧柏叶和何首乌。\n2. 雅雾防脱精华液\n成分：侧柏叶。"
        )
        self.assertEqual(saver.numbered_product_block_count(answer), 2)

    def test_owned_product_body_evidence_rejects_negative_or_unrelated_mentions(self):
        product = "科熙本控油蓬松洗发水"
        self.assertEqual(
            owned_product_recommendations("这次不推荐科熙本控油蓬松洗发水。", [product]),
            [],
        )
        self.assertEqual(
            owned_product_recommendations("避雷：科熙本控油蓬松洗发水。", [product]),
            [],
        )
        self.assertEqual(
            owned_product_recommendations("首选科熙本控油蓬松洗发水，清爽蓬松。", [product]),
            [product],
        )
        self.assertEqual(
            owned_product_recommendations("推荐梵玢祛痘精华液。", [product]),
            [],
        )

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
        self.assertEqual(article["content_analysis_status"], "complete")
        self.assertTrue(article["body_analysis_ready"])
        self.assertEqual(article["own_products"], ["科熙本鱼子酱修护柔顺护发素"])
        self.assertEqual(article["brand_match_scope"], "正文")
        self.assertFalse(video["own_brand"])
        self.assertEqual(video["content_analysis_status"], "title_only")

    def test_unarchived_article_is_pending_not_confirmed_negative(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        url = "https://example.com/new-article"
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-1", "sequence": 1,
            "question": "Question", "finished_at": "2026-08-11T10:00:00+08:00",
            "day": "2026-08-11", "serial": "local", "answer": "answer",
            "status": "success", "brands": [], "products": [],
            "sources": [{"title": "New article", "url": url, "canonical_url": url,
                         "domain": "example.com", "media": "Example", "type": "文章"}],
        }]}
        with mock.patch("monitor_core.analytics._content_index", return_value={}), \
                mock.patch("monitor_core.analytics.CONTENT_INDEX_PATH") as path:
            path.stat.return_value.st_mtime_ns = 11
            source = build_analytics(metadata, runs)["models"][0]["recent_runs"][0]["sources"][0]
        self.assertEqual(source["content_analysis_status"], "pending")
        self.assertFalse(source["body_analysis_ready"])

    def test_brand_only_mention_is_marked_as_owned_brand_without_product(self):
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
            self.assertEqual(source["own_products"], [])
            self.assertEqual(source["owned_brands"], ["梵玢 FBCY"])
            self.assertIn("梵玢 FBCY", source["body_brand_mentions"])
            self.assertEqual(source["brand_match_scope"], "正文")

    def test_related_content_brand_only_mention_is_an_owned_brand(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        article_url = "https://baijiahao.baidu.com/s?id=example"
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-1", "sequence": 1,
            "question": "洗发水推荐", "finished_at": "2026-08-05T10:00:00+08:00",
            "day": "2026-08-05", "serial": "local", "answer": "answer",
            "status": "success", "brands": [], "products": [],
            "sources": [{"title": "什么洗发水好用？排行榜十款产品揭秘", "url": article_url,
                         "canonical_url": article_url, "domain": "baijiahao.baidu.com",
                         "media": "百度", "type": "文章"}],
        }]}
        index = {"entries": {article_url: {
            "status": "ok", "extraction_quality": "medium",
            "brand_mentions": ["科熙本"], "owned_brand_mentions": ["科熙本"],
            "own_product_mentions": [], "own_product_schema_version": OWN_PRODUCT_SCHEMA_VERSION,
        }}}
        with mock.patch("monitor_core.analytics._content_index", return_value=index), \
                mock.patch("monitor_core.analytics.CONTENT_INDEX_PATH") as path:
            path.stat.return_value.st_mtime_ns = 3
            source = build_analytics(metadata, runs)["models"][0]["recent_runs"][0]["sources"][0]
        self.assertIn("科熙本", source["body_brand_mentions"])
        self.assertTrue(source["own_brand"])
        self.assertEqual(source["own_products"], [])
        self.assertEqual(source["owned_brands"], ["科熙本"])

    def test_brand_daily_drops_non_brand_entities_and_merges_aliases(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-brand-cleanup", "sequence": 1,
            "question": "美白面霜推荐", "finished_at": "2026-08-18T10:00:00+08:00",
            "day": "2026-08-18", "serial": "local",
            "answer": "兰蔻极光面霜含积雪草；OLAY玉兰油面霜",
            "status": "success",
            "brands": ["兰蔻", "极光", "积雪草", "OLAY", "玉兰油", "贵妇", "郝邵文"],
            "products": [
                {"brand": "兰蔻", "product_name": "兰蔻极光面霜", "rank": 1},
                {"brand": "极光", "product_name": "极光面霜", "rank": 2},
                {"brand": "OLAY", "product_name": "OLAY玉兰油面霜", "rank": 3},
            ],
            "sources": [],
        }]}
        model = build_analytics(metadata, runs, view="brands")["models"][0]
        self.assertEqual(
            [item["name"] for item in model["brand_daily"][0]["items"]],
            ["OLAY 玉兰油", "兰蔻"],
        )
        self.assertEqual(
            {item["name"] for item in model["product_daily"][0]["items"]},
            {"OLAY 玉兰油 面霜", "兰蔻 极光面霜"},
        )

    def test_brand_daily_ignores_trailing_related_content_titles(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        runs = {"demo": [{
            "model_id": "demo", "run_id": "demo-related-content", "sequence": 1,
            "question": "护发精油推荐", "finished_at": "2026-08-18T10:00:00+08:00",
            "day": "2026-08-18", "serial": "local",
            "answer": "推荐欧莱雅护发精油。\n相关视频\n方松丹护发精油 #东方宝石 #郝邵文推荐",
            "status": "success", "brands": ["欧莱雅", "方松丹", "东方宝石", "郝邵文"],
            "products": [{"brand": "欧莱雅", "product_name": "护发精油", "rank": 1}],
            "sources": [],
        }]}
        model = build_analytics(metadata, runs, view="brands")["models"][0]
        self.assertEqual(
            [item["name"] for item in model["brand_daily"][0]["items"]],
            ["欧莱雅"],
        )

    def test_recommendation_body_removes_doubao_related_video_drawer(self):
        answer = (
            "推荐花王泡沫染。\n选购前先做皮试。\n相关视频\n"
            "FOW 植萃染发膏 #染发剂推荐"
        )
        self.assertEqual(
            recommendation_body(answer),
            "推荐花王泡沫染。\n选购前先做皮试。",
        )

    def test_recommendation_body_keeps_related_words_inside_prose(self):
        self.assertEqual(
            recommendation_body("相关推荐很多，但我仍推荐花王泡沫染。"),
            "相关推荐很多，但我仍推荐花王泡沫染。",
        )

    def test_recommendation_body_never_falls_back_to_source_only_fragment(self):
        self.assertEqual(
            recommendation_body("相关视频\n梵玢染发剂 #染发"),
            "",
        )

    def test_canonical_brand_groups_seed_scoped_text_matching(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        runs = {"demo": [{
            "model_id": "demo", "run_id": "scoped-brands", "sequence": 1,
            "question": "控油蓬松洗发水推荐", "finished_at": "2026-08-22T10:00:00+08:00",
            "day": "2026-08-22", "serial": "local",
            "answer": "推荐欧莱雅、滋源和广州白云山敬修堂洗发水。",
            "status": "success", "product_review_status": "ai_verified",
            "brands": [], "products": [], "sources": [],
        }]}
        model = build_analytics(metadata, runs, view="brands")["models"][0]
        self.assertEqual(
            [item["name"] for item in model["brand_daily"][0]["items"]],
            ["欧莱雅", "滋源", "白云山"],
        )

    def test_pending_product_review_keeps_deterministic_brand_table_evidence(self):
        metadata = {"demo": {"id": "demo", "name": "Demo", "short_name": "D", "tone": "demo"}}
        runs = {"demo": [{
            "model_id": "demo", "run_id": "pending-brand", "sequence": 1,
            "question": "控油蓬松洗发水推荐", "finished_at": "2026-08-22T10:00:00+08:00",
            "day": "2026-08-22", "serial": "local", "answer": "推荐欧莱雅洗发水",
            "status": "success", "product_review_status": "ai_pending",
            "brands": [], "products": [], "sources": [],
        }]}
        model = build_analytics(metadata, runs, view="brands")["models"][0]
        self.assertEqual(
            [(item["name"], item["mentions"], item["mention_rate"])
             for item in model["brand_daily"][0]["items"]],
            [("欧莱雅", 1, 100.0)],
        )
        self.assertEqual(model["product_daily"][0]["runs"], 0)
        self.assertEqual(model["product_daily"][0]["pending_runs"], 1)
        self.assertEqual(model["analysis_pending_runs"], 1)

    def test_historical_seed_brands_match_scoped_pending_answers(self):
        metadata = {"wenxin": {"id": "wenxin", "name": "文心", "short_name": "文", "tone": "wenxin"}}
        runs = {"wenxin": [{
            "model_id": "wenxin", "run_id": "pending-bath-oil", "sequence": 1,
            "question": "沐浴精油推荐", "finished_at": "2026-09-09T10:00:00+08:00",
            "day": "2026-09-09", "serial": "remote",
            "answer": "敏感肌可以选择黛馥莉，也可以试试 nyny 和阿芙。",
            "status": "success", "product_review_status": "ai_pending",
            "brands": [], "products": [], "sources": [],
        }]}
        prepared = prepare_analytics(
            runs, seed_brands={"黛馥莉", "nyny", "阿芙"},
        )

        model = build_analytics(
            metadata, runs, question="沐浴精油推荐", date="2026-09-09",
            view="brands", prepared=prepared,
        )["models"][0]

        self.assertEqual(
            [item["name"] for item in model["brand_daily"][0]["items"]],
            ["nyny", "阿芙", "黛馥莉"],
        )
        self.assertEqual(model["product_daily"][0]["runs"], 0)
        self.assertEqual(model["analysis_pending_runs"], 1)

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

    def test_known_brand_prefix_drops_product_series_leakage(self):
        self.assertEqual(
            canonical_brand_name("欧舒丹甜扁桃紧致"), "L'OCCITANE 欧舒丹",
        )
        self.assertEqual(canonical_brand_name("选花王"), "花王")
        self.assertEqual(canonical_brand_name("选REVITALASH"), "REVITALASH")
        self.assertEqual(canonical_brand_name("达霏欣5"), "达霏欣")
        self.assertEqual(canonical_brand_name("潘婷三分钟奇迹"), "潘婷")
        self.assertEqual(canonical_brand_name("德国小甘菊"), "小甘菊")
        self.assertEqual(canonical_brand_name("奥斯曼乌斯曼草"), "奥斯曼")
        self.assertEqual(canonical_brand_name("焕颜计小白罐"), "焕颜计")
        self.assertEqual(canonical_brand_name("B5"), "")
        self.assertEqual(canonical_brand_name("大众"), "")
        self.assertEqual(canonical_brand_name("产品"), "")
        self.assertEqual(canonical_brand_name("干皮首选"), "")
        self.assertEqual(canonical_brand_name("无眉"), "")
        self.assertEqual(canonical_brand_name("粗硬沙发首选"), "")
        self.assertEqual(canonical_brand_name("日常防晒可直接洗净"), "")
        self.assertEqual(canonical_brand_name("选先生驾到或施华蔻"), "")
        self.assertEqual(canonical_brand_name("科熙本控油蓬松造型喷雾"), "科熙本")
        self.assertEqual(canonical_brand_name("极方防脱控油"), "极方")
        self.assertEqual(canonical_brand_name("可复美胶原舒舒贴"), "可复美")
        self.assertEqual(canonical_brand_name("蔓迪米诺地尔泡沫剂"), "蔓迪")
        self.assertEqual(canonical_brand_name("SK-II"), "SK-II")
        self.assertEqual(canonical_brand_name("3CE"), "3CE")

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
        self.assertEqual(set(plugins), {"deepseek", "wenxin", "yuanbao"})
        self.assertTrue(plugins["deepseek"].ingest_only)
        self.assertFalse(plugins["deepseek"].supports_control)
        self.assertIn("collectors", str(plugins["deepseek"].results))

    def test_all_date_single_model_dashboard_uses_scoped_postgresql_load(self):
        import doubao_dashboard_server as server
        with mock.patch.object(server.monitor_database, "enabled", return_value=True):
            self.assertTrue(server._uses_scoped_database_load(("", "", "quark", "overview")))
            self.assertTrue(server._uses_scoped_database_load(("", "", "", "overview")))

    def test_all_date_source_dashboard_uses_bounded_database_load(self):
        import doubao_dashboard_server as server
        with mock.patch.object(server.monitor_database, "enabled", return_value=True):
            self.assertTrue(server._uses_scoped_database_load(("", "", "", "sources")))

    def test_source_summary_overrides_recent_detail_kpis(self):
        import doubao_dashboard_server as server

        result = {
            "models": [{"id": "yuanbao", "runs": 7, "sources": 20,
                        "unique_sources": 12, "question_count": 1,
                        "device_count": 1, "analysis_ready_runs": 7,
                        "analysis_pending_runs": 0, "owned_source_count": 2,
                        "branded_source_count": 3}],
            "questions": [], "dates": [],
        }
        summary = {"yuanbao": {
            "runs": 9000, "sources": 180000, "unique_sources": 12000,
            "question_count": 15, "device_count": 2,
            "analysis_ready_runs": 8970, "analysis_pending_runs": 30,
            "owned_source_count": 2500, "branded_source_count": 6000,
        }}
        detail_scope = {"kind": "latest_day", "dates": ["2026-08-25"],
                        "date_from": "2026-08-25", "date_to": "2026-08-25"}
        with mock.patch.object(server, "build_analytics", return_value=result), \
                mock.patch.object(server, "_persist_unified_analytics"), \
                mock.patch.object(server.monitor_database, "cache_put"):
            built = server._build_unified_analytics_from_snapshot(
                ("", "", "yuanbao", "sources"),
                (("v",), {"yuanbao": []}, (set(), {}, (None, {}))),
                discard_building=False, model_summary=summary,
                detail_scope=detail_scope,
            )

        self.assertEqual(built["models"][0]["runs"], 9000)
        self.assertEqual(built["models"][0]["sources"], 180000)
        self.assertEqual(built["models"][0]["unique_sources"], 12000)
        self.assertEqual(built["detail_scope"], detail_scope)

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
        brand, product, _rank = _product_fields({
            "brand_name": "卡诗 > 欧莱雅小红瓶 > 爱茉莉",
            "product_name": "精油",
        })
        self.assertEqual((brand, product), ("", ""))
        brand, product, _rank = _product_fields({
            "brand_name": "尤岚希",
            "product_name": "或 寒慕",
        })
        self.assertEqual((brand, product), ("尤岚希", ""))
        brand, product, _rank = _product_fields({
            "brand_name": "依思佩尔",
            "product_name": "EASPEER) 眉毛增长液",
        })
        self.assertEqual((brand, product), ("依思佩尔", "眉毛增长液"))
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

    def test_records_dashboard_accepts_database_normalized_answer_and_sources(self):
        from monitor_core.jsonl_dashboard import build_records_dashboard
        payload = build_records_dashboard("wenxin", [{
            "model_id": "wenxin", "status": "success", "run_id": "wenxin-db-1",
            "sequence": 12, "question": "面膜推荐",
            "answer": "这是一段来自数据库的完整面膜推荐正文，包含足够的信息用于质量校验。",
            "finished_at": "2026-09-04T10:00:00+08:00",
            "sources": [{"url": "https://example.com/mask", "title": "面膜实测信源"}],
        }])
        self.assertEqual(payload["successful_runs"], 1)
        self.assertEqual(payload["total_sources"], 1)
        self.assertEqual(payload["runs"][0]["run_id"], "wenxin-db-1")

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
        self.assertEqual(len(model["owned_sources"]), 1)
        self.assertEqual(model["owned_sources"][0]["canonical_url"], "https://bilibili.com/v/owned")
        self.assertEqual(model["source_brand_daily"][0]["owned_source_rate"], 100.0)

    def test_common_owned_links_require_all_three_models_and_count_each_run(self):
        common = {
            "title": "自有洗发水实测",
            "url": "https://example.com/owned?a=1",
            "canonical_url": "https://example.com/owned",
            "media": "示例网",
            "type": "文章",
            "own_brand": True,
            "owned_brands": ["自有品牌"],
            "own_products": ["控油蓬松洗发水"],
        }
        two_models_only = {
            **common,
            "title": "仅两个模型提取",
            "url": "https://example.com/two",
            "canonical_url": "https://example.com/two",
        }

        def run(model, number, sources, day="2026-08-14"):
            return {
                "model_id": model,
                "run_id": f"{model}-{number}",
                "question": "推荐一款控油蓬松洗发水",
                "day": day,
                "sources": sources,
            }

        runs = {
            "doubao": [run("doubao", 1, [common, common]), run("doubao", 2, [common, two_models_only])],
            "yuanbao": [run("yuanbao", 1, [common, two_models_only])],
            "wenxin": [run("wenxin", 1, [common])],
        }
        rows = common_owned_source_links(runs, date="2026-08-14")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["canonical_url"], "https://example.com/owned")
        self.assertEqual(rows[0]["model_counts"], {"doubao": 2, "yuanbao": 1, "wenxin": 1})
        self.assertEqual(rows[0]["total_count"], 4)
        two_model_rows = common_owned_source_links(
            runs, date="2026-08-14", exact_models=2
        )
        self.assertEqual(len(two_model_rows), 1)
        self.assertEqual(two_model_rows[0]["canonical_url"], "https://example.com/two")
        self.assertEqual(two_model_rows[0]["matched_models"], ["doubao", "yuanbao"])

    def test_all_competitors_groups_multiple_brand_hits_as_one_article(self):
        source = {
            "title": "控油蓬松洗发水横评",
            "url": "https://g.pconline.com.cn/article/123?utm_source=test",
            "canonical_url": "https://g.pconline.com.cn/article/123",
            "media": "太平洋科技",
            "type": "文章",
            "body_analysis_ready": True,
            "body_brand_mentions": ["沙宣", "欧莱雅", "生活家"],
            "owned_brands": [],
        }

        def run(model, number):
            return {
                "model_id": model,
                "run_id": f"{model}-{number}",
                "question": "控油蓬松洗发水推荐",
                "day": "2026-08-22",
                "sources": [source],
            }

        runs = {
            "doubao": [run("doubao", 1), run("doubao", 2)],
            "yuanbao": [run("yuanbao", 1)],
            "wenxin": [],
        }
        rows = common_all_competitor_source_links(
            runs,
            date="2026-08-22",
            exact_models=2,
            eligible_brands={"沙宣", "欧莱雅", "生活家"},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["competitor_brands"], ["欧莱雅", "沙宣", "生活家"])
        self.assertEqual(rows[0]["model_counts"], {"doubao": 2, "yuanbao": 1, "wenxin": 0})
        self.assertEqual(rows[0]["total_count"], 3)


class KeywordAnalyticsTests(unittest.TestCase):
    def test_keyword_counts_drops_equally_frequent_subterm(self):
        with mock.patch(
            "monitor_core.analytics._title_terms",
            side_effect=[("护发", "护发精油"), ("护发", "护发精油")],
        ):
            rows = keyword_counts(("a", "b"))
        self.assertEqual(rows, [{"term": "护发精油", "count": 2}])

    def test_keyword_counts_keeps_more_frequent_subterm(self):
        with mock.patch(
            "monitor_core.analytics._title_terms",
            side_effect=[("护发", "护发精油"), ("护发", "护发精油"), ("护发",)],
        ):
            rows = keyword_counts(("a", "b", "c"))
        self.assertEqual(rows[0], {"term": "护发", "count": 3})


class PluginCommandTests(unittest.TestCase):
    def test_yuanbao_command_receives_per_question_rounds_and_mode(self):
        plugin = discover_plugins()["yuanbao"]
        command, _ = plugin.command({"rounds": 3, "question_mode": "sequential"})
        self.assertIn("--rounds-per-question", command)
        self.assertEqual(command[command.index("--rounds-per-question") + 1], "3")
        # The collector discovers every running MEmu instance itself.  Passing
        # one fixed serial here would silently disable multi-instance capture.
        self.assertNotIn("--serial", command)
        self.assertEqual(command[command.index("--mode") + 1], "sequential")
        self.assertEqual(command[command.index("--max-retries") + 1], "0")
        self.assertEqual(command[command.index("--retry-wait") + 1], "90")
        self.assertEqual(Path(command[command.index("--results") + 1]), plugin.collector_results)
        self.assertNotEqual(plugin.collector_results, plugin.results)

    def test_deepseek_plugin_is_ingest_only(self):
        plugin = discover_plugins()["deepseek"]
        self.assertTrue(plugin.ingest_only)
        self.assertFalse(plugin.supports_control)
        with self.assertRaisesRegex(RuntimeError, "Chrome 扩展"):
            plugin.command({"rounds": 2, "question_mode": "interleaved"})

    def test_wenxin_command_uses_separate_collector_results(self):
        plugin = discover_plugins()["wenxin"]
        command, _ = plugin.command({
            "rounds": 2,
            "question_mode": "interleaved",
            "restart_completed": True,
        })
        self.assertEqual(Path(command[command.index("--results") + 1]), plugin.collector_results)
        self.assertNotEqual(plugin.collector_results, plugin.results)
        self.assertEqual(command[command.index("--baidu-capture-retries") + 1], "5")
        self.assertEqual(command[command.index("--baidu-card-daily-rounds") + 1], "5")
        self.assertIn("--restart-completed", command)

    def test_wenxin_stats_reads_postgresql_runs_when_database_is_enabled(self):
        plugin = discover_plugins()["wenxin"]
        database_run = {
            "model_id": "wenxin", "run_id": "db-wenxin-1", "sequence": 1,
            "status": "success", "question": "面膜推荐",
            "answer": "这是一段来自数据库的完整面膜推荐正文，包含足够的信息用于质量校验。",
            "finished_at": "2026-09-04T10:00:00+08:00",
            "sources": [{"url": "https://example.com/mask", "title": "面膜实测信源"}],
        }
        with mock.patch("monitor_core.database.enabled", return_value=True), \
                mock.patch("monitor_core.database.load_runs_by_model", return_value={"wenxin": [database_run]}):
            stats = plugin.stats()
        self.assertEqual(stats["successful_runs"], 1)
        self.assertEqual(stats["total_sources"], 1)

    def test_yuanbao_command_uses_a_humanized_random_interval(self):
        command, _ = discover_plugins()["yuanbao"].command(
            {"rounds": 2, "question_mode": "interleaved"}
        )
        self.assertEqual(command[command.index("--wait") + 1], "10")
        self.assertEqual(command[command.index("--random-wait") + 1], "20")


if __name__ == "__main__":
    unittest.main()
