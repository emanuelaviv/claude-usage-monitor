"""advisor.rate.compute_rate on synthetic meters and calls.

Every priced call below is 100,000 uncached input tokens on gpt-6-astra ($10 per M): $1.00 each. The weekly windows
(plan "plus"), by their readings and the calls between them:
    W1  resets T0+7d   10% -> 30% over 8 h    rise 20, 30 calls  $30   1.5 $/pt
    W2  resets T0+14d   5% -> 17% over 10 h   rise 12, 20 calls + 4 approval reviews priced like gpt-6-astra
                                              (+ 2 small calls of an unpriced model, 0.1% of the tokens)  $24   2.0 $/pt
    W3  resets T0+21d   0% -> 25% over 7 h    rise 25, 25 calls  $25   1.0 $/pt
    W4  resets T0+28d   2% -> 22% over 8 h    rise 20, 50 calls  $50   2.5 $/pt
    W5  resets T0+35d   0% -> 15% over 2 h    spans less than 6 h: left out
    W6  resets T0-1d    0% ->  5% over 1 d    rose less than 10 points: left out
The rate is the median of the 3 most recent windows that count (W2, W3, W4): median(2.0, 1.0, 2.5) = 2.0.
"""
import os
import tempfile
import unittest
from unittest import mock

from advisor import rate, rollout
from tests import fixtures as fx

T0, HOUR, DAY = fx.T0, fx.HOUR, fx.DAY
NOW = T0 + 30 * DAY


class RateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = fx.make_home(self._tmp.name)
        self.cwd = os.path.join(self._tmp.name, "project")

    def tearDown(self):
        self._tmp.cleanup()

    def week(self, start, hours, pct0, pct1, resets, calls, plan="plus", reviews=0, unpriced=0,
             unpriced_size=100_000, unpriced_model="gpt-test-unpriced", mtime=None):
        """A main thread with two weekly readings `hours` apart and `calls` $1 calls spread up to the second one,
        plus one call before the first reading and one after the second (neither counts). Approval reviews (1 h
        apart) and calls of an unpriced model (unpriced_size input tokens each, spread evenly) go in a subagent thread
        over the same span. mtime: the main log's modification time (default its last record's)."""
        end = start + hours * HOUR
        r = fx.Rollout(self.home, fx.new_id(), self.cwd)
        r.meta(t=start - 120)
        r.turn("gpt-6-astra", 258_400, t=start - 100)
        r.call(start - 60, 100_000, 0, 0)
        r.meter(start, pct0, resets_at=resets, plan=plan)
        for i in range(calls):
            r.call(start + (i + 1) * (end - start) / calls, 100_000, 0, 0)
        r.meter(end, pct1, resets_at=resets, plan=plan)
        r.call(end + 60, 100_000, 0, 0)
        r.write(mtime=mtime)
        if reviews or unpriced:
            s = fx.Rollout(self.home, fx.new_id(), self.cwd, kind="subagent", parent=r.thread_id)
            s.meta(t=start)
            s.turn("codex-auto-review", 258_400, t=start)
            for i in range(reviews):
                s.call(start + (i + 1) * HOUR, 100_000, 0, 0)
            s.turn_context(unpriced_model, t=start + 1)
            for i in range(unpriced):
                s.call(start + (i + 1) * (end - start) / (unpriced + 1), unpriced_size, 0, 0)
            s.write()
        return r

    def all_weeks(self):
        self.week(T0 + 1 * HOUR, 8, 10, 30, T0 + 7 * DAY, 30)
        self.week(T0 + 7 * DAY + HOUR, 10, 5, 17, T0 + 14 * DAY, 20, reviews=4, unpriced=2, unpriced_size=1_000)
        self.week(T0 + 14 * DAY + HOUR, 7, 0, 25, T0 + 21 * DAY, 25)
        self.week(T0 + 21 * DAY + HOUR, 8, 2, 22, T0 + 28 * DAY, 50)
        self.week(T0 + 28 * DAY + HOUR, 2, 0, 15, T0 + 35 * DAY, 10)
        self.week(T0 - 4 * DAY, 24, 0, 5, T0 - DAY, 10)

    def test_median_of_the_three_most_recent_windows(self):
        self.all_weeks()
        got = rate.compute_rate(self.home, now=NOW, days=35)
        self.assertEqual(got["usd_per_pt"], 2.0)
        self.assertEqual((got["windows"], got["computed_at"]), (3, NOW))
        detail = [(w["resets"], w["plan"], w["rise"], w["usd"], w["calls"], w["unpriced"]) for w in got["detail"]]
        self.assertEqual(detail,
                         [(T0 + 14 * DAY, "plus", 12.0, 24.0, 26, 2),
                          (T0 + 21 * DAY, "plus", 25.0, 25.0, 25, 0),
                          (T0 + 28 * DAY, "plus", 20.0, 50.0, 50, 0)])

    def test_windows_older_than_the_period_are_left_out(self):
        self.all_weeks()
        got = rate.compute_rate(self.home, now=NOW, days=15)     # from T0+15d: only W4 starts inside the period
        self.assertEqual((got["usd_per_pt"], got["windows"]), (2.5, 1))

    def test_none_without_a_qualifying_window(self):
        self.assertIsNone(rate.compute_rate(self.home, now=NOW))
        self.week(T0 + 28 * DAY + HOUR, 2, 0, 15, T0 + 35 * DAY, 10)        # too short
        self.week(T0 + 25 * DAY, 24, 0, 5, T0 + 26 * DAY, 10)              # rose too little
        self.assertIsNone(rate.compute_rate(self.home, now=NOW))

    def test_meter_rise_without_local_requests_is_left_out(self):
        self.week(T0 + 21 * DAY + HOUR, 8, 2, 22, T0 + 28 * DAY, 0)
        self.assertIsNone(rate.compute_rate(self.home, now=NOW))

    def test_a_window_with_unpriced_usage_is_left_out(self):
        """The meter counts requests on a model without a known price (a new one, or a Pro model) as much as any:
        20 $1 calls and 20 unpriced ones of the same size moved it 20 points, so 1% is about $2, not $1."""
        for i, model in enumerate(("gpt-6-astra-pro", "gpt-test-unpriced")):
            with self.subTest(model=model):
                self.home = fx.make_home(os.path.join(self._tmp.name, "case-%d" % i))
                self.week(T0 + 21 * DAY + HOUR, 10, 0, 20, T0 + 28 * DAY, 20, unpriced=20, unpriced_model=model)
                self.assertIsNone(rate.compute_rate(self.home, now=NOW))
                self.week(T0 + 14 * DAY + HOUR, 10, 0, 20, T0 + 21 * DAY, 40)      # all priced: $40 / 20 points
                got = rate.compute_rate(self.home, now=NOW)
                self.assertEqual((got["usd_per_pt"], got["windows"]), (2.0, 1))

    def test_a_log_whose_modification_time_stayed_still_counts(self):
        """Codex can leave a log's modification time at its creation while it keeps writing: here it is older than
        the 21 days read, though the log's readings and requests are inside them."""
        start = NOW - 21 * DAY + HOUR
        self.week(start, 8, 2, 22, start + 3 * DAY, 30, mtime=start - 7 * HOUR)
        got = rate.compute_rate(self.home, now=NOW)
        self.assertEqual((got["usd_per_pt"], got["windows"]), (1.5, 1))

    def test_only_the_current_plan_counts(self):
        self.week(T0 + 7 * DAY + HOUR, 10, 5, 17, T0 + 14 * DAY, 24)
        self.week(T0 + 14 * DAY + HOUR, 7, 0, 25, T0 + 21 * DAY, 25, plan="team")
        got = rate.compute_rate(self.home, now=NOW)
        self.assertEqual((got["usd_per_pt"], got["windows"], got["detail"][0]["plan"]), (1.0, 1, "team"))

    def test_time_budget(self):
        self.all_weeks()
        self.assertIsNone(rate.compute_rate(self.home, now=NOW, days=35, budget_s=0))
        # time for two logs (W5's, then W4's): W4 starts after the newest log left unread, so it still counts
        with mock.patch.object(rate.time, "monotonic", side_effect=[0.0, 0.0, 0.0, 100.0]):
            got = rate.compute_rate(self.home, now=NOW, days=35, budget_s=50)
        self.assertEqual((got["usd_per_pt"], got["windows"]), (2.5, 1))

    def test_unreadable_compressed_logs_leave_their_period_out(self):
        self.all_weeks()
        fake = os.path.join(self.home, "sessions", "2026", "11", "07",
                            "rollout-2026-11-07T09-00-00-%s.jsonl.zst" % fx.new_id())
        with open(fake, "wb") as fh:
            fh.write(b"\x28\xb5\x2f\xfd not a real frame")
        os.utime(fake, (T0 + 14 * DAY + 2 * HOUR,) * 2)      # it may hold requests from up to W3
        with mock.patch.object(rollout, "ZSTD_AVAILABLE", False):
            got = rate.compute_rate(self.home, now=NOW, days=35)
        self.assertEqual((got["usd_per_pt"], got["windows"]), (2.5, 1))


if __name__ == "__main__":
    unittest.main()
