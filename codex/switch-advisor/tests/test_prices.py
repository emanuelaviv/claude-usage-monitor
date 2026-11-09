"""advisor.prices: which rates a request pays (short or long context, the speed tier) and what a logged request costs.
The rule, the rate and the monitor all price requests with it."""
import unittest

from advisor import prices
from advisor.prices import LONG_AT, rates, request_cost

M = 1e-6     # $ per token from $ per million tokens


class RatesTest(unittest.TestCase):
    def assertRates(self, got, want):
        self.assertIsNotNone(got)
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, places=12)

    def test_short_and_long_context_at_272k(self):
        self.assertEqual(LONG_AT, 272_000)
        self.assertRates(rates("gpt-6-astra", 272_000), (10 * M, 1 * M, 50 * M))      # up to 272K: short rates
        self.assertRates(rates("gpt-6-astra", 272_001), (20 * M, 2 * M, 75 * M))      # above: the request as a whole
        self.assertRates(rates("gpt-6-astra"), (10 * M, 1 * M, 50 * M))

    def test_a_model_without_long_rates_keeps_its_rates(self):
        self.assertRates(rates("gpt-5.3-codex", 900_000), rates("gpt-5.3-codex", 1_000))

    def test_speed_tiers(self):
        base = rates("gpt-6-astra", 100_000)
        self.assertRates(rates("gpt-6-astra", 100_000, "fast"), [2 * x for x in base])
        self.assertRates(rates("gpt-6-astra", 100_000, "Ultrafast"), [6 * x for x in base])
        for tier in (None, "", "default", "something-new"):
            with self.subTest(tier=tier):
                self.assertRates(rates("gpt-6-astra", 100_000, tier), base)

    def test_priority_is_fast_mode_by_its_old_name(self):
        base = rates("gpt-6-astra", 100_000)
        self.assertRates(rates("gpt-6-astra", 100_000, "priority"), [2 * x for x in base])
        self.assertRates(rates("gpt-6-astra", 100_000, "Priority"), rates("gpt-6-astra", 100_000, "fast"))

    def test_gpt_5_5(self):
        self.assertRates(rates("gpt-5.5", 272_000), (5 * M, 0.5 * M, 30 * M))
        self.assertRates(rates("gpt-5.5", 272_001), (10 * M, 1 * M, 45 * M))
        self.assertRates(rates("gpt-5.5", 100_000, "fast"), (12.5 * M, 1.25 * M, 75 * M))      # 2.5x on GPT-5.5
        self.assertRates(rates("gpt-5.5", 100_000, "priority"), (12.5 * M, 1.25 * M, 75 * M))

    def test_a_pro_model_is_not_priced_as_its_base_model(self):
        for model in ("gpt-5.5-pro", "gpt-6-astra-pro-preview"):
            with self.subTest(model=model):
                self.assertIsNone(rates(model, 100_000))

    def test_unknown_models_have_no_price(self):
        for model in (None, "", "gpt-unknown-9", "codex-auto-review"):
            with self.subTest(model=model):
                self.assertIsNone(rates(model, 100_000))
                self.assertIsNone(request_cost(model, 100_000, 50_000, 1_000))

    def test_names_with_a_date_or_other_case_match_the_longest_known_name(self):
        self.assertRates(rates(" GPT-6-Astra ", 1_000), rates("gpt-6-astra", 1_000))
        self.assertRates(rates("gpt-6-astra-2026-11-01", 1_000), rates("gpt-6-astra", 1_000))
        self.assertRates(rates("gpt-6.1-sol-preview", 1_000), rates("gpt-6.1-sol", 1_000))
        self.assertNotEqual(rates("gpt-6.1-sol", 1_000), rates("gpt-6-sol", 1_000))

    def test_every_listed_model_is_priced(self):
        for model, (short, long) in prices.PRICES.items():
            with self.subTest(model=model):
                self.assertEqual(len(short), 3)
                self.assertTrue(long is None or len(long) == 3)
                self.assertIsNotNone(rates(model, 1_000))


class RequestCostTest(unittest.TestCase):
    def test_cached_input_is_part_of_the_input(self):
        # 100K input of which 80K cached, 2K output, at $10 / $1 / $50 per M: 20K x 10 + 80K x 1 + 2K x 50
        self.assertAlmostEqual(request_cost("gpt-6-astra", 100_000, 80_000, 2_000), 0.2 + 0.08 + 0.1, places=10)

    def test_a_long_request_pays_long_rates_on_all_of_it(self):
        self.assertAlmostEqual(request_cost("gpt-6-astra", 300_000, 0, 0), 300_000 * 20 * M, places=10)

    def test_more_cached_than_input_counts_as_all_cached(self):
        self.assertAlmostEqual(request_cost("gpt-6-astra", 10_000, 12_000, 0), 10_000 * 1 * M, places=10)

    def test_fast_doubles_the_cost(self):
        self.assertAlmostEqual(request_cost("gpt-6-astra", 100_000, 80_000, 2_000, "fast"),
                               2 * request_cost("gpt-6-astra", 100_000, 80_000, 2_000), places=10)


if __name__ == "__main__":
    unittest.main()
