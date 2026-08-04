from argparse import Namespace
import unittest
from unittest.mock import Mock, patch

from doubao_mumu_controller import doubao_remote_startup as startup


class CaptureOnlyStartupTests(unittest.TestCase):
    def capture_args(self) -> Namespace:
        return Namespace(
            check_only=False,
            no_open_dashboard=False,
            panel_only=False,
            capture_only=True,
        )

    @patch.object(startup, "start_sync_agent", return_value=True)
    @patch.object(startup, "log")
    @patch.object(startup, "start_dashboard")
    @patch.object(startup, "check_main_receiver", return_value={"ok": True})
    @patch.object(startup, "environment_check", return_value={"ok": True})
    @patch.object(startup, "parse_args")
    @patch.object(startup.subprocess, "Popen")
    def test_capture_only_runs_job_without_local_dashboard(
        self, popen, parse_args, _environment, _receiver, start_dashboard, _log,
        start_sync_agent,
    ) -> None:
        parse_args.return_value = self.capture_args()
        process = Mock()
        process.wait.return_value = 0
        popen.return_value = process

        self.assertEqual(startup.main(), 0)

        start_dashboard.assert_not_called()
        start_sync_agent.assert_called_once_with()
        command = popen.call_args.args[0]
        self.assertEqual(command[1], str(startup.JOB_RUNNER))
        self.assertIn("--config", command)

    @patch.object(startup, "log")
    @patch.object(
        startup, "check_main_receiver", return_value={"ok": True, "disabled": True}
    )
    @patch.object(startup, "environment_check", return_value={"ok": True})
    @patch.object(startup, "parse_args")
    def test_capture_only_requires_pairing(
        self, parse_args, _environment, _receiver, _log
    ) -> None:
        parse_args.return_value = self.capture_args()

        with self.assertRaisesRegex(RuntimeError, "doubao_lan_pairing.json"):
            startup.main()


if __name__ == "__main__":
    unittest.main()
