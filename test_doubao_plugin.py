import unittest

from model_plugins.doubao.plugin import _saved_rows


class DoubaoActivityTests(unittest.TestCase):
    def test_saved_rows_ignores_warning_before_json_output(self) -> None:
        save = {
            "output": (
                "package.py:1: UserWarning: dependency is deprecated\n"
                '{"ok": true, "rows_written": 12, "count": 12}'
            )
        }

        self.assertEqual(_saved_rows(save), 12)

    def test_saved_rows_accepts_structured_output(self) -> None:
        self.assertEqual(_saved_rows({"output": {"count": 15}}), 15)


if __name__ == "__main__":
    unittest.main()
