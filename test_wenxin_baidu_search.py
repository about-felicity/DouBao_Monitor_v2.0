from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import re

from monitor_core.jsonl_dashboard import build_jsonl_dashboard
from wenxin_monitor.controller import (
    BAIDU_AI_SNAPSHOT_JS,
    BAIDU_VISIBLE_SOURCES_JS,
    WenxinWebCollector,
)
from wenxin_monitor.wenxin_loop import (
    repeated_search_card,
    replace_repeated_search_card,
    reserve_unique_observation,
)


class _Response:
    def __init__(self, location: str):
        self.headers = {"Location": location}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, location: str):
        self.location = location
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.location)


class _Page:
    is_stealth = True

    def __init__(self, *, captured_citations: int = 2):
        self.calls = []
        self.captured_citations = captured_citations
        self.citation_index = -1

    def call(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}, timeout))
        return {"loaderId": f"loader-{len(self.calls)}"} if method == "Page.navigate" else {}

    def click(self, x, y):
        self.calls.append(("click", {"x": x, "y": y}, 0))

    def replace_tab(self, url):
        self.calls.append(("replace_tab", {"url": url}, 0))
        return {"old_target": "old", "new_target": "new", "url": url}

    def evaluate(self, expression, timeout=15):
        if expression == "location.href":
            return "https://www.baidu.com/"
        if expression == "String(performance.timeOrigin||0)":
            return "0"
        if expression == BAIDU_AI_SNAPSHOT_JS:
            return {
                "ok": True,
                "query": "推荐一款染发剂",
                "body": (
                    "综合近期实测口碑，推荐首迷植萃染发剂。它兼顾温和遮白与固色表现，"
                    "适合多数居家染发人群，使用前仍需按说明完成皮肤过敏测试。"
                ),
                "citationCount": 2,
                "readyState": "complete",
            }
        if "展开剩余" in expression:
            return None
        if "const rect = card.getBoundingClientRect()" in expression:
            return {"top": 0, "bottom": 1040}
        if expression.startswith("window.scrollTo"):
            return None
        point_match = re.search(r"querySelectorAll\('\.cosd-citation'\)\[(\d+)\]", expression)
        if point_match:
            self.citation_index = int(point_match.group(1))
            return {"x": 100 + self.citation_index, "y": 200}
        if expression == BAIDU_VISIBLE_SOURCES_JS:
            return {
                "sources": ([
                    {
                        "url": "http://www.baidu.com/link?url=source-token",
                        "title": "2026 年染发剂实测",
                        "media": "桂林生活网",
                    }
                ] if self.citation_index < self.captured_citations else []),
            }
        raise AssertionError(f"unexpected expression: {expression[:80]}")


class WenxinBaiduSearchTests(unittest.TestCase):
    def test_repeated_card_is_scoped_by_question_body_and_natural_day(self):
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "cards.sqlite3"
            self.assertFalse(repeated_search_card(
                "推荐一款染发剂", "推荐首迷植萃染发剂。",
                history_path=history, natural_day="2026-08-24",
            ))
            self.assertTrue(repeated_search_card(
                "推荐一款染发剂", "推荐 首迷植萃染发剂。\n",
                history_path=history, natural_day="2026-08-24",
            ))
            self.assertFalse(repeated_search_card(
                "推荐一款染发剂", "今天推荐另一款染发剂。",
                history_path=history, natural_day="2026-08-24",
            ))
            self.assertTrue(repeated_search_card(
                "推荐一款染发剂", "今天推荐另一款染发剂。",
                history_path=history, natural_day="2026-08-24",
            ))
            self.assertFalse(repeated_search_card(
                "推荐一款染发剂", "今天推荐另一款染发剂。",
                history_path=history, natural_day="2026-08-25",
            ))

    def test_parallel_tasks_atomically_detect_the_second_identical_card(self):
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "cards.sqlite3"

            def observe_card(_):
                return repeated_search_card(
                    "推荐一款控油蓬松洗发水",
                    "推荐科颜本控油蓬松洗发水。",
                    history_path=history,
                    natural_day="2026-08-24",
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(observe_card, range(2)))
            self.assertEqual(sorted(outcomes), [False, True])

    def test_final_observation_is_unique_across_card_and_wenxin_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "cards.sqlite3"
            self.assertTrue(reserve_unique_observation(
                "推荐一款眉毛增长液", "完整回答 A",
                history_path=history, natural_day="2026-08-25",
            ))
            self.assertFalse(reserve_unique_observation(
                "推荐一款眉毛增长液", "完整回答 A\n",
                history_path=history, natural_day="2026-08-25",
            ))
            self.assertTrue(reserve_unique_observation(
                "推荐一款眉毛增长液", "完整回答 B",
                history_path=history, natural_day="2026-08-25",
            ))

    def test_second_identical_search_card_closes_page_and_uses_wenxin(self):
        search_card = {
            "capture_mode": "baidu_search_ai",
            "body": "推荐首迷植萃染发剂。",
        }
        fallback = {
            "capture_mode": "baidu_wenxin_search",
            "body": "文心重新生成的完整回答。",
        }
        web = mock.Mock()
        web.reset_after_round.return_value = {"old_target": "card", "new_target": "clean"}
        web.collect_wenxin_search.return_value = fallback
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "cards.sqlite3"
            first_result, first_tab = replace_repeated_search_card(
                web, "推荐一款染发剂", search_card, 30,
                history_path=history, natural_day="2026-08-24",
            )
            second_result, second_tab = replace_repeated_search_card(
                web, "推荐一款染发剂", search_card, 30,
                history_path=history, natural_day="2026-08-24",
            )

        self.assertIs(first_result, search_card)
        self.assertIsNone(first_tab)
        self.assertIs(second_result, fallback)
        self.assertEqual(second_tab["old_target"], "card")
        web.reset_after_round.assert_called_once_with()
        web.collect_wenxin_search.assert_called_once_with("推荐一款染发剂", timeout=45)

    def test_search_collector_extracts_answer_sources_and_resolves_redirect(self):
        page = _Page()
        session = _Session("https://baijiahao.baidu.com/s?id=123456")
        collector = WenxinWebCollector(page=page, session=session)

        with mock.patch("wenxin_monitor.controller.time.sleep", return_value=None):
            result = collector.collect_search("推荐一款染发剂", timeout=10)

        navigation = page.calls[0]
        self.assertEqual(navigation[0], "Page.navigate")
        self.assertIn("wd=%E6%8E%A8%E8%8D%90%E4%B8%80%E6%AC%BE%E6%9F%93%E5%8F%91%E5%89%82", navigation[1]["url"])
        self.assertEqual(result["capture_mode"], "baidu_search_ai")
        self.assertTrue(result["page_navigation_id"].startswith("loader:loader-"))
        self.assertIn("首迷植萃染发剂", result["body"])
        self.assertEqual(result["sources"][0]["url"], "https://baijiahao.baidu.com/s?id=123456")
        self.assertEqual(result["sources"][0]["media"], "桂林生活网")
        self.assertTrue(result["source_capture_complete"])

    def test_visible_citation_without_source_uses_wenxin_fallback(self):
        collector = WenxinWebCollector(page=_Page(captured_citations=1), session=_Session("https://example.com/a"))
        fallback = {
            "capture_mode": "baidu_wenxin_search", "body": "完整兜底回答",
            "sources": [{"url": "https://example.com/source", "title": "资料"}],
            "source_capture_complete": True,
        }
        with mock.patch("wenxin_monitor.controller.time.sleep", return_value=None), \
             mock.patch.object(collector, "collect_wenxin_search", return_value=fallback) as collect_fallback:
            result = collector.collect_search("推荐一款染发剂", timeout=10)
        self.assertEqual(result["capture_mode"], "baidu_wenxin_search")
        collect_fallback.assert_called_once()

    def test_search_rejects_a_navigation_without_a_new_loader(self):
        page = _Page()
        original_call = page.call

        def missing_loader(method, params=None, timeout=15):
            if method == "Page.navigate":
                page.calls.append((method, params or {}, timeout))
                return {}
            return original_call(method, params, timeout)

        page.call = missing_loader
        collector = WenxinWebCollector(page=page, session=_Session("https://example.com/a"))
        with self.assertRaisesRegex(RuntimeError, "导航凭证"):
            collector.collect_search("推荐一款染发剂", timeout=10)

    def test_non_baidu_source_is_not_resolved_again(self):
        session = _Session("https://should-not-be-used.example/")
        collector = WenxinWebCollector(page=_Page(), session=session)
        self.assertEqual(collector._resolve_source_url("https://example.com/article"), "https://example.com/article")
        self.assertEqual(session.calls, [])

    def test_completed_round_replaces_old_tab_with_clean_baidu_tab(self):
        page = _Page()
        collector = WenxinWebCollector(page=page, session=_Session("https://example.com/a"))
        result = collector.reset_after_round()
        self.assertEqual(result["old_target"], "old")
        self.assertEqual(result["new_target"], "new")
        self.assertEqual(page.calls[-1], ("replace_tab", {"url": "https://www.baidu.com/"}, 0))

    def test_source_media_flows_into_existing_wenxin_dashboard_pipeline(self):
        record = {
            "collector_model": "wenxin",
            "status": "success",
            "round": 1,
            "question": "推荐一款染发剂",
            "reply": "综合近期实测口碑，推荐首迷植萃染发剂，适合多数居家染发人群。",
            "finished_at": "2026-08-20T15:00:00+08:00",
            "sources": [
                {
                    "url": "https://baijiahao.baidu.com/s?id=123456",
                    "title": "2026 年染发剂实测",
                    "media": "桂林生活网",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "wenxin.jsonl"
            dashboard = root / "dashboard.json"
            results.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            payload = build_jsonl_dashboard("wenxin", results, dashboard)

        self.assertEqual(payload["successful_runs"], 1)
        self.assertEqual(payload["runs"][0]["sources"][0]["media"], "桂林生活网")


if __name__ == "__main__":
    unittest.main()
