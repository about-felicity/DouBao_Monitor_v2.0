from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

import monitor_core.lan_result_receiver as receiver
import monitor_core.lan_result_sync as sync
import monitor_core.plugins as plugins
from monitor_core.database import capture_quarantine_reason, normalized_answer_fingerprint
from wenxin_monitor.wenxin_loop import answer_fingerprint


class LanResultReceiverTest(unittest.TestCase):
    def test_wenxin_search_card_is_classified_by_capture_completeness(self) -> None:
        complete = {
            "status": "success", "capture_mode": "baidu_search_ai",
            "answer": "完整回答", "body_capture_complete": True,
            "sources": [{"url": "https://example.com/source"}],
            "source_capture_complete": True,
        }
        self.assertEqual(capture_quarantine_reason("wenxin", complete), "")
        self.assertEqual(
            capture_quarantine_reason("wenxin", {**complete, "sources": []}),
            "wenxin_source_capture_empty",
        )

    def test_every_model_quarantines_a_success_record_without_an_answer(self) -> None:
        for model in ("doubao", "yuanbao", "wenxin", "quark"):
            with self.subTest(model=model):
                self.assertEqual(
                    capture_quarantine_reason(model, {"status": "success", "answer": "  "}),
                    "empty_answer",
                )

    def test_wenxin_duplicate_fingerprints_ignore_invisible_formatting(self) -> None:
        self.assertEqual(
            normalized_answer_fingerprint("同一 份\u200b回答\n"),
            normalized_answer_fingerprint("同一份回答"),
        )
        self.assertEqual(
            answer_fingerprint("控油蓬松洗发水推荐", "同一 份回答\n"),
            answer_fingerprint("控油蓬松洗发水推荐", "同一份回答"),
        )
    def test_sender_stamps_model_and_rejects_cross_model_record(self) -> None:
        stamped = sync.stamp_record_model("wenxin", {"question": "q"})
        self.assertEqual(stamped["collector_model"], "wenxin")
        with self.assertRaisesRegex(ValueError, "collector model mismatch"):
            sync.stamp_record_model("yuanbao", stamped)

    def test_quark_is_accepted_by_the_shared_callback_contract(self) -> None:
        record = sync.stamp_record_model("quark", {
            "status": "success",
            "question": "推荐一款染发剂",
            "reply": "这是一条来自夸克的完整产品推荐回答。",
            "sources": [{"url": "https://example.com/quark", "title": "夸克信源"}],
        })
        request_id = sync._request_id("quark", record, "quark-worker")
        envelope = {
            "model": "quark",
            "request_id": request_id,
            "source_device": "quark-worker",
            "record": record,
        }
        validated_id, validated = receiver.validate_result_envelope("quark", envelope)
        self.assertEqual(validated_id, request_id)
        self.assertEqual(validated["collector_model"], "quark")
        self.assertIn("quark", receiver.MODELS)
        self.assertIn("quark", sync.ALLOWED_MODELS)

    def test_receiver_rejects_cross_model_and_tampered_identity(self) -> None:
        record = {"collector_model": "wenxin", "question": "q"}
        device = "worker-1"
        request_id = sync._request_id("wenxin", record, device)
        envelope = {"model": "wenxin", "request_id": request_id,
                    "source_device": device, "record": record}
        validated_id, validated = receiver.validate_result_envelope("wenxin", envelope)
        self.assertEqual(validated_id, request_id)
        self.assertEqual(validated["collector_model"], "wenxin")
        with self.assertRaisesRegex(ValueError, "collector model mismatch"):
            receiver.validate_result_envelope("yuanbao", {**envelope, "model": "yuanbao"})
        with self.assertRaisesRegex(ValueError, "request identity mismatch"):
            receiver.validate_result_envelope("wenxin", {**envelope, "request_id": "f" * 64})

    def test_legacy_unstamped_result_remains_compatible_without_crossing_models(self) -> None:
        record = {"question": "legacy"}
        device = "old-worker"
        request_id = sync._request_id("wenxin", record, device)
        envelope = {"model": "wenxin", "request_id": request_id,
                    "source_device": device, "record": record}
        _, stamped = receiver.validate_result_envelope("wenxin", envelope)
        self.assertEqual(stamped["collector_model"], "wenxin")
        receiver.validate_result_envelope("wenxin", envelope)

    def test_receipt_does_not_mask_incomplete_sources(self) -> None:
        record = {"status": "success", "question": "q", "web_body": "answer",
                  "sources": [{"title": "one", "url": "https://example.com/1"}],
                  "expected_source_count": 3, "source_capture_complete": False}
        receipt = receiver.result_receipt("wenxin", "a" * 64, {"source_device": "pc"}, record)
        self.assertEqual(receipt["analysis"]["source_count"], 1)
        self.assertEqual(receipt["analysis"]["expected_source_count"], 3)
        self.assertFalse(receipt["analysis"]["source_capture_complete"])

    def test_discovery_response_is_authenticated(self) -> None:
        token = "x" * 32
        nonce = "a" * 32
        response = receiver.discovery_response(
            {"service": receiver.DISCOVERY_SERVICE, "nonce": nonce,
             "fingerprint": receiver.token_fingerprint(token)},
            {"token": token, "port": 8791},
            "192.168.1.88",
        )
        self.assertIsNotNone(response)
        self.assertEqual(sync._validated_discovery_url(response, token, nonce), "http://192.168.1.88:8791")
        response["receiver_url"] = "http://192.168.1.99:8791"
        self.assertEqual(sync._validated_discovery_url(response, token, nonce), "")

    def test_accepts_duplicate_upload_once(self) -> None:
        original_queue = receiver.QUEUE
        original_target = receiver.TARGETS["deepseek"]
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                receiver.QUEUE = root / "queue"
                receiver.TARGETS["deepseek"] = root / "deepseek_results.jsonl"
                store = mock.patch.object(receiver, "store_ingested_run", return_value={"run_id": "test", "sequence": 1})
                mocked_store = store.start()
                server = receiver.Server(("127.0.0.1", 0), {"token": "x" * 32})
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    envelope = {
                        "version": 1,
                        "model": "deepseek",
                        "request_id": "a" * 64,
                        "source_device": "test-worker",
                        "record": {"collector_model": "deepseek", "status": "success", "question": "test", "sources": []},
                    }
                    envelope["request_id"] = sync._request_id("deepseek", envelope["record"], "test-worker")
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_address[1]}/api/v1/models/deepseek/results",
                        data=json.dumps(envelope).encode("utf-8"), method="POST",
                        headers={"Authorization": "Bearer " + "x" * 32, "Content-Type": "application/json"},
                    )
                    self.assertTrue(json.loads(urllib.request.urlopen(request, timeout=5).read())["ok"])
                    self.assertTrue(json.loads(urllib.request.urlopen(request, timeout=5).read())["ok"])
                    deadline = time.time() + 5
                    done_path = receiver.QUEUE / "deepseek" / "done" / (envelope["request_id"] + ".json")
                    while time.time() < deadline and not done_path.exists():
                        time.sleep(0.05)
                    self.assertFalse(receiver.TARGETS["deepseek"].exists())
                    self.assertGreaterEqual(mocked_store.call_count, 1)
                    receipt = json.loads(done_path.read_text(encoding="utf-8"))
                    self.assertEqual(receipt["source_device"], "test-worker")
                    self.assertEqual(receipt["question"], "test")
                    self.assertTrue(receipt["analysis"]["question_present"])
                finally:
                    server.shutdown()
                    server.server_close()
                    store.stop()
        finally:
            receiver.QUEUE = original_queue
            receiver.TARGETS["deepseek"] = original_target

    def test_old_ip_discovers_new_ip_updates_config_and_uploads(self) -> None:
        original_queue = receiver.QUEUE
        original_target = receiver.TARGETS["deepseek"]
        original_root = sync.ROOT
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                receiver.QUEUE = root / "receiver_queue"
                receiver.TARGETS["deepseek"] = root / "deepseek_results.jsonl"
                sync.ROOT = root / "worker"
                token = "z" * 32
                store = mock.patch.object(receiver, "store_ingested_run", return_value={"run_id": "test", "sequence": 1})
                mocked_store = store.start()
                server = receiver.Server(("127.0.0.1", 0), {"token": token})
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    new_url = f"http://127.0.0.1:{server.server_address[1]}"
                    config_path = sync._config_path("deepseek")
                    config_path.parent.mkdir(parents=True)
                    config_path.write_text(json.dumps({"enabled": True, "token": token,
                                                       "receiver_url": "http://127.0.0.1:1",
                                                       "upload_timeout": 1}), encoding="utf-8")
                    record = {"collector_model": "deepseek", "status": "success"}
                    envelope = {"version": 1, "model": "deepseek",
                                "request_id": sync._request_id("deepseek", record, "remote-pc"),
                                "source_device": "remote-pc", "record": record}
                    config = sync._load_config("deepseek")
                    with mock.patch.object(sync, "_discover", return_value=[new_url]):
                        result = sync._post(config, envelope)
                    self.assertTrue(result["ok"])
                    updated = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(updated["receiver_url"], new_url)
                    deadline = time.time() + 5
                    done_path = receiver.QUEUE / "deepseek" / "done" / (envelope["request_id"] + ".json")
                    while time.time() < deadline and not done_path.exists():
                        time.sleep(0.05)
                    self.assertTrue(done_path.exists())
                    self.assertFalse(receiver.TARGETS["deepseek"].exists())
                    self.assertEqual(mocked_store.call_count, 1)
                finally:
                    server.shutdown()
                    server.server_close()
                    store.stop()
        finally:
            receiver.QUEUE = original_queue
            receiver.TARGETS["deepseek"] = original_target
            sync.ROOT = original_root

    def test_dashboard_fallback_is_added_for_result_receiver(self) -> None:
        self.assertEqual(
            sync._urls({"receiver_url": "http://192.168.1.233:8791"}),
            ["http://192.168.1.233:8791", "http://192.168.1.233:8765"],
        )

    def test_remote_plugin_activity_exposes_analysis_audit(self) -> None:
        original_root = plugins.ROOT
        try:
            with tempfile.TemporaryDirectory() as directory:
                plugins.ROOT = Path(directory)
                done = plugins.ROOT / "runtime" / "lan_result_receiver" / "deepseek" / "done"
                done.mkdir(parents=True)
                (done / ("c" * 64 + ".json")).write_text(json.dumps({
                    "request_id": "c" * 64,
                    "source_device": "worker-1",
                    "received_at": "2026-08-06T12:00:00+00:00",
                    "question": "推荐一款睫毛增长液",
                    "rows_written": 1,
                    "analysis": {
                        "question_present": True,
                        "answer_present": True,
                        "answer_length": 128,
                        "source_count": 1,
                        "missing_source_links": 0,
                        "missing_source_titles": 0,
                        "recommendation_question": True,
                        "product_parse_complete": True,
                        "sources": [{"title": "资料", "href": "https://example.com/a"}],
                    },
                }), encoding="utf-8")

                class RemotePlugin(plugins.ModelPlugin):
                    id = "deepseek"
                    execution = "remote"

                result = RemotePlugin().activity()
                self.assertEqual(result["queue"]["processed"], 1)
                self.assertEqual(result["events"][0]["source_device"], "worker-1")
                self.assertEqual(result["events"][0]["source_count"], 1)
                self.assertEqual(result["events"][0]["analysis_status"], "pending")
        finally:
            plugins.ROOT = original_root

    def test_pending_retry_is_not_double_counted_as_error(self) -> None:
        original_root = plugins.ROOT
        try:
            with tempfile.TemporaryDirectory() as directory:
                plugins.ROOT = Path(directory)
                root = plugins.ROOT / "runtime" / "lan_result_receiver" / "wenxin"
                request_id = "d" * 64
                envelope = {
                    "request_id": request_id,
                    "source_device": "worker-2",
                    "record": {
                        "question": "推荐一款护发精油",
                        "web_body": "回答正文",
                        "sources": [{"title": "资料", "url": "https://example.com/source"}],
                    },
                }
                (root / "inbox").mkdir(parents=True)
                (root / "errors").mkdir(parents=True)
                (root / "inbox" / f"{request_id}.json").write_text(json.dumps(envelope), encoding="utf-8")
                (root / "errors" / f"{request_id}.json").write_text(json.dumps({
                    "request_id": request_id,
                    "last_error": "RuntimeError: product analysis is pending; queued result will retry automatically",
                }), encoding="utf-8")

                class RemotePlugin(plugins.ModelPlugin):
                    id = "wenxin"
                    execution = "remote"

                result = RemotePlugin().activity()
                self.assertEqual(result["queue"], {"queued": 1, "processed": 0, "errors": 0})
                self.assertEqual(len(result["events"]), 1)
                self.assertEqual(result["events"][0]["question"], "推荐一款护发精油")
                self.assertEqual(result["events"][0]["source_count"], 1)
        finally:
            plugins.ROOT = original_root

    def test_stale_error_receipt_is_not_counted_after_processing(self) -> None:
        original_root = plugins.ROOT
        try:
            with tempfile.TemporaryDirectory() as directory:
                plugins.ROOT = Path(directory)
                root = plugins.ROOT / "runtime" / "lan_result_receiver" / "yuanbao"
                request_id = "e" * 64
                (root / "done").mkdir(parents=True)
                (root / "errors").mkdir(parents=True)
                payload = {"request_id": request_id, "question": "q", "analysis": {}}
                (root / "done" / f"{request_id}.json").write_text(json.dumps(payload), encoding="utf-8")
                (root / "errors" / f"{request_id}.json").write_text(json.dumps({
                    "request_id": request_id, "last_error": "old transient lock",
                }), encoding="utf-8")

                class RemotePlugin(plugins.ModelPlugin):
                    id = "yuanbao"
                    execution = "remote"

                result = RemotePlugin().activity()
                self.assertEqual(result["queue"]["processed"], 1)
                self.assertEqual(result["queue"]["errors"], 0)
        finally:
            plugins.ROOT = original_root

    def test_pending_product_analysis_does_not_block_result_ingestion(self) -> None:
        record = {
            "question": "推荐一款护发精油",
            "web_body": "包含完整回答正文",
            "sources": [{"title": "资料", "url": "https://example.com/source"}],
        }
        with mock.patch("save_doubao_refs.review_products_with_ai") as review:
            receiver.analyze_record_products(record)

        review.assert_not_called()
        self.assertEqual(record["product_review_status"], "ai_pending")
        self.assertEqual(record["products"], [])
        self.assertEqual(record["brands"], [])

    def test_receiver_no_longer_writes_jsonl_results(self) -> None:
        self.assertNotIn("target.open", Path(receiver.__file__).read_text(encoding="utf-8"))

    def test_server_uses_one_analysis_worker_per_model(self) -> None:
        server = receiver.Server(("127.0.0.1", 0), {"token": "x" * 32})
        try:
            self.assertEqual({worker.models for worker in server.workers}, {(model,) for model in receiver.MODELS})
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
