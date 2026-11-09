"""monitor/codex.py (python -m monitor.codex) on synthetic Codex homes: now, spend, replay (verdicts, savings, the
measured inputs, --write, the decision logs and the guardrail rule), handoff, the command line and the nightly step.

Nothing here reads the real ~/.codex or writes into the checkout: CODEX_HOME and the checkout's data folder point into
a temporary directory. Prices are gpt-6-astra's: $10 / $1 per M input / cached input and $50 output up to 272K
input tokens; $20 / $2 / $75 above. With the default Params a new conversation is N = 27K + 80K + 2K = 109K tokens.
"""
import calendar
import contextlib
import io
import json
import math
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from advisor import rollout, rule, state  # noqa: E402
from monitor import codex, nightly  # noqa: E402
from tests import fixtures as fx  # noqa: E402

T0, HOUR, DAY = fx.T0, fx.HOUR, fx.DAY
ASTRA = "gpt-6-astra"
SECRET = "ZQX"                       # in every synthetic request, reply and title: must never reach the output
REQUEST = "Please refactor the ZQX parser"
REPLY = "Done with the ZQX parser."
A = "a0000000-0000-4000-8000-000000000001"
B = "b0000000-0000-4000-8000-000000000002"
C = "c0000000-0000-4000-8000-000000000003"
SUB = "d0000000-0000-4000-8000-0000000000d1"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = fx.make_home(self._tmp.name)
        self.cwd = os.path.join(self._tmp.name, "project")
        self.data = os.path.join(self._tmp.name, "checkout-data")
        for p in (mock.patch.dict(os.environ, {"CODEX_HOME": self.home}), mock.patch.object(codex, "DATA", self.data)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def run_cmd(self, fn, *args, **kw):
        """(stdout, return value) of one command."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            result = fn(*args, **kw)
        return out.getvalue(), result

    def thread(self, steps, tid=None, model=ASTRA, window=258_400, kind="main", parent=None, day=(2026, 11, 7)):
        """A thread from steps. (idle, input, cached, output): you type a request `idle` s after the previous
        response (a new turn), and its response comes 30 s later. "compact": Codex compacts 1 s after the previous
        response."""
        r = fx.Rollout(self.home, tid or fx.new_id(), self.cwd, kind=kind, parent=parent, day=day)
        r.meta(t=r.start)
        t = r.start
        for step in steps:
            if step == "compact":
                t += 1
                r.compacted(t, [REQUEST])
                continue
            idle, inp, cached, out = step
            t += idle
            r.turn(model, window, t=t)
            r.user(REQUEST, t=t)
            t += 30
            r.call(t, inp, cached, out)
            r.complete(t, REPLY)
        r.write()
        return r

    def plugin_dir(self, name="switch-advisor-local"):
        d = os.path.join(self.home, "plugins", "data", name)
        os.makedirs(d, exist_ok=True)
        return d

    def files_text(self, top):
        """Everything written under a folder, as one text."""
        out = []
        for folder, _dirs, names in os.walk(top):
            for name in names:
                with open(os.path.join(folder, name), encoding="utf-8", errors="replace") as fh:
                    out.append(fh.read())
        return "\n".join(out)


# ---- now -----------------------------------------------------------------------------------------------------------

class NowTest(Base):
    def demo_thread(self, title=True):
        """The `switch-advisor demo` case as a log: 410K tokens of context (400K input + 10K output) on gpt-6-astra
        with an 832,200 window, and a reply that asks nothing. 8.2 h later P(miss) is 0.76."""
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 832_200, t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, 400_000, 0, 10_000)
        r.meter(T0 + 31, 64.0, resets_at=T0 + 3 * DAY, five_h=(12.0, T0 + 4 * HOUR))
        r.complete(T0 + 31, REPLY)
        r.write()
        if title:
            fx.add_title(self.home, A, "The ZQX title")
        return T0 + 30 + 29_520          # now: 8.2 h after the last response

    def test_card_numbers_and_wording(self):
        now = self.demo_thread()
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("Codex weekly limit: 64% used, resets", out)
        self.assertIn("5-hour limit: 12% used at the last reading, but it has reset since", out)
        self.assertIn("1% of it: not measured yet", out)
        self.assertIn("The rule's inputs: the plugin's defaults.", out)
        self.assertIn("Codex a0000000 “The ZQX title”", out)
        self.assertIn("410K tokens of context (window 832K)", out)
        self.assertIn("8.2 h ago · gpt-6-astra", out)
        # 410K is above 272K, so $20 / $2 per M: expected 20,000 * $2/M + 390,000 * (0.76 * $20 + 0.24 * $2)/M
        # = 0.04 + 390,000 * 15.68e-6 = 6.1552; warm 410,000 * $2/M = 0.82; N = 109K at $10 / $1: 0.91
        self.assertIn("about $6.16 expected, with P(cache missed) 0.76 after a break this long ($7.84 if it missed); "
                      "$0.82 with a warm cache.", out)
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation.", out)
        self.assertIn("A new conversation would save $5.25 now and typically $20.89 to $40.80 by your next break; "
                      "$4.55 if it re-reads 150K tokens of files.", out)
        self.assertIn("Quality risk: medium (awaiting-answer). The card also weighs your prompt", out)

    def test_hide_titles_and_no_conversation_text(self):
        now = self.demo_thread()
        out, _ = self.run_cmd(codex.show_now, hide_titles=True, home=self.home, now=now)
        self.assertIn("Codex a0000000\n", out)
        self.assertNotIn(SECRET, out)

    def test_only_main_threads_with_a_recent_request(self):
        now = T0 + 10 * HOUR
        self.thread([(0, 150_000, 0, 1_000)], tid=A)                                   # 10 h ago: listed
        self.thread([(0, 150_000, 0, 1_000)], tid=B, day=(2026, 11, 5))               # 2 days ago: not
        self.thread([(0, 150_000, 0, 1_000)], tid=SUB, kind="subagent", parent=A)      # a subagent: not
        out, _ = self.run_cmd(codex.show_now, hours=24, home=self.home, now=now)
        self.assertIn("Codex a0000000", out)
        self.assertNotIn("b0000000", out)
        self.assertNotIn("d0000000", out)
        out, _ = self.run_cmd(codex.show_now, hours=2, home=self.home, now=now)
        self.assertIn("No requests in main Codex conversations in the last 2 h.", out)

    def test_compacted_and_unpriced(self):
        now = T0 + 5 * HOUR
        self.thread([(0, 300_000, 0, 1_000), "compact"], tid=A)
        self.thread([(0, 300_000, 0, 1_000)], tid=B, model="mystery-model")
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("Codex compacted it after its last reply", out)
        self.assertIn("The switch advisor can't price it: no price is known for mystery-model.", out)

    def test_rate_params_and_guardrail(self):
        """A weekly window read 10% -> 30% over 7 h with 20 calls of $1.00 between: $1.00 per 1%. The plugin's
        params.json raises the medium-risk bar to $100, and its guardrail is off."""
        r = fx.Rollout(self.home, C, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 258_400, t=T0)
        r.user(REQUEST, t=T0)
        r.meter(T0 + 60, 10.0, resets_at=T0 + 5 * DAY)
        for i in range(20):
            r.call(T0 + 120 + i * 60, 100_000, 0, 0)
        r.meter(T0 + 7 * HOUR, 30.0, resets_at=T0 + 5 * DAY)
        r.complete(T0 + 7 * HOUR, REPLY)
        r.write()
        now = T0 + 9 * HOUR
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("1% of it is about $1.00 at API prices (from 1 recent weekly window).", out)
        # 100K context, idle 9 h - 1,260 s: P(miss) 0.76; expected 0.76 * 0.82 + 0.24 * 0.10 = 0.6472 = 0.6% at $1/pt
        self.assertIn("about $0.65 expected (0.6% of your weekly limit)", out)
        self.assertIn("The rule now: a note: your prompt goes through, with what it re-reads. No card, because if it "
                      "re-reads 150K tokens of files, it saves under $0.50; the typical saving is under the $5.00 bar "
                      "for medium risk.", out)
        # save_now 0.6472 - 0.91; per_call 0.10 - 0.109 = -0.009; floor 0.6472 - 1.61
        self.assertIn("(negative: it costs more): -$0.26 now, typically -$0.71 to -$0.46 by your next break, -$0.96 "
                      "if it re-reads 150K tokens of files.", out)

        d = self.plugin_dir()
        state.write_json(os.path.join(d, "params.json"), {"bar_medium": 100})
        state.write_json(os.path.join(d, "guardrail.json"), {"after_break_enabled": False})
        now = self.demo_thread(title=False)
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("The rule's inputs: params.json in the plugin's data folder. The guardrail has turned the cards "
                      "off.", out)
        self.assertIn("the typical saving is under the $100.00 bar for medium risk", out)

    def test_a_card_the_guardrail_turned_off_is_a_note(self):
        state.write_json(os.path.join(self.plugin_dir(), "guardrail.json"), {"after_break_enabled": False})
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=self.demo_thread())
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation, but the "
                      "guardrail has turned the cards off, so your prompt goes through with the note instead.", out)

    def test_a_card_while_another_handoff_waits_is_a_note(self):
        """The folder keeps one handoff: while another conversation's is waiting, the hook gives the note."""
        now = self.demo_thread()
        state.put_pending(self.plugin_dir(), self.cwd, "another-session", "text", {}, now - 60)
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation, but another "
                      "conversation's handoff is waiting in this project folder, so your prompt goes through with the "
                      "note instead.", out)
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now + 1801)       # it has expired
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation.", out)

    def test_a_tests_handoff_or_one_ahead_of_the_clock_holds_no_card(self):
        """As in the prompt hook (hook._waiting): a `switch-advisor test` handoff doesn't hold the folder, and nor does
        one made more than a minute ahead of the clock (state.stale), so `now` gives the card beside either."""
        now = self.demo_thread()
        for at, test in ((now - 60, True), (now + 3 * HOUR, False)):
            with self.subTest(test=test):
                state.put_pending(self.plugin_dir(), self.cwd, "another-session", "text", {}, at, test=test)
                out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
                self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation.", out)

    def test_a_card_with_no_typed_request_to_hand_off_is_a_note(self):
        """Older Codex logs mark no text as typed by you, so a handoff would carry no request: the hook gives the
        note instead of the card, and `now` says so."""
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 832_200, t=T0)
        r.harness(REQUEST, t=T0)
        r.call(T0 + 30, 400_000, 0, 10_000)
        r.complete(T0 + 31, REPLY)
        r.write()
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=T0 + 30 + 29_520)
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation, but its log "
                      "holds no typed request for the handoff to carry, so your prompt goes through with the note "
                      "instead.", out)

    def one_call(self, inp, window, tid=A):
        r = fx.Rollout(self.home, tid, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, window, t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, inp, 0, 10_000)
        r.complete(T0 + 31, REPLY)
        r.write()

    def test_the_rule_past_codexs_compaction_point(self):
        """720K on an 832,200 window, 3 h away: Codex compacts it within a call, so the typical saving falls from
        $6.30 (22 calls) to $4.34 (50): under the $5 bar for medium risk, which is why there is no card. The range
        reads low to high, and the worst case by the next break ($0.14 at 50 calls) is under $0.50 too."""
        self.one_call(710_000, 832_200)
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=T0 + 30 + 3 * HOUR)
        self.assertIn("The rule now: a note: your prompt goes through, with what it re-reads. No card, because if it "
                      "re-reads 150K tokens of files, it saves under $0.50 by your next break; the typical saving is "
                      "under the $5.00 bar for medium risk.", out)
        self.assertIn("A new conversation would save $7.84 now and typically $4.34 to $6.30 by your next break; $7.14 "
                      "now and $0.14 to $4.06 by your next break if it re-reads 150K tokens of files.", out)

    def test_a_negative_typical_saving_reads_as_one(self):
        """215K on a 258,400 window after 17.5 h: the saving now is positive, but by 50 calls it is -$1.17."""
        self.one_call(205_000, 258_400)
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=T0 + 30 + 17.5 * HOUR)
        self.assertIn("What a new conversation would save (negative: it costs more): $0.92 now, typically -$1.17 to "
                      "$0.79 by your next break, $0.22 now and -$5.37 to -$1.45 by your next break if it re-reads 150K "
                      "tokens of files.", out)

    def test_a_card_is_held_while_you_chose_to_stay(self):
        """After "send again to stay" the prompt hook doesn't check this conversation until quiet_until (the plugin's
        sessions/<id>.json), card or note: `now` says so."""
        now = self.demo_thread()
        d = self.plugin_dir()
        state.save_session(d, A, {"quiet_until": now + HOUR})
        before = sorted(os.listdir(os.path.join(d, "sessions")))
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation, but you chose to "
                      f"stay, so this conversation isn't checked until {codex.at(now + HOUR)}.", out)
        self.assertEqual(sorted(os.listdir(os.path.join(d, "sessions"))), before)          # read only
        self.one_call(90_000, 258_400, tid=B)                       # a note, 8.2 h after its last response too
        state.save_session(d, B, {"quiet_until": now + 2 * HOUR})
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("The rule now: a note: your prompt goes through, with what it re-reads, but you chose to stay, "
                      f"so this conversation isn't checked until {codex.at(now + 2 * HOUR)}.", out)
        state.save_session(d, A, {"quiet_until": now - 1})          # over
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation.", out)

    def test_a_prompt_soon_after_a_card_counts_as_staying(self):
        """Within resend_window_s of a card (offered_at), the hook takes the next prompt here as "stay"."""
        now = self.demo_thread()
        d = self.plugin_dir()
        state.save_session(d, A, {"offered_at": now - 300})
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation, but a card was "
                      f"shown here at {codex.at(now - 300)}, so a prompt now counts as staying.", out)
        state.write_json(os.path.join(d, "params.json"), {"resend_window_s": 120})     # that folder's own window
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=now)
        self.assertIn("The rule now: a card: it holds your prompt back and offers a new conversation.", out)

    def test_a_turn_still_running_is_left_alone(self):
        """The latest turn made a call and a tool has run for 8.2 h since: the hook stays quiet, whatever is sent."""
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 832_200, t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, 400_000, 0, 10_000)
        r.write()
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=T0 + 30 + 29_520)
        self.assertIn("  Its latest turn is still running: Codex calls the model again when its tool returns, whatever "
                      "you send, so the switch advisor stays quiet.", out)
        self.assertNotIn("The rule now", out)

    def test_a_card_refused_by_less_than_a_cent_says_why(self):
        """gpt-6-astra in Fast mode, 203,999 tokens after 25 h: the worst case is $0.49998, under the $0.50 the rule
        needs, though it reads 0.5 to 4 decimals."""
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 832_200, tier="fast", t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, 193_999, 0, 10_000)
        r.complete(T0 + 31, REPLY)
        r.write()
        out, _ = self.run_cmd(codex.show_now, hours=48, home=self.home, now=T0 + 30 + 25 * HOUR)
        self.assertIn("The rule now: a note: your prompt goes through, with what it re-reads. No card, because if it "
                      "re-reads 150K tokens of files, it saves under $0.50.", out)

    def test_the_figures_are_rounded_once(self):
        """The rule's figures are rounded to 4 decimals in card.numbers; `now` formats the unrounded ones (card.raw)
        when the rule gives them, so its cents are the card's: 1.0450016 reads $1.05, while 1.045, as a float just
        under it, reads $1.04."""
        self.one_call(140_740, 258_400)
        real = rule.after_break

        def with_raw(*args, **kw):
            c = real(*args, **kw)
            return SimpleNamespace(show=c.show, text=c.text, note=c.note, risk=c.risk, flags=c.flags,
                                   numbers=dict(c.numbers, resend_here=1.045),
                                   raw=dict(c.numbers, resend_here=1.0450016))
        with mock.patch.object(codex.rule, "after_break", side_effect=with_raw):
            out, _ = self.run_cmd(codex.show_now, home=self.home, now=T0 + 30 + 8 * HOUR)
        self.assertIn("Your next message here re-reads it: about $1.05 expected", out)
        self.assertEqual(codex.usd(1.045), "$1.04")

    def test_now_gives_the_cards_cents(self):
        """gpt-6-astra, 150,740 tokens after 8 h on a 258,400 window: the expected cost is just above $1.045."""
        card = rule.after_break(150_740, 8 * HOUR, [], rule.Params(), ASTRA, 258_400)
        self.assertEqual(codex.usd(card.numbers["resend_here"]), "$1.04")      # rounded twice
        self.assertIn("$1.05", card.text)
        self.one_call(140_740, 258_400)
        out, _ = self.run_cmd(codex.show_now, home=self.home, now=T0 + 30 + 8 * HOUR)
        self.assertIn("Your next message here re-reads it: about $1.05 expected", out)

    def test_a_saving_that_reads_zero_is_not_negative(self):
        """gpt-6-sol, 177,761 tokens after 24 h on a 1,000,000 window: the worst case is -$0.0025, which reads $0.00,
        so the line doesn't say the switch costs more."""
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn("gpt-6-sol", 1_000_000, t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, 167_761, 0, 10_000)
        r.complete(T0 + 31, REPLY)
        r.write()
        out, _ = self.run_cmd(codex.show_now, hours=48, home=self.home, now=T0 + 30 + 24 * HOUR)
        self.assertIn("A new conversation would save $0.14 now and typically $0.44 to $0.83 by your next break; $0.00 "
                      "if it re-reads 150K tokens of files.", out)

    def test_the_weekly_limit_from_a_reading_older_than_the_hours(self):
        """The last request, with the last meter reading, was 17 h ago; the week it belongs to runs for days yet."""
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 258_400, t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, 50_000, 40_000, 1_000)
        r.meter(T0 + 31, 30.0, resets_at=T0 + 3 * DAY)
        r.complete(T0 + 32, REPLY)
        r.write()
        out, _ = self.run_cmd(codex.show_now, hours=2, home=self.home, now=T0 + 17 * HOUR)
        self.assertIn("Codex weekly limit: 30% used, resets ", out)
        self.assertIn("No requests in main Codex conversations in the last 2 h.", out)

    def test_a_log_whose_modification_time_stayed(self):
        """Codex can leave a log's modification time at its creation while it keeps writing: this one was created
        30 h ago and last answered 20 h ago. It belongs in the last 24 h."""
        r = fx.Rollout(self.home, A, self.cwd, day=(2026, 11, 6))       # starts 24 h before T0
        r.meta(t=r.start)
        r.turn(ASTRA, 258_400, t=r.start)
        r.user(REQUEST, t=r.start)
        r.call(r.start + 30, 100_000, 0, 0)
        r.turn(ASTRA, 258_400, t=r.start + 10 * HOUR)
        r.user(REQUEST, t=r.start + 10 * HOUR)
        r.call(r.start + 10 * HOUR + 30, 100_000, 0, 0)
        r.complete(r.start + 10 * HOUR + 31, REPLY)
        r.write(mtime=r.start)
        now = T0 + 6 * HOUR
        out, _ = self.run_cmd(codex.show_now, hide_titles=True, home=self.home, now=now)
        self.assertIn("Codex a0000000\n", out)
        out, _ = self.run_cmd(codex.show_spend, days=1, home=self.home, now=now)
        total = next(line for line in out.splitlines() if line.startswith("total"))
        self.assertEqual(total.split(), ["total", "$1.00", "$0.00", "$0.00", "$1.00", "25"])


class WhyNoCardTest(unittest.TestCase):
    """why_no_card words the conditions the rule lists as failed (card.failed); a card without that list is checked
    on its unrounded figures (card.raw), else on the rounded ones, and a generic reason covers a miss by less than the
    rounding."""
    WORST = "if it re-reads 150K tokens of files, it saves under $0.50"

    def card(self, **fields):
        numbers = {"p_miss": 1.0, "floor": 0.5, "floor_typical": [1.6, 3.0], "typical": [6.08, 11.4]}
        return SimpleNamespace(**dict({"show": False, "risk": "medium", "numbers": numbers}, **fields))

    def test_the_rules_own_list(self):
        P, idle = rule.Params(), 25 * HOUR
        self.assertEqual(codex.why_no_card(self.card(failed=["floor"]), P, idle), [self.WORST])
        self.assertEqual(codex.why_no_card(self.card(failed=["floor_typical", "typical"]), P, idle),
                         [self.WORST + " by your next break", "the typical saving is under the $5.00 bar for medium "
                                                              "risk"])
        self.assertEqual(codex.why_no_card(self.card(failed=["idle", "miss"]), P, 1800),
                         ["the break is under 1.0 h", "P(cache missed) is under 0.50"])
        self.assertEqual(codex.why_no_card(self.card(risk="high", failed=["risk"]), P, idle),
                         ["the quality risk is high"])
        self.assertEqual(codex.why_no_card(self.card(risk="high", failed=["typical"]), P, idle),
                         ["the typical saving is under the $5.00 bar for high risk", "the quality risk is high"])

    def test_without_the_list(self):
        P, idle = rule.Params(), 25 * HOUR
        raw = {"p_miss": 1.0, "floor": 0.49998, "floor_typical": [1.6, 3.0], "typical": [6.08, 11.4]}
        self.assertEqual(codex.why_no_card(self.card(raw=raw), P, idle), [self.WORST])
        self.assertEqual(codex.why_no_card(self.card(failed=[], raw=raw), P, idle), [self.WORST])
        self.assertEqual(codex.why_no_card(self.card(numbers=raw), P, idle), [self.WORST])
        self.assertEqual(codex.why_no_card(self.card(), P, idle),
                         ["one of its figures falls short of what a card needs by less than a hundredth of a cent"])


# ---- spend ---------------------------------------------------------------------------------------------------------

class SpendTest(Base):
    def test_compressed_logs_that_cant_hold_these_days_are_not_reported(self):
        """On a Python without zstd, `spend` says how many compressed logs it left out, counting only those that may
        hold requests in its days (rollout.zst_bound): one made now from a log last written 10 days ago can't."""
        folder = os.path.join(self.home, "sessions", "2026", "10", "28")
        os.makedirs(folder)
        for mtime in (T0 - 10 * DAY, T0 - DAY):
            path = os.path.join(folder, "rollout-2026-10-28T09-00-00-%s.jsonl.zst" % fx.new_id())
            with open(path, "wb") as fh:
                fh.write(b"\x28\xb5\x2f\xfd not a real frame")
            os.utime(path, (mtime, mtime))
        err = io.StringIO()
        with mock.patch.object(rollout, "ZSTD_AVAILABLE", False), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(err):
            codex.show_spend(days=7, home=self.home, now=T0)
        self.assertEqual(err.getvalue(), "Left out 1 compressed Codex log (.jsonl.zst) that may hold records from "
                                         "the days read: reading it needs Python 3.14 or later.\n")

    def test_columns_totals_and_unknown_prices(self):
        """Every priced call is 100,000 uncached input tokens on gpt-6-astra: $1.00. Main $2, subagents $1, an
        approval review priced like gpt-6-astra $1: $4.00, 100 credits. One call on an unknown model is left out,
        and a log from 8 days ago is outside the 7 days."""
        self.thread([(0, 100_000, 0, 0), (60, 100_000, 0, 0)], tid=A)
        self.thread([(0, 100_000, 0, 0)], tid=SUB, kind="subagent", parent=A)
        self.thread([(0, 100_000, 0, 0)], kind="subagent", parent=A, model=rule_review())
        self.thread([(0, 100_000, 0, 0)], tid=B, model="mystery-model")
        self.thread([(0, 100_000, 0, 0)], tid=C, day=(2026, 10, 30))
        out, _ = self.run_cmd(codex.show_spend, days=7, home=self.home, now=T0 + HOUR)
        total = next(line for line in out.splitlines() if line.startswith("total"))
        self.assertEqual(total.split(), ["total", "$2.00", "$1.00", "$1.00", "$4.00", "100"])
        self.assertIn("priced like gpt-6-astra, the most-used priced model in these days, so that column is an "
                      "estimate.", out)
        self.assertIn("Codex counts 25 credits per $ of API price.", out)
        self.assertIn("(1 call on a model without a known price is left out: mystery-model 1.)", out)
        self.assertNotIn(SECRET, out)

    def test_nothing(self):
        out, _ = self.run_cmd(codex.show_spend, days=3, home=self.home, now=T0)
        self.assertIn("No Codex requests in the last 3 days.", out)


def rule_review():
    return codex.rate.AUTO_REVIEW


# ---- replay --------------------------------------------------------------------------------------------------------

def two_returns(case):
    """Thread A: the demo return. 410K of context after 8.2 h (P(miss) 0.76, window 832,200): the return request
    reads 20K from the cache (it missed), then one warm call. Thread B: 200K after 1.5 h (P(miss) 0.39): the cache
    held; then a call 30 min later, which is no return."""
    case.thread([(0, 400_000, 0, 10_000), (29_520, 412_000, 20_000, 2_000), (60, 414_000, 412_000, 1_000)],
                tid=A, window=832_200)
    case.thread([(0, 195_000, 0, 5_000), (5_400, 201_000, 199_000, 1_000), (1_800, 202_000, 201_000, 500)], tid=B)


class ReplayTest(Base):
    def test_rows(self):
        two_returns(self)
        threads, _stats = rollout.load(self.home)
        rows = {r["thread"]: r for r in codex.replay_returns(threads, rule.Params())}
        self.assertEqual(set(rows), {A, B})
        a, b = rows[A], rows[B]
        self.assertEqual((a["verdict"], a["ctx"], a["calls"], a["missed"]), ("card", 410_000, 2, True))
        self.assertAlmostEqual(a["idle"], 29_520)
        self.assertAlmostEqual(a["p_miss"], 0.76)
        self.assertAlmostEqual(a["expected"], 6.1552, places=4)
        # realized: 20,000 cached at $2/M + 390,000 at $20/M
        self.assertAlmostEqual(a["realized"], 7.84, places=6)
        # the return: 8.03 here against 1.03 there (111K: 20K cached, 91K new, 2K out); the next call 0.939 against
        # 0.181 (113K: 2K new, 111K cached, 1K out)
        self.assertAlmostEqual(a["saving"], 7.758, places=6)
        self.assertEqual((b["verdict"], b["ctx"], b["calls"], b["missed"]), ("note", 200_000, 2, False))
        self.assertAlmostEqual(b["p_miss"], 0.39)
        self.assertAlmostEqual(b["expected"], 0.8318, places=4)
        self.assertAlmostEqual(b["realized"], 0.209, places=6)
        # the return: 0.269 here against 0.97 there (110K, 20K cached); then 0.236 against 0.145
        self.assertAlmostEqual(b["saving"], -0.61, places=6)

    def test_summary_and_no_write(self):
        two_returns(self)
        out, summary = self.run_cmd(codex.replay, home=self.home, now=T0 + DAY)
        self.assertIn("2 returns after a break of 1 h or more in 2 of 2 main threads.", out)
        self.assertIn("The rule's verdicts, with the inputs the plugin uses (the plugin's defaults): 1 card, 0 more if "
                      "the quality risk is low, 1 note, 0 with nothing shown.", out)      # too few samples to measure
        self.assertIn("A switch at each card: $7.76 saved in all by the next return, median $7.76 over 1 return; 0 "
                      "would have cost more.", out)
        self.assertIn("A switch at every return (the most a switch could do, losses included): $7.15 saved in all by "
                      "the next return, median $3.57 over 2 returns; 1 would have cost more.", out)
        # |7.84 - 6.1552| / 7.84 = 0.215 and |0.209 - 0.8318| / 0.209 = 2.980: median 1.597
        self.assertIn("off by 160% at the median (|realized - expected| / realized, 2 returns); in all, $8.05 realized "
                      "against $6.99 expected.", out)
        self.assertIn("The cache missed on 1 of these returns", out)
        self.assertIn("No plugin data folder under CODEX_HOME/plugins/data", out)
        self.assertIn("Run with --write", out)
        self.assertNotIn(SECRET, out)
        self.assertEqual((summary["returns"], summary["cards"], summary["notes"], summary["written"]), (2, 1, 1, []))
        self.assertFalse(os.path.exists(self.data))

    def test_the_verdicts_say_which_inputs_they_use(self):
        """Five more threads open with 300K-token requests, so base_tokens is measured at 300K: a new conversation
        would then start at 382K, and the demo return's card becomes a note. The plugin decides with its defaults until
        --write saves the measured inputs, so the verdicts are given under both."""
        two_returns(self)
        for _ in range(5):
            self.thread([(0, 300_000, 0, 1_000)])
        out, summary = self.run_cmd(codex.replay, home=self.home, now=T0 + DAY)
        self.assertIn("The rule's verdicts, with the inputs measured below, which the plugin uses after --write: 0 "
                      "cards, 0 more if the quality risk is low, 2 notes, 0 with nothing shown.\nWith the inputs it "
                      "uses until then (the plugin's defaults): 1 card, 0 more if the quality risk is low, 1 note, 0 "
                      "with nothing shown.", out)
        self.assertIn("base_tokens              300,000", out)
        self.assertEqual((summary["cards"], summary["notes"]), (0, 2))
        os.makedirs(self.data)
        state.write_json(os.path.join(self.data, "codex_params.json"), {"base_tokens": 300_000})
        out, _ = self.run_cmd(codex.replay, home=self.home, now=T0 + DAY)
        self.assertIn("The rule's verdicts, with the inputs measured below, which the plugin already uses: 0 cards, 0 "
                      "more if the quality risk is low, 2 notes, 0 with nothing shown.", out)
        self.assertNotIn("With the inputs it uses until then", out)

    def test_text_codex_adds_after_a_long_tool_wait_is_no_return(self):
        """The model waited 1.2 h on a tool; when it returned, Codex added its <environment_context> as a user message
        with no metadata, and the model carried on. Nobody sent a prompt, so the prompt hook never ran: no return."""
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 258_400, t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, 150_000, 0, 1_000)
        r.tool_output(T0 + 1.2 * HOUR)
        r.plain_user("<environment_context>synthetic</environment_context>")
        r.world_state()
        r.call(T0 + 1.2 * HOUR + 10, 152_000, 20_000, 1_000)
        r.complete(T0 + 1.2 * HOUR + 11, REPLY)
        r.write()
        threads, _stats = rollout.load(self.home)
        self.assertEqual(codex.replay_returns(threads, rule.Params()), [])

    def test_a_long_wait_on_a_tool_is_no_return(self):
        """The model waited 4 h on its agents (a tool), then carried on: nobody sent a prompt, so the prompt hook never
        ran then. Only the prompt 4 h after that is a return."""
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 258_400, t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, 150_000, 0, 1_000)
        r.tool_output(T0 + 4 * HOUR)
        r.call(T0 + 4 * HOUR + 10, 152_000, 20_000, 1_000)
        r.turn(ASTRA, 258_400, t=T0 + 8 * HOUR)
        r.user(REQUEST, t=T0 + 8 * HOUR)
        r.call(T0 + 8 * HOUR + 30, 155_000, 20_000, 1_000)
        r.complete(T0 + 8 * HOUR + 31, REPLY)
        r.write()
        threads, _stats = rollout.load(self.home)
        rows = codex.replay_returns(threads, rule.Params())
        self.assertEqual([(r["at"], r["calls"]) for r in rows], [(T0 + 8 * HOUR, 1)])
        _values, samples, _curve, _recent = codex.measure(threads, now=T0 + DAY)
        self.assertEqual(samples["calls_between_returns"], 1)

    def test_unpriced_and_compacted_returns_are_left_out(self):
        self.thread([(0, 300_000, 0, 1_000), (3 * HOUR, 301_000, 20_000, 1_000)], tid=A, model="mystery-model")
        self.thread([(0, 300_000, 0, 1_000), "compact", (3 * HOUR, 40_000, 0, 1_000)], tid=B)
        threads, _stats = rollout.load(self.home)
        rows = {r["thread"]: r for r in codex.replay_returns(threads, rule.Params())}
        self.assertEqual((rows[A]["verdict"], rows[B]["verdict"]), ("unpriced", "compacted"))
        out, _ = self.run_cmd(codex.replay, home=self.home, now=T0 + DAY)
        self.assertIn("(1 right after a compaction and 1 on models without a price are left out)", out)
        self.assertIn("A switch at each card: none, since no such card would have been shown.", out)


class MeasureTest(Base):
    """Six main threads (k = 0..5) built so every input is measured; window 30,000, gpt-6-astra.
        c0                first request, 20,000 + 1,000k
        c1   60 s later   +2,000, warm
        c1a  600 s later  +500, missed for k = 0 and 1 (cached 0), else warm
        c1b  600 s later  +500, warm
        c2   21,700 s     +1,000, missed: 10,000 + 500k cached; a return after 2 h (and after 6 h)
        c3   60 s later   +1,000, warm; then Codex compacts at 25,000 + 1,000k tokens
        c4   21 s later   39,000 + 100k, the first request after the compaction
        c5   60 s later   +3,000, warm; then k more calls 60 s apart, +500 each
    A subagent with a huge first request must not count."""

    def build(self, n=6):
        for k in range(n):
            c0 = 20_000 + 1_000 * k
            c1, c1a = c0 + 2_000, c0 + 2_500
            c4 = 39_000 + 100 * k
            steps = [(0, c0, 0, 1_000), (60, c1, c0, 1_000), (600, c1a, 0 if k < 2 else c1, 1_000),
                     (600, c0 + 3_000, c1a, 1_000), (21_700, c0 + 4_000, 10_000 + 500 * k, 1_000),
                     (60, c0 + 5_000, c0 + 4_000, 1_000), "compact", (20, c4, 0, 1_000), (60, c4 + 3_000, c4, 1_000)]
            size = c4 + 3_000
            for _ in range(k):
                steps.append((60, size + 500, size, 1_000))
                size += 500
            self.thread(steps, window=30_000, day=(2026, 10, 1 + 3 * k))
        self.thread([(0, 999_000, 0, 1_000)], kind="subagent", parent=A, window=30_000)

    def test_values_and_samples(self):
        """All of it is more than 14 days old, so the four inputs that follow Codex's own behaviour use the newest 5
        of their 6 samples (threads k = 1..5); the others use all the logs."""
        self.build()
        threads, _stats = rollout.load(self.home)
        newest = {}
        values, samples, curve_n, recent = codex.measure(threads, now=T0 + 40 * DAY, newest=newest)
        self.assertEqual(recent, set())                               # all of it more than 14 days old
        self.assertEqual(sorted(newest), ["base_shared", "base_tokens", "compact_share", "compacted_tokens"])
        self.assertEqual(values["base_tokens"], 23_000)               # 21K..25K, the middle
        self.assertEqual(values["base_shared"], 11_500)               # 10,500..12,500 cached after 6 h
        self.assertEqual(values["calls_between_returns"], [5, 6])     # 4 + k calls from c2 to the end
        self.assertEqual(values["growth_per_call"], 500)              # 27 of 51 deltas are 500
        self.assertEqual(values["compact_share"], 0.933)              # 26K..30K of 30K
        self.assertEqual(values["compacted_tokens"], 39_300)
        self.assertEqual(samples, {"base_tokens": 5, "base_shared": 5, "calls_between_returns": 6,
                                   "growth_per_call": 51, "compact_share": 5, "compacted_tokens": 5})
        self.assertEqual(curve_n, [33, 12, 0, 0, 0, 0, 0, 0, 0, 6, 0, 0])
        curve = values["miss_curve"]
        # 0-5 min: 0 of 33 missed; 5-15 min: 2 of 12; 15-30 min keeps 0.167 (its default 0.11 is lower); the
        # 6-12 h bucket has 6 requests, so its default 0.76 stays
        self.assertEqual([p for _upper, p in curve], [0.0, 0.167, 0.167, 0.19, 0.19, 0.25, 0.39, 0.58, 0.64, 0.76,
                                                      0.92, 1.0])
        fresh = codex.fresh_params(values, samples, curve_n)
        self.assertEqual(fresh["miss_curve"][1], [900, 0.167])
        self.assertEqual(fresh["miss_curve"][-1], [None, 1.0])
        P = rule.params_from(json.loads(json.dumps(fresh)))
        self.assertEqual((P.base_tokens, P.base_shared, P.calls_between_returns, P.growth_per_call, P.compact_share,
                          P.compacted_tokens), (23_000, 11_500, (5, 6), 500, 0.933, 39_300))
        self.assertEqual(P.miss_curve[-1], (math.inf, 1.0))

    def test_few_samples_keep_the_defaults(self):
        self.build(n=3)
        threads, _stats = rollout.load(self.home)
        values, samples, curve_n, _recent = codex.measure(threads, now=T0 + 40 * DAY)
        fresh = codex.fresh_params(values, samples, curve_n)
        self.assertEqual(samples["base_tokens"], 3)
        self.assertNotIn("base_tokens", fresh)
        self.assertIn("growth_per_call", fresh)                          # 3 threads still give 21 deltas
        out, _ = self.run_cmd(codex.replay, home=self.home, now=T0 + 40 * DAY)
        self.assertIn("(3 threads, all logs; default 27,000 stays)", out)

    def compactions(self, share, after, days):
        """One thread per day: a request of `share` of a 100,000 window, Codex compacts, then `after` tokens."""
        for day in days:
            self.thread([(0, int(share * 100_000), 0, 1_000), "compact", (20, after, 0, 1_000)], window=100_000,
                        day=day)

    def test_where_codex_compacts_now(self):
        """Codex changed where it compacts: a month ago at 0.85 of the window, leaving 39K; in the last two weeks at
        0.94, leaving 27K. Those inputs follow the recent compactions once there are 5 of them, and the newest 5 until
        then."""
        self.compactions(0.85, 39_000, [(2026, 10, d) for d in range(1, 8)])
        self.compactions(0.94, 27_000, [(2026, 11, d) for d in range(1, 5)])
        threads, _stats = rollout.load(self.home)
        values, samples, _curve, recent = codex.measure(threads, now=T0)
        self.assertEqual((values["compact_share"], values["compacted_tokens"], samples["compact_share"]),
                         (0.94, 27_000, 5))           # 4 recent ones are too few: the newest 5, one of them from Oct 7
        self.assertNotIn("compact_share", recent)
        self.compactions(0.94, 27_000, [(2026, 11, 5)])
        threads, _stats = rollout.load(self.home)
        values, samples, _curve, recent = codex.measure(threads, now=T0)
        self.assertEqual((values["compact_share"], values["compacted_tokens"], samples["compact_share"]),
                         (0.94, 27_000, 5))
        self.assertEqual(recent, {"base_tokens", "compact_share", "compacted_tokens"})
        self.assertEqual(values["base_tokens"], 94_000)
        out, _ = self.run_cmd(codex.replay, home=self.home, now=T0)
        self.assertIn("compact_share              0.940  input before an auto-compaction, as a share of the window (5 "
                      "compactions, last 14 days)", out)

    def test_after_a_break_the_inputs_follow_the_newest_compactions(self):
        """Two weeks or more without Codex: the last 14 days hold no compaction. The inputs that follow Codex's own
        behaviour then come from the newest 5 samples (Codex's latest behaviour, 0.94 and 27K), not from all the logs,
        whose older compactions (0.85 and 39K) are the majority; the period they span is printed and saved."""
        self.compactions(0.85, 39_000, [(2026, 9, d) for d in range(1, 11)])
        self.compactions(0.94, 27_000, [(2026, 10, d) for d in range(1, 6)])
        threads, _stats = rollout.load(self.home)
        everything = codex.codex_timed(threads)
        self.assertEqual(codex.q([v for _t, v in everything["compact_share"]], 0.5), 0.85)    # what all would give
        newest = {}
        values, samples, _curve, recent = codex.measure(threads, now=T0, newest=newest)
        self.assertEqual(recent, set())
        self.assertEqual((values["compact_share"], values["compacted_tokens"], values["base_tokens"],
                          samples["compact_share"]), (0.94, 27_000, 94_000, 5))
        first, last = calendar.timegm((2026, 10, 1, 9, 0, 31)), calendar.timegm((2026, 10, 5, 9, 0, 31))
        self.assertEqual(newest["compact_share"], (first, last))       # each compaction 31 s into its thread
        self.assertEqual(sorted(newest), ["base_tokens", "compact_share", "compacted_tokens"])
        out, _ = self.run_cmd(codex.replay, write=True, home=self.home, now=T0)
        self.assertIn("compact_share              0.940  input before an auto-compaction, as a share of the window (5 "
                      f"compactions, the newest: {codex.at(first, '%Y-%m-%d')} to {codex.at(last, '%Y-%m-%d')})", out)
        self.assertIn("else their newest 5; the others all your logs:", out)
        saved = state.read_json(os.path.join(self.data, "codex_params.json"))
        self.assertEqual((saved["compact_share"], saved["compacted_tokens"], saved["measured_recent"]),
                         (0.94, 27_000, []))
        self.assertEqual(saved["measured_newest"]["compact_share"], [first, last])

    def test_a_past_switch_starts_and_compacts_as_codex_did_then(self):
        """The replay's new conversation at a return in September starts and compacts as Codex did in September, not
        as it does today."""
        self.compactions(0.85, 39_000, [(2026, 9, d) for d in range(1, 16)])
        self.compactions(0.94, 27_000, [(2026, 11, 1 + d % 5) for d in range(15)])
        threads, _stats = rollout.load(self.home)
        timed = codex.codex_timed(threads)
        self.assertEqual({k: len(v) for k, v in timed.items()},
                         {"base_tokens": 30, "base_shared": 0, "compact_share": 30, "compacted_tokens": 30})
        P = rule.Params()
        september, november = codex._then(P, timed, T0 - 60 * DAY), codex._then(P, timed, T0)
        self.assertEqual((september.compact_share, september.compacted_tokens, september.base_tokens),
                         (0.85, 39_000, 85_000))
        self.assertEqual((november.compact_share, november.compacted_tokens, november.base_tokens),
                         (0.94, 27_000, 94_000))
        self.assertEqual(september.base_shared, P.base_shared)                # no samples: as given
        few = {k: v[:4] for k, v in timed.items()}
        self.assertEqual(codex._then(P, few, T0), P)                           # too few: the inputs as given
        # a return in September: what a switch would have saved follows September's inputs
        self.thread([(0, 100_000, 0, 1_000), (3 * HOUR, 101_000, 20_000, 1_000), (60, 111_000, 101_000, 1_000),
                     (60, 121_000, 111_000, 1_000)], tid=A, window=150_000, day=(2026, 9, 8))
        threads, _stats = rollout.load(self.home)
        P = rule.params_from({"compact_share": 0.94, "compacted_tokens": 27_000, "base_tokens": 94_000})
        (row,) = [r for r in codex.replay_returns(threads, P) if r["thread"] == A]
        cs = sorted(threads[A].calls, key=lambda c: c.end)
        then = codex._then(P, codex.codex_timed(threads), cs[1].end)
        self.assertEqual((then.compact_share, then.compacted_tokens, then.base_tokens), (0.85, 39_000, 85_000))
        self.assertAlmostEqual(row["saving"], codex._switch_saving(cs, 1, 4, 101_000, then)[0], places=6)
        self.assertNotAlmostEqual(row["saving"], codex._switch_saving(cs, 1, 4, 101_000, P)[0], places=2)


class WriteAndGuardrailTest(Base):
    def switches(self, d, went_back, stayed_away):
        """Switch events from old threads. The old thread of a switch that went back got a request 2 h after it;
        the others got one only 2 days later."""
        t = T0 + 5 * HOUR
        for i in range(went_back + stayed_away):
            old = "e%07d-0000-4000-8000-000000000000" % i
            later = 2 * HOUR if i < went_back else 2 * DAY
            self.thread([(0, 50_000, 0, 1_000), (5 * HOUR - 30 + later, 51_000, 50_000, 1_000)], tid=old)
            state.log(d, "switched", t=t, session=fx.new_id(), from_session=old, numbers={}, cwd_key="k")

    def test_write_params_and_guardrail_off(self):
        two_returns(self)
        d = self.plugin_dir()
        state.write_json(os.path.join(d, "params.json"), {"bar_low": 3.0})
        state.log(d, "offer", t=T0, session=A, model=ASTRA, risk="medium", flags=["awaiting-answer"], numbers={},
                  cwd_key="k")
        state.log(d, "kept", t=T0 + 60, session=A)
        state.log(d, "note", t=T0 + 120, session=B, model=ASTRA, numbers={})
        state.log(d, "expired", t=T0 + 180, from_session=A, cwd_key="k")
        self.switches(d, went_back=2, stayed_away=2)
        out, summary = self.run_cmd(codex.replay, write=True, home=self.home, now=T0 + 10 * DAY)
        self.assertIn("The plugin's decisions: 1 card shown, 1 stay, 4 switches, 1 note, 1 unused handoff, 0 tests and "
                      "demos.", out)
        self.assertIn("2 of the 4 switches went back to the old conversation within 24 h.", out)
        self.assertIn("Guardrail: half or more of the switches went back, so the cards are now turned off "
                      "(guardrail.json)", out)
        guard = state.read_json(os.path.join(d, "guardrail.json"))
        self.assertIs(guard["after_break_enabled"], False)
        self.assertEqual((guard["switches"], guard["went_back"]), (4, 2))
        self.assertTrue(state.guardrail_off(d))
        params = state.read_json(os.path.join(d, "params.json"))
        self.assertEqual(params["bar_low"], 3.0)                       # a field set by hand stays
        self.assertIn("growth_per_call", params)                       # 8 deltas over the 6 threads
        self.assertIn("measured_at", params)
        mine = state.read_json(os.path.join(self.data, "codex_params.json"))
        self.assertEqual(mine["growth_per_call"], params["growth_per_call"])
        self.assertTrue(summary["guardrail_off"])
        self.assertEqual(len(summary["written"]), 3)
        self.assertNotIn(SECRET, self.files_text(self.data) + self.files_text(d))

    def test_guardrail_needs_four_switches_and_half_going_back(self):
        for i, (back, away, off) in enumerate(((1, 3, False), (3, 0, False), (2, 2, True), (4, 1, True))):
            with self.subTest(back=back, away=away):
                self.home = fx.make_home(os.path.join(self._tmp.name, "case-%d" % i))
                d = self.plugin_dir()
                self.switches(d, back, away)
                out, summary = self.run_cmd(codex.replay, write=True, home=self.home, now=T0 + 10 * DAY)
                self.assertEqual(os.path.exists(os.path.join(d, "guardrail.json")), off)
                self.assertEqual(summary["guardrail_off"], off)
                if not off:
                    self.assertIn("Guardrail: the cards stay on.", out)

    def test_write_deletes_handoffs_nobody_used(self):
        """The hooks delete an unused handoff only when one of them next runs, which can be the next day: the
        nightly replay --write deletes it too. Without --write it stays."""
        d = self.plugin_dir()
        now = T0 + 10 * DAY
        state.put_pending(d, self.cwd, "s-old", "handoff text", {}, now - HOUR)
        self.run_cmd(codex.replay, home=self.home, now=now)
        self.assertIsNotNone(state.peek_pending(d, self.cwd))
        self.run_cmd(codex.replay, write=True, home=self.home, now=now)
        self.assertIsNone(state.peek_pending(d, self.cwd))
        self.assertEqual(os.listdir(os.path.join(d, "pending")), [])
        self.assertEqual([e["event"] for e in codex.read_decisions([d])], ["expired"])

    def test_without_write_nothing_is_written(self):
        d = self.plugin_dir()
        self.switches(d, went_back=4, stayed_away=0)
        before = sorted(os.listdir(d))
        out, _ = self.run_cmd(codex.replay, home=self.home, now=T0 + 10 * DAY)
        self.assertIn("so the cards should be turned off; run with --write to do it.", out)
        self.assertEqual(sorted(os.listdir(d)), before)
        self.assertFalse(os.path.exists(self.data))

    def test_a_guardrail_turned_off_stays_off(self):
        d = self.plugin_dir()
        state.write_json(os.path.join(d, "guardrail.json"), {"after_break_enabled": False, "reason": "by hand"})
        self.switches(d, went_back=0, stayed_away=4)
        out, summary = self.run_cmd(codex.replay, write=True, home=self.home, now=T0 + 10 * DAY)
        self.assertIn("Guardrail: the cards are turned off (guardrail.json)", out)
        self.assertEqual(state.read_json(os.path.join(d, "guardrail.json"))["reason"], "by hand")
        self.assertTrue(summary["guardrail_off"])

    def test_cards_you_turned_back_on_stay_on(self):
        """You set after_break_enabled to true in guardrail.json after the replay turned the cards off: the replay
        leaves them on, though the switches still meet its rule. Deleting the file instead starts it over."""
        d = self.plugin_dir()
        self.switches(d, went_back=4, stayed_away=0)
        path = os.path.join(d, "guardrail.json")
        self.run_cmd(codex.replay, write=True, home=self.home, now=T0 + 10 * DAY)
        self.assertTrue(state.guardrail_off(d))
        guard = dict(state.read_json(path), after_break_enabled=True)          # turned back on by hand
        state.write_json(path, guard)
        for day in (11, 12):                                                   # the next nights
            out, summary = self.run_cmd(codex.replay, write=True, home=self.home, now=T0 + day * DAY)
            self.assertIn("Guardrail: you turned the cards back on (after_break_enabled is true in guardrail.json), "
                          "so it leaves them on, though half or more of the switches went back.", out)
            self.assertEqual(state.read_json(path), guard)
            self.assertFalse(summary["guardrail_off"])
            self.assertFalse(state.guardrail_off(d))
        os.remove(path)
        out, summary = self.run_cmd(codex.replay, write=True, home=self.home, now=T0 + 13 * DAY)
        self.assertTrue(state.guardrail_off(d))
        self.assertTrue(summary["guardrail_off"])

    def test_a_hand_edit_saved_from_powershell_is_honoured(self):
        """Windows PowerShell's Set-Content -Encoding UTF8 writes a byte order mark: the true still reads, so the cards
        stay on. Out-File writes UTF-16, which can't be read: the replay then leaves the file alone rather than
        overwrite what may be your edit."""
        d = self.plugin_dir()
        self.switches(d, went_back=4, stayed_away=0)
        path = os.path.join(d, "guardrail.json")
        for name, data in (("bom", b"\xef\xbb\xbf" + b'{"after_break_enabled": true}\r\n'),
                           ("utf-16", '{"after_break_enabled": true}'.encode("utf-16"))):
            with self.subTest(name):
                with open(path, "wb") as fh:
                    fh.write(data)
                out, summary = self.run_cmd(codex.replay, write=True, home=self.home, now=T0 + 10 * DAY)
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(), data)                       # untouched
                self.assertFalse(summary["guardrail_off"])
                self.assertFalse(state.guardrail_off(d))
                if name == "utf-16":
                    self.assertIn("guardrail.json can't be read as JSON, so the guardrail leaves it as it is.", out)

    def test_switches_tried_after_a_test_are_left_out(self):
        """`switch-advisor test` makes a handoff so that a switch can be tried; going back after it is no sign the
        cards mislead, so those switches (logged with "test": true) never count toward the guardrail."""
        d = self.plugin_dir()
        self.switches(d, went_back=1, stayed_away=3)
        for i in range(4):                      # four test switches, all going back 2 h later
            old = "f%07d-0000-4000-8000-000000000000" % i
            self.thread([(0, 50_000, 0, 1_000), (7 * HOUR - 30, 51_000, 50_000, 1_000)], tid=old)
            state.log(d, "test", t=T0 + 5 * HOUR - 60, session=old)
            state.log(d, "switched", t=T0 + 5 * HOUR, session=fx.new_id(), from_session=old, numbers={},
                      cwd_key="k", test=True)
        out, summary = self.run_cmd(codex.replay, write=True, home=self.home, now=T0 + 10 * DAY)
        self.assertEqual((summary["switches"], summary["went_back"], summary["guardrail_off"]), (4, 1, False))
        self.assertIn("0 cards shown, 0 stays, 4 switches, 0 notes, 0 unused handoffs, 4 tests and demos (4 switches "
                      "after a test, which the guardrail leaves out).", out)
        self.assertIn("1 of the 4 switches went back to the old conversation within 24 h.", out)
        self.assertFalse(os.path.exists(os.path.join(d, "guardrail.json")))

    def test_went_back_reads_times_of_both_kinds(self):
        old = SimpleNamespace(calls=[SimpleNamespace(start=None, end=T0 + HOUR)])
        events = [{"event": "switched", "t": T0, "from_session": A}, {"event": "switched", "t": T0, "from_session": B},
                  {"event": "kept", "t": T0, "session": A}]
        self.assertEqual(codex.went_back(events, {A: old}), (2, 1))
        self.assertEqual(codex._epoch(T0 * 1000), T0)
        self.assertEqual(codex._epoch(fx.iso(T0)), T0)
        self.assertIsNone(codex._epoch("soon"))
        self.assertIsNone(codex._epoch(True))


class HooksToReplayTest(Base):
    def test_the_decisions_the_hooks_log_are_the_ones_the_replay_reads(self):
        """The plugin's own hooks write the decision log: a card in conversation A, a switch to a new conversation B,
        then A used again 2 h later, which the replay counts as a switch that went back."""
        from advisor import handoff, hook
        d = self.plugin_dir()
        env = {"CODEX_HOME": self.home, "PLUGIN_DATA": d}
        r = fx.Rollout(self.home, A, self.cwd)
        r.meta(t=T0)
        r.turn(ASTRA, 832_200, t=T0)
        r.user(REQUEST, t=T0)
        r.call(T0 + 30, 400_000, 390_000, 10_000)
        r.complete(T0 + 31, REPLY)
        path = r.write()
        away = T0 + 30 + 29_520                  # the demo case: 410K tokens after 8.2 h
        prompt = "Carry on with the ZQX parser: stream the records, and keep the public interface as it is now."
        with mock.patch.object(handoff, "git_status", return_value=[]):
            out = hook.on_prompt_submit({"session_id": A, "transcript_path": path, "cwd": self.cwd,
                                         "prompt": prompt}, now=away, env=env)
            self.assertEqual(set(out), {"decision", "reason"})
            started = hook.on_session_start({"session_id": B, "transcript_path": None, "cwd": self.cwd,
                                             "source": "startup"}, now=away + 300, env=env)
        self.assertIn(REQUEST, started["hookSpecificOutput"]["additionalContext"])
        r.turn(ASTRA, 832_200, t=away + 2 * HOUR)
        r.user(REQUEST, t=away + 2 * HOUR)
        r.call(away + 2 * HOUR + 30, 410_000, 20_000, 1_000)
        r.write()
        out, summary = self.run_cmd(codex.replay, home=self.home, now=away + DAY)
        self.assertIn("The plugin's decisions: 1 card shown, 0 stays, 1 switch, 0 notes, 0 unused handoffs, 0 tests "
                      "and demos.", out)
        self.assertEqual((summary["switches"], summary["went_back"]), (1, 1))
        self.assertNotIn(SECRET, out)


# ---- handoff -------------------------------------------------------------------------------------------------------

class HandoffTest(Base):
    def build(self):
        self.thread([(0, 50_000, 0, 1_000)], tid=B, day=(2026, 11, 5))     # an older main thread
        self.thread([(0, 50_000, 0, 1_000)], tid=A, day=(2026, 11, 6))     # the newest main thread
        self.thread([(0, 50_000, 0, 1_000)], tid=SUB, kind="subagent", parent=A)   # newer, but a subagent

    def test_picks_the_newest_main_thread(self):
        self.build()
        calls, folders = [], []
        stub = SimpleNamespace(git_status=lambda cwd: folders.append(cwd) or ["M  a.py"],
                               build=lambda f, files, now=None: calls.append((f.thread, files)) or "HANDOFF")
        with mock.patch.object(codex, "_handoff", return_value=stub):
            out, code = self.run_cmd(codex.show_handoff, home=self.home)
            self.assertEqual((out, code, calls[-1], folders[-1]), ("HANDOFF\n", 0, (A, ["M  a.py"]), self.cwd))
            out, code = self.run_cmd(codex.show_handoff, session=B, home=self.home)
            self.assertEqual((code, calls[-1]), (0, (B, ["M  a.py"])))

    def test_the_real_handoff_text_and_nothing_saved(self):
        self.build()
        before = sorted(os.path.join(f, n) for f, _d, ns in os.walk(self._tmp.name) for n in ns)
        out, code = self.run_cmd(codex.show_handoff, home=self.home)
        self.assertEqual(code, 0)
        self.assertIn("continues work from an earlier Codex conversation", out)
        self.assertIn(REQUEST, out)
        self.assertIn(REPLY, out)
        after = sorted(os.path.join(f, n) for f, _d, ns in os.walk(self._tmp.name) for n in ns)
        self.assertEqual(after, before)

    def test_missing(self):
        out, code = self.run_cmd(codex.show_handoff, session="no-such-thread", home=self.home)
        self.assertEqual((out.strip(), code), ("No Codex log for thread no-such-thread.", 1))
        out, code = self.run_cmd(codex.show_handoff, home=self.home)
        self.assertEqual((out.strip(), code), ("No main Codex conversation in the logs.", 1))


# ---- command line and the nightly step -----------------------------------------------------------------------------

class CommandLineTest(Base):
    def test_now_is_the_default(self):
        with mock.patch.object(codex, "show_now") as now:
            self.assertEqual(codex.main([]), 0)
            now.assert_called_with(24, False)
            codex.main(["--hide-titles", "--hours", "6"])
            now.assert_called_with(6.0, True)

    def test_commands_on_an_empty_home(self):
        out, code = self.run_cmd(codex.main, ["spend", "--days", "3"])
        self.assertEqual(code, 0)
        self.assertIn("No Codex requests in the last 3 days.", out)
        out, code = self.run_cmd(codex.main, ["replay"])
        self.assertIn("Returns to a main conversation after a break of 1 h or more", out)
        out, code = self.run_cmd(codex.main, ["handoff", "--session", "nope"])
        self.assertEqual(code, 1)


class NightlyTest(Base):
    def test_skipped_without_codex_logs(self):
        os.rmdir(os.path.join(self.home, "sessions"))
        with mock.patch.object(codex, "replay") as replay:
            self.assertEqual(nightly.codex_replay(), [])
            replay.assert_not_called()

    def test_runs_replay_with_write(self):
        summary = {"returns": 7, "cards": 2, "notes": 3, "switches": 1, "went_back": 0, "guardrail_off": False,
                   "written": []}
        with mock.patch.object(codex, "replay", return_value=summary) as replay:
            self.assertEqual(nightly.codex_replay(), ["codex replay: ok (7 returns, 2 cards, 1 switch; cards on)"])
            replay.assert_called_once_with(write=True)

    def test_a_failure_is_logged(self):
        with mock.patch.object(codex, "replay", side_effect=RuntimeError("synthetic")):
            lines = nightly.codex_replay()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("codex replay: FAILED\n"))
        self.assertIn("RuntimeError", lines[0])


if __name__ == "__main__":
    unittest.main()
