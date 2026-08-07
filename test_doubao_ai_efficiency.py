import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import save_doubao_refs as saver
import doubao_product_ai_worker as product_worker
import doubao_source_ai_worker as source_worker


class ProductAiEfficiencyTests(unittest.TestCase):
    def test_simple_extractors_disable_expensive_thinking_mode(self):
        self.assertEqual(saver.DEEPSEEK_THINKING_MODE, {"type": "disabled"})
        self.assertEqual(
            saver.deepseek_thinking_options("https://api.deepseek.com/anthropic"),
            {"thinking": {"type": "disabled"}},
        )
        self.assertEqual(
            saver.deepseek_thinking_options("https://api.openai.com"),
            {},
        )

    def test_product_prompt_is_compact_and_keeps_complete_body(self):
        answer = (
            "推荐三款：\n"
            "1. 欧莱雅小金瓶，适合干枯发质。\n"
            "2. 卡诗山茶花护发油，适合烫染受损。\n"
            "3. 且初护发精油，适合细软发。"
        )
        encoded = json.dumps(saver.build_product_prompt(answer), ensure_ascii=False)
        self.assertIn("欧莱雅小金瓶", encoded)
        self.assertLess(len(encoded), 1100)
        self.assertLessEqual(saver.product_ai_max_tokens(answer), 1200)

    def test_product_prompt_does_not_truncate_long_answers(self):
        marker = "UNIQUE_PRODUCT_AT_THE_END"
        answer = "正文推荐信息。" * 1000 + marker
        self.assertTrue(saver.build_product_prompt(answer)["text"].endswith(marker))

    def test_output_budget_expands_for_long_numbered_lists(self):
        answer = "\n".join(
            "%d. 品牌%d产品，推荐理由。" % (index, index)
            for index in range(1, 11)
        )
        self.assertGreaterEqual(saver.product_ai_max_tokens(answer), 1000)

    def test_unnumbered_product_lists_have_safe_json_budget(self):
        answer = "推荐几款不同定位的产品，可以根据发质和预算选择。" * 20
        self.assertEqual(saver.product_ai_max_tokens(answer), 1200)

    def test_repeated_contiguous_brand_prefix_is_collapsed(self):
        self.assertEqual(
            saver.normalize_ai_product_brand_prefix("儒曼 儒曼控油蓬松洗发水", "儒曼"),
            "儒曼 控油蓬松洗发水",
        )
        self.assertEqual(
            saver.normalize_ai_product_brand_prefix("JohnJeffJeff二硫化硒", "John Jeff"),
            "John Jeff 二硫化硒",
        )

    def test_grounding_rejects_evidence_not_present_in_answer(self):
        with self.assertRaises(ValueError):
            saver.validate_grounded_ai_products(
                "正文只推荐欧莱雅小金瓶。",
                [{
                    "product_name": "虚构产品",
                    "brand_name": "虚构",
                    "evidence": "这是正文中不存在的证据",
                }],
            )

    def test_grounding_rejects_product_name_unrelated_to_evidence(self):
        with self.assertRaises(ValueError):
            saver.validate_grounded_ai_products(
                "推荐欧莱雅小金瓶，适合干枯发质。",
                [{
                    "product_name": "虚构品牌神奇精华液",
                    "brand_name": "虚构品牌",
                    "evidence": "推荐欧莱雅小金瓶",
                }],
            )

    def test_complete_check_allows_duplicate_model_items_after_deduplication(self):
        parsed = {"products": [
            {"product_name": "欧莱雅小金瓶", "evidence": "推荐欧莱雅小金瓶"},
            {"product_name": "欧莱雅小金瓶", "evidence": "推荐欧莱雅小金瓶"},
        ]}
        products = [{"product_name": "欧莱雅小金瓶", "evidence": "推荐欧莱雅小金瓶"}]
        self.assertEqual(
            saver.ensure_complete_ai_products("推荐欧莱雅小金瓶", parsed, products),
            products,
        )

    def test_verified_result_uses_fingerprint_cache(self):
        answer = "推荐欧莱雅小金瓶，适合干枯发质。"
        products = [{
            "product_name": "欧莱雅小金瓶",
            "brand_name": "欧莱雅",
            "evidence": "推荐欧莱雅小金瓶",
            "rank": 1,
            "rank_type": "appearance_order",
        }]
        cache = {
            saver.product_ai_cache_key(answer): {
                "products": products,
                "method": "anthropic",
                "model": "test",
            }
        }
        with mock.patch.object(saver, "load_json_cache", return_value=cache):
            hit = saver.cached_product_ai_result(answer)
        self.assertEqual(hit["products"], products)


class RetryBackoffTests(unittest.TestCase):
    def test_product_retry_skips_answers_before_next_retry(self):
        row = {"run_no": "1", "answer_hash": "abc"}
        state = {"abc": {"attempts": 1, "next_retry_at": time.time() + 600}}
        with mock.patch.object(product_worker, "pending_rows", return_value=[row]):
            self.assertEqual(
                product_worker.eligible_pending_rows(state, max_attempts=2),
                [],
            )

    def test_source_retry_is_bounded(self):
        row = {"href": "https://example.com/a", "title": "Example"}
        state = {"example.com": {"attempts": 2, "next_retry_at": 0}}
        self.assertFalse(
            source_worker.needs_ai(
                "example.com",
                row,
                {},
                state,
                max_attempts=2,
            )
        )


if __name__ == "__main__":
    unittest.main()
