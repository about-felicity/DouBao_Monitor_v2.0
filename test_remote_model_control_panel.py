from __future__ import annotations

import sys
import unittest
from unittest import mock
from PIL import Image, ImageDraw

from remote_model_control_panel import account_gate_open, build_worker_command
from wenxin_monitor import controller as wenxin_controller


class RemoteModelPanelTest(unittest.TestCase):
    def test_wenxin_login_check_does_not_install_android_input_method(self):
        device = mock.Mock()
        with mock.patch.object(wenxin_controller.u2, "connect", return_value=device):
            wenxin_controller.WenxinAppController("127.0.0.1:16384")
        device.set_input_ime.assert_not_called()
        device.set_fastinput_ime.assert_not_called()

    def test_wenxin_mobile_accept_requires_cleared_input(self):
        controller = wenxin_controller.WenxinAppController.__new__(wenxin_controller.WenxinAppController)
        controller.d = mock.Mock()
        controller.d.app_current.return_value = {"package": controller.PACKAGE}
        edit = mock.Mock()
        edit.exists = True
        edit.info = {"text": ""}
        controller._edit = mock.Mock(return_value=edit)
        result = controller.wait_for_mobile_accept(3, "推荐一款护发素")
        self.assertTrue(result["input_cleared"])

    def test_wenxin_generation_indicator_is_read_from_accessibility_xml(self):
        xml = """<hierarchy><node text="" content-desc="停止生成" resource-id="com.baidu.newapp:id/stop" /></hierarchy>"""
        self.assertTrue(wenxin_controller.WenxinAppController.generation_indicator_in_xml(xml))
        self.assertFalse(wenxin_controller.WenxinAppController.generation_indicator_in_xml("<hierarchy />"))

    def test_wenxin_generation_indicator_has_image_fallback_for_stop_square(self):
        xml = """<hierarchy><node clickable="true" bounds="[160,240][200,280]" /></hierarchy>"""
        stop_image = Image.new("RGB", (200, 300), "white")
        stop_draw = ImageDraw.Draw(stop_image)
        stop_draw.rectangle((176, 256, 184, 264), fill="black")
        self.assertTrue(wenxin_controller.WenxinAppController.generation_indicator_in_image(xml, stop_image))

        plus_image = Image.new("RGB", (200, 300), "white")
        plus_draw = ImageDraw.Draw(plus_image)
        plus_draw.line((176, 260, 184, 260), fill="black", width=1)
        plus_draw.line((180, 256, 180, 264), fill="black", width=1)
        self.assertFalse(wenxin_controller.WenxinAppController.generation_indicator_in_image(xml, plus_image))

    def test_wenxin_waits_for_stop_button_to_appear_then_disappear(self):
        controller = wenxin_controller.WenxinAppController.__new__(wenxin_controller.WenxinAppController)
        controller.generation_indicator_visible = mock.Mock(side_effect=[False, True, True, False, False])
        with mock.patch.object(wenxin_controller.time, "sleep", return_value=None):
            result = controller.wait_for_generation_complete(10)
        self.assertTrue(result["generation_indicator_seen"])
        self.assertTrue(result["generation_complete"])

    def test_wenxin_generation_wait_accepts_indicator_seen_during_send(self):
        controller = wenxin_controller.WenxinAppController.__new__(wenxin_controller.WenxinAppController)
        controller.generation_indicator_visible = mock.Mock(side_effect=[False, False])
        with mock.patch.object(wenxin_controller.time, "sleep", return_value=None):
            result = controller.wait_for_generation_complete(10, already_seen=True)
        self.assertTrue(result["generation_complete"])

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
