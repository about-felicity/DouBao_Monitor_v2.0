import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from doubao_mumu_controller import doubao_lan_client as client
from doubao_mumu_controller import doubao_lan_receiver as receiver


class FakeResponse:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.value).encode("utf-8")


class FakeOpener:
    def __init__(self, value: dict):
        self.value = value

    def open(self, _request, timeout):
        if timeout > 5:
            raise AssertionError("upload timeout must stay short")
        return FakeResponse(self.value)


class LanSyncTests(unittest.TestCase):
    def test_capture_enqueue_never_calls_network_flush(self) -> None:
        queued = {"enabled": True, "status": "queued", "request_id": "a" * 64}
        with (
            patch.object(client, "enqueue", return_value=(Path("queued.json"), queued)),
            patch.object(client, "ensure_sync_agent_running", return_value=True) as agent,
            patch.object(client, "flush_outbox") as flush,
        ):
            result = client.enqueue_for_background_upload({"answerText": "ok"})
        self.assertEqual(result["status"], "queued_for_background_upload")
        agent.assert_called_once_with()
        flush.assert_not_called()

    def test_ack_must_match_request_id_and_durable_status(self) -> None:
        request_id = "a" * 64
        envelope = {"request_id": request_id, "source_device": "remote", "payload": {}}
        config = {
            "enabled": True,
            "receiver_url": "http://192.168.1.25:8790",
            "token": "x" * 32,
            "upload_timeout": 20,
        }
        wrong = {"ok": True, "request_id": "b" * 64, "status": "queued"}
        with patch.object(client.urllib.request, "build_opener", return_value=FakeOpener(wrong)):
            with self.assertRaisesRegex(RuntimeError, "request_id"):
                client.post_envelope(config, envelope)

        accepted = {"ok": True, "request_id": request_id, "status": "queued"}
        with patch.object(client.urllib.request, "build_opener", return_value=FakeOpener(accepted)):
            result = client.post_envelope(config, envelope)
        self.assertEqual(result["request_id"], request_id)

    def test_failed_upload_stays_in_outbox_until_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            outbox = root / "outbox"
            sent = root / "sent"
            lock = root / "upload.lock"
            request_id = "c" * 64
            outbox.mkdir()
            envelope_path = outbox / f"{request_id}.json"
            envelope_path.write_text(
                json.dumps({"request_id": request_id, "payload": {}}),
                encoding="utf-8",
            )
            config = {"enabled": True}
            patches = (
                patch.object(client, "OUTBOX_DIR", outbox),
                patch.object(client, "SENT_DIR", sent),
                patch.object(client, "OUTBOX_LOCK", lock),
                patch.object(client, "load_config", return_value=config),
            )
            with patches[0], patches[1], patches[2], patches[3], patch.object(
                client, "post_envelope", side_effect=TimeoutError("offline")
            ):
                failed = client.flush_outbox()
            self.assertEqual(failed["failures"], 1)
            self.assertTrue(envelope_path.exists())

            ack = {"ok": True, "request_id": request_id, "status": "queued"}
            with patches[0], patches[1], patches[2], patches[3], patch.object(
                client, "post_envelope", return_value=ack
            ):
                uploaded = client.flush_outbox()
            self.assertEqual(uploaded["sent"], 1)
            self.assertFalse(envelope_path.exists())
            self.assertTrue((sent / envelope_path.name).exists())

    def test_receiver_ack_is_only_possible_after_atomic_queue_write(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inbox, done = root / "inbox", root / "done"
            request_id = "d" * 64
            envelope = {"request_id": request_id, "payload": {}}
            with (
                patch.object(receiver, "INBOX_DIR", inbox),
                patch.object(receiver, "DONE_DIR", done),
            ):
                status, duplicate = receiver.queue_envelope(envelope)
            self.assertEqual(status, "queued")
            self.assertFalse(duplicate)
            self.assertTrue((inbox / f"{request_id}.json").exists())

    def test_real_http_ack_is_fast_and_matches_durable_inbox(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inbox, done, errors = root / "inbox", root / "done", root / "errors"
            for directory in (inbox, done, errors):
                directory.mkdir()
            token = "z" * 32
            request_id = "e" * 64
            envelope = {
                "version": 1,
                "request_id": request_id,
                "source_device": "remote-test",
                "sent_at": "2026-08-03T17:00:00+08:00",
                "payload": {
                    "url": "https://www.doubao.com/chat/test",
                    "question": "测试问题",
                    "answerText": "测试回答",
                },
            }
            with (
                patch.object(receiver, "INBOX_DIR", inbox),
                patch.object(receiver, "DONE_DIR", done),
                patch.object(receiver, "ERROR_DIR", errors),
            ):
                server = receiver.ReceiverServer(
                    ("127.0.0.1", 0), receiver.ReceiverHandler, {"token": token}
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    port = server.server_address[1]
                    started = time.monotonic()
                    ack = client.post_envelope(
                        {
                            "enabled": True,
                            "receiver_url": f"http://127.0.0.1:{port}",
                            "token": token,
                            "upload_timeout": 3,
                        },
                        envelope,
                    )
                    elapsed = time.monotonic() - started
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
            self.assertLess(elapsed, 1.0)
            self.assertEqual(ack["request_id"], request_id)
            self.assertEqual(ack["status"], "queued")
            self.assertTrue((inbox / f"{request_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
