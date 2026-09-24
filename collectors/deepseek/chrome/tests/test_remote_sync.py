from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from remote_sync import RemoteSync


class UploadHandler(BaseHTTPRequestHandler):
    received: list[dict] = []

    def log_message(self, *_args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/v1/models/deepseek/results" or self.headers.get("Authorization") != "Bearer " + "x" * 32:
            self.send_response(403)
            self.end_headers()
            return
        value = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"))
        self.received.append(value)
        body = json.dumps({"ok": True, "request_id": value["request_id"], "status": "queued"}).encode("utf-8")
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RemoteSyncTest(unittest.TestCase):
    def test_durable_deepseek_upload_and_success_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            server = ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            config = Path(temporary) / "sync.json"
            config.write_text(json.dumps({
                "enabled": True,
                "receiver_url": f"http://127.0.0.1:{server.server_port}",
                "token": "x" * 32,
                "upload_timeout": 2,
            }), encoding="utf-8")
            previous = os.environ.get("DEEPSEEK_REMOTE_SYNC_CONFIG")
            os.environ["DEEPSEEK_REMOTE_SYNC_CONFIG"] = str(config)
            events: list[dict] = []
            try:
                sync = RemoteSync(Path(temporary) / "data", lambda event: events.append(event) or event)
                queued = sync.enqueue({
                    "result_id": "result-1", "prompt": "推荐一款护发精油", "reply": "正文",
                    "finished_at": "2026-08-24T16:00:00+08:00", "sources": [],
                })
                result = sync.flush()
                self.assertEqual(queued["status"], "queued")
                self.assertEqual(result["pending"], 0)
                self.assertEqual(result["sent_now"], 1)
                self.assertEqual(UploadHandler.received[-1]["model"], "deepseek")
                self.assertEqual(UploadHandler.received[-1]["record"]["collector_model"], "deepseek")
                self.assertIn("remote_sync_queued", [event["event"] for event in events])
                self.assertIn("remote_sync_success", [event["event"] for event in events])
            finally:
                if previous is None:
                    os.environ.pop("DEEPSEEK_REMOTE_SYNC_CONFIG", None)
                else:
                    os.environ["DEEPSEEK_REMOTE_SYNC_CONFIG"] = previous
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
