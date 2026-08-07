from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remote_model_worker


class RemoteModelWorkerTests(unittest.TestCase):
    def test_preflight_validates_questions_dependencies_and_sync(self):
        class Plugin:
            def ready(self):
                return True

            def load_questions(self):
                return ["question"]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "runtime" / "remote_workers" / "wenxin_sync.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                '{"enabled":true,"model":"wenxin","receiver_url":"http://127.0.0.1:8791","token":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}',
                encoding="utf-8",
            )
            with mock.patch.object(remote_model_worker, "ROOT", root), \
                    mock.patch.object(remote_model_worker, "discover_plugins", return_value={"wenxin": Plugin()}):
                result = remote_model_worker.preflight("wenxin")
            self.assertTrue(result["ok"])
            self.assertEqual(result["questions"], 1)

    def test_sync_config_rejects_wrong_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sync.json"
            path.write_text(
                '{"enabled":true,"model":"yuanbao","receiver_url":"http://127.0.0.1:8791","token":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                remote_model_worker.validate_sync_config("wenxin", path)

    def test_worker_holds_single_collector_guard_and_runs_plugin(self):
        class Plugin:
            def prepare(self, options, progress):
                progress("ready")

            def command(self, options):
                return [sys.executable, "-c", "raise SystemExit(0)"], Path.cwd()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "runtime" / "remote_workers" / "deepseek_sync.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                '{"enabled":true,"model":"deepseek","receiver_url":"http://127.0.0.1:8791","token":"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}',
                encoding="utf-8",
            )
            with mock.patch.object(remote_model_worker, "ROOT", root), \
                    mock.patch.object(remote_model_worker, "start_result_sync") as start_sync, \
                    mock.patch.object(remote_model_worker, "discover_plugins", return_value={"deepseek": Plugin()}), \
                    mock.patch.object(sys, "argv", ["remote_model_worker.py", "--model", "deepseek", "--rounds", "2"]):
                self.assertEqual(remote_model_worker.main(), 0)
                start_sync.assert_called_once_with("deepseek")


if __name__ == "__main__":
    unittest.main()
