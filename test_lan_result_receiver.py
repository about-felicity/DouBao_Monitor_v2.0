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


class LanResultReceiverTest(unittest.TestCase):
    def test_sender_stamps_model_and_rejects_cross_model_record(self) -> None:
        stamped = sync.stamp_record_model("wenxin", {"question": "q"})
        self.assertEqual(stamped["collector_model"], "wenxin")
        with self.assertRaisesRegex(ValueError, "collector model mismatch"):
            sync.stamp_record_model("yuanbao", stamped)

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
                    while time.time() < deadline and not receiver.TARGETS["deepseek"].exists():
                        time.sleep(0.05)
                    lines = receiver.TARGETS["deepseek"].read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(lines), 1)
                    self.assertEqual(json.loads(lines[0])["remote_source_device"], "test-worker")
                    receipt = json.loads((receiver.QUEUE / "deepseek" / "done" / (envelope["request_id"] + ".json")).read_text(encoding="utf-8"))
                    self.assertEqual(receipt["source_device"], "test-worker")
                    self.assertEqual(receipt["question"], "test")
                    self.assertTrue(receipt["analysis"]["question_present"])
                finally:
                    server.shutdown()
                    server.server_close()
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
                    while time.time() < deadline and not receiver.TARGETS["deepseek"].exists():
                        time.sleep(0.05)
                    rows = receiver.TARGETS["deepseek"].read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(json.loads(rows[0])["remote_source_device"], "remote-pc")
                finally:
                    server.shutdown()
                    server.server_close()
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


if __name__ == "__main__":
    unittest.main()
