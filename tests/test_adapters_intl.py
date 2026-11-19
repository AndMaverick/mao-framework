"""Provider adapters for DeepSeek, Moonshot (Kimi) and Google Gemini, plus the dispatcher.

These test documented response shapes. They do not call any API. Each provider counts cached input
differently, which is exactly where cost reports go wrong, so each case is worked out by hand.
"""

import unittest
from types import SimpleNamespace as NS

from mao import PriceBook, Usage, usage_from, usage_from_deepseek, usage_from_gemini, usage_from_moonshot


class DeepSeek(unittest.TestCase):
    def test_hit_and_miss_split_the_input(self):
        resp = {"usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                          "prompt_cache_hit_tokens": 700, "prompt_cache_miss_tokens": 300}}
        self.assertEqual(usage_from_deepseek(resp), Usage(300, 50, 700, 0))

    def test_miss_only_is_enough(self):
        resp = {"usage": {"completion_tokens": 5, "prompt_cache_miss_tokens": 40}}
        self.assertEqual(usage_from_deepseek(resp), Usage(40, 5, 0, 0))

    def test_no_cache_fields_means_all_input_is_uncached(self):
        self.assertEqual(usage_from_deepseek({"usage": {"prompt_tokens": 90, "completion_tokens": 10}}), Usage(90, 10, 0, 0))

    def test_contradictory_counts_are_refused(self):
        with self.assertRaises(ValueError):
            usage_from_deepseek({"usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 10}})
        with self.assertRaises(ValueError):
            usage_from_deepseek({"usage": {"prompt_tokens": 10, "prompt_cache_hit_tokens": 60}})
        with self.assertRaises(ValueError):
            usage_from_deepseek({"usage": {"completion_tokens": 3}})


class Moonshot(unittest.TestCase):
    def test_prompt_tokens_includes_reads_and_writes(self):
        resp = NS(usage=NS(prompt_tokens=1000, completion_tokens=80,
                           prompt_tokens_details=NS(cached_tokens=600, cache_write_tokens=100)))
        self.assertEqual(usage_from_moonshot(resp), Usage(300, 80, 600, 100))

    def test_top_level_cached_tokens_is_read_when_details_are_absent(self):
        resp = {"usage": {"prompt_tokens": 500, "completion_tokens": 5, "cached_tokens": 200}}
        self.assertEqual(usage_from_moonshot(resp), Usage(300, 5, 200, 0))

    def test_cache_beyond_total_is_refused(self):
        with self.assertRaises(ValueError):
            usage_from_moonshot({"usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 8}}})


class Gemini(unittest.TestCase):
    def test_cached_content_is_inside_the_prompt_count_and_thoughts_are_output(self):
        resp = {"usageMetadata": {"promptTokenCount": 1000, "cachedContentTokenCount": 400,
                                  "candidatesTokenCount": 120, "thoughtsTokenCount": 300, "totalTokenCount": 1420}}
        self.assertEqual(usage_from_gemini(resp), Usage(600, 420, 400, 0))

    def test_tool_use_input_is_uncached_input(self):
        resp = {"usageMetadata": {"promptTokenCount": 100, "toolUsePromptTokenCount": 30, "candidatesTokenCount": 10}}
        self.assertEqual(usage_from_gemini(resp), Usage(130, 10, 0, 0))

    def test_sdk_object_with_snake_case_fields(self):
        resp = NS(usage_metadata=NS(prompt_token_count=50, cached_content_token_count=10, candidates_token_count=5,
                                    thoughts_token_count=2))
        self.assertEqual(usage_from_gemini(resp), Usage(40, 7, 10, 0))

    def test_missing_or_impossible_metadata_is_refused(self):
        with self.assertRaises(ValueError):
            usage_from_gemini({})
        with self.assertRaises(ValueError):
            usage_from_gemini({"usageMetadata": {"promptTokenCount": 5, "cachedContentTokenCount": 9}})


class Dispatch(unittest.TestCase):
    def test_every_provider_name_routes_to_its_adapter(self):
        self.assertEqual(usage_from("Kimi", {"usage": {"prompt_tokens": 9, "completion_tokens": 1}}), Usage(9, 1))
        self.assertEqual(usage_from("anthropic", {"usage": {"input_tokens": 3, "output_tokens": 4}}), Usage(3, 4))
        self.assertEqual(usage_from("openai", {"usage": {"prompt_tokens": 3, "completion_tokens": 4}}), Usage(3, 4))
        self.assertEqual(usage_from("google", {"usageMetadata": {"promptTokenCount": 2}}), Usage(2, 0))

    def test_unknown_provider_is_an_error(self):
        with self.assertRaises(ValueError):
            usage_from("acme", {})

    def test_cache_pricing_stays_exact_across_providers(self):
        # The same 1M-token prompt, 600k of it cached, priced identically however the provider counts it.
        prices = PriceBook({"m": {"input_per_million": 1.0, "output_per_million": 2.0, "cached_input_per_million": 0.1}})
        shapes = [
            usage_from_deepseek({"usage": {"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 600_000,
                                           "prompt_cache_miss_tokens": 400_000, "completion_tokens": 0}}),
            usage_from_moonshot({"usage": {"prompt_tokens": 1_000_000, "prompt_tokens_details": {"cached_tokens": 600_000}}}),
            usage_from_gemini({"usageMetadata": {"promptTokenCount": 1_000_000, "cachedContentTokenCount": 600_000}}),
        ]
        self.assertEqual({round(prices.cost("m", u), 9) for u in shapes}, {0.46})


if __name__ == "__main__":
    unittest.main()
