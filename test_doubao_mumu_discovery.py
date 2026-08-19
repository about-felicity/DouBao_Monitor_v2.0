import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
CONTROLLER = ROOT / "doubao_mumu_controller"
sys.path.insert(0, str(CONTROLLER))
SPEC = importlib.util.spec_from_file_location(
    "doubao_mumu_web_pipeline",
    CONTROLLER / "doubao_mumu_web_pipeline.py",
)
assert SPEC and SPEC.loader
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)


class MuMuAdbDiscoveryTests(unittest.TestCase):
    def test_prefers_memu_when_memu_and_mumu_are_both_installed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            memu = root / "memuc.exe"
            mumu = root / "MuMuManager.exe"
            memu.touch()
            mumu.touch()
            with (
                mock.patch.object(pipeline, "MEMU_CONSOLE_CANDIDATES", [memu]),
                mock.patch.object(pipeline, "MUMU_MANAGER_CANDIDATES", [mumu]),
                mock.patch.object(pipeline, "_registry_app_path", return_value=None),
                mock.patch.object(
                    pipeline, "_registry_emulator_manager", return_value=None
                ),
                mock.patch.object(pipeline, "_where_executable", return_value=None),
            ):
                self.assertEqual(pipeline.resolve_mumu_manager(), memu)

    def test_parses_authoritative_current_account_preferences(self) -> None:
        xml = """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
  <string name="screen_name">测试用户</string>
  <long name="user_id" value="1234567890123456" />
  <string name="user_name">备用昵称</string>
</map>"""
        self.assertEqual(
            pipeline.parse_current_account_preferences(xml),
            {"uid": "1234567890123456", "screen_name": "测试用户"},
        )

    def test_rejects_invalid_current_account_preferences(self) -> None:
        self.assertEqual(
            pipeline.parse_current_account_preferences(
                '<map><string name="user_id">not-a-uid</string></map>'
            ),
            {},
        )

    def test_maps_standard_mumu_ports_to_instance_indexes(self) -> None:
        output = """List of devices attached
127.0.0.1:16384 device product:a model:a
127.0.0.1:16416 device product:b model:b
127.0.0.1:16448 device product:c model:c
"""
        devices = pipeline.parse_mumu_adb_devices(output, None)
        self.assertEqual(
            [(item["index"], item["serial"]) for item in devices],
            [
                ("0", "127.0.0.1:16384"),
                ("1", "127.0.0.1:16416"),
                ("2", "127.0.0.1:16448"),
            ],
        )

    def test_filters_requested_instance_and_ignores_offline_devices(self) -> None:
        output = """List of devices attached
127.0.0.1:16384 offline
127.0.0.1:16416 device
emulator-5554 device
127.0.0.1:16448 device
"""
        devices = pipeline.parse_mumu_adb_devices(output, "2")
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["serial"], "127.0.0.1:16448")

    def test_maps_memu_ports_to_instance_indexes(self) -> None:
        output = """List of devices attached
127.0.0.1:21503 device product:a model:a
127.0.0.1:21513 device product:b model:b
127.0.0.1:21523 device product:c model:c
127.0.0.1:21533 offline
"""
        devices = pipeline.parse_memu_adb_devices(output, None)
        self.assertEqual(
            [(item["index"], item["serial"]) for item in devices],
            [
                ("0", "127.0.0.1:21503"),
                ("1", "127.0.0.1:21513"),
                ("2", "127.0.0.1:21523"),
            ],
        )
        self.assertTrue(all(item["emulator"] == "memu" for item in devices))

    def test_filters_requested_memu_instance(self) -> None:
        output = """List of devices attached
127.0.0.1:21503 device
127.0.0.1:21513 device
127.0.0.1:21523 device
"""
        devices = pipeline.parse_memu_adb_devices(output, "1")
        self.assertEqual([item["serial"] for item in devices], ["127.0.0.1:21513"])

    def test_default_browser_port_does_not_reuse_another_slot_mapping(self) -> None:
        original_path = pipeline.BROWSER_SLOT_MAP_PATH
        with tempfile.TemporaryDirectory() as temporary_dir:
            pipeline.BROWSER_SLOT_MAP_PATH = (
                Path(temporary_dir) / "doubao_browser_slots.json"
            )
            pipeline.BROWSER_SLOT_MAP_PATH.write_text(
                '{"2": 9301}',
                encoding="utf-8",
            )
            try:
                self.assertIsNone(pipeline.browser_port_for_slot("1"))
                self.assertEqual(pipeline.browser_port_for_slot("2"), 9301)
                self.assertEqual(pipeline.browser_port_for_slot("0"), 9300)
            finally:
                pipeline.BROWSER_SLOT_MAP_PATH = original_path

    def test_adb_shell_health_requires_a_real_shell_response(self) -> None:
        ready = pipeline.subprocess.CompletedProcess(
            [], 0, stdout=pipeline.ADB_SHELL_READY_MARKER + "\n", stderr=""
        )
        stuck = pipeline.subprocess.CompletedProcess(
            [], 0, stdout="", stderr=""
        )
        with mock.patch.object(pipeline, "adb_command", return_value=ready):
            self.assertTrue(pipeline.adb_shell_ready(Path("adb"), "serial"))
        with mock.patch.object(pipeline, "adb_command", return_value=stuck):
            self.assertFalse(pipeline.adb_shell_ready(Path("adb"), "serial"))

    def test_multi_instance_health_check_repairs_all_aliases_once(self) -> None:
        devices = [
            {"serial": "127.0.0.1:16384"},
            {"serial": "127.0.0.1:16416"},
            {"serial": "127.0.0.1:16448"},
        ]
        with (
            mock.patch.object(
                pipeline,
                "adb_shell_ready",
                side_effect=[False, False, True],
            ),
            mock.patch.object(pipeline, "restart_adb_connections") as restart,
        ):
            repaired = pipeline.ensure_adb_shells_ready(
                pipeline.logging.getLogger("test"), Path("adb"), devices
            )
        self.assertTrue(repaired)
        restart.assert_called_once_with(
            mock.ANY,
            Path("adb"),
            ["127.0.0.1:16384", "127.0.0.1:16416", "127.0.0.1:16448"],
        )


if __name__ == "__main__":
    unittest.main()
