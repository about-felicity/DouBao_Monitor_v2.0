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
    page_name,
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


class NewChatNavigationTests(unittest.TestCase):
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
