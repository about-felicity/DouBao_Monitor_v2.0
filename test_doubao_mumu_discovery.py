import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


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


if __name__ == "__main__":
    unittest.main()
