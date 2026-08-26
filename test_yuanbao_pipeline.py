import unittest
from pathlib import Path
from unittest.mock import patch

from yuanbao_monitor import controller
from yuanbao_monitor.collector import YuanbaoSourceCollector
from yuanbao_monitor import yuanbao_loop


class _Exists:
    def __bool__(self):
        return True


class _FakeSelector:
    exists = _Exists()


class _ChangingDevice:
    def __init__(self):
        self.index = 0

    def dump_hierarchy(self):
        self.index += 1
        return f"""<hierarchy>
          <node class='android.widget.TextView' text='这是一段已经完成并且足够长的元宝回答正文，用于验证稳定判断不会受到系统时钟、电量或者动画变化的影响。' />
          <node class='android.widget.EditText' resource-id='{controller.YuanbaoController.INPUT_ID}' enabled='true' />
          <node class='android.widget.TextView' resource-id='com.android.systemui:id/clock' text='14:{self.index:02d}' />
        </hierarchy>"""

    def __call__(self, **kwargs):
        return _FakeSelector()


class YuanbaoCompletionTests(unittest.TestCase):
    def test_generation_failure_banner_is_rejected_immediately(self):
        class _FailedDevice:
            def dump_hierarchy(self):
                return "<hierarchy><node class='android.widget.TextView' text='系统异常，回答生成失败。' /></hierarchy>"

        bot = controller.YuanbaoController.__new__(controller.YuanbaoController)
        bot.d = _FailedDevice()
        with patch.object(controller.time, "sleep", return_value=None):
            with self.assertRaisesRegex(controller.YuanbaoGenerationError, "回答生成失败"):
                bot._wait_for_reply("推荐一款祛痘精华液", max_wait=2, poll_interval=0)

    def test_reply_stability_ignores_clock_and_other_unrelated_xml_changes(self):
        bot = controller.YuanbaoController.__new__(controller.YuanbaoController)
        bot.d = _ChangingDevice()
        with patch.object(controller.time, "sleep", return_value=None):
            xml = bot._wait_for_reply("推荐一款染发剂", max_wait=2, poll_interval=0)
        self.assertIn("已经完成", xml)
        self.assertGreaterEqual(bot.d.index, 4)

    def test_plus_button_transition_starts_web_capture_once(self):
        class _GeneratingThenReadyDevice:
            def __init__(self):
                self.index = 0

            def dump_hierarchy(self):
                self.index += 1
                plus = "" if self.index == 1 else (
                    f"<node class='android.widget.ImageView' "
                    f"resource-id='{controller.YuanbaoController.PLUS_ID}' enabled='true' />"
                )
                return f"""<hierarchy>
                  <node class='android.widget.TextView' text='这是一段已经完整生成并且长度足够用于触发网页并发抓取的元宝回答正文。' />
                  <node class='android.widget.EditText' resource-id='{controller.YuanbaoController.INPUT_ID}' enabled='true' />
                  {plus}
                </hierarchy>"""

        bot = controller.YuanbaoController.__new__(controller.YuanbaoController)
        bot.d = _GeneratingThenReadyDevice()
        notifications = []
        with patch.object(controller.time, "sleep", return_value=None):
            bot._wait_for_reply(
                max_wait=2,
                poll_interval=0,
                on_generation_complete=lambda: notifications.append("start-web"),
            )
        self.assertEqual(notifications, ["start-web"])


class _SourceCountDriver:
    def __init__(self, text="引用17篇资料作为参考"):
        self.script = ""
        self.text = text

    def execute_script(self, script):
        self.script = script
        return self.text


class YuanbaoSourceCountTests(unittest.TestCase):
    def test_expected_source_count_uses_valid_javascript_newline_escape(self):
        collector = YuanbaoSourceCollector.__new__(YuanbaoSourceCollector)
        collector.driver = _SourceCountDriver()

        self.assertEqual(collector._expected_source_count(), 17)
        self.assertIn("join('\\n')", collector.driver.script)
        self.assertNotIn("join('\n')", collector.driver.script)

    def test_expected_source_count_ignores_years_in_source_titles(self):
        collector = YuanbaoSourceCollector.__new__(YuanbaoSourceCollector)
        collector.driver = _SourceCountDriver("引用15篇资料作为参考 2026年度热门眉毛增长液")

        self.assertEqual(collector._expected_source_count(), 15)

    def test_expected_source_count_ignores_product_reference_price(self):
        collector = YuanbaoSourceCollector.__new__(YuanbaoSourceCollector)
        collector.driver = _SourceCountDriver("引用16篇资料作为参考 参考价69元")

        self.assertEqual(collector._expected_source_count(), 16)


class _DrawerElement:
    def click(self):
        pass


class _AlreadyOpenDrawerDriver:
    def __init__(self):
        self.scripts = []

    def find_element(self, by, value):
        return _DrawerElement()

    def execute_script(self, script, *args):
        self.scripts.append(script)
        if "map(function(el)" in script:
            return "引用2篇资料作为参考"
        if "function extractTitle" in script:
            return [
                {"title": "来源一", "url": "https://example.com/1"},
                {"title": "来源二", "url": "https://example.com/2"},
            ]
        return None


class YuanbaoOpenDrawerRetryTests(unittest.TestCase):
    def test_retry_does_not_toggle_an_already_open_drawer_closed(self):
        collector = YuanbaoSourceCollector.__new__(YuanbaoSourceCollector)
        collector.driver = _AlreadyOpenDrawerDriver()
        collector.debug = False

        sources = collector._collect_all_sources(_DrawerElement())

        self.assertEqual(len(sources), 2)
        self.assertFalse(
            any("dispatchEvent(new MouseEvent" in script for script in collector.driver.scripts)
        )


class YuanbaoMEmuDiscoveryTests(unittest.TestCase):
    @patch.object(yuanbao_loop, "resolve_memuc")
    @patch.object(yuanbao_loop.subprocess, "run")
    def test_discovers_every_running_memu_instance(self, run, resolve_memuc):
        resolve_memuc.return_value = Path(r"C:\Program Files\Microvirt\MEmu\memuc.exe")
        run.side_effect = [
            type("Result", (), {"stdout": "0,逍遥模拟器,1,1,10\n1,逍遥模拟器 - 1,2,1,11\n2,未启动,0,0,0\n", "returncode": 0})(),
            type("Result", (), {"stdout": "127.0.0.1:21503\n", "returncode": 0})(),
            type("Result", (), {"stdout": "127.0.0.1:21513\n", "returncode": 0})(),
        ]
        records = yuanbao_loop.discover_device_records()
        self.assertEqual(
            [(item["index"], item["serial"]) for item in records],
            [("0", "127.0.0.1:21503"), ("1", "127.0.0.1:21513")],
        )


if __name__ == "__main__":
    unittest.main()
