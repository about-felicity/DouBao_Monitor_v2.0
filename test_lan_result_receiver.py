from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

import monitor_core.lan_result_receiver as receiver
import monitor_core.lan_result_sync as sync


class LanResultReceiverTest(unittest.TestCase):
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
                        "record": {"status": "success", "question": "test", "sources": []},
                    }
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{server.server_address[1]}/api/v1/models/deepseek/results",
                        data=json.dumps(envelope).encode("utf-8"), method="POST",
                        headers={"Authorization": "Bearer " + "x" * 32, "Content-Type": "application/json"},
                    )
                    self.assertTrue(json.loads(urllib.request.urlopen(request, timeout=5).read())["ok"])
                    self.assertTrue(json.loads(urllib.request.urlopen(request, timeout=5).read())["ok"])
                    lines = receiver.TARGETS["deepseek"].read_text(encoding="utf-8").splitlines()
                    self.assertEqual(len(lines), 1)
                    self.assertEqual(json.loads(lines[0])["remote_source_device"], "test-worker")
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
                    envelope = {"version": 1, "model": "deepseek", "request_id": "b" * 64,
                                "source_device": "remote-pc", "record": {"status": "success"}}
                    config = sync._load_config("deepseek")
                    with mock.patch.object(sync, "_discover", return_value=[new_url]):
                        result = sync._post(config, envelope)
                    self.assertTrue(result["ok"])
                    updated = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertEqual(updated["receiver_url"], new_url)
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


if __name__ == "__main__":
    unittest.main()
