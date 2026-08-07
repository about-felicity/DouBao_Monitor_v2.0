import unittest

from doubao_mumu_controller.doubao_lan_client import candidate_receiver_urls


class DoubaoLanClientTests(unittest.TestCase):
    def test_dashboard_fallback_is_added_for_receiver_urls(self) -> None:
        urls = candidate_receiver_urls(
            {
                "receiver_url": "http://192.168.1.233:8790",
                "receiver_host": "DESKTOP-FO0CT85",
            }
        )

        self.assertEqual(
            urls,
            [
                "http://192.168.1.233:8790",
                "http://192.168.1.233:8765",
                "http://DESKTOP-FO0CT85:8790",
                "http://desktop-fo0ct85:8765",
            ],
        )


if __name__ == "__main__":
    unittest.main()
