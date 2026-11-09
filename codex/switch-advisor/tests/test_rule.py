"""Tests for the after-break rule (advisor/rule.py): the hand-computed numbers in the spec (section 3.4), when a card
or a note is shown, the card's wording, the quality flags, and reading params.json."""
import itertools
import json
import math
import os
import re
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor import rule  # noqa: E402
from advisor.rule import Flag, Params, after_break, level_of, p_miss, params_from, quality_flags  # noqa: E402

ASTRA = "gpt-6-astra"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
LONG_PROMPT = ("Please refactor the parser so that it streams records instead of loading the whole log, and keep the "
               "public interface unchanged.")      # 100+ characters, names no file, starts with no short reply word
RUNNING = "agents started in this conversation are still running, and their results come back here, not to the new conversation"
AWAITING = "your message answers its last reply; the handoff carries that reply, not what was said before it"
CARRIES = "the handoff carries your first and latest requests, the last reply and the uncommitted files"
ASKED_ONE = ("your message answers the question it asked you; the handoff carries that question and its options, not "
             "what was said before it")
QUESTION = {"title": "Which database should the service use?", "options": ["Postgres", "SQLite"]}
OTHER_QUESTION = {"title": "Should the old tables be dropped?", "options": ["Yes", "No, keep them"]}


def facts(asks_you=False, last_reply="Done: the parser streams now.", agents_running=0, agents_spawned=0,
          open_questions=None):
    """The Facts fields quality_flags reads (rollout.Facts has more). open_questions: left out when None, as in Facts
    from before the field."""
    f = SimpleNamespace(asks_you=asks_you, last_reply=last_reply, agents_running=agents_running,
                        agents_spawned=agents_spawned)
    if open_questions is not None:
        f.open_questions = open_questions
    return f


def awaiting():
    return quality_flags(facts(), "switch-advisor demo", 0)


def demo(flags=None, P=None, **kw):
    """The demo case of section 3.4: 410K tokens, 8.2 h idle, gpt-6-astra, an 832,200 window, $5 per 1%, meter 64%."""
    args = dict(window=832_200, tier=None, meter_pct=64, label="DEMO · ")
    args.update(kw)
    return after_break(410_000, 29_520, awaiting() if flags is None else flags, P or Params(usd_per_pt=5.0), ASTRA, **args)


def paragraphs(card):
    return card.text.split("\n\n")


def all_flag_sets():
    """Every distinct list of flags quality_flags returns over the cases that matter."""
    seen = {}
    for asks, reply, running, spawned, prompt, files, asked in itertools.product(
            (False, True), ("Done.", "x" * 1600 + "?", "y" * 3999 + "?"), (0, 2), (0, 1, 4), ("yes", LONG_PROMPT),
            (0, 1, 3, 13), (None, [QUESTION], [QUESTION, OTHER_QUESTION])):
        flags = quality_flags(facts(asks, reply, running, spawned, asked), prompt, files)
        seen[tuple((f.key, f.text) for f in flags)] = flags
    return list(seen.values())


class ParamsTest(unittest.TestCase):
    def test_defaults_are_the_spec_values(self):
        P = Params()
        self.assertEqual((P.base_tokens, P.base_shared, P.reread_tokens, P.reread_worst, P.handoff_tokens),
                         (27_000, 20_000, 80_000, 150_000, 2_000))
        self.assertEqual(P.calls_between_returns, (22, 50))
        self.assertEqual((P.growth_per_call, P.compact_share, P.compacted_tokens), (900, 0.86, 39_000))
        self.assertEqual((P.min_idle_s, P.min_miss, P.bar_low, P.bar_medium, P.floor_min), (3600, 0.5, 2.0, 5.0, 0.5))
        self.assertEqual((P.note_min_usd, P.note_min_miss), (0.5, 0.25))
        self.assertEqual((P.quiet_s, P.resend_window_s, P.handoff_ttl_s), (43_200, 900, 1800))
        self.assertIsNone(P.usd_per_pt)
        self.assertEqual(len(P.miss_curve), 12)
        self.assertEqual(P.miss_curve[-1], (math.inf, 1.0))
        probs = [p for _, p in P.miss_curve]
        self.assertEqual(probs, sorted(probs))

    def test_nothing_usable_gives_the_defaults(self):
        for d in (None, {}, [], "params", 3, [["base_tokens", 1]]):
            with self.subTest(d=d):
                self.assertEqual(params_from(d), Params())

    def test_unknown_keys_are_ignored(self):
        P = params_from({"base_tokens": 30_000, "measured_at": "2026-11-07", "threads": 41, "code": {"base_tokens": 1}})
        self.assertEqual(P.base_tokens, 30_000)
        self.assertEqual(P, Params(base_tokens=30_000))

    def test_wrong_types_and_values_are_ignored(self):
        bad = {
            "base_tokens": "30000", "base_shared": None, "reread_tokens": True, "reread_worst": [150_000],
            "handoff_tokens": -1, "growth_per_call": float("nan"), "compacted_tokens": float("inf"),
            "min_idle_s": {"s": 1}, "quiet_s": 10 ** 400, "bar_low": "2", "bar_medium": -5.0, "floor_min": False,
            "note_min_usd": float("-inf"), "min_miss": 1.5, "note_min_miss": -0.1, "compact_share": 0,
            "calls_between_returns": [10], "miss_curve": "steep", "usd_per_pt": 0,
        }
        self.assertEqual(params_from(bad), Params())
        self.assertEqual(params_from({"compact_share": 1.2}).compact_share, 0.86)
        self.assertEqual(params_from({"usd_per_pt": -4.2}).usd_per_pt, None)
        self.assertEqual(params_from({"usd_per_pt": "4.2"}).usd_per_pt, None)

    def test_valid_values_take_the_field_type(self):
        P = params_from({"base_tokens": 26_907.6, "growth_per_call": 3_186, "bar_low": 3, "compact_share": 0.9,
                         "min_miss": 1, "usd_per_pt": 4})
        self.assertEqual(P.base_tokens, 26_908)
        self.assertIsInstance(P.base_tokens, int)
        self.assertEqual(P.growth_per_call, 3_186)
        self.assertEqual(P.bar_low, 3.0)
        self.assertIsInstance(P.bar_low, float)
        self.assertEqual((P.compact_share, P.min_miss, P.usd_per_pt), (0.9, 1.0, 4.0))

    def test_calls_between_returns(self):
        self.assertEqual(params_from({"calls_between_returns": [22, 50]}).calls_between_returns, (22, 50))
        self.assertEqual(params_from({"calls_between_returns": [50, 22]}).calls_between_returns, (22, 50))
        self.assertEqual(params_from({"calls_between_returns": [10, 20, 30]}).calls_between_returns, (10, 20))
        self.assertEqual(params_from({"calls_between_returns": (12.5, 31)}).calls_between_returns, (12.5, 31))
        for v in ([10], ["a", 2], [-1, 5], [True, 4], 22, None, [None, 3]):
            with self.subTest(v=v):
                self.assertEqual(params_from({"calls_between_returns": v}).calls_between_returns, (22, 50))

    def test_miss_curve_from_a_list_of_pairs(self):
        P = params_from({"miss_curve": [[600, 0.1], [3600, 0.3], [None, 0.9]]})
        self.assertEqual(P.miss_curve, ((600.0, 0.1), (3600.0, 0.3), (math.inf, 0.9)))
        self.assertEqual([p_miss(s, P) for s in (0, 599, 600, 3599, 3600, 10 ** 9)], [0.1, 0.1, 0.3, 0.3, 0.9, 0.9])

    def test_miss_curve_survives_a_json_round_trip(self):
        """json.dumps writes the open-ended bound as Infinity, and json.loads reads it back."""
        d = json.loads(json.dumps({"miss_curve": Params().miss_curve}))
        self.assertEqual(params_from(d).miss_curve, Params().miss_curve)

    def test_miss_curve_is_sorted_and_made_non_decreasing(self):
        P = params_from({"miss_curve": [[3600, 0.5], [900, 0.2], [1800, 0.1]]})
        self.assertEqual(P.miss_curve, ((900.0, 0.2), (1800.0, 0.2), (3600.0, 0.5)))

    def test_malformed_miss_curves_are_ignored(self):
        for v in ([], {}, "steep", [[600]], [[600, 0.1, 2]], [[600, 1.5]], [[600, -0.1]], [[600, "x"]], [["x", 0.1]],
                  [[0, 0.1]], [[-60, 0.1]], [[600, 0.1], [600, 0.2]], [[600, 0.1], None], [[600, True]],
                  [[float("nan"), 0.1]], [[600, float("nan")]]):
            with self.subTest(v=v):
                self.assertEqual(params_from({"miss_curve": v}).miss_curve, Params().miss_curve)


class PMissTest(unittest.TestCase):
    def test_bucket_edges(self):
        """Each bucket holds idle times below its upper bound: a bound itself belongs to the next bucket."""
        P = Params()
        cases = [(-60, 0.0), (0, 0.0), (299.9, 0.0), (300, 0.07), (899, 0.07), (900, 0.11), (1799, 0.11),
                 (1800, 0.19), (2700, 0.19), (3599, 0.19), (3600, 0.25), (5399, 0.25), (5400, 0.39), (7199, 0.39),
                 (7200, 0.58), (10_800, 0.58), (14_399, 0.58), (14_400, 0.64), (21_599, 0.64), (21_600, 0.76),
                 (28_800, 0.76), (29_520, 0.76), (43_199, 0.76), (43_200, 0.92), (86_399, 0.92), (86_400, 1.0),
                 (10 ** 9, 1.0)]
        for idle, expected in cases:
            with self.subTest(idle=idle):
                self.assertEqual(p_miss(idle, P), expected)

    def test_beyond_the_last_bound_the_last_value_holds(self):
        P = params_from({"miss_curve": [[600, 0.1], [3600, 0.4]]})
        self.assertEqual(p_miss(3600, P), 0.4)
        self.assertEqual(p_miss(10 ** 7, P), 0.4)


class QualityFlagsTest(unittest.TestCase):
    def keys(self, *args):
        return [f.key for f in quality_flags(*args)]

    @unittest.skipUnless(os.path.isfile(os.path.join(REPO, "mod", "switch-advisor", "hooks", "rule.ts")),
                         "the Claude mod's rule.ts is not in this checkout")
    def test_patterns_are_the_ones_in_rule_ts(self):
        with open(os.path.join(REPO, "mod", "switch-advisor", "hooks", "rule.ts"), encoding="utf-8") as fh:
            src = fh.read()
        for name, compiled in (("SHORT_REPLY", rule.SHORT_REPLY), ("NAMES_FILE", rule.NAMES_FILE)):
            with self.subTest(name=name):
                m = re.search(r"^const %s = /(.+)/i$" % name, src, re.M)
                self.assertIsNotNone(m)
                self.assertEqual(compiled.pattern, m.group(1))
                self.assertTrue(compiled.flags & re.I)

    def test_names_file(self):
        for text in ("open src/app.tsx", "C:\\work\\Notes.MD please", "update build.gradle", "see config.yml",
                     "see config.yaml", "edit app.kts", "data/report.csv", "the spec.pdf"):
            with self.subTest(text=text):
                self.assertIsNotNone(rule.NAMES_FILE.search(text))
        for text in ("notes.mdx", "version 1.2", "file.", "the .py files", "readme"):
            with self.subTest(text=text):
                self.assertIsNone(rule.NAMES_FILE.search(text))

    def test_short_reply(self):
        for text in ("yes", "  Yes please", "OK then", "okay", "go ahead and", "Do it", "next", "sure, why not"):
            with self.subTest(text=text):
                self.assertIsNotNone(rule.SHORT_REPLY.search(text))
        for text in ("yesterday", "okayish", "nextjs", "not yet", "please continue", "goon"):
            with self.subTest(text=text):
                self.assertIsNone(rule.SHORT_REPLY.search(text))

    def test_nothing_to_flag(self):
        self.assertGreaterEqual(len(LONG_PROMPT), 100)
        flags = quality_flags(facts(), LONG_PROMPT, 0)
        self.assertEqual(flags, [])
        self.assertEqual(level_of(flags), "low")

    def test_a_short_prompt_answers_the_last_reply(self):
        flags = quality_flags(facts(), "yes", 0)
        self.assertEqual([(f.key, f.level, f.text) for f in flags], [("awaiting-answer", "medium", AWAITING)])
        self.assertEqual(level_of(flags), "medium")
        self.assertEqual(self.keys(facts(), "   ", 0), ["awaiting-answer"])

    def test_a_long_prompt_that_starts_with_a_short_reply_word(self):
        self.assertEqual(self.keys(facts(), "Yes, and " + LONG_PROMPT, 0), ["awaiting-answer"])

    def test_a_prompt_naming_a_file_is_not_an_answer(self):
        self.assertEqual(self.keys(facts(), "fix app.py", 0), [])
        self.assertEqual(self.keys(facts(), "ok, then update docs/README.md: " + LONG_PROMPT, 0), [])

    def test_a_reply_that_asks_you_something(self):
        self.assertEqual(self.keys(facts(asks_you=True, last_reply="Which one?"), "fix app.py: " + LONG_PROMPT, 0),
                         ["awaiting-answer"])

    def test_a_long_question_is_high_and_replaces_awaiting_answer(self):
        flags = quality_flags(facts(asks_you=True, last_reply="x" * 1600 + "?"), "yes", 0)
        self.assertEqual([(f.key, f.level) for f in flags], [("long-question", "high")])
        self.assertEqual(flags[0].text, "its last reply asks you something and runs 1,601 characters, and the handoff "
                                        "keeps the first 1,500")
        self.assertEqual(level_of(flags), "high")

    def test_the_reply_is_measured_after_stripping(self):
        f = facts(asks_you=True, last_reply="   " + "x" * 1499 + "?\n\n  ")
        self.assertEqual(self.keys(f, LONG_PROMPT, 0), ["awaiting-answer"])
        self.assertEqual(self.keys(facts(asks_you=False, last_reply="x" * 3000), LONG_PROMPT, 0), [])

    def test_a_reply_cut_at_4000_characters_reads_at_least(self):
        flags = quality_flags(facts(asks_you=True, last_reply="y" * 3999 + "?"), LONG_PROMPT, 0)
        self.assertIn("runs at least 4,000 characters", flags[0].text)

    def test_the_whole_length_from_rollout_is_exact(self):
        """rollout.facts keeps 4,000 characters of the reply and measures the whole of it in last_reply_len."""
        from advisor import rollout
        f = rollout.Facts(last_reply="y" * 3999 + "?", asks_you=True, last_reply_len=6_234)
        self.assertIn("runs 6,234 characters, and the handoff keeps the first 1,500",
                      quality_flags(f, "yes", 0)[0].text)
        f.last_reply_len = 0                    # not measured (a Facts made by hand): the cut text is all there is
        self.assertIn("runs at least 4,000 characters", quality_flags(f, "yes", 0)[0].text)
        self.assertEqual(rule.TEXT_KEPT, rollout.TEXT_MAX)

    def test_running_agents_are_high(self):
        flags = quality_flags(facts(agents_running=2, agents_spawned=3), LONG_PROMPT, 0)
        self.assertEqual([(f.key, f.level, f.text) for f in flags], [("agents-running", "high", RUNNING)])

    def test_agents_used(self):
        flags = quality_flags(facts(agents_spawned=3), LONG_PROMPT, 0)
        self.assertEqual([(f.key, f.level) for f in flags], [("agents-used", "medium")])
        self.assertEqual(flags[0].text, "this conversation coordinated 3 agents; their reports are only in this "
                                        "conversation, and the handoff keeps its last reply")
        one = quality_flags(facts(agents_spawned=1), LONG_PROMPT, 0)[0]
        self.assertEqual(one.text, "this conversation coordinated 1 agent; its report is only in this conversation, "
                                   "and the handoff keeps the last reply")

    def test_uncommitted_files(self):
        self.assertEqual(quality_flags(facts(), LONG_PROMPT, 0), [])
        one = quality_flags(facts(), LONG_PROMPT, 1)
        self.assertEqual([(f.key, f.level, f.text) for f in one],
                         [("uncommitted", "info", "1 file has uncommitted changes; the new conversation sees it with "
                                                  "git status")])
        twelve = quality_flags(facts(), LONG_PROMPT, 12)
        self.assertEqual([(f.key, f.level, f.text) for f in twelve],
                         [("uncommitted", "info", "12 files have uncommitted changes; the new conversation sees them "
                                                  "with git status")])
        many = quality_flags(facts(), LONG_PROMPT, 13)
        self.assertEqual([(f.key, f.level, f.text) for f in many],
                         [("many-files", "medium", "13 files have uncommitted changes, and the handoff lists 12")])
        self.assertEqual(level_of(one), "low")
        self.assertEqual(level_of(many), "medium")

    def test_flags_come_in_a_fixed_order(self):
        f = facts(asks_you=True, last_reply="z" * 2000 + "?", agents_running=1, agents_spawned=1)
        self.assertEqual(self.keys(f, "yes", 40), ["agents-running", "long-question", "many-files"])
        f = facts(asks_you=True, last_reply="Which one?", agents_spawned=2)
        self.assertEqual(self.keys(f, "yes", 2), ["awaiting-answer", "agents-used", "uncommitted"])

    def test_missing_facts_and_prompt(self):
        empty = SimpleNamespace(asks_you=None, last_reply=None, agents_running=None, agents_spawned=None)
        self.assertEqual(self.keys(empty, None, None), ["awaiting-answer"])
        self.assertEqual(self.keys(object(), "", 0), ["awaiting-answer"])
        self.assertEqual(self.keys(empty, LONG_PROMPT, 0), [])

    def test_level_of(self):
        info, medium, high = Flag("i", "info", "t"), Flag("m", "medium", "t"), Flag("h", "high", "t")
        self.assertEqual(level_of([]), "low")
        self.assertEqual(level_of([info]), "low")
        self.assertEqual(level_of([info, medium]), "medium")
        self.assertEqual(level_of([medium, high, info]), "high")


class OpenQuestionsTest(unittest.TestCase):
    """A question asked in Codex's question panel (request_user_input_async), still waiting for an answer: the handoff
    carries it with its options (handoff.build), and the awaiting-answer flag says so."""

    def test_the_flag_says_the_handoff_carries_the_question(self):
        f = facts(asks_you=True, last_reply="I sent you a question in the panel.", open_questions=[QUESTION])
        flags = quality_flags(f, LONG_PROMPT, 0)
        self.assertEqual([(f.key, f.level, f.text) for f in flags], [("awaiting-answer", "medium", ASKED_ONE)])
        self.assertEqual(quality_flags(f, "Postgres", 0)[0].text, ASKED_ONE)

    def test_several_questions_or_none_with_options(self):
        two = quality_flags(facts(asks_you=True, open_questions=[QUESTION, OTHER_QUESTION]), "Postgres", 0)
        self.assertEqual(two[0].text, "your message answers the 2 questions it asked you; the handoff carries those "
                                      "questions and their options, not what was said before them")
        bare = quality_flags(facts(asks_you=True, open_questions=[{"title": "Which one?", "options": []}]), "yes", 0)
        self.assertEqual(bare[0].text, "your message answers the question it asked you; the handoff carries that "
                                       "question, not what was said before it")

    def test_an_open_question_counts_even_without_asks_you(self):
        flags = quality_flags(facts(asks_you=False, open_questions=[QUESTION]), LONG_PROMPT, 0)
        self.assertEqual([(f.key, f.level, f.text) for f in flags], [("awaiting-answer", "medium", ASKED_ONE)])

    def test_without_open_questions_the_flag_is_as_before(self):
        for asked in (None, [], [{"title": "", "options": []}], ["not a question"], [{"title": 5, "options": "x"}],
                      "x", {"title": "Which?"}):
            with self.subTest(asked=asked):
                flags = quality_flags(facts(asks_you=True, last_reply="Which one?", open_questions=asked), LONG_PROMPT,
                                      0)
                self.assertEqual([(f.key, f.level, f.text) for f in flags], [("awaiting-answer", "medium", AWAITING)])

    def test_a_long_reply_that_asks_is_still_a_long_question(self):
        """The handoff keeps 1,500 characters of the last reply whether or not it carries a panel question: when the
        reply itself asks something, its question may come after them."""
        f = facts(asks_you=True, last_reply="x" * 1600 + "?", open_questions=[QUESTION])
        self.assertEqual([(f.key, f.level) for f in quality_flags(f, "yes", 0)], [("long-question", "high")])
        from advisor import rollout
        cut = rollout.Facts(asks_you=True, reply_asks=True, last_reply="x" * 4000, last_reply_len=6_000,
                            open_questions=[QUESTION])           # the reply's question is past the 4,000 kept
        self.assertEqual([(f.key, f.level, f.text) for f in quality_flags(cut, "yes", 0)],
                         [("long-question", "high", "its last reply asks you something and runs 6,000 characters, "
                                                    "and the handoff keeps the first 1,500")])
        self.assertEqual(rule._ASKS.pattern, rollout._ASKS.pattern)

    def test_a_long_reply_that_asks_nothing_with_a_panel_question(self):
        """A question asked in Codex's question panel is carried whole, so after a long reply that asks nothing itself
        the flag is the medium awaiting-answer one, which says so, not long-question (high, which keeps the card away
        and says the reply asks something)."""
        from advisor import rollout
        plan = "The migration plan, step by step. " * 60
        for f in (facts(asks_you=True, last_reply=plan, open_questions=[QUESTION]),        # reply_asks not measured
                  rollout.Facts(asks_you=True, reply_asks=False, last_reply=plan, last_reply_len=len(plan),
                                open_questions=[QUESTION]),
                  rollout.Facts(asks_you=True, reply_asks=False, last_reply="y" * 3999 + "?", last_reply_len=6_000,
                                open_questions=[QUESTION])):     # the 4,000 kept end with "?", the whole reply not
            with self.subTest(f=type(f).__name__):
                flags = quality_flags(f, "Postgres", 0)
                self.assertEqual([(x.key, x.level, x.text) for x in flags], [("awaiting-answer", "medium", ASKED_ONE)])
        # without open questions, asks_you of Facts made by hand is the reply's own question, as before
        self.assertEqual(self.keys(facts(asks_you=True, last_reply=plan), "Postgres"), ["long-question"])

    def test_a_panel_question_the_handoff_cant_read_is_a_high_risk(self):
        """The log asks you something (asks_you) that is neither a question the handoff can carry (open_questions:
        the call's arguments aren't in a shape the plugin reads) nor a question in the reply: the new conversation
        wouldn't see it, whatever the reply's length, so no card; also with no reply at all."""
        from advisor import rollout
        unread = ("unread-question", "high", "it asked you something in the question panel that the handoff can't "
                                             "read, so the new conversation wouldn't see the question")
        plan = "The migration plan, step by step. " * 60
        for name, f in (("long reply", rollout.Facts(asks_you=True, reply_asks=False, last_reply=plan,
                                                     last_reply_len=len(plan))),
                        ("short reply", rollout.Facts(asks_you=True, reply_asks=False, last_reply="Sent.",
                                                      last_reply_len=5)),
                        ("no reply", rollout.Facts(asks_you=True)),
                        ("no reply, made by hand", facts(asks_you=True, last_reply=None))):
            with self.subTest(name):
                self.assertEqual([(x.key, x.level, x.text) for x in quality_flags(f, "Postgres", 0)], [unread])
        # without unread_calls (Facts made by hand), a reply that asks, or a question the handoff can carry, keeps the
        # flags as they were
        asks = rollout.Facts(asks_you=True, reply_asks=True, last_reply="Which one?", last_reply_len=10)
        self.assertEqual(self.keys(asks, LONG_PROMPT), ["awaiting-answer"])
        carried = rollout.Facts(asks_you=True, reply_asks=False, last_reply=plan, last_reply_len=len(plan),
                                open_questions=[QUESTION])
        self.assertEqual(self.keys(carried, "Postgres"), ["awaiting-answer"])
        self.assertEqual(self.keys(rollout.Facts(), LONG_PROMPT), [])

    def test_a_call_the_plugin_cant_read_is_high_whatever_else_asks(self):
        """rollout.facts counts the open question calls it can't read (unread_calls): one of those is the high flag
        also when the reply asks something itself or another open question is readable, as the handoff carries
        neither that call nor a note of what it asked; with none, the flags are as without it."""
        from advisor import rollout
        for name, f in (("the reply asks", rollout.Facts(asks_you=True, reply_asks=True, last_reply="Which one?",
                                                          last_reply_len=10, unread_calls=1)),
                        ("another question is readable", rollout.Facts(
                            asks_you=True, reply_asks=False, last_reply="Sent.", last_reply_len=5,
                            open_questions=[QUESTION], open_question_count=1, unread_calls=1)),
                        ("only that call", rollout.Facts(asks_you=True, reply_asks=False, last_reply="Sent.",
                                                         last_reply_len=5, unread_calls=2))):
            with self.subTest(name):
                self.assertEqual(self.keys(f, "Postgres"), ["unread-question"])
                self.assertEqual(quality_flags(f, "Postgres", 0)[0].level, "high")
        none = rollout.Facts(asks_you=True, reply_asks=False, last_reply="Sent.", last_reply_len=5,
                             open_questions=[QUESTION], open_question_count=1, unread_calls=0)
        self.assertEqual(self.keys(none, "Postgres"), ["awaiting-answer"])
        long_ask = rollout.Facts(asks_you=True, reply_asks=True, last_reply="x" * 1600 + "?", last_reply_len=1601,
                                 unread_calls=1)                # both high: the long question is named first
        self.assertEqual(self.keys(long_ask, "yes"), ["long-question"])
        for odd in (True, "1", 1.0, None):
            with self.subTest(unread_calls=odd):                # not a count: the rule for Facts made by hand
                f = rollout.Facts(asks_you=True, reply_asks=True, last_reply="Which one?", last_reply_len=10,
                                  unread_calls=odd)
                self.assertEqual(self.keys(f, LONG_PROMPT), ["awaiting-answer"])

    def test_more_questions_than_the_handoff_carries(self):
        """Facts.open_question_count counts the questions still open, also those older than the newest 3 the
        handoff carries; the flag says how many it leaves out."""
        from advisor import rollout
        three = [{"title": "Q%d?" % i, "options": ["a", "b"]} for i in (3, 4, 5)]
        for count, text in ((5, "your message answers the 5 questions it asked you; the handoff carries the newest 3 "
                                "and their options, not the 2 earlier ones or what was said before them"),
                            (4, "your message answers the 4 questions it asked you; the handoff carries the newest 3 "
                                "and their options, not the earlier one or what was said before them"),
                            (3, "your message answers the 3 questions it asked you; the handoff carries those "
                                "questions and their options, not what was said before them"),
                            (0, "your message answers the 3 questions it asked you; the handoff carries those "
                                "questions and their options, not what was said before them")):
            with self.subTest(count=count):
                f = rollout.Facts(asks_you=True, reply_asks=False, last_reply="Sent.", last_reply_len=5,
                                  open_questions=three, open_question_count=count)
                self.assertEqual([(x.key, x.text) for x in quality_flags(f, "yes", 0)], [("awaiting-answer", text)])
                self.assertEqual(rule.open_question_total(f), max(count, 3))
        one = rollout.Facts(asks_you=True, reply_asks=False, open_questions=[{"title": "Which?", "options": []}],
                            open_question_count=2)
        self.assertEqual(quality_flags(one, "yes", 0)[0].text, "your message answers the 2 questions it asked you; the "
                         "handoff carries the newest one, not the earlier one or what was said before them")
        for odd in (None, True, "5", 2.0, -1):
            with self.subTest(open_question_count=odd):
                f = facts(asks_you=True, open_questions=three)
                f.open_question_count = odd
                self.assertEqual(rule.open_question_total(f), 3)

    def keys(self, f, prompt):
        return [x.key for x in quality_flags(f, prompt, 0)]

    def test_the_shape_and_the_caps(self):
        """The newest 3 questions; a title up to 500 characters, at most 8 options, each up to 120 characters on one
        line, a cut marked; anything that isn't a question is left out."""
        many = [{"title": "Question %d?" % i, "options": ["a"]} for i in range(5)]
        self.assertEqual([t for t, _o in rule.open_questions(facts(open_questions=many))],
                         ["Question 2?", "Question 3?", "Question 4?"])
        self.assertEqual(rule.open_questions(facts(open_questions=[{"title": "t" * 500, "options": ["o" * 120]}])),
                         [("t" * 500, ["o" * 120])])                   # whole, at the caps
        long = {"title": "t" * 600, "options": ["o%d " % i + "x" * 200 for i in range(10)]}
        ((title, options),) = rule.open_questions(facts(open_questions=[long]))
        self.assertEqual(title, "t" * 500 + " […]")
        self.assertEqual(options, [("o%d " % i + "x" * 200)[:120] + " […]" for i in range(8)])
        odd = {"title": "  Pick one:\n  ", "options": ["first\nline", "", "  ", 3, None, {"label": "x"}, "last"]}
        self.assertEqual(rule.open_questions(facts(open_questions=[odd, 7, None, {"options": ["only options"]}])),
                         [("Pick one:", ["first line", "last"]), ("", ["only options"])])
        for f in (object(), facts(), facts(open_questions="x"), facts(open_questions={"title": "x"})):
            self.assertEqual(rule.open_questions(f), [])
        from advisor import rollout
        self.assertEqual(rule.open_questions(rollout.Facts()), [])


class DurationTest(unittest.TestCase):
    def test_never_longer_than_the_time_it_states(self):
        """The hook keeps to the seconds exactly, so the card rounds down: seconds under 2 minutes, whole minutes,
        and hours only when they are exact."""
        cases = {0: "0 seconds", 1: "1 second", 60: "60 seconds", 90: "90 seconds", 119: "119 seconds",
                 120: "2 minutes", 150: "2 minutes", 900: "15 minutes", 900.9: "15 minutes", 1800: "30 minutes",
                 3599: "59 minutes", 3600: "1 hour", 3660: "61 minutes", 5400: "1.5 hours", 7140: "119 minutes",
                 7200: "2 hours", 9000: "2.5 hours", 9060: "151 minutes", 43_200: "12 hours", 86_400: "24 hours"}
        for s, text in cases.items():
            with self.subTest(s=s):
                self.assertEqual(rule._duration(s), text)


class AfterBreakTest(unittest.TestCase):
    def assertNumbers(self, card, expected):
        for key, value in expected.items():
            with self.subTest(key=key):
                if isinstance(value, list):
                    self.assertEqual(len(card.numbers[key]), len(value))
                    for got, want in zip(card.numbers[key], value):
                        self.assertAlmostEqual(got, want, places=4)
                elif isinstance(value, float):
                    self.assertAlmostEqual(card.numbers[key], value, places=4)
                else:
                    self.assertEqual(card.numbers[key], value)

    def test_demo_case_numbers(self):
        """410K is above 272K: long-context rates ($20 / $2 per M) for this request, short ones for N and NW."""
        card = demo()
        self.assertNumbers(card, {
            "resend_warm": 0.82,               # 410,000 x $2/M
            "resend_here": 6.1552,             # 0.04 + 390,000 x (0.76 x $20/M + 0.24 x $2/M)
            "new_conv": 0.91,                  # N = 109K: 0.02 + 89,000 x $10/M
            "new_worst": 1.61,                 # NW = 179K: 0.02 + 159,000 x $10/M
            "save_now": 5.2452,
            "floor": 4.5452,
            "calls_left": 339.6578,            # (0.86 x 832,200 - 410,000) / 900
            "per_call": 0.711,                 # 0.82 - 0.109
            "typical": [20.8872, 40.7952],     # 5.2452 + 22 x 0.711, 5.2452 + 50 x 0.711
            "p_miss": 0.76, "ctx": 410_000, "idle_s": 29_520, "model": ASTRA,
        })
        self.assertAlmostEqual(card.numbers["calls_left"], 339.66, places=2)
        self.assertTrue(card.show)
        self.assertEqual(card.note, "")
        self.assertEqual(card.risk, "medium")
        self.assertEqual(card.flags, ["awaiting-answer"])

    def test_demo_card_reads_like_the_spec_example(self):
        expected = (
            "DEMO · Switch advisor: you were away 8.2 h, so this conversation's prompt cache has most likely expired "
            "(it had after 76% of breaks this long). Sending here re-reads about 410K tokens: about $7.84 if the cache "
            "expired (1.6% of your weekly limit; you're at 64%), against $0.82 with a warm cache, so about $6.16 on "
            "average.\n\n"
            "A new conversation starts from a handoff built from this one on your computer, with no model call, and "
            "with about 109K tokens once it has re-read its files: about $0.91. That saves about $5.25 now and "
            "typically $20.89–$40.80 by your next break. Even if it re-reads 150K tokens of files, it saves about "
            "$4.55.\n\n"
            "Quality risk: medium. The handoff carries your first and latest requests, the last reply and the "
            "uncommitted files. It may miss: your message answers its last reply; the handoff carries that reply, "
            "not what was said before it.\n\n"
            "To switch: start a new conversation in this project folder and send your prompt there within 30 minutes; "
            "it starts from the handoff. To stay: send your prompt here again within 15 minutes, and this "
            "conversation won't be checked again for 12 hours.")
        self.assertEqual(demo().text, expected)

    def test_the_card_and_the_note_give_the_same_cost_of_a_miss(self):
        """The note the hook gives instead of this card (the guardrail, another card's handoff waiting, no typed
        request) and the card say the same of the same moment: $7.84 if the cache expired, 1.6% of the weekly limit,
        and $0.82 with a warm cache. The card adds the average, $6.16, which its saving is counted from: $6.16 - $0.91
        = $5.25."""
        card = demo(label="")
        note = demo(P=Params(usd_per_pt=5.0, bar_low=math.inf, bar_medium=math.inf), label="").note
        self.assertEqual(note, "Switch advisor: back after 8.2 h, this message likely re-reads about 410K tokens without "
                               "the cache (it had expired after 76% of breaks this long): about $7.84 (1.6% of your "
                               "weekly limit; you're at 64%), against $0.82 with a warm cache.")
        self.assertTrue(paragraphs(card)[0].endswith(
            "Sending here re-reads about 410K tokens: about $7.84 if the cache expired (1.6% of your weekly limit; "
            "you're at 64%), against $0.82 with a warm cache, so about $6.16 on average."))
        self.assertIn("about $0.91. That saves about $5.25 now", card.text)
        self.assertAlmostEqual(card.numbers["resend_here"] - card.numbers["new_conv"], card.numbers["save_now"], places=4)

    def test_no_average_when_it_is_the_cost_of_a_miss(self):
        """A day away, the cache had missed after every break that long: the average is the cost of a miss."""
        card = after_break(410_000, 86_400, [], Params(), ASTRA, 832_200)
        self.assertEqual(card.numbers["p_miss"], 1.0)
        self.assertTrue(paragraphs(card)[0].endswith(
            "Sending here re-reads about 410K tokens: about $7.84 if the cache expired, against $0.82 with a warm "
            "cache."))
        self.assertIn("That saves about $6.93 now", card.text)       # $7.84 - $0.91

    def test_200k_after_8_hours_gets_a_note_not_a_card(self):
        """Short rates: re-sending here costs less than a new conversation would in the worst case."""
        card = after_break(200_000, 28_800, [], Params(), ASTRA, 258_400)
        self.assertNumbers(card, {
            "resend_warm": 0.2,                # 200,000 x $1/M
            "resend_cold": 1.82,               # 0.02 + 180,000 x $10/M
            "resend_here": 1.4312,             # 0.02 + 180,000 x 7.84e-6
            "new_conv": 0.91, "new_worst": 1.61,
            "save_now": 0.5212,
            "floor": -0.1788,                  # 1.4312 - 1.61
            "p_miss": 0.76,
        })
        self.assertFalse(card.show)
        self.assertEqual(card.note, "Switch advisor: back after 8.0 h, this message likely re-reads about 200K tokens "
                                    "without the cache (it had expired after 76% of breaks this long): about $1.82, "
                                    "against $0.20 with a warm cache.")
        self.assertTrue(card.text)            # built anyway, for `switch-advisor test`

    def test_the_note_gives_the_cost_of_a_miss_next_to_its_chance(self):
        """An hour away, the cache has missed after 25% of such breaks. The note's figure is what the message costs
        when it did ($1.82), not the expectation over both cases ($0.61), which would read as the cost of a miss."""
        card = after_break(200_000, 3600, [], Params(usd_per_pt=1.0), ASTRA, 258_400)
        # expected: 0.25 x 1.82 + 0.75 x 0.2
        self.assertNumbers(card, {"resend_cold": 1.82, "resend_here": 0.605, "p_miss": 0.25})
        self.assertEqual(card.note, "Switch advisor: back after 1.0 h, this message may re-read about 200K tokens "
                                    "without the cache (it had expired after 25% of breaks this long): about $1.82 "
                                    "(1.8% of your weekly limit), against $0.20 with a warm cache.")
        quiet = after_break(200_000, 3600, [], Params(note_min_usd=0.61), ASTRA, 258_400)
        self.assertEqual(quiet.note, "")      # whether a note is worth showing still goes by the expected cost

    def test_30_minutes_idle_says_nothing(self):
        card = after_break(410_000, 1800, awaiting(), Params(usd_per_pt=5.0), ASTRA, 832_200, None, 64)
        self.assertEqual(card.numbers["p_miss"], 0.19)     # 1,800 is not below 1,800: the 2,700 bucket
        self.assertFalse(card.show)
        self.assertEqual(card.note, "")

    def test_the_first_note_comes_after_an_hour(self):
        before = after_break(400_000, 3599, [], Params(), ASTRA, 832_200)
        self.assertEqual((before.show, before.note), (False, ""))
        at = after_break(400_000, 3600, [], Params(), ASTRA, 832_200)
        self.assertEqual(at.numbers["p_miss"], 0.25)
        self.assertAlmostEqual(at.numbers["resend_here"], 2.51, places=4)    # 0.25 x 7.64 + 0.75 x 0.80
        self.assertFalse(at.show)                           # a card needs P(miss) >= 0.5
        self.assertEqual(at.note, "Switch advisor: back after 1.0 h, this message may re-read about 400K tokens "
                                  "without the cache (it had expired after 25% of breaks this long): about $7.64, "
                                  "against $0.80 with a warm cache.")

    def test_high_risk_never_blocks(self):
        flags = quality_flags(facts(agents_running=1), "yes", 0)
        self.assertEqual([f.key for f in flags], ["agents-running", "awaiting-answer"])
        card = demo(flags, label="")
        self.assertFalse(card.show)
        self.assertEqual(card.risk, "high")
        self.assertEqual(card.note,
                         "Switch advisor: back after 8.2 h, this message likely re-reads about 410K tokens without the "
                         "cache (it had expired after 76% of breaks this long): about $7.84 (1.6% of your weekly "
                         "limit; you're at 64%), against $0.82 with a warm cache. A new conversation would save about "
                         "$5.25 on average, but the quality risk is high: " + RUNNING + ".")
        lenient = Params(usd_per_pt=5.0, bar_low=0, bar_medium=0, floor_min=0, min_miss=0, note_min_usd=100)
        card = demo(flags, P=lenient)
        self.assertFalse(card.show)
        self.assertIn("but the quality risk is high", card.note)

    def test_the_note_names_the_first_high_flag(self):
        flags = [Flag("awaiting-answer", "medium", AWAITING), Flag("long-question", "high", "the long one"),
                 Flag("agents-running", "high", RUNNING)]
        card = demo(flags, label="")
        self.assertTrue(card.note.endswith(" but the quality risk is high: the long one."))

    def test_nothing_to_price_gives_nothing(self):
        for model, ctx in ((None, 410_000), ("", 410_000), ("gpt-unknown", 410_000), ("codex-auto-review", 410_000),
                           (ASTRA, 0), (ASTRA, -5), (ASTRA, float("nan"))):
            with self.subTest(model=model, ctx=ctx):
                card = after_break(ctx, 29_520, awaiting(), Params(usd_per_pt=5.0), model, 832_200, None, 64, "TEST · ")
                self.assertEqual((card.show, card.text, card.note, card.numbers), (False, "", "", {}))
                self.assertEqual((card.risk, card.flags), ("medium", ["awaiting-answer"]))
        card = after_break(410_000, float("nan"), [], Params(), ASTRA)
        self.assertEqual((card.show, card.text, card.note), (False, "", ""))

    def test_low_risk_has_the_lower_bar(self):
        """272,000 tokens is still at short rates; 12 h idle is the 0.92 bucket; few calls keep the typical saving
        between the $2 and $5 bars."""
        P = Params(calls_between_returns=(5, 10))
        low = after_break(272_000, 43_200, [], P, ASTRA)
        self.assertNumbers(low, {
            "resend_warm": 0.272,
            "resend_here": 2.3586,             # 0.92 x (0.02 + 252,000 x $10/M) + 0.08 x 0.272
            "save_now": 1.4486, "floor": 0.7486,
            "per_call": 0.163,                 # 0.272 - 0.109
            "typical": [2.2636, 3.0786],
            "calls_left": None, "p_miss": 0.92,
        })
        self.assertTrue(low.show)
        self.assertEqual(low.note, "")
        medium = after_break(272_000, 43_200, awaiting(), P, ASTRA)
        self.assertFalse(medium.show)
        self.assertTrue(medium.note.startswith("Switch advisor: back after 12.0 h, this message likely re-reads"))

    def test_a_card_needs_a_likely_miss_and_a_safe_worst_case(self):
        unlikely = after_break(410_000, 7199, [], Params(), ASTRA, 832_200)       # P(miss) 0.39
        self.assertFalse(unlikely.show)
        self.assertTrue(unlikely.note)
        worst = after_break(410_000, 29_520, [], Params(floor_min=5.0), ASTRA, 832_200)    # floor 4.55 < 5
        self.assertFalse(worst.show)

    def test_each_request_is_priced_at_its_own_size(self):
        short = after_break(272_000, 29_520, [], Params(), ASTRA)
        long = after_break(272_001, 29_520, [], Params(), ASTRA)
        self.assertAlmostEqual(short.numbers["resend_warm"], 0.272, places=4)
        self.assertAlmostEqual(long.numbers["resend_warm"], 0.544, places=4)
        self.assertEqual(short.numbers["new_conv"], long.numbers["new_conv"])
        self.assertAlmostEqual(long.numbers["new_conv"], 0.91, places=4)
        # A large worst case crosses the line by itself: NW = 27K + 250K + 2K = 279K pays the long input rate.
        big = after_break(410_000, 29_520, [], Params(reread_worst=250_000), ASTRA)
        self.assertAlmostEqual(big.numbers["new_worst"], 0.04 + 259_000 * 20e-6, places=4)
        self.assertAlmostEqual(big.numbers["new_conv"], 0.91, places=4)

    def test_fast_mode_doubles_every_figure(self):
        fast, normal = demo(tier="fast"), demo()
        for key in ("resend_here", "resend_warm", "new_conv", "new_worst", "save_now", "floor", "per_call"):
            with self.subTest(key=key):
                self.assertAlmostEqual(fast.numbers[key], 2 * normal.numbers[key], places=4)
        self.assertAlmostEqual(fast.numbers["typical"][0], 41.7744, places=4)

    def test_compaction_caps_the_typical_saving(self):
        """700K with an 832,200 window: Codex compacts at 0.86 x 832,200 = 715,692 tokens, about 17 calls away. After
        that it holds about 39K tokens, less than the new conversation's 109K, so the later calls count against the
        switch."""
        capped = after_break(700_000, 29_520, [], Params(), ASTRA, 832_200)
        self.assertNumbers(capped, {
            "resend_here": 10.7024,            # 0.76 x 13.64 + 0.24 x 1.4
            "save_now": 9.7924,
            "per_call": 1.291,                 # 1.4 - 0.109
            "after_call": -0.07,               # 39K x $1/M - 109K x $1/M
            "calls_left": 17.4356,             # (715,692 - 700,000) / 900
            "typical": [31.9822, 30.0222],     # 9.7924 + 17.4356 x 1.291 - (22, 50 - 17.4356) x 0.07
            "floor": 9.0924,                   # 10.7024 - 1.61
            # a new conversation that re-read 150K is 179K: each later call re-reads 1.4 - 0.179 = 1.221 less, and
            # 0.039 - 0.179 = 0.14 more once this one is compacted
            "floor_typical": [29.7422, 25.8222],
        })
        self.assertIn("That saves about $9.79 now and typically $30.02–$31.98 by your next break. Codex compacts this "
                      "conversation by itself within about 17 calls; after that it is smaller than the new one, so the "
                      "typical figure counts the later calls against the switch. Even if it re-reads 150K tokens of "
                      "files, it saves about $9.09 now and typically $25.82–$29.74 by your next break.", capped.text)
        self.assertTrue(capped.show)            # both ends of the range clear the $2 bar for low risk
        free = after_break(700_000, 29_520, [], Params(), ASTRA)
        self.assertIsNone(free.numbers["calls_left"])
        self.assertNumbers(free, {"typical": [38.1944, 74.3424]})      # 9.7924 + 22 x 1.291, + 50 x 1.291
        self.assertNotIn("compacts", free.text)
        self.assertNotIn("compacts", demo().text)

    def test_the_worst_case_holds_until_the_next_break(self):
        """408K on a 475,000 window: Codex compacts this conversation within a call, so by the next break the old
        conversation is the smaller one. The worst case now still saves $4.51, but a new conversation that re-read
        150K is 179K on every later call: by 50 calls the switch costs $2.05 more. No card, and a test card says so."""
        card = after_break(408_000, 29_520, [], Params(), ASTRA, 475_000)
        self.assertNumbers(card, {
            "save_now": 5.2138, "floor": 4.5138, "calls_left": 0.5556,
            "typical": [4.1055, 2.1455],       # both above the $2 bar for low risk
            "floor_typical": [1.8655, -2.0545],    # 4.5138 + 0.5556 x 0.637 - (21.4444, 49.4444) x 0.14
        })
        self.assertFalse(card.show)
        test = after_break(408_000, 29_520, [], Params(), ASTRA, 475_000, label="TEST · ")
        self.assertTrue(test.text.startswith("TEST · A switch wouldn't pay here"))
        self.assertIn("Even if it re-reads 150K tokens of files, against sending here it is about $4.51 less now and "
                      "typically $2.05 more to $1.87 less by your next break.", test.text)
        lenient = after_break(408_000, 29_520, [], Params(floor_min=-3.0), ASTRA, 475_000)
        self.assertTrue(lenient.show)

    def test_raw_is_numbers_unrounded(self):
        cases = [demo(), after_break(203_999, 90_000, [], Params(), ASTRA, 832_200, "fast"),
                 after_break(700_000, 29_520, [], Params(), ASTRA, 832_200), after_break(31_337, 7777.7, [], Params(),
                                                                                       "gpt-6.1-sol", None, "fast")]
        for card in cases:
            self.assertEqual(set(card.raw), set(card.numbers))
            for key, value in card.numbers.items():
                with self.subTest(ctx=card.numbers["ctx"], key=key):
                    got = card.raw[key]
                    if isinstance(value, list):
                        self.assertEqual([round(x, 4) for x in got], value)
                    elif isinstance(value, float):
                        self.assertEqual(round(got, 4), value)
                    elif key == "ctx":
                        self.assertEqual(int(got), value)
                    else:
                        self.assertEqual(got, value)        # the model; calls_left None without a window
        self.assertIsNone(cases[3].raw["calls_left"])
        self.assertNotEqual(cases[1].raw["floor"], cases[1].numbers["floor"])    # really unrounded

    def test_a_card_refused_by_less_than_a_cent_names_the_condition(self):
        """gpt-6-astra in Fast mode, an 832,200 window, 203,999 tokens after 25 h: the worst case saves $0.49998,
        under the $0.50 floor, though numbers rounds it to 0.5."""
        card = after_break(203_999, 90_000, [], Params(), ASTRA, 832_200, "fast")
        self.assertFalse(card.show)
        self.assertEqual(card.numbers["floor"], 0.5)
        self.assertLess(card.raw["floor"], 0.5)
        self.assertEqual(card.failed, ["floor"])
        # the same just under a bar: the typical saving, a tenth of a cent short
        low = after_break(272_000, 43_200, [], Params(calls_between_returns=(5, 10)), ASTRA)
        self.assertTrue(low.show)
        bar = min(low.raw["typical"]) + 0.001
        card = after_break(272_000, 43_200, [], Params(calls_between_returns=(5, 10), bar_low=bar), ASTRA)
        self.assertFalse(card.show)
        self.assertEqual(card.failed, ["typical"])

    def test_failed_lists_each_condition_in_order(self):
        cases = [
            (after_break(50_000, 1800, [], Params(), ASTRA), ["idle", "miss", "floor", "floor_typical", "typical"]),
            (after_break(410_000, 3599, [], Params(min_miss=0.0), ASTRA, 832_200), ["idle"]),
            (after_break(410_000, 7199, [], Params(), ASTRA, 832_200), ["miss"]),
            (after_break(408_000, 29_520, [], Params(), ASTRA, 475_000), ["floor_typical"]),
            (after_break(410_000, 29_520, [], Params(bar_low=100.0), ASTRA, 832_200), ["typical"]),
            (demo(quality_flags(facts(agents_running=1), LONG_PROMPT, 0)), ["risk"]),
            (demo(quality_flags(facts(agents_running=1), LONG_PROMPT, 0), P=Params(floor_min=5.0)), ["floor"]),
            (demo(), []),
            (after_break(410_000, 29_520, [], Params(), "gpt-unknown"), []),
        ]
        for card, failed in cases:
            with self.subTest(failed=failed):
                self.assertEqual(card.failed, failed)
                self.assertEqual(card.show, not failed and bool(card.numbers))
        self.assertEqual(cases[-1][0].raw, {})

    def test_numbers_keys_and_rounding(self):
        card = demo()
        self.assertEqual(set(card.numbers), {"resend_here", "resend_cold", "resend_warm", "new_conv", "new_worst",
                                             "save_now", "typical", "floor", "floor_typical", "per_call", "after_call",
                                             "calls_left", "p_miss", "ctx", "idle_s", "model"})
        for ctx, idle, window in ((123_457, 12_345.678, 258_400), (987_653, 99_999.9, 1_050_000), (31_337, 7777, None)):
            numbers = after_break(ctx, idle, [], Params(), "gpt-6.1-sol", window, "fast").numbers
            for key, value in numbers.items():
                for v in (value if isinstance(value, list) else [value]):
                    if isinstance(v, float):
                        with self.subTest(ctx=ctx, key=key):
                            self.assertEqual(round(v, 4), v)
        json.dumps(card.numbers, allow_nan=False)


class CardTextTest(unittest.TestCase):
    def test_weekly_limit_wording(self):
        """The share is of the cost of a miss ($7.84), as in the note."""
        cases = [
            (5.0, 64, "about $7.84 if the cache expired (1.6% of your weekly limit; you're at 64%), against $0.82 with "
                      "a warm cache, so about $6.16 on average."),
            (5.0, None, "about $7.84 if the cache expired (1.6% of your weekly limit), against"),
            (None, 64, "about $7.84 if the cache expired (you're at 64% of your weekly limit), against"),
            (None, None, "about $7.84 if the cache expired, against"),
            (100.0, None, "about $7.84 if the cache expired (<0.1% of your weekly limit), against"),      # 0.08%
            (50.0, 63.6, "about $7.84 if the cache expired (0.2% of your weekly limit; you're at 64%), against"),
        ]
        for usd_per_pt, meter, part in cases:
            with self.subTest(usd_per_pt=usd_per_pt, meter=meter):
                card = demo(P=Params(usd_per_pt=usd_per_pt), meter_pct=meter)
                self.assertIn(part, paragraphs(card)[0])
                if usd_per_pt is None and meter is None:
                    self.assertNotIn("weekly", card.text)
        note = after_break(200_000, 28_800, [], Params(usd_per_pt=5.0), ASTRA, 258_400, None, 64).note
        self.assertIn("about $1.82 (0.4% of your weekly limit; you're at 64%), against $0.20 with a warm cache.", note)
        note = after_break(200_000, 28_800, [], Params(), ASTRA, 258_400, None, None).note
        self.assertNotIn("weekly", note)

    def test_labels_prefix_the_first_sentence(self):
        self.assertTrue(demo().text.startswith("DEMO · Switch advisor: you were away 8.2 h, "))
        self.assertTrue(demo(label="TEST · ").text.startswith("TEST · Switch advisor: you were away 8.2 h, "))
        self.assertTrue(demo(label="").text.startswith("Switch advisor: you were away 8.2 h, "))
        note = after_break(200_000, 28_800, [], Params(), ASTRA, 258_400, label="TEST · ").note
        self.assertTrue(note.startswith("TEST · Switch advisor: back after 8.0 h, "))
        self.assertEqual(note.count("TEST · "), 1)

    def test_a_test_card_that_would_not_show_says_so(self):
        """`switch-advisor test`: this conversation's numbers as if after 3 hours (P(miss) 0.58)."""
        card = after_break(200_000, 10_800, [], Params(), ASTRA, 258_400, label="TEST · ")
        self.assertFalse(card.show)
        self.assertTrue(card.text.startswith(
            "TEST · A switch wouldn't pay here; this is what the card would look like. Switch advisor: you were away "
            "3.0 h, so this conversation's prompt cache has most likely expired (it had after 58% of breaks this long)."))
        self.assertIn("That saves about $0.23 now and typically $0.71–$2.23 by your next break. Codex compacts this "
                      "conversation by itself within about 25 calls", card.text)
        self.assertIn("Even if it re-reads 150K tokens of files, it costs about $0.47 more now and typically "
                      "$0.01–$3.49 more by your next break.", card.text)
        self.assertEqual(card.note, "TEST · Switch advisor: back after 3.0 h, this message likely re-reads about 200K "
                                    "tokens without the cache (it had expired after 58% of breaks this long): about "
                                    "$1.82, against $0.20 with a warm cache.")

    def test_a_high_risk_test_card_says_why_it_would_not_show(self):
        flags = quality_flags(facts(agents_running=1), LONG_PROMPT, 0)
        card = demo(flags, label="TEST · ")
        self.assertFalse(card.show)
        self.assertTrue(card.text.startswith(
            "TEST · A switch would pay here, but the quality risk is too high for a card; this is what it would look "
            "like. Switch advisor: you were away 8.2 h"))
        self.assertEqual(paragraphs(card)[2], "Quality risk: high. The handoff carries your first and latest requests, "
                                              "the last reply and the uncommitted files. It may miss: " + RUNNING + ".")

    def test_a_switch_that_costs_more_reads_as_a_cost(self):
        small = after_break(50_000, 10_800, [], Params(), ASTRA, label="TEST · ")
        self.assertIn("That costs about $0.70 more now and typically $2.00–$3.65 more by your next break.", small.text)
        self.assertIn("Even if it re-reads 150K tokens of files, it costs about $1.40 more.", small.text)
        mixed = after_break(120_000, 10_800, [], Params(), ASTRA, label="TEST · ")
        self.assertIn("Against sending here, that is about $0.27 more now and typically $0.03 more to $0.28 less by "
                      "your next break.", mixed.text)
        for card in (small, mixed):
            self.assertNotIn("$-", card.text)
            self.assertNotIn("-$", card.text)

    def test_low_risk_paragraph_and_info_flags(self):
        low = demo(quality_flags(facts(), LONG_PROMPT, 0))
        self.assertTrue(low.show)
        self.assertEqual(paragraphs(low)[2], "Quality risk: low. Its last reply doesn't ask you anything, no agents are "
                                             "running, and " + CARRIES + ".")
        info = demo(quality_flags(facts(), LONG_PROMPT, 3))
        self.assertEqual(paragraphs(info)[2], "Quality risk: low. Its last reply doesn't ask you anything, no agents "
                                              "are running, and " + CARRIES + ". (3 files have uncommitted changes; "
                                              "the new conversation sees them with git status.)")

    def test_a_card_with_an_open_question(self):
        """The conversation waits for an answer in Codex's question panel: the card can still block at medium risk,
        and says the handoff carries the question and its options."""
        f = facts(asks_you=True, last_reply="I sent you a question in the panel.", open_questions=[QUESTION])
        card = demo(quality_flags(f, "Postgres", 0), label="")
        self.assertTrue(card.show)
        self.assertEqual(card.risk, "medium")
        self.assertEqual(paragraphs(card)[2], "Quality risk: medium. The handoff carries your first and latest requests, "
                                              "the last reply and the uncommitted files. It may miss: " + ASKED_ONE + ".")

    def test_medium_paragraph_joins_its_flags(self):
        flags = quality_flags(facts(agents_spawned=3), "yes", 2)
        card = demo(flags)
        self.assertEqual(paragraphs(card)[2],
                         "Quality risk: medium. The handoff carries your first and latest requests, the last reply and "
                         "the uncommitted files. It may miss: " + AWAITING + "; this conversation coordinated 3 agents; "
                         "their reports are only in this conversation, and the handoff keeps its last reply. (2 files "
                         "have uncommitted changes; the new conversation sees them with git status.)")

    def test_durations_follow_the_params(self):
        card = demo(P=Params(usd_per_pt=5.0, handoff_ttl_s=3600, quiet_s=5400))
        self.assertIn("send your prompt there within 1 hour;", card.text)
        self.assertTrue(card.text.endswith("won't be checked again for 1.5 hours."))
        card = demo(P=Params(usd_per_pt=5.0, handoff_ttl_s=600, quiet_s=86_400))
        self.assertIn("send your prompt there within 10 minutes;", card.text)
        self.assertTrue(card.text.endswith("won't be checked again for 24 hours."))

    def test_the_card_says_how_soon_sending_again_means_stay(self):
        """A prompt sent again counts as "stay" only within resend_window_s of the card; a later one is a new return
        and gets the card again, so the card says how long the person has."""
        self.assertIn("To stay: send your prompt here again within 15 minutes, and this conversation won't be checked "
                      "again for 12 hours.", demo().text)
        # never longer than the hook's window: 90 s is not "2 minutes", 3660 s is not "1.0 hours"
        for s, said in ((60, "60 seconds"), (90, "90 seconds"), (3660, "61 minutes"), (7140, "119 minutes")):
            with self.subTest(resend_window_s=s):
                card = demo(P=Params(usd_per_pt=5.0, resend_window_s=s))
                self.assertIn("To stay: send your prompt here again within %s," % said, card.text)

    def test_no_text_says_none_nan_or_inf(self):
        odd = re.compile(r"\b(none|nan|inf|infinity)\b", re.I)
        flag_sets = [[], quality_flags(facts(asks_you=True, last_reply="x" * 2000 + "?", agents_spawned=2), "yes", 40)]
        for model, ctx, idle, window, meter, usd_per_pt, flags, label in itertools.product(
                (ASTRA, "gpt-5.3-codex", None), (1, 999, 120_000, 410_000), (0, 3600, 10_800, 10 ** 7),
                (None, 0, -5, 258_400), (None, float("nan"), -3, 0, 64.4), (None, float("nan"), 0, 5.0, 1e9),
                flag_sets, ("", "TEST · ")):
            card = after_break(ctx, idle, flags, Params(usd_per_pt=usd_per_pt), model, window, None, meter, label)
            for text in (card.text, card.note):
                if odd.search(text) or "$-" in text:
                    self.fail(f"{text!r} from ctx={ctx} idle={idle} window={window} meter={meter} rate={usd_per_pt}")

    def test_card_text_stays_under_1800_characters(self):
        longest = ""
        for flags, ctx, idle, window, tier, meter, usd_per_pt, label in itertools.product(
                all_flag_sets(), (50_000, 410_000, 1_000_000), (1800, 10_800, 400_000), (None, 258_400, 832_200),
                (None, "ultrafast"), (None, 99.6), (None, 0.5), ("", "TEST · ")):
            card = after_break(ctx, idle, flags, Params(usd_per_pt=usd_per_pt), ASTRA, window, tier, meter, label)
            if len(card.text) > len(longest):
                longest = card.text
            self.assertLess(len(card.note), 600)
        self.assertLess(len(longest), 1800, longest)


if __name__ == "__main__":
    unittest.main()
