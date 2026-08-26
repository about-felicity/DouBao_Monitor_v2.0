from __future__ import annotations

import sys
import unittest
from unittest import mock
from PIL import Image, ImageDraw

from remote_model_control_panel import (
    RemoteModelPanel,
    account_gate_open,
    build_worker_command,
    console_python_executable,
)
from monitor_core.collector_guard import collector_guard_port
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

    def test_wenxin_security_verification_is_detected_immediately(self):
        check = wenxin_controller.WenxinWebCollector._is_security_verification
        self.assertTrue(check({"url": "https://wappass.baidu.com/static/captcha/example"}))
        self.assertTrue(check({"url": "https://www.baidu.com/", "pageText": "百度安全验证 请完成下方验证"}))
        self.assertFalse(check({"url": "https://www.baidu.com/s?wd=test", "pageText": "普通搜索结果"}))

    def test_only_yuanbao_requires_successful_login_check(self):
        self.assertTrue(account_gate_open("wenxin", False))
        self.assertFalse(account_gate_open("yuanbao", False))
        self.assertTrue(account_gate_open("wenxin", True))
        self.assertTrue(account_gate_open("deepseek", False))

    def test_wenxin_baidu_check_is_allowed_while_another_collector_runs(self):
        panel = RemoteModelPanel.__new__(RemoteModelPanel)
        panel.model = "wenxin"
        panel.account_check_running = False
        panel.external_worker_running = True
        panel.process = None
        panel.status_var = mock.Mock()
        panel.account_var = mock.Mock()
        panel.account_button = mock.Mock()
        panel.start_button = mock.Mock()
        panel.append_log = mock.Mock()
        panel.account_worker = mock.Mock()
        thread = mock.Mock()
        with mock.patch("remote_model_control_panel.threading.Thread", return_value=thread):
            panel.check_account()
        self.assertTrue(panel.account_check_running)
        thread.start.assert_called_once_with()

    def test_worker_command_is_bound_to_one_model(self) -> None:
        command = build_worker_command("deepseek", 20, "interleaved", "D:/pairing.json")
        self.assertEqual(command[0], console_python_executable())
        self.assertEqual(command[command.index("--model") + 1], "deepseek")
        self.assertEqual(command[command.index("--rounds") + 1], "20")
        self.assertEqual(command[command.index("--pairing") + 1], "D:/pairing.json")

    def test_worker_command_omits_empty_pairing(self) -> None:
        command = build_worker_command("yuanbao", 0, "sequential")
        self.assertNotIn("--pairing", command)
        self.assertEqual(command[command.index("--rounds") + 1], "1")

    def test_wenxin_worker_command_accepts_up_to_four_tasks(self) -> None:
        command = build_worker_command("wenxin", 10, "interleaved", tasks=4)
        self.assertEqual(command[command.index("--tasks") + 1], "4")
        clamped = build_worker_command("wenxin", 10, "interleaved", tasks=9)
        self.assertEqual(clamped[clamped.index("--tasks") + 1], "4")

    def test_worker_uses_console_python_when_panel_runs_under_pythonw(self) -> None:
        with mock.patch("remote_model_control_panel.sys.executable", r"C:\Python\pythonw.exe"), \
                mock.patch("remote_model_control_panel.Path.is_file", return_value=True):
            self.assertEqual(console_python_executable(), r"C:\Python\python.exe")

    def test_web_only_wenxin_has_an_independent_collector_guard(self) -> None:
        self.assertNotEqual(collector_guard_port("wenxin"), collector_guard_port("yuanbao"))
        self.assertEqual(collector_guard_port("yuanbao"), collector_guard_port("deepseek"))

    def test_panel_stop_terminates_its_worker_process_tree(self) -> None:
        panel = RemoteModelPanel.__new__(RemoteModelPanel)
        panel.process = mock.Mock(pid=4321)
        panel.process.poll.return_value = None
        panel.append_log = mock.Mock()
        with mock.patch("remote_model_control_panel.os.name", "nt"), \
                mock.patch("remote_model_control_panel.subprocess.run") as run:
            panel.stop()
        run.assert_called_once_with(
            ["taskkill", "/PID", "4321", "/T", "/F"], capture_output=True, check=False
        )
        panel.append_log.assert_called_once()


if __name__ == "__main__":
    unittest.main()
