from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remote_model_worker


class RemoteModelWorkerTests(unittest.TestCase):
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
            config.write_text("{}", encoding="utf-8")
            with mock.patch.object(remote_model_worker, "ROOT", root), \
                    mock.patch.object(remote_model_worker, "discover_plugins", return_value={"deepseek": Plugin()}), \
                    mock.patch.object(sys, "argv", ["remote_model_worker.py", "--model", "deepseek", "--rounds", "2"]):
                self.assertEqual(remote_model_worker.main(), 0)


if __name__ == "__main__":
    unittest.main()
