"""Guard the supported collector and transport boundary."""

from __future__ import annotations

import unittest

from monitor_core.performance import MODEL_ORDER
from monitor_core.plugins import discover_plugins
from transport.sync import ALLOWED_MODELS


EXPECTED_MODELS = {"deepseek", "wenxin", "yuanbao"}


class ThreeModelBoundaryTests(unittest.TestCase):
    def test_plugin_registry_contains_exactly_the_supported_models(self) -> None:
        self.assertEqual(set(discover_plugins()), EXPECTED_MODELS)

    def test_transport_accepts_exactly_the_supported_models(self) -> None:
        self.assertEqual(set(ALLOWED_MODELS), EXPECTED_MODELS)

    def test_dashboard_performance_uses_only_supported_models(self) -> None:
        self.assertEqual(set(MODEL_ORDER), EXPECTED_MODELS)


if __name__ == "__main__":
    unittest.main()
