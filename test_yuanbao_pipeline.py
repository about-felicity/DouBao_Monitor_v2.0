import unittest
from pathlib import Path
from unittest.mock import patch

from yuanbao_monitor import controller
from yuanbao_monitor.collector import YuanbaoSourceCollector


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
    def test_reply_stability_ignores_clock_and_other_unrelated_xml_changes(self):
        bot = controller.YuanbaoController.__new__(controller.YuanbaoController)
        bot.d = _ChangingDevice()
        with patch.object(controller.time, "sleep", return_value=None):
            xml = bot._wait_for_reply("推荐一款染发剂", max_wait=2, poll_interval=0)
        self.assertIn("已经完成", xml)
        self.assertGreaterEqual(bot.d.index, 4)


class _SourceCountDriver:
    def __init__(self, text="参考来源 17"):
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
        collector.driver = _SourceCountDriver("参考来源 15 2026年度热门眉毛增长液来源推荐")

        self.assertEqual(collector._expected_source_count(), 15)


if __name__ == "__main__":
    unittest.main()
