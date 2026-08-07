from __future__ import annotations

import sys
import unittest
from unittest import mock

from remote_model_control_panel import account_gate_open, build_worker_command
from wenxin_monitor import controller as wenxin_controller


class RemoteModelPanelTest(unittest.TestCase):
    def test_wenxin_login_check_does_not_install_android_input_method(self):
        device = mock.Mock()
        with mock.patch.object(wenxin_controller.u2, "connect", return_value=device):
            wenxin_controller.WenxinAppController("127.0.0.1:16384")
        device.set_input_ime.assert_not_called()
        device.set_fastinput_ime.assert_not_called()

    def test_wenxin_and_yuanbao_require_successful_login_check(self):
        self.assertFalse(account_gate_open("wenxin", False))
        self.assertFalse(account_gate_open("yuanbao", False))
        self.assertTrue(account_gate_open("wenxin", True))
        self.assertTrue(account_gate_open("deepseek", False))

    def test_worker_command_is_bound_to_one_model(self) -> None:
        command = build_worker_command("deepseek", 20, "interleaved", "D:/pairing.json")
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[command.index("--model") + 1], "deepseek")
        self.assertEqual(command[command.index("--rounds") + 1], "20")
        self.assertEqual(command[command.index("--pairing") + 1], "D:/pairing.json")

    def test_worker_command_omits_empty_pairing(self) -> None:
        command = build_worker_command("yuanbao", 0, "sequential")
        self.assertNotIn("--pairing", command)
        self.assertEqual(command[command.index("--rounds") + 1], "1")


if __name__ == "__main__":
    unittest.main()
