from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

import monitor_core.lan_result_receiver as receiver


class LanResultReceiverTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
