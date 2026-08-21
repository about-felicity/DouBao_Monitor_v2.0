import unittest
from unittest.mock import Mock

from doubao_mumu_controller.doubao_mumu_loop import AppiumClient, AutomationError
from doubao_mumu_controller.doubao_mumu_web_pipeline import (
    stale_appium_sessions_for_cleanup,
)


def session(session_id, serial, system_port="missing"):
    capabilities = {"udid": serial}
    if system_port != "missing":
        capabilities["systemPort"] = system_port
    return {"id": session_id, "capabilities": capabilities}


class AppiumCleanupSelectionTests(unittest.TestCase):
    def test_only_conflicting_port_or_missing_port_for_same_device_is_selected(self):
        sessions = [
            session("same-device-other-port", "device-2", 8242),
            session("same-port-other-device", "device-1", 8241),
            session("same-device-missing-port", "device-2"),
            session("same-device-same-port", "device-2", 8241),
        ]

        selected, skipped = stale_appium_sessions_for_cleanup(
            sessions,
            "device-2",
            8241,
            limit=12,
        )

        self.assertEqual(
            [item["id"] for item in selected],
            [
                "same-device-same-port",
                "same-device-missing-port",
                "same-port-other-device",
            ],
        )
        self.assertEqual(skipped, 0)

    def test_cleanup_is_bounded_and_prefers_newest_sessions(self):
        sessions = [
            session("oldest", "device-2", 8241),
            session("middle", "device-2", 8241),
            session("newest", "device-2", 8241),
        ]

        selected, skipped = stale_appium_sessions_for_cleanup(
            sessions,
            "device-2",
            8241,
            limit=2,
        )

        self.assertEqual(
            [item["id"] for item in selected],
            ["newest", "middle"],
        )
        self.assertEqual(skipped, 1)


class AppiumClientReleaseTests(unittest.TestCase):
    def test_invalidate_deletes_server_session_before_forgetting_it(self):
        client = AppiumClient.__new__(AppiumClient)
        client.session_id = "session-to-release"
        client.logger = Mock()
        client._json_request = Mock(return_value={"value": None})

        client.invalidate_session()

        self.assertIsNone(client.session_id)
        client._json_request.assert_called_once_with(
            "DELETE",
            "session/session-to-release",
            timeout=8,
            allow_error=True,
        )

    def test_failed_health_check_deletes_old_session_before_replacement(self):
        client = AppiumClient.__new__(AppiumClient)
        client.session_id = "stale-session"
        client.logger = Mock()
        client.adb = Mock()
        client.ensure_server = Mock()
        client._create_session = Mock(return_value="replacement-session")
        client._existing_session = Mock(return_value=None)
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                raise AutomationError("stale")
            return {"value": None}

        client._json_request = request

        self.assertEqual(client.ensure_session(), "replacement-session")
        self.assertEqual(client.session_id, "replacement-session")
        self.assertIn(
            (
                "DELETE",
                "session/stale-session",
                {"timeout": 8, "allow_error": True},
            ),
            calls,
        )


if __name__ == "__main__":
    unittest.main()
