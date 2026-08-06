from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from build_remote_model_packages import MODELS, build_package, clean_generated_packages, copy_source_tree


class RemoteModelPackageTests(unittest.TestCase):
    def test_catalog_contains_only_the_three_supported_remote_models(self):
        self.assertEqual(tuple(MODELS), ("deepseek", "yuanbao", "wenxin"))

    def test_package_embeds_pairing_and_only_one_plugin(self):
        pairing = {"receiver_url": "http://192.168.1.20:8791", "token": "x" * 48}
        with tempfile.TemporaryDirectory() as temporary:
            folder, archive = build_package("deepseek", pairing, Path(temporary), "test")
            sync = json.loads(
                (folder / "runtime" / "remote_workers" / "deepseek_sync.json").read_text(encoding="utf-8")
            )
            self.assertEqual(sync["receiver_url"], pairing["receiver_url"])
            self.assertEqual(sync["token"], pairing["token"])
            self.assertEqual(
                [path.parent.name for path in (folder / "model_plugins").glob("*/plugin.py")],
                ["deepseek"],
            )
            self.assertTrue((folder / "一键启动DeepSeek远端采集.bat").exists())
            self.assertTrue(archive.exists())

    def test_clean_removes_only_generated_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generated = root / "afu_remote_20260806_010203"
            generated.mkdir()
            (root / "keep.txt").write_text("keep", encoding="utf-8")
            clean_generated_packages(root)
            self.assertFalse(generated.exists())
            self.assertTrue((root / "keep.txt").exists())

    def test_copy_prunes_virtual_environments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            target = root / "target"
            (source / ".venv" / "Lib").mkdir(parents=True)
            (source / ".venv" / "Lib" / "large.py").write_text("ignored", encoding="utf-8")
            (source / "collector.py").write_text("kept", encoding="utf-8")
            copy_source_tree(source, target)
            self.assertTrue((target / "collector.py").exists())
            self.assertFalse((target / ".venv").exists())


if __name__ == "__main__":
    unittest.main()
