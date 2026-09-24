from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys


class ReceiverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        os.environ["DEEPSEEK_MONITOR_DATA_DIR"] = cls.temp.name
        source = Path(__file__).resolve().parents[1] / "local_receiver.py"
        if str(source.parent) not in sys.path:
            sys.path.insert(0, str(source.parent))
        spec = importlib.util.spec_from_file_location("deepseek_local_receiver_test", source)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.server = cls.module.make_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.temp.cleanup()

    def request(self, path: str, value: dict | None = None) -> tuple[int, dict]:
        data = None if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        return response.status, json.loads(response.read().decode("utf-8"))

    def test_health_and_deduplicated_append(self) -> None:
        _, before = self.request("/api/health")
        before_count = int(before.get("result_count") or 0)
        before_sources = int(before.get("source_count") or 0)
        before_artifacts = len(list(Path(before["result_files_path"]).rglob("*.txt")))
        row = {
            "result_id": "test-result-1",
            "prompt": "推荐一款染发剂",
            "reply": "这是测试正文。",
            "sources": [{"url": "https://example.com/a", "title": "测试信源"}],
            "source_capture_complete": True,
            "finished_at": "2026-08-24T12:00:00+08:00",
        }
        first_status, first = self.request("/api/results", row)
        second_status, second = self.request("/api/results", row)
        self.assertEqual(first_status, 201)
        self.assertTrue(first["created"])
        self.assertEqual(second_status, 200)
        self.assertFalse(second["created"])
        status, health = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["result_count"], before_count + 1)
        self.assertEqual(health["source_count"], before_sources + 1)
        self.assertTrue(Path(health["events_path"]).exists())
        self.assertTrue(Path(health["log_path"]).exists())
        artifacts = list(Path(health["result_files_path"]).rglob("*.txt"))
        self.assertEqual(len(artifacts), before_artifacts + 1)
        self.assertTrue(any("这是测试正文" in item.read_text(encoding="utf-8") for item in artifacts))

    def test_rejects_incomplete_payload(self) -> None:
        status, payload = self.request("/api/results", {"result_id": "bad"})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_accepts_explicit_failed_round(self) -> None:
        row = {
            "result_id": "failed-round", "status": "failed", "prompt": "测试失败轮",
            "reply": "", "skip_reason": "DeepSeek未生成回答", "sources": [],
            "source_capture_complete": False, "finished_at": "2026-08-24T12:04:00+08:00",
        }
        status, payload = self.request("/api/results", row)
        self.assertEqual(status, 201)
        self.assertTrue(payload["created"])

    def test_source_title_cache_replaces_domain_placeholder(self) -> None:
        first = {
            "result_id": "title-cache-good", "prompt": "标题缓存一", "reply": "正文",
            "sources": [{"url": "https://news.example.org/article/1", "title": "真实文章标题"}],
            "finished_at": "2026-08-24T12:01:00+08:00",
        }
        second = {
            "result_id": "title-cache-domain", "prompt": "标题缓存二", "reply": "正文",
            "sources": [{"url": "https://news.example.org/article/1", "title": "news.example.org"}],
            "finished_at": "2026-08-24T12:02:00+08:00",
        }
        self.assertEqual(self.request("/api/results", first)[0], 201)
        self.assertEqual(self.request("/api/results", second)[0], 201)
        status, payload = self.request("/api/results?limit=10")
        self.assertEqual(status, 200)
        row = next(item for item in payload["results"] if item["result_id"] == "title-cache-domain")
        self.assertEqual(row["sources"][0]["title"], "真实文章标题")

    def test_site_specific_title_fallback_before_save(self) -> None:
        row = {
            "result_id": "site-title-fallback", "prompt": "站点标题兜底", "reply": "正文",
            "sources": [{
                "url": "https://baike.baidu.com/item/%E6%A4%8D%E7%89%A9%E7%B2%BE%E6%B2%B9/6452885",
                "title": "baike.baidu.com",
            }],
            "finished_at": "2026-08-24T12:03:00+08:00",
        }
        self.assertEqual(self.request("/api/results", row)[0], 201)
        status, payload = self.request("/api/results?limit=20")
        self.assertEqual(status, 200)
        saved = next(item for item in payload["results"] if item["result_id"] == "site-title-fallback")
        self.assertEqual(saved["sources"][0]["title"], "植物精油_百度百科")

    def test_plugin_data_apis_and_no_web_dashboard(self) -> None:
        event_status, event = self.request(
            "/api/events",
            {"event": "capture_complete", "prompt": "测试问题", "details": {"source_count": 1}},
        )
        self.assertEqual(event_status, 201)
        self.assertEqual(event["event"], "capture_complete")
        results_status, results = self.request("/api/results?limit=10")
        events_status, events = self.request("/api/events")
        root_status, root = self.request("/")
        self.assertEqual(results_status, 200)
        self.assertEqual(events_status, 200)
        self.assertEqual(root_status, 200)
        self.assertTrue(results["results"])
        self.assertTrue(events["events"])
        self.assertIsInstance(root, dict)
        self.assertNotIn("html", root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
