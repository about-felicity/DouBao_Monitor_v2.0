import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


DEEPSEEK_DIR = Path(__file__).resolve().parent / "deepseek_monitor"
if str(DEEPSEEK_DIR) not in sys.path:
    sys.path.insert(0, str(DEEPSEEK_DIR))

import controller
import deepseek_loop
from model_plugins.deepseek.plugin import Plugin as DeepSeekPlugin


class DeepSeekSchedulingTests(unittest.TestCase):
    def test_interval_is_always_clamped_strictly_between_90_and_120_seconds(self):
        for requested in ((0, 999), (90, 120), (120, 300), (95, 115)):
            lower, upper = deepseek_loop.safe_interval_bounds(*requested)
            self.assertGreater(lower, 90)
            self.assertLess(upper, 120)
            self.assertLessEqual(lower, upper)

    def test_remote_plugin_uses_the_safe_interval_window(self):
        command, _cwd = DeepSeekPlugin().command({"rounds": 1, "question_mode": "interleaved"})
        self.assertEqual(command[command.index("--min-interval") + 1], "92")
        self.assertEqual(command[command.index("--max-interval") + 1], "118")


class _FakeDevice:
    def __init__(self, xml: str):
        self.xml = xml

    def dump_hierarchy(self, compressed=False):
        return self.xml


class DeepSeekAppCompletionTests(unittest.TestCase):
    def test_new_chat_locator_accepts_top_right_plus_and_rejects_composer_plus(self):
        xml = """<hierarchy>
          <node content-desc='上传文件' bounds='[402,889][437,925]' />
          <node content-desc='开启新对话' bounds='[487,55][520,88]' />
        </hierarchy>"""
        self.assertEqual(controller._new_chat_point(xml, 540, 960), (503, 71))

    def test_completed_mobile_answer_must_be_idle_stable_and_match_topic(self):
        xml = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
        <hierarchy>
          <node class='android.widget.TextView' text='染发剂推荐指南' content-desc='' resource-id='' />
          <node class='android.widget.TextView' text='这是已经完整加载的染发剂回答，正文长度足够用于后续判断。' content-desc='' resource-id='' />
          <node class='android.widget.TextView' text='发消息或按住说话' content-desc='' resource-id='' />
        </hierarchy>"""
        app = controller.DeepSeekAppController.__new__(controller.DeepSeekAppController)
        app.d = _FakeDevice(xml)
        with patch.object(controller.time, "sleep", return_value=None):
            result = app.wait_for_answer("推荐一款染发剂", timeout=2, stable_seconds=0)
        self.assertTrue(result["saw_question"])
        self.assertTrue(result["has_answer"])
        self.assertTrue(result["input_ready"])
        self.assertFalse(result["busy"])


class DeepSeekWebConversationTests(unittest.TestCase):
    def test_collection_waits_for_a_new_conversation_url(self):
        web = controller.DeepSeekWebCollector(9333)
        web.latest_chat = Mock(side_effect=[
            {"ok": True, "links": [{"href": "https://chat.deepseek.com/a/chat/s/old"}]},
            {"ok": True, "links": [{"href": "https://chat.deepseek.com/a/chat/s/new"}]},
        ])
        web._navigate = Mock()
        web.evaluate = Mock(return_value={"currentQuestion": "推荐一款染发剂", "body": "完整回答"})
        web.wait_and_collect = Mock(return_value={"ok": True, "sources": []})
        with patch.object(controller.time, "sleep", return_value=None):
            result = web.collect_latest(
                "推荐一款染发剂", timeout=3, stable_seconds=0,
                previous_chat_url="https://chat.deepseek.com/a/chat/s/old",
            )
        self.assertEqual(result["conversation_url"], "https://chat.deepseek.com/a/chat/s/new")
        web._navigate.assert_called_once_with("https://chat.deepseek.com/a/chat/s/new")


if __name__ == "__main__":
    unittest.main()
