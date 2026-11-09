"""monitor.guardrail's file handling (the Claude plugin's guardrail, beside the Codex replay's tests): once off, the
card stays off until you set after_break_enabled back to true by hand; from then on it stays on; deleting the file
starts the guardrail over; the reason doesn't grow night after night; and a file it can't read is left alone."""
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from monitor import guardrail as g  # noqa: E402

UPDATED = "2026-11-08T00:00:00+00:00"


def verdict(on, reason):
    """What one run of the rule says, as main() builds it before settle()."""
    return {"after_break_enabled": on, "reason": reason, "switches": 6, "updated": UPDATED}


class SettleTest(unittest.TestCase):
    def test_no_file_takes_the_rule(self):
        self.assertEqual(g.settle(verdict(True, "OK: x"), {}), verdict(True, "OK: x"))
        out = g.settle(verdict(False, "After a switch, trouble came 4/6 times"), {})
        self.assertEqual((out["after_break_enabled"], out["turned_off"]), (False, True))

    def test_once_off_it_stays_off_and_the_reason_does_not_grow(self):
        prev = g.settle(verdict(False, "After a switch, trouble came 4/6 times"), {})
        for _ in range(3):                                        # three more nights, the rule now saying on
            prev = g.settle(verdict(True, "OK: trouble 1/7"), prev)
            self.assertIs(prev["after_break_enabled"], False)
            self.assertEqual(prev["reason"], "Turned off earlier: After a switch, trouble came 4/6 times")

    def test_a_hand_set_true_sticks(self):
        off = g.settle(verdict(False, "The switches so far cost more than they saved ($-1.00)."), {})
        by_hand = dict(off, after_break_enabled=True)
        for _ in range(3):                                        # the rule keeps saying off
            by_hand = g.settle(verdict(False, "After a switch, trouble came 5/7 times"), by_hand)
            self.assertIs(by_hand["after_break_enabled"], True)
            self.assertTrue(by_hand["kept_on_by_hand"])
            self.assertEqual(by_hand["reason"], g.KEPT_ON + "After a switch, trouble came 5/7 times")

    def test_a_file_rewritten_by_hand_as_just_true_sticks(self):
        out = g.settle(verdict(False, "After a switch, trouble came 4/6 times"), {"after_break_enabled": True})
        self.assertEqual((out["after_break_enabled"], out.get("kept_on_by_hand")), (True, True))

    def test_a_file_from_before_the_mark_flipped_by_hand(self):
        old = {"after_break_enabled": True, "reason": "Turned off earlier: After a switch, trouble came 4/6 times",
               "updated": UPDATED}
        self.assertIs(g.settle(verdict(False, "After a switch, trouble came 4/6 times"), old)["after_break_enabled"], True)

    def test_an_ok_file_the_guardrail_wrote_is_no_hand_set_true(self):
        ok = {"after_break_enabled": True, "reason": "OK: trouble 1/5", "updated": UPDATED}
        out = g.settle(verdict(False, "After a switch, trouble came 4/6 times"), ok)
        self.assertEqual((out["after_break_enabled"], out.get("kept_on_by_hand")), (False, None))

    def test_deleting_the_file_starts_over(self):
        self.assertIs(g.settle(verdict(False, "After a switch, trouble came 4/6 times"), {})["after_break_enabled"], False)


class ReadPrevTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "guardrail.json")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data):
        with open(self.path, "wb") as fh:
            fh.write(data)

    def test_no_file(self):
        self.assertEqual(g.read_prev(self.path), ({}, True))

    def test_utf8_with_or_without_a_byte_order_mark(self):
        for data in (b'{"after_break_enabled": true}', b"\xef\xbb\xbf" + b'{"after_break_enabled": true}\r\n'):
            with self.subTest(bom=data[:3] == b"\xef\xbb\xbf"):
                self.write(data)
                self.assertEqual(g.read_prev(self.path), ({"after_break_enabled": True}, True))

    def test_a_file_it_cant_read_is_left_alone(self):
        for data in ('{"after_break_enabled": true}'.encode("utf-16"), b"{not json", b"[1, 2]"):
            with self.subTest(data=data[:8]):
                self.write(data)
                self.assertEqual(g.read_prev(self.path), ({}, False))


if __name__ == "__main__":
    unittest.main()
