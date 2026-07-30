import unittest

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


if __name__ == "__main__":
    unittest.main()
