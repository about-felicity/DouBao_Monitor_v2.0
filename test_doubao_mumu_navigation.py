import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from doubao_mumu_controller.doubao_mumu_loop import (
    BACK_ID,
    CHAT_ROOT_ID,
    INPUT_ID,
    LIST_ID,
    NEW_CHAT_ID,
    SIDEBAR_NEW_CHAT_ID,
    DoubaoAutomation,
    find_new_chat_node,
    has_app_crash_dialog,
    page_name,
    parse_dalvik_heap_alloc_kb,
    parse_xml,
)


def page(*resource_ids):
    nodes = "".join(
        '<node resource-id="%s" clickable="true" bounds="[10,10][60,60]" />'
        % resource_id
        for resource_id in resource_ids
    )
    return '<?xml version="1.0" encoding="UTF-8"?><hierarchy>%s</hierarchy>' % nodes


class FakeAppium:
    def __init__(self, sources):
        self.sources = iter(sources)
        self.clicked_ids = []

    def source(self):
        return next(self.sources)

    def click_id(self, resource_id, timeout=0):
        self.clicked_ids.append(resource_id)

    def back(self):
        pass


class FakeAdb:
    def shell(self, *args, **kwargs):
        return None

    def press_back(self):
        return None

    def bring_doubao_foreground(self):
        return None


class MemoryPressureAdb(FakeAdb):
    def __init__(self, heap_kb):
        self.heap_kb = heap_kb
        self.restarts = 0
        self.shell_calls = []

    def shell(self, *args, **kwargs):
        self.shell_calls.append(args)
        return ""

    def doubao_pid(self):
        return "1234"

    def doubao_heap_alloc_kb(self):
        return self.heap_kb

    def force_stop_and_restart(self):
        self.restarts += 1


class NewChatNavigationTests(unittest.TestCase):
    def test_detects_android_repeated_crash_dialog(self):
        root = parse_xml(
            '<?xml version="1.0"?><hierarchy>'
            '<node text="豆包屡次停止运行" />'
            '<node text="关闭应用" />'
            '</hierarchy>'
        )
        self.assertTrue(has_app_crash_dialog(root))

    def test_parses_dalvik_heap_allocation(self):
        meminfo = (
            " Dalvik Heap   240926   240856        0        0"
            "   262144   255311     6833\n"
        )
        self.assertEqual(parse_dalvik_heap_alloc_kb(meminfo), 255311)

    @patch("doubao_mumu_controller.doubao_mumu_loop.time.monotonic", return_value=120)
    def test_restarts_doubao_before_question_when_heap_is_near_limit(self, _clock):
        adb = MemoryPressureAdb(220 * 1024)
        automation = DoubaoAutomation(
            logging.getLogger("test-memory-guard"),
            adb,
            FakeAppium([]),
            Path("."),
        )

        automation.recover_before_question()

        self.assertEqual(adb.restarts, 1)

    def test_keeps_healthy_doubao_process_running(self):
        adb = MemoryPressureAdb(150 * 1024)
        automation = DoubaoAutomation(
            logging.getLogger("test-memory-guard-healthy"),
            adb,
            FakeAppium([]),
            Path("."),
        )

        automation.recover_before_question()

        self.assertEqual(adb.restarts, 0)

    def test_releases_app_heap_after_completed_round(self):
        adb = MemoryPressureAdb(150 * 1024)
        automation = DoubaoAutomation(
            logging.getLogger("test-memory-release"),
            adb,
            FakeAppium([]),
            Path("."),
        )

        automation.release_memory_after_round()

        self.assertIn(("am", "force-stop", "com.larus.nova"), adb.shell_calls)

    def test_sidebar_takes_precedence_over_chat_root(self):
        root = parse_xml(page(CHAT_ROOT_ID, SIDEBAR_NEW_CHAT_ID))
        self.assertEqual(page_name(root), "sidebar")
        self.assertEqual(
            find_new_chat_node(root).attrib["resource-id"],
            SIDEBAR_NEW_CHAT_ID,
        )

    def test_legacy_list_compose_button_is_still_supported(self):
        root = parse_xml(page(LIST_ID, NEW_CHAT_ID))
        self.assertEqual(page_name(root), "list")
        self.assertEqual(
            find_new_chat_node(root).attrib["resource-id"],
            NEW_CHAT_ID,
        )

    @patch("doubao_mumu_controller.doubao_mumu_loop.time.sleep", return_value=None)
    def test_chat_drawer_flow_clicks_sidebar_compose(self, _sleep):
        appium = FakeAppium(
            [
                page(CHAT_ROOT_ID, BACK_ID, INPUT_ID),
                page(CHAT_ROOT_ID, SIDEBAR_NEW_CHAT_ID),
                page(CHAT_ROOT_ID, INPUT_ID),
            ]
        )
        automation = DoubaoAutomation(
            logging.getLogger("test-new-chat"),
            FakeAdb(),
            appium,
            Path("."),
        )

        automation.create_new_chat()

        self.assertEqual(
            appium.clicked_ids,
            [BACK_ID, SIDEBAR_NEW_CHAT_ID],
        )


if __name__ == "__main__":
    unittest.main()
