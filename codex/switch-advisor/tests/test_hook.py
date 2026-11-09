"""advisor.hook end to end, on a temporary CODEX_HOME with synthetic logs and a temporary data folder: the card and
what it keeps, staying, switching, notes, the triggers, the rate refresh, and that a failure never blocks a prompt.
Every output is checked to be one of the four a hook may give, and every decision line to have exactly the fields the
monitor's replay reads."""
import json
import os
import re
import tempfile
import time
import unittest
from unittest import mock

from advisor import VERSION, handoff, hook, rate, rollout, rule, state
from tests import fixtures as fx

T0, HOUR, DAY = fx.T0, fx.HOUR, fx.DAY
MAIN = "0b1c2d3e-0000-4000-8000-000000000001"
NEW = "0b1c2d3e-0000-4000-8000-000000000002"
SUB = "0b1c2d3e-0000-4000-8000-0000000000a1"
ASTRA = "gpt-6-astra"
LAST = T0                 # the conversation's last model response
AWAY = 29_520             # 8.2 h: the spec's demo break, P(miss) 0.76
PROMPT = ("Carry on with the streaming parser: handle quoted fields that span lines, and keep the public interface as "
          "it is. Marker zq-7731.")      # 100+ characters, names no file, no short reply: low risk on its own
MARKER = "zq-7731"
FILES = [" M src/parser.py", "?? notes/"]
FIRST = "Build a streaming parser for the log files."
SECOND = "Now handle quoted fields."
REPLY = "Done: quoted fields work now."

KEYS = {
    "offer": {"t", "event", "session", "version", "model", "risk", "flags", "numbers", "cwd_key"},
    "note": {"t", "event", "session", "version", "model", "numbers"},
    "kept": {"t", "event", "session", "version"},
    "switched": {"t", "event", "session", "version", "from_session", "numbers", "cwd_key"},
    "expired": {"t", "event", "session", "version", "from_session", "cwd_key"},
    "test": {"t", "event", "session", "version"},
    "demo": {"t", "event", "session", "version"},
}
NUMBERS = {"resend_here", "resend_cold", "resend_warm", "new_conv", "new_worst", "save_now", "typical", "floor",
           "floor_typical", "per_call", "after_call", "calls_left", "p_miss", "ctx", "idle_s", "model"}

SPEC_DEMO = "\n\n".join([
    "DEMO · Switch advisor: you were away 8.2 h, so this conversation's prompt cache has most likely expired (it had "
    "after 76% of breaks this long). Sending here re-reads about 410K tokens: about $7.84 if the cache expired (1.6% "
    "of your weekly limit; you're at 64%), against $0.82 with a warm cache, so about $6.16 on average.",
    "A new conversation starts from a handoff built from this one on your computer, with no model call, and with about "
    "109K tokens once it has re-read its files: about $0.91. That saves about $5.25 now and typically $20.89–$40.80 by "
    "your next break. Even if it re-reads 150K tokens of files, it saves about $4.55.",
    "Quality risk: medium. The handoff carries your first and latest requests, the last reply and the uncommitted "
    "files. It may miss: your message answers its last reply; the handoff carries that reply, not what was said before "
    "it.",
    "To switch: start a new conversation in this project folder and send your prompt there within 30 minutes; it "
    "starts from the handoff. To stay: send your prompt here again within 15 minutes, and this conversation won't be "
    "checked again for 12 hours.",
    "These are example numbers, not this conversation's.",
])


def shape(out):
    """Which of the four outputs a hook may give this is: "none", "block", "note" or "context". Fails otherwise. A block
    may carry a systemMessage too (`switch-advisor test` shows the note a real break would give instead)."""
    if out is None:
        return "none"
    json.dumps(out, allow_nan=False)
    if (set(out) in ({"decision", "reason"}, {"decision", "reason", "systemMessage"}) and out["decision"] == "block"
            and isinstance(out["reason"], str)):
        kind, text = "block", out["reason"]
        if "systemMessage" in out:
            note = out["systemMessage"]
            if not isinstance(note, str) or not note.strip() or re.search(r"\b(None|nan|inf)\b|\$-", note):
                raise AssertionError("empty or broken note with a block: %r" % (note,))
    elif set(out) == {"systemMessage"} and isinstance(out["systemMessage"], str):
        kind, text = "note", out["systemMessage"]
    elif (set(out) == {"hookSpecificOutput"} and isinstance(out["hookSpecificOutput"], dict)
          and set(out["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
          and out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
          and isinstance(out["hookSpecificOutput"]["additionalContext"], str)):
        kind, text = "context", out["hookSpecificOutput"]["additionalContext"]
    else:
        raise AssertionError("not an output a hook may give: %r" % (out,))
    if not text.strip() or re.search(r"\b(None|nan|inf)\b|\$-", text):
        raise AssertionError("empty or broken text: %r" % (text[:200],))
    return kind


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = fx.make_home(self._tmp.name)
        self.data = os.path.join(self._tmp.name, "plugin-data")
        self.cwd = os.path.join(self._tmp.name, "project")
        os.makedirs(self.cwd)
        self.env = {"CODEX_HOME": self.home, "PLUGIN_DATA": self.data}
        git = mock.patch.object(handoff, "git_status", return_value=list(FILES))
        self.git = git.start()
        self.addCleanup(git.stop)

    # ---- building logs ----

    def conversation(self, thread=MAIN, model=ASTRA, window=832_200, last=(400_000, 390_000, 10_000), reply=REPLY,
                     typed=True, meter=None, compacted=False, then=None, kind="main", parent=None, first=FIRST,
                     trigger=None, turn_id="turn-new"):
        """A conversation whose last model response was at LAST and resent `last` = (input, cached, output) tokens:
        two typed requests (`first`, then SECOND), two calls, two replies. then: a prompt Codex has already logged as
        the new turn when the hook runs. typed=False: its user messages are all text Codex injected, none typed.
        trigger: the new turn's start, as Codex writes it before the prompt hook runs (turn_attribution.turn_trigger,
        under `turn_id`, by default the hook payload's): "remote_android" for a prompt sent from the phone app."""
        r = fx.Rollout(self.home, thread, self.cwd, kind=kind, parent=parent)
        say = r.user if typed else r.harness
        r.meta()
        r.turn(model, window, t=LAST - 2 * HOUR)
        say(first, t=LAST - 2 * HOUR + 5)
        r.call(LAST - 2 * HOUR + 60, 150_000, 140_000, 3_000)
        r.complete(LAST - 2 * HOUR + 61, "The parser streams now; quoted fields are next.")
        r.turn(model, window, t=LAST - 600)
        say(SECOND, t=LAST - 595)
        r.call(LAST, *last)
        r.complete(LAST + 1, reply)
        if meter:
            r.meter(LAST + 2, meter[0], resets_at=meter[1])
        if compacted:
            r.compacted(LAST + 3, [FIRST, SECOND])
        if then is not None:
            r.turn(model, window, t=LAST + AWAY - 1)
            r.world_state()
            r.user(then)
        if trigger is not None:
            r.turn(model, window, t=LAST + AWAY - 1, trigger=trigger, turn_id=turn_id)
            r.world_state()
        return r.write()

    def running_agent(self, at):
        """A subagent of MAIN in the middle of a turn, its log written a minute before `at`."""
        r = fx.Rollout(self.home, SUB, self.cwd, kind="subagent", parent=MAIN)
        r.meta(t=at - 600)
        r.turn(ASTRA, 258_400, t=at - 300)
        r.user("Check the parser tests.")
        return r.write(mtime=at - 60)

    # ---- calling the hooks ----

    def submit(self, path, prompt=PROMPT, at=AWAY, sid=MAIN, cwd=None, env=None):
        payload = {"session_id": sid, "transcript_path": path, "cwd": cwd or self.cwd,
                   "hook_event_name": "UserPromptSubmit", "model": ASTRA, "turn_id": "turn-new",
                   "permission_mode": "default", "prompt": prompt}
        out = hook.on_prompt_submit(payload, now=LAST + at, env=self.env if env is None else env)
        shape(out)
        return out

    def start(self, sid=NEW, at=AWAY + 600, cwd=None, source="startup", path=None, env=None):
        payload = {"session_id": sid, "transcript_path": path, "cwd": cwd or self.cwd, "hook_event_name": "SessionStart",
                   "model": ASTRA, "permission_mode": "default", "source": source}
        out = hook.on_session_start(payload, now=LAST + at, env=self.env if env is None else env)
        shape(out)
        return out

    # ---- reading the data folder ----

    def decisions(self):
        folder = os.path.join(self.data, "decisions")
        lines = []
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                lines.extend(json.loads(line) for line in fh if line.strip())
        for line in lines:
            extra = {"test"} if line["event"] == "switched" and line.get("test") is True else set()
            if line["event"] in ("note", "offer", "test") and "trigger" in line:     # only when Codex logged one
                self.assertRegex(line["trigger"], r"^[A-Za-z0-9_.-]{1,40}$")
                extra = extra | {"trigger"}
            self.assertEqual(set(line), KEYS[line["event"]] | extra, line["event"])
            self.assertEqual(line["version"], VERSION)
        return lines

    def events(self):
        return [line["event"] for line in self.decisions()]

    def pending(self, cwd=None):
        """(the pending .json, its text) for a folder, or None."""
        meta = os.path.join(self.data, "pending", state.cwd_key(cwd or self.cwd) + ".json")
        if not os.path.exists(meta):
            return None
        with open(meta[:-len(".json")] + ".txt", encoding="utf-8") as fh:
            return state.read_json(meta), fh.read()

    def files(self):
        """Every file in the data folder: {relative path: bytes}."""
        out = {}
        for folder, _dirs, names in os.walk(self.data):
            for name in names:
                path = os.path.join(folder, name)
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, self.data)] = fh.read()
        return out

    def assertNowhere(self, text):
        """No file in the data folder holds `text`."""
        for name, data in self.files().items():
            self.assertNotIn(text.encode("utf-8"), data, name)

    def expected(self, ctx=410_000, idle=AWAY, flags=None, P=None, window=832_200, **kw):
        """The card rule.after_break gives for these numbers, with the flags the hook finds for PROMPT."""
        if flags is None:
            flags = rule.quality_flags(rollout.Facts(last_reply=REPLY), PROMPT, len(FILES))
        return rule.after_break(ctx, idle, flags, P or rule.Params(), ASTRA, window, **kw)


class CardTest(Base):
    def test_a_card_holds_the_prompt_and_keeps_the_handoff(self):
        path = self.conversation()
        out = self.submit(path)
        card = self.expected()
        self.assertTrue(card.show)
        self.assertEqual(out, {"decision": "block", "reason": card.text})
        self.assertTrue(out["reason"].startswith("Switch advisor: you were away 8.2 h"))
        key = state.cwd_key(self.cwd)
        meta, text = self.pending()
        self.assertEqual(meta, {"from_session": MAIN, "at": LAST + AWAY, "numbers": card.numbers})
        self.assertEqual(text, handoff.build(rollout.facts(path, now=LAST + AWAY), FILES, prompt=PROMPT))
        self.assertIn("The first request (first 1,500 characters):\n" + FIRST, text)
        self.assertIn("- " + SECOND, text)
        self.assertIn(REPLY, text)
        self.assertIn("-  M src/parser.py", text)
        s = state.session(self.data, MAIN)
        self.assertEqual(s, {"offered_at": LAST + AWAY, "numbers": card.numbers, "cwd_key": key})
        (offer,) = self.decisions()
        self.assertEqual(offer, {"t": LAST + AWAY, "event": "offer", "session": MAIN, "version": VERSION, "model": ASTRA,
                                 "risk": "low", "flags": ["uncommitted"], "numbers": card.numbers, "cwd_key": key})
        self.assertEqual(set(offer["numbers"]), NUMBERS)
        self.assertEqual(offer["numbers"]["ctx"], 410_000)

    def test_the_prompt_is_in_no_file(self):
        path = self.conversation(then=PROMPT)              # Codex logged the new prompt before the hook ran
        self.assertIn(PROMPT, rollout.facts(path).requests)
        self.assertEqual(shape(self.submit(path)), "block")
        self.assertIn(SECOND, self.pending()[1])            # the handoff has the earlier requests ...
        self.assertNowhere(PROMPT)                          # ... and not the prompt held back
        self.assertNowhere(MARKER)
        for name, data in self.files().items():             # only the handoff holds conversation text
            if not name.endswith(".txt"):
                for text in (FIRST, SECOND, REPLY):
                    self.assertNotIn(text.encode("utf-8"), data, name)
        self.assertIsNone(self.submit(path, at=AWAY + 60))  # sent again: stay
        self.assertNowhere(MARKER)

    def test_the_note_files_hold_no_text(self):
        self.submit(self.conversation(last=(195_000, 190_000, 5_000), window=258_400, then=PROMPT))
        self.assertEqual(self.events(), ["note"])
        self.assertNowhere(MARKER)
        self.assertNowhere(FIRST)

    def test_sending_again_within_15_minutes_stays(self):
        path = self.conversation()
        self.submit(path)
        self.assertIsNone(self.submit(path, at=AWAY + 900))
        self.assertEqual(state.session(self.data, MAIN)["quiet_until"], LAST + AWAY + 900 + 12 * HOUR)
        self.assertNotIn("offered_at", state.session(self.data, MAIN))
        self.assertIsNone(self.pending())
        self.assertEqual(self.events(), ["offer", "kept"])
        self.assertEqual(self.decisions()[1], {"t": LAST + AWAY + 900, "event": "kept", "session": MAIN,
                                               "version": VERSION})

    def test_after_a_switch_a_prompt_here_is_no_stay(self):
        """A shows a card, a new conversation takes its handoff, then something is sent in A within 15 minutes. The
        card had one outcome, the switch: no "kept". A is still quiet for 12 hours, sparing it a second card."""
        path = self.conversation()
        self.submit(path)
        self.assertEqual(shape(self.start(at=AWAY + 120)), "context")
        self.assertIsNone(self.submit(path, at=AWAY + 300))
        self.assertEqual(self.events(), ["offer", "switched"])
        s = state.session(self.data, MAIN)
        self.assertEqual(s["quiet_until"], LAST + AWAY + 300 + 12 * HOUR)
        self.assertNotIn("offered_at", s)
        self.assertIsNone(self.submit(path, at=AWAY + 2 * HOUR))
        self.assertEqual(self.events(), ["offer", "switched"])

    def test_a_test_after_a_switch_keeps_its_handoff(self):
        """A shows a card, a new conversation takes its handoff, `switch-advisor test` in A makes a test handoff, and
        then a prompt is sent in A within 15 minutes of the card. That handoff isn't the card's: no "kept" (the card
        had one outcome, the switch), and the test's handoff stays for the new conversation the test card offered. A
        is still quiet for 12 hours."""
        path = self.conversation()
        self.submit(path)
        self.assertEqual(shape(self.start(at=AWAY + 60)), "context")
        out = self.submit(path, prompt="switch-advisor test", at=AWAY + 120)
        self.assertIn("it starts from the handoff made just now", out["reason"])
        made = self.pending()
        self.assertIs(made[0]["test"], True)
        self.assertIsNone(self.submit(path, at=AWAY + 300))
        self.assertEqual(self.events(), ["offer", "switched", "test"])
        self.assertEqual(self.pending(), made)
        s = state.session(self.data, MAIN)
        self.assertEqual(s["quiet_until"], LAST + AWAY + 300 + 12 * HOUR)
        self.assertNotIn("offered_at", s)
        ctx = self.start(sid="0b1c2d3e-0000-4000-8000-000000000004", at=AWAY + 600)
        self.assertEqual(ctx["hookSpecificOutput"]["additionalContext"], made[1])
        self.assertEqual([(e["event"], e.get("test")) for e in self.decisions()],
                         [("offer", None), ("switched", None), ("test", None), ("switched", True)])

    def panel_log(self, reply, arguments=None):
        """MAIN, whose last turn asked in Codex's question panel (request_user_input_async) and ended with `reply`;
        the answer comes after the break. arguments: the call's arguments as written instead (fixtures ask())."""
        r = fx.Rollout(self.home, MAIN, self.cwd)
        r.meta()
        r.turn(ASTRA, 832_200, t=LAST - 2 * HOUR)
        r.user(FIRST, t=LAST - 2 * HOUR + 5)
        r.call(LAST - 2 * HOUR + 60, 150_000, 140_000, 3_000)
        r.complete(LAST - 2 * HOUR + 61, "The parser streams now; quoted fields are next.")
        r.turn(ASTRA, 832_200, t=LAST - 600)
        r.user(SECOND, t=LAST - 595)
        r.ask(LAST - 30, [{"title": "Which database should the service use?", "options": ["Postgres", "SQLite"]}],
              arguments=arguments)
        r.call(LAST, 400_000, 390_000, 10_000)
        r.complete(LAST + 1, reply)
        return r.write()

    PLAN = "The migration plan, step by step. " + "Each step moves one table and checks its row counts. " * 35

    def test_a_long_reply_that_asks_nothing_and_an_open_panel_question(self):
        """The last reply runs over 1,500 characters and asks nothing itself; the question is in the panel, and the
        handoff carries it whole: the medium awaiting-answer flag, so the card shows."""
        reply = self.PLAN + "I sent you the question in the panel."
        self.assertGreater(len(reply), rule.REPLY_KEPT)
        out = self.submit(self.panel_log(reply), prompt="Postgres")
        self.assertEqual(shape(out), "block")
        self.assertIn("It may miss: your message answers the question it asked you; the handoff carries that question "
                      "and its options, not what was said before it.", out["reason"])
        self.assertIn("Waiting for the user's answer to:\n- Which database should the service use?\n  Options: "
                      "Postgres; SQLite", self.pending()[1])
        self.assertEqual(self.events(), ["offer"])

    def test_a_long_reply_that_asks_and_an_open_panel_question(self):
        """The same, but the long reply ends with a question of its own, which may be past the 1,500 characters the
        handoff keeps: long-question (high), so only the note."""
        reply = self.PLAN + "Shall I start with the first table?"
        out = self.submit(self.panel_log(reply), prompt="Postgres")
        self.assertEqual(shape(out), "note")
        self.assertTrue(out["systemMessage"].endswith(
            "but the quality risk is high: its last reply asks you something and runs %s characters, and the handoff "
            "keeps the first 1,500." % format(len(reply), ",")))
        self.assertEqual(self.events(), ["note"])

    def test_a_panel_question_the_plugin_cant_read(self):
        """The question in the panel has arguments the plugin can't read (JSON in a shape it doesn't know): the
        handoff couldn't carry it, so the risk is high and only the note shows, also after a reply that asks."""
        unknown = json.dumps({"questions": [{"id": "db", "choices": ["Postgres", "SQLite"]}]})
        for reply in (self.PLAN + "I sent you the question in the panel.", "Shall I also keep the old tables?"):
            with self.subTest(reply=reply[-40:]):
                out = self.submit(self.panel_log(reply, arguments=unknown), prompt="Postgres")
                self.assertEqual(shape(out), "note")
                self.assertTrue(out["systemMessage"].endswith(
                    "but the quality risk is high: it asked you something in the question panel that the handoff "
                    "can't read, so the new conversation wouldn't see the question."))
        self.assertEqual(self.events(), ["note", "note"])

    def test_a_refused_call_and_the_re_ask_give_the_card(self):
        """The model's first question call is in a shape Codex refuses (an error comes back, and the panel shows
        nothing), and it asks again correctly: only the re-ask waits, carried whole, so the card shows (medium)."""
        r = fx.Rollout(self.home, MAIN, self.cwd)
        r.meta()
        r.turn(ASTRA, 832_200, t=LAST - 2 * HOUR)
        r.user(FIRST, t=LAST - 2 * HOUR + 5)
        r.call(LAST - 2 * HOUR + 60, 150_000, 140_000, 3_000)
        r.complete(LAST - 2 * HOUR + 61, "The parser streams now; quoted fields are next.")
        r.turn(ASTRA, 832_200, t=LAST - 600)
        r.user(SECOND, t=LAST - 595)
        r.ask(LAST - 40, [], arguments=json.dumps({"questions": [{"id": "db"}]}),
              refused="failed to parse function arguments: missing field `title`")
        r.ask(LAST - 30, [{"title": "Which database should the service use?", "options": ["Postgres", "SQLite"]}])
        r.call(LAST, 400_000, 390_000, 10_000)
        r.complete(LAST + 1, self.PLAN + "I sent you the question in the panel.")
        out = self.submit(r.write(), prompt="Postgres")
        self.assertEqual(shape(out), "block")
        text = self.pending()[1]
        self.assertIn("Waiting for the user's answer to:\n- Which database should the service use?", text)
        self.assertNotIn("couldn't be read", text)
        self.assertEqual(self.events(), ["offer"])

    def test_a_question_call_codex_rejected_asks_nothing(self):
        """A question call whose arguments aren't JSON at all: Codex answers it with an error and the panel shows
        nothing, so it isn't waiting for an answer and the card shows as without it."""
        out = self.submit(self.panel_log(self.PLAN + "I asked again in the panel.", arguments="{not json"),
                          prompt="Postgres")
        self.assertEqual(shape(out), "block")
        self.assertNotIn("Waiting for the user's answer to:", self.pending()[1])
        self.assertEqual(self.events(), ["offer"])

    def test_a_card_with_an_open_question_and_its_handoff(self):
        """The conversation waits for an answer in Codex's question panel (rollout.facts' open_questions): the card
        says the handoff carries the question and its options, and it does."""
        question = {"title": "Which database should the service use?", "options": ["Postgres", "SQLite"]}
        real = rollout.facts

        def with_question(*args, **kw):
            f = real(*args, **kw)
            f.asks_you, f.open_questions = True, [question]
            return f
        with mock.patch.object(rollout, "facts", side_effect=with_question):
            out = self.submit(self.conversation(reply="I sent you a question in the panel."), prompt="Postgres")
        self.assertEqual(shape(out), "block")
        self.assertIn("Quality risk: medium. The handoff carries your first and latest requests, the last reply and the "
                      "uncommitted files. It may miss: your message answers the question it asked you; the handoff "
                      "carries that question and its options, not what was said before it.", out["reason"])
        text = self.pending()[1]
        self.assertIn("I sent you a question in the panel.\n\nWaiting for the user's answer to:\n"
                      "- Which database should the service use?\n  Options: Postgres; SQLite\n\n", text)

    def test_an_open_panel_question_from_the_log(self):
        """The same from a log: the last turn asked in Codex's question panel (request_user_input_async) and ended,
        and the answer comes after the break."""
        out = self.submit(self.panel_log("I sent you a question in the panel."), prompt="Postgres")
        self.assertEqual(shape(out), "block")
        self.assertIn("It may miss: your message answers the question it asked you; the handoff carries that question "
                      "and its options, not what was said before it.", out["reason"])
        self.assertIn("Waiting for the user's answer to:\n- Which database should the service use?\n  Options: "
                      "Postgres; SQLite", self.pending()[1])

    def test_a_quiet_conversation_gets_nothing_for_12_hours(self):
        path = self.conversation()
        self.submit(path)
        self.submit(path, at=AWAY + 60)
        for later in (AWAY + 2 * HOUR, AWAY + 60 + 12 * HOUR - 1):
            self.assertIsNone(self.submit(path, at=later))
        self.assertEqual(self.events(), ["offer", "kept"])
        self.assertEqual(shape(self.submit(path, at=AWAY + 60 + 12 * HOUR + 1)), "block")   # still no response since

    def test_after_15_minutes_it_is_a_new_card(self):
        path = self.conversation()
        self.submit(path)
        self.assertEqual(shape(self.submit(path, at=AWAY + 901)), "block")
        self.assertEqual(self.events(), ["offer", "offer"])
        self.assertEqual(self.pending()[0]["at"], LAST + AWAY + 901)

    def test_a_short_break_says_nothing_and_writes_nothing(self):
        path = self.conversation()
        for at in (60, 1800, 3599):
            self.assertIsNone(self.submit(path, at=at))
        self.assertEqual(self.decisions(), [])
        self.assertEqual(list(self.files()), [])

    def test_a_note_for_a_mid_size_conversation(self):
        out = self.submit(self.conversation(last=(195_000, 190_000, 5_000), window=258_400), at=28_800)
        card = self.expected(ctx=200_000, idle=28_800, window=258_400)
        self.assertFalse(card.show)
        self.assertEqual(out, {"systemMessage": card.note})
        self.assertEqual(card.note, "Switch advisor: back after 8.0 h, this message likely re-reads about 200K tokens "
                                    "without the cache (it had expired after 76% of breaks this long): about $1.82, "
                                    "against $0.20 with a warm cache.")
        (note,) = self.decisions()
        self.assertEqual(note, {"t": LAST + 28_800, "event": "note", "session": MAIN, "version": VERSION,
                                "model": ASTRA, "numbers": card.numbers})
        self.assertIsNone(self.pending())
        self.assertEqual(state.session(self.data, MAIN), {})

    def test_running_agents_give_a_note_not_a_card(self):
        path = self.conversation()
        self.running_agent(LAST + AWAY)
        out = self.submit(path)
        self.assertEqual(shape(out), "note")
        self.assertIn("A new conversation would save about $5.25 on average, but the quality risk is high: agents "
                      "started in this conversation are still running", out["systemMessage"])
        self.assertIsNone(self.pending())
        self.assertEqual(self.events(), ["note"])

    def test_nothing_for_an_unpriced_model_a_subagent_or_a_compacted_conversation(self):
        self.assertIsNone(self.submit(self.conversation(model="gpt-unknown-9")))
        sub = self.conversation(thread=SUB, kind="subagent", parent=MAIN)
        self.assertIsNone(self.submit(sub, sid=SUB))
        self.assertIsNone(self.submit(self.conversation(compacted=True)))
        self.assertEqual(self.decisions(), [])

    def test_no_data_folder_never_blocks(self):
        path = self.conversation()
        out = self.submit(path, env={"CODEX_HOME": self.home})
        self.assertEqual(out, {"systemMessage": self.expected(P=hook._no_card(rule.Params())).note})
        self.assertTrue(out["systemMessage"].startswith("Switch advisor: back after 8.2 h, this message likely re-reads "
                                                        "about 410K tokens"))
        self.assertFalse(os.path.exists(self.data))

    def test_the_claude_variable_names_the_data_folder_too(self):
        out = self.submit(self.conversation(), env={"CODEX_HOME": self.home, "CLAUDE_PLUGIN_DATA": self.data})
        self.assertEqual(shape(out), "block")

    def test_the_guardrail_turns_the_card_off(self):
        state.write_json(os.path.join(self.data, "guardrail.json"), {"after_break_enabled": False, "reason": "x"})
        out = self.submit(self.conversation())
        self.assertEqual(shape(out), "note")
        self.assertIsNone(self.pending())
        self.assertEqual(self.events(), ["note"])

    def test_no_card_when_the_handoff_would_carry_no_request(self):
        out = self.submit(self.conversation(typed=False))
        self.assertEqual(shape(out), "note")
        self.assertIsNone(self.pending())
        out = self.submit(self.conversation(then=PROMPT), prompt=PROMPT, at=AWAY + 1000)
        self.assertEqual(shape(out), "block")

    def test_params_json_is_used(self):
        path = self.conversation()
        state.write_json(os.path.join(self.data, "params.json"), {"min_idle_s": 40_000})
        self.assertIsNone(self.submit(path))
        state.write_json(os.path.join(self.data, "params.json"), {"bar_low": 100.0})
        self.assertEqual(shape(self.submit(path)), "note")
        state.write_json(os.path.join(self.data, "params.json"), {"resend_window_s": 60, "quiet_s": 3600})
        self.submit(path, at=AWAY + 1000)
        self.assertEqual(shape(self.submit(path, at=AWAY + 1061)), "block")        # 61 s later is not "again"
        self.assertIsNone(self.submit(path, at=AWAY + 1100))
        self.assertEqual(state.session(self.data, MAIN)["quiet_until"], LAST + AWAY + 1100 + 3600)

    def test_rate_json_adds_the_weekly_share(self):
        path = self.conversation()
        self.assertNotIn("weekly limit", self.submit(path)["reason"])
        state.write_json(os.path.join(self.data, "rate.json"), {"usd_per_pt": 5.0, "computed_at": LAST})
        out = self.submit(path, at=AWAY + 1000)
        self.assertIn("about $7.84 if the cache expired (1.6% of your weekly limit), against $0.82 with a warm cache, so "
                      "about $6.16 on average.", out["reason"])

    def test_the_weekly_meter_from_the_log(self):
        out = self.submit(self.conversation(meter=(64.0, LAST + 3 * DAY)))
        self.assertIn("(you're at 64% of your weekly limit)", out["reason"])
        out = self.submit(self.conversation(meter=(64.0, LAST + HOUR)), at=AWAY + 1000)   # that week has ended
        self.assertNotIn("you're at", out["reason"])

    def test_a_newer_meter_reading_in_another_log_wins(self):
        """After the break, another conversation used more of the limit: the card says where the meter is now."""
        path = self.conversation(meter=(40.0, LAST + 3 * DAY))
        other = fx.Rollout(self.home, "thread-other", self.cwd)
        other.turn(ASTRA, 258_400, t=LAST + AWAY - 1800)
        other.call(LAST + AWAY - 1200, 50_000, 40_000, 1_000)
        other.meter(LAST + AWAY - 1199, 55.0, resets_at=LAST + 3 * DAY)
        other.write()
        self.assertIn("(you're at 55% of your weekly limit)", self.submit(path)["reason"])

    def test_missing_fields_give_none(self):
        path = self.conversation()
        good = {"session_id": MAIN, "transcript_path": path, "cwd": self.cwd, "prompt": PROMPT}
        for drop in ("session_id", "cwd", "prompt"):
            payload = {k: v for k, v in good.items() if k != drop}
            with self.subTest(drop=drop):
                self.assertIsNone(hook.on_prompt_submit(payload, now=LAST + AWAY, env=self.env))
        for payload in (None, [], "text", {"session_id": 5, "cwd": self.cwd, "prompt": PROMPT}):
            self.assertIsNone(hook.on_prompt_submit(payload, now=LAST + AWAY, env=self.env))
        self.assertEqual(self.decisions(), [])

    def test_the_log_is_found_by_the_session_id(self):
        self.conversation()
        self.assertEqual(shape(self.submit(None)), "block")
        missing = os.path.join(self.home, "sessions", "not-there.jsonl")
        self.assertEqual(shape(self.submit(missing, at=AWAY + 1000)), "block")
        self.assertIsNone(self.submit(None, sid=NEW))                # no log for that session

    def test_a_pasted_blob_is_fast(self):
        path = self.conversation()
        began = time.perf_counter()
        out = self.submit(path, prompt="x" * 300_000)
        self.assertLess(time.perf_counter() - began, 2.0)
        self.assertEqual(shape(out), "block")


class FailureTest(Base):
    def errors(self):
        try:
            with open(os.path.join(self.data, "errors.log"), encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def test_an_exception_lets_the_prompt_through(self):
        path = self.conversation()
        with mock.patch.object(rollout, "snapshot", side_effect=RuntimeError(PROMPT)):
            self.assertIsNone(self.submit(path))
        with mock.patch.object(rule, "after_break", side_effect=KeyError(MARKER)):
            self.assertIsNone(self.submit(path))
        lines = self.errors().splitlines()
        self.assertEqual([line.split(" ", 1)[1] for line in lines],
                         ["prompt_submit/snapshot RuntimeError", "prompt_submit/facts KeyError"])
        self.assertNowhere(MARKER)
        self.assertEqual(self.decisions(), [])

    def test_a_failed_offer_leaves_no_handoff(self):
        with mock.patch.object(state, "save_session", side_effect=OSError("disk full")):
            self.assertIsNone(self.submit(self.conversation()))
        self.assertIsNone(self.pending())
        self.assertIn("prompt_submit/offer OSError", self.errors())

    def test_session_start_and_refresh_never_raise(self):
        with mock.patch.object(state, "take_pending", side_effect=RuntimeError("x")):
            self.assertIsNone(self.start())
        with mock.patch.object(rate, "compute_rate", side_effect=ValueError("x")):
            self.assertIsNone(hook.refresh_rate(now=LAST, env=self.env))
        self.assertIn("session_start/pending RuntimeError", self.errors())
        self.assertIn("refresh_rate/compute ValueError", self.errors())
        self.assertFalse(os.path.exists(os.path.join(self.data, "rate.lock")))

    def test_errors_log_is_started_again_when_large(self):
        os.makedirs(self.data)
        with open(os.path.join(self.data, "errors.log"), "w") as fh:
            fh.write("x" * 100_001)
        with mock.patch.object(rollout, "snapshot", side_effect=RuntimeError("x")):
            self.submit(self.conversation())
        self.assertTrue(self.errors().endswith("prompt_submit/snapshot RuntimeError\n"))
        self.assertEqual(len(self.errors().splitlines()), 1)
        self.assertTrue(os.path.exists(os.path.join(self.data, "errors.log.1")))


class TriggerTest(Base):
    def test_demo(self):
        out = self.submit(None, prompt="  Switch-Advisor   DEMO \n")        # no log is needed
        self.assertEqual(out, {"decision": "block", "reason": SPEC_DEMO})
        self.assertEqual(self.decisions(), [{"t": LAST + AWAY, "event": "demo", "session": MAIN, "version": VERSION}])
        self.assertIsNone(self.pending())
        self.assertEqual(state.session(self.data, MAIN), {})
        self.assertEqual(self.submit(None, prompt="switch-advisor demo", env={})["reason"], SPEC_DEMO)

    def test_test_card_with_this_conversations_numbers(self):
        path = self.conversation(then="switch-advisor test")
        out = self.submit(path, prompt="switch-advisor test", at=600)       # no break at all
        flags = rule.quality_flags(rollout.facts(path, now=LAST + 600), hook.TEST_PROMPT, len(FILES))
        card = rule.after_break(410_000, 3 * HOUR, flags, rule.Params(), ASTRA, 832_200, label="TEST · ")
        paras = out["reason"].split("\n\n")
        self.assertTrue(card.show)
        self.assertEqual(set(out), {"decision", "reason"})        # a card that would show: no note with it
        self.assertEqual(paras[:-1], card.text.split("\n\n")[:-1])
        self.assertTrue(paras[0].startswith("TEST · Switch advisor: you were away 3.0 h, so this conversation's "
                                            "prompt cache has most likely expired (it had after 58% of breaks"))
        self.assertEqual(paras[-1], "To try the switch: start a new conversation in this project folder and send a "
                                    "prompt there within 30 minutes; it starts from the handoff made just now. This was "
                                    "only a test: nothing was sent, and this conversation is checked as usual.")
        meta, text = self.pending()
        self.assertEqual((meta["from_session"], meta["test"], meta["numbers"]), (MAIN, True, card.numbers))
        self.assertNotIn("switch-advisor test", text)
        self.assertEqual(self.events(), ["test"])
        self.assertEqual(state.session(self.data, MAIN), {})
        ctx = self.start(at=900)
        self.assertEqual(ctx["hookSpecificOutput"]["additionalContext"], text)
        switched = self.decisions()[-1]
        self.assertEqual((switched["event"], switched["test"], switched["from_session"]), ("switched", True, MAIN))

    def test_test_card_when_a_switch_would_not_pay(self):
        out = self.submit(self.conversation(last=(40_000, 35_000, 2_000), window=258_400), prompt="switch-advisor test")
        self.assertTrue(out["reason"].startswith("TEST · A switch wouldn't pay here; this is what the card would look "
                                                 "like. Switch advisor: you were away 3.0 h"))
        self.assertIn("That costs about", out["reason"])
        self.assertEqual(set(out), {"decision", "reason"})        # too small even for a note: nothing with it
        self.assertEqual(self.events(), ["test"])

    def test_test_card_shows_the_note_a_real_break_would_give(self):
        """200K tokens on a 258,400 window: after 3 hours the rule gives no card but the one-line note. The test shows
        that note with the card (Codex shows a block's systemMessage and still holds the prompt back), and says so."""
        path = self.conversation(last=(195_000, 190_000, 5_000), window=258_400)
        out = self.submit(path, prompt="switch-advisor test", at=600)
        flags = rule.quality_flags(rollout.facts(path, now=LAST + 600), hook.TEST_PROMPT, len(FILES))
        card = rule.after_break(200_000, 3 * HOUR, flags, rule.Params(), ASTRA, 258_400, label="TEST · ")
        self.assertFalse(card.show)
        self.assertEqual(set(out), {"decision", "reason", "systemMessage"})
        self.assertEqual(out["systemMessage"], card.note)
        self.assertEqual(card.note, "TEST · Switch advisor: back after 3.0 h, this message likely re-reads about 200K "
                                    "tokens without the cache (it had expired after 58% of breaks this long): about "
                                    "$1.82, against $0.20 with a warm cache.")
        self.assertEqual(out["reason"].split("\n\n")[-1],
                         "To try the switch: start a new conversation in this project folder and send a prompt there "
                         "within 30 minutes; it starts from the handoff made just now. This was only a test: nothing was "
                         "sent, and this conversation is checked as usual. After a real break you would get the one-line "
                         "note shown with this card instead.")
        self.assertEqual(self.events(), ["test"])
        # with the guardrail off too, the note is still the reason: one sentence, not the guardrail's as well
        state.write_json(os.path.join(self.data, "guardrail.json"), {"after_break_enabled": False})
        out = self.submit(path, prompt="switch-advisor test", at=700)
        self.assertTrue(out["reason"].endswith("checked as usual. After a real break you would get the one-line note "
                                               "shown with this card instead."))
        self.assertEqual(out["systemMessage"], card.note)

    def test_test_card_durations_as_the_card_states_them(self):
        """hook._duration is rule._duration: the test card never promises more time than the hook gives."""
        for s in (60, 90, 900, 1800, 3600, 3660, 5400, 7140):
            self.assertEqual(hook._duration(s), rule._duration(s))
        state.write_json(os.path.join(self.data, "params.json"), {"handoff_ttl_s": 90})
        out = self.submit(self.conversation(), prompt="switch-advisor test")
        self.assertIn("To try the switch: start a new conversation in this project folder and send a prompt there "
                      "within 90 seconds;", out["reason"])

    def test_test_card_without_a_data_folder(self):
        """Without a data folder the prompt hook keeps no handoff, so a real break gives the note: the test shows it."""
        out = self.submit(self.conversation(), prompt="switch-advisor test", env={"CODEX_HOME": self.home})
        self.assertTrue(out["reason"].endswith("This was only a test: nothing was sent, and no handoff was made, "
                                               "because Codex gave the plugin no data folder to keep it in. After a "
                                               "real break you would get the one-line note shown with this card "
                                               "instead."))
        self.assertTrue(out["systemMessage"].startswith("TEST · Switch advisor: back after 3.0 h"))
        self.assertFalse(os.path.exists(self.data))

    def test_test_card_mentions_the_guardrail(self):
        state.write_json(os.path.join(self.data, "guardrail.json"), {"after_break_enabled": False})
        out = self.submit(self.conversation(), prompt="switch-advisor test")
        self.assertTrue(out["reason"].endswith("After a real break you would get the one-line note shown with this "
                                               "card instead, because the monitor's guardrail has turned the card "
                                               "off."))
        self.assertTrue(out["systemMessage"].startswith("TEST · Switch advisor: back after 3.0 h"))

    def test_test_card_in_the_quiet_period_says_nothing_would_show(self):
        """After you chose to stay, the hook leaves this conversation alone for 12 hours: the test says so instead of
        promising a card or note, or a conversation checked as usual."""
        path = self.conversation()
        self.submit(path)
        self.submit(path, at=AWAY + 60)                           # stay: quiet for 12 hours
        out = self.submit(path, prompt="switch-advisor test", at=AWAY + HOUR + 60)
        self.assertTrue(out["reason"].endswith("This was only a test: nothing was sent. After a real break you would "
                                               "get nothing here, because you chose to stay in this conversation, so "
                                               "it isn't checked for 11 more hours."))
        self.assertNotIn("systemMessage", out)
        self.assertIsNone(self.submit(path, at=AWAY + 2 * HOUR))  # and a real prompt then gets nothing indeed

    def test_test_card_with_no_note_says_the_card_wouldnt_show(self):
        """With the note's thresholds raised by hand (params.json), a card the hook would turn into the note leaves
        nothing at all: the test says so, and why."""
        state.write_json(os.path.join(self.data, "params.json"), {"note_min_usd": 1000})
        state.write_json(os.path.join(self.data, "guardrail.json"), {"after_break_enabled": False})
        out = self.submit(self.conversation(), prompt="switch-advisor test")
        self.assertTrue(out["reason"].endswith("After a real break this card wouldn't show, because the monitor's "
                                               "guardrail has turned the card off, and the rule gives no note for it "
                                               "either."))
        self.assertNotIn("systemMessage", out)

    def test_test_card_without_a_typed_request_shows_the_note(self):
        """A log from before Codex marked typed text holds no request for a handoff to carry, so the prompt hook gives
        the note instead of the card: the test shows that note, and says why."""
        out = self.submit(self.conversation(typed=False), prompt="switch-advisor test")
        self.assertTrue(out["reason"].endswith("After a real break you would get the one-line note shown with this "
                                               "card instead, because this conversation's log holds no request you "
                                               "typed for a handoff to carry."))
        self.assertTrue(out["systemMessage"].startswith("TEST · Switch advisor: back after 3.0 h"))
        # and the real prompt after such a break gets just that note
        self.assertEqual(set(self.submit(self.conversation(typed=False), at=AWAY + 7200)), {"systemMessage"})

    def test_test_with_nothing_to_show_still_holds_the_trigger_back(self):
        def nothing_yet():
            r = fx.Rollout(self.home, "0b1c2d3e-0000-4000-8000-0000000000b5", self.cwd)
            r.meta()
            r.turn(ASTRA, 258_400)
            r.user("Hello.")
            return r.write()
        cases = [
            (lambda: None, "there is no log of this conversation yet"),
            (lambda: self.conversation(thread="0b1c2d3e-0000-4000-8000-0000000000b1", model="gpt-unknown-9"),
             "there is no known price for this conversation's model (gpt-unknown-9)"),
            (lambda: self.conversation(thread="0b1c2d3e-0000-4000-8000-0000000000b2", compacted=True),
             "Codex compacted this conversation after its last response"),
            (lambda: self.conversation(thread=SUB, kind="subagent", parent=MAIN), "this is an agent's thread"),
            (nothing_yet, "this conversation has no model response yet"),
        ]
        for make, words in cases:
            with self.subTest(words=words):
                out = self.submit(make(), prompt="switch-advisor test", sid="0b1c2d3e-0000-4000-8000-00000000dead")
                self.assertTrue(out["reason"].startswith("TEST · Switch advisor: "))
                self.assertIn(words, out["reason"])
        self.assertIsNone(self.pending())

    def test_triggers_set_no_offer(self):
        path = self.conversation()
        self.submit(path, prompt="switch-advisor test")
        self.submit(path, prompt="switch-advisor demo", at=AWAY + 10)
        self.assertEqual(shape(self.submit(path, at=AWAY + 20)), "block")      # a card, not "stay here"
        self.assertEqual(self.events(), ["test", "demo", "offer"])

    def test_only_the_whole_prompt_triggers(self):
        out = self.submit(self.conversation(), prompt="please run switch-advisor demo for me")
        self.assertNotEqual(out["reason"], SPEC_DEMO)
        self.assertEqual(self.events(), ["offer"])


class SessionStartTest(Base):
    def offer(self):
        self.submit(self.conversation())
        return self.pending()

    def test_a_new_conversation_in_the_same_folder_gets_the_handoff(self):
        meta, text = self.offer()
        out = self.start()
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
        self.assertTrue(text.startswith(handoff.INTRO))
        self.assertIsNone(self.pending())
        offer, switched = self.decisions()
        self.assertEqual(switched, {"t": LAST + AWAY + 600, "event": "switched", "session": NEW, "version": VERSION,
                                    "from_session": MAIN, "numbers": meta["numbers"], "cwd_key": state.cwd_key(self.cwd)})
        self.assertIsNone(self.start(sid="0b1c2d3e-0000-4000-8000-000000000009"))     # used once

    def test_another_folder_gets_nothing(self):
        self.offer()
        other = os.path.join(self._tmp.name, "other-project")
        self.assertIsNone(self.start(cwd=other))
        self.assertIsNotNone(self.pending())

    def test_an_old_handoff_is_deleted(self):
        self.offer()
        self.assertIsNone(self.start(at=AWAY + 1801))
        self.assertIsNone(self.pending())
        self.assertEqual(self.decisions()[-1], {"t": LAST + AWAY + 1801, "event": "expired", "session": None,
                                                "version": VERSION, "from_session": MAIN,
                                                "cwd_key": state.cwd_key(self.cwd)})

    def test_a_startup_or_a_clear_counts(self):
        """The CLI's /clear starts a new conversation (source "clear"), as /new and the app do ("startup"); resume,
        compact and fork continue an existing one."""
        _meta, text = self.offer()
        for source in ("resume", "compact", "fork", None, "Startup"):
            with self.subTest(source=source):
                self.assertIsNone(self.start(source=source))
        self.assertIsNotNone(self.pending())
        out = self.start(source="clear")
        self.assertEqual(out, {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
        self.assertEqual(self.events(), ["offer", "switched"])

    def test_a_clear_in_the_conversation_that_made_it_leaves_it(self):
        self.offer()
        self.assertIsNone(self.start(sid=MAIN, source="clear"))
        self.assertIsNotNone(self.pending())

    def test_the_conversation_that_made_it_leaves_it(self):
        self.offer()
        self.assertIsNone(self.start(sid=MAIN))
        self.assertIsNotNone(self.pending())
        self.assertEqual(self.pending()[0]["at"], LAST + AWAY)
        self.assertEqual(shape(self.start()), "context")

    def test_an_agents_thread_leaves_it(self):
        self.offer()
        sub = fx.Rollout(self.home, NEW, self.cwd, kind="subagent", parent=MAIN)
        sub.meta(t=LAST + AWAY + 500)
        self.assertIsNone(self.start(path=sub.write()))
        self.assertIsNotNone(self.pending())

    def test_no_data_folder_or_fields(self):
        self.offer()
        self.assertIsNone(self.start(env={"CODEX_HOME": self.home}))
        for payload in ({"source": "startup", "cwd": self.cwd}, {"source": "startup", "session_id": NEW}, None, []):
            self.assertIsNone(hook.on_session_start(payload, now=LAST + AWAY + 600, env=self.env))
        self.assertIsNotNone(self.pending())

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_the_folder_spelled_another_way(self):
        self.offer()
        self.assertEqual(shape(self.start(cwd=self.cwd.upper().replace("\\", "/") + "/")), "context")

    def test_the_folder_opened_through_a_link(self):
        """The new conversation is opened through a junction (Windows) or a symbolic link to the same folder."""
        link = os.path.join(self._tmp.name, "project-link")
        if not fx.link_folder(self.cwd, link):
            self.skipTest("this system can't make a link to a folder")
        self.offer()
        self.assertEqual(shape(self.start(cwd=link)), "context")
        self.assertEqual(self.decisions()[-1]["from_session"], MAIN)


OTHER = "0b1c2d3e-0000-4000-8000-000000000003"
NOTES = "Write the release notes for the parser."


class OneFolderTest(Base):
    """Two conversations in one project folder, both back after the break. The folder keeps one handoff: the second
    one must not replace the first while that one's card can still be followed."""

    def two(self):
        return self.conversation(), self.conversation(thread=OTHER, first=NOTES)

    def test_a_second_card_leaves_the_first_handoff(self):
        a, b = self.two()
        self.assertEqual(shape(self.submit(a)), "block")
        out = self.submit(b, sid=OTHER, at=AWAY + 120)
        self.assertEqual(shape(out), "note")          # B's card would have replaced A's handoff: B gets the note
        self.assertTrue(out["systemMessage"].startswith("Switch advisor: back after 8.2 h, this message likely "
                                                        "re-reads about 410K tokens"))
        self.assertEqual(shape(self.submit(b, sid=OTHER, at=AWAY + 180)), "note")    # sent again: still the note
        meta, text = self.pending()
        self.assertEqual(meta["from_session"], MAIN)
        ctx = self.start(at=AWAY + 300)                # the person follows A's card
        self.assertEqual(ctx["hookSpecificOutput"]["additionalContext"], text)
        self.assertIn(FIRST, text)
        self.assertNotIn(NOTES, text)
        self.assertEqual([(e["event"], e["session"], e.get("from_session")) for e in self.decisions()],
                         [("offer", MAIN, None), ("note", OTHER, None), ("note", OTHER, None),
                          ("switched", NEW, MAIN)])
        self.assertEqual(state.session(self.data, OTHER), {})
        # the folder's handoff has been used: B's next prompt can have its own card
        self.assertEqual(shape(self.submit(b, sid=OTHER, at=AWAY + 400)), "block")
        self.assertEqual(self.pending()[0]["from_session"], OTHER)

    def test_an_expired_handoff_no_longer_holds_the_folder(self):
        a, b = self.two()
        self.submit(a)
        self.assertEqual(shape(self.submit(b, sid=OTHER, at=AWAY + 1801)), "block")
        self.assertEqual(self.pending()[0]["from_session"], OTHER)
        self.assertEqual(self.events(), ["offer", "expired", "offer"])

    def test_a_test_leaves_the_first_handoff(self):
        a, b = self.two()
        self.submit(a)
        out = self.submit(b, sid=OTHER, prompt="switch-advisor test", at=AWAY + 60)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].endswith(
            "This was only a test: nothing was sent, and no handoff was made, because a handoff from another "
            "conversation in this project is still waiting for a new conversation, and a test doesn't replace it. "
            "After a real break you would get the one-line note shown with this card instead."))
        self.assertIn("systemMessage", out)          # what B's real prompt would get while A's handoff waits
        self.assertEqual(self.pending()[0]["from_session"], MAIN)
        self.start(at=AWAY + 300)
        switched = self.decisions()[-1]
        self.assertEqual((switched["event"], switched["from_session"], switched.get("test")), ("switched", MAIN, None))

    def test_a_test_never_replaces_a_cards_handoff_even_its_own(self):
        """A shows a card, and a minute later `switch-advisor test` is typed in A itself. The handoff is the card's:
        it stays, so following the card starts from it and counts as a real switch."""
        a, _b = self.two()
        self.assertEqual(shape(self.submit(a)), "block")
        before = self.pending()
        out = self.submit(a, prompt="switch-advisor test", at=AWAY + 60)
        self.assertTrue(out["reason"].endswith(
            "This was only a test: nothing was sent, and no handoff was made, because the handoff from this "
            "conversation's card is still waiting for a new conversation, and a test doesn't replace it."))
        self.assertEqual(self.pending(), before)
        ctx = self.start(at=AWAY + 300)
        self.assertEqual(ctx["hookSpecificOutput"]["additionalContext"], before[1])
        self.assertEqual([(e["event"], e.get("test")) for e in self.decisions()],
                         [("offer", None), ("test", None), ("switched", None)])

    def test_a_test_replaces_an_earlier_tests_handoff(self):
        a, b = self.two()
        self.submit(b, sid=OTHER, prompt="switch-advisor test", at=60)
        self.assertEqual(self.pending()[0]["from_session"], OTHER)
        out = self.submit(a, prompt="switch-advisor test", at=120)            # another conversation's test
        self.assertIn("it starts from the handoff made just now", out["reason"])
        self.assertEqual((self.pending()[0]["from_session"], self.pending()[0]["test"]), (MAIN, True))
        self.submit(a, prompt="switch-advisor test", at=180)                  # its own earlier test
        self.assertEqual(self.pending()[0]["at"], LAST + 180)

    def test_a_tests_handoff_does_not_hold_the_folder(self):
        """`switch-advisor test` in A keeps a test handoff. B, back after its break 10 minutes later, still gets its
        card, whose handoff replaces the test's."""
        a, b = self.two()
        self.submit(a, prompt="switch-advisor test", at=AWAY - 600)
        self.assertIs(self.pending()[0]["test"], True)
        self.assertEqual(shape(self.submit(b, sid=OTHER)), "block")
        meta, text = self.pending()
        self.assertEqual(meta["from_session"], OTHER)
        self.assertNotIn("test", meta)
        self.assertIn(NOTES, text)
        self.assertEqual(self.events(), ["test", "offer"])
        self.assertEqual(shape(self.submit(a, at=AWAY + 60)), "note")       # now A's card would replace B's handoff


class ClockTest(Base):
    """A handoff made while the clock ran fast, set right since: its time is ahead of the clock. More than a minute
    ahead, it counts as expired everywhere: it holds no folder and reaches no new conversation."""

    def test_it_holds_no_folder(self):
        b = self.conversation(thread=OTHER, first=NOTES)
        os.makedirs(self.data)
        state.put_pending(self.data, self.cwd, MAIN, "notes from a fast clock", {}, LAST + AWAY + 3 * HOUR)
        self.assertEqual(shape(self.submit(b, sid=OTHER)), "block")         # B's card, not the note
        self.assertEqual(self.pending()[0]["from_session"], OTHER)
        self.assertEqual([(e["event"], e.get("from_session")) for e in self.decisions()],
                         [("expired", MAIN), ("offer", None)])

    def test_it_reaches_no_new_conversation(self):
        os.makedirs(self.data)
        state.put_pending(self.data, self.cwd, MAIN, "notes from a fast clock", {}, LAST + AWAY + 3 * HOUR)
        self.assertIsNone(self.start(at=AWAY))
        self.assertIsNone(self.pending())
        self.assertEqual(self.events(), ["expired"])

    def test_each_check_counts_it_expired(self):
        """Even where prune didn't get to it first."""
        os.makedirs(self.data)
        P = rule.Params()
        now = LAST + AWAY
        for ahead, waiting in ((3 * HOUR, False), (61, False), (60, True), (0, True)):
            state.put_pending(self.data, self.cwd, MAIN, "notes", {}, now + ahead)
            with self.subTest(ahead=ahead):
                self.assertIs(hook._waiting(self.data, self.cwd, OTHER, now, P), waiting)
        state.put_pending(self.data, self.cwd, MAIN, "notes", {}, now + 3 * HOUR)
        with mock.patch.object(state, "prune"):
            self.assertIsNone(self.start(at=AWAY))
        self.assertIsNone(self.pending())
        self.assertEqual(self.decisions()[-1], {"t": now, "event": "expired", "session": None, "version": VERSION,
                                                "from_session": MAIN, "cwd_key": state.cwd_key(self.cwd)})


class PhoneTest(Base):
    """A prompt sent from the ChatGPT phone app to a conversation on this computer: its turn runs here, so the hooks
    run, and Codex writes the turn's task_started, with turn_attribution.turn_trigger "remote_android" and the hook's
    turn_id, before the prompt hook runs. The phone may not show a card, so the prompt is never held back: it gets the
    note instead. The same goes for every trigger but the app's composer's and the CLI's. The decision log records
    the trigger."""

    def test_a_phone_prompt_after_a_break_gets_the_note(self):
        out = self.submit(self.conversation(trigger="remote_android"))
        self.assertEqual(shape(out), "note")
        self.assertTrue(out["systemMessage"].startswith("Switch advisor: back after"))
        self.assertIsNone(self.pending())
        last = self.decisions()[-1]
        self.assertEqual((last["event"], last["trigger"]), ("note", "remote_android"))
        self.assertNotIn("offered_at", state.session(self.data, MAIN))   # nothing to stay with: the next is checked

    def test_a_prompt_typed_at_the_computer_still_gets_the_card(self):
        out = self.submit(self.conversation(trigger="composer"))
        self.assertEqual(shape(out), "block")
        last = self.decisions()[-1]
        self.assertEqual((last["event"], last["trigger"]), ("offer", "composer"))

    def test_any_remote_trigger_counts_and_only_for_this_turn(self):
        """Any trigger starting with "remote" (an iPhone's may differ) is the phone. A task_started of another turn
        (an id that isn't the hook's) or one without a trigger (older Codex) can't tell, so the card shows as before."""
        self.assertEqual(shape(self.submit(self.conversation(trigger="remote_ios"))), "note")
        for name, kw in (("another turn", {"trigger": "remote_android", "turn_id": "turn-older"}),
                         ("no trigger", {})):
            with self.subTest(name):
                state.drop_pending(self.data, self.cwd)
                state.save_session(self.data, MAIN, {})
                path = self.conversation(**kw)          # each writes the same log, so built just before its submit
                self.assertEqual(rollout.snapshot(path).trigger, kw.get("trigger"))
                self.assertEqual(shape(self.submit(path)), "block")

    def test_a_trigger_that_isnt_an_identifier_is_logged_as_other_and_gets_the_note(self):
        """A trigger that isn't a short identifier (too long, or with other characters) isn't one typed here either:
        it gets the note, and the decision log says "other" rather than the raw value."""
        for odd in ("remote android", "remote_" + "x" * 60, "composer\n", "automation:cron", "a" * 41):
            with self.subTest(trigger=odd):
                state.drop_pending(self.data, self.cwd)
                state.save_session(self.data, MAIN, {})
                out = self.submit(self.conversation(trigger=odd))
                self.assertEqual(shape(out), "note")
                self.assertEqual(self.decisions()[-1]["trigger"], "other")
        state.drop_pending(self.data, self.cwd)
        state.save_session(self.data, MAIN, {})
        out = self.submit(self.conversation(trigger="automation:cron"), prompt="switch-advisor test")
        self.assertTrue(out["reason"].endswith("because Codex doesn't mark it as typed at this computer, and nobody "
                                               "may be there to answer the card."))

    def test_switch_advisor_test_from_the_phone_shows_the_card_and_the_note(self):
        """The test is held back on the phone too, so you can see whether the phone shows a card; it says that a real
        break would give the note there, and shows it."""
        out = self.submit(self.conversation(trigger="remote_android"), prompt="switch-advisor test")
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].endswith("After a real break you would get the one-line note shown with this "
                                               "card instead, because it came from your phone, which may not show "
                                               "the card."))
        self.assertTrue(out["systemMessage"].startswith("TEST · Switch advisor: back after 3.0 h"))
        last = self.decisions()[-1]
        self.assertEqual((last["event"], last["trigger"]), ("test", "remote_android"))
        self.assertTrue(self.pending()[0]["test"])            # the switch can still be tried, from the phone too

    def test_the_demo_is_held_back_on_the_phone_too(self):
        self.assertEqual(self.submit(self.conversation(trigger="remote_android"),
                                     prompt="switch-advisor demo")["decision"], "block")

    def test_only_a_prompt_typed_here_can_be_held_back(self):
        """The card holds back only a prompt typed at this computer: the app's composer, the CLI ("user"), or a turn
        with no trigger recorded. An automation, a retry, an answer from the question panel or a trigger Codex adds
        later gets the note: nobody may be there to answer the card, and an automation's prompt would just be lost."""
        for trigger, held in (("user", True), ("composer", True), ("automation_cron_7f3a", False),
                              ("automation_heartbeat_7f3a", False), ("capacity_retry_manual", False),
                              ("send_user_message_async_question", False), ("safety_recourse_follow_up", False),
                              ("something_new", False)):
            with self.subTest(trigger=trigger):
                state.drop_pending(self.data, self.cwd)
                state.save_session(self.data, MAIN, {})
                out = self.submit(self.conversation(trigger=trigger))
                self.assertEqual(shape(out), "block" if held else "note")
                self.assertEqual(self.decisions()[-1]["trigger"], trigger)
                self.assertEqual(self.pending() is not None, held)

    def test_a_trigger_prompt_that_fails_is_still_held_back(self):
        """The two trigger prompts never need the model: when one fails partway (a data folder that can't be written,
        say), it is held back with what failed, not sent; errors.log gets the step. Any other prompt is sent."""
        path = self.conversation()
        with mock.patch.object(state, "put_pending", side_effect=PermissionError("denied")):
            out = self.submit(path, prompt="switch-advisor test")
        self.assertEqual(out, {"decision": "block", "reason": "Switch advisor: \"switch-advisor test\" stopped at step "
                                                              "\"test\" (PermissionError), so there is no card to show. "
                                                              "Nothing was sent."})
        with mock.patch.object(hook, "_demo", side_effect=OSError("no")):
            self.assertEqual(self.submit(path, prompt="switch-advisor demo")["decision"], "block")
        with open(os.path.join(self.data, "errors.log"), encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("prompt_submit/test PermissionError", log)
        self.assertIn("prompt_submit/demo OSError", log)
        with mock.patch.object(state, "put_pending", side_effect=PermissionError("denied")):
            self.assertIsNone(self.submit(path))          # a real prompt whose card fails: sent, as always

    def test_switch_advisor_test_from_an_automation_says_why(self):
        out = self.submit(self.conversation(trigger="automation_cron_7f3a"), prompt="switch-advisor test")
        self.assertTrue(out["reason"].endswith("After a real break you would get the one-line note shown with this "
                                               "card instead, because Codex marks it automation_cron_7f3a, not typed "
                                               "at this computer, and nobody may be there to answer the card."))
        self.assertIn("systemMessage", out)


class AgentTest(Base):
    """A prompt typed in an agent's thread (after switching to it in the CLI): Codex sends the main conversation's
    session_id, the agent's agent_id and agent_type, and the agent's own log. The hook leaves it alone."""

    def in_agent(self, path, prompt=PROMPT, at=AWAY + 300, **fields):
        payload = {"session_id": MAIN, "transcript_path": path, "cwd": self.cwd, "hook_event_name": "UserPromptSubmit",
                   "model": ASTRA, "turn_id": "turn-agent", "permission_mode": "default", "prompt": prompt,
                   "agent_id": "019a0000-0000-7000-8000-0000000000a1", "agent_type": "worker"}
        payload.update(fields)
        out = hook.on_prompt_submit(payload, now=LAST + at, env=self.env)
        shape(out)
        return out

    def agent_log(self):
        r = fx.Rollout(self.home, SUB, self.cwd, kind="subagent", parent=MAIN)
        r.meta(t=LAST - HOUR)
        r.turn(ASTRA, 258_400, t=LAST - HOUR)
        r.user("Check the parser tests.")
        r.call(LAST - HOUR + 60, 120_000, 100_000, 2_000)
        r.complete(LAST - HOUR + 61, "The parser tests pass.")
        return r.write()

    def test_the_main_conversations_card_stays_as_it_was(self):
        path = self.conversation()
        self.assertEqual(shape(self.submit(path)), "block")              # the card in the main conversation
        before = (self.pending(), state.session(self.data, MAIN))
        self.assertIsNone(self.in_agent(self.agent_log()))                # 5 minutes later, in its agent's thread
        self.assertEqual((self.pending(), state.session(self.data, MAIN)), before)   # not a stay
        self.assertEqual(self.events(), ["offer"])
        ctx = self.start(at=AWAY + 600)                                    # the card's switch still works
        self.assertEqual(ctx["hookSpecificOutput"]["additionalContext"], before[0][1])

    def test_no_card_or_note_in_an_agents_thread(self):
        self.conversation()
        self.assertIsNone(self.in_agent(self.agent_log(), at=AWAY))
        self.assertIsNone(self.in_agent(None, at=AWAY))      # no log given: the main conversation's would be found
        self.assertIsNone(self.in_agent(None, at=AWAY, agent_id=None))     # agent_type alone
        self.assertEqual(self.decisions(), [])

    def test_the_test_trigger_says_it_is_an_agents_thread(self):
        self.conversation()
        for path in (self.agent_log(), None):
            out = self.in_agent(path, prompt="switch-advisor test")
            self.assertEqual(out, {"decision": "block", "reason": "TEST · Switch advisor: this is an agent's thread; "
                                                                  "only your own conversations are checked."})
        self.assertIsNone(self.pending())

    def review_log(self, at):
        """A /review delegate's own log: Codex marks it a subagent's (source {"subagent": "review"}), and it has no
        model call yet when the delegate's prompt hook runs."""
        r = fx.Rollout(self.home, "0b1c2d3e-0000-4000-8000-0000000000b1", self.cwd, kind="subagent", parent=MAIN)
        r.meta(t=LAST + at)
        r.turn(ASTRA, 258_400, t=LAST + at)
        path = r.write()
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
        meta = json.loads(lines[0])
        meta["payload"]["source"] = {"subagent": "review"}
        lines[0] = json.dumps(meta, separators=(",", ":"))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines))
        return path

    def test_a_review_soon_after_a_card_is_not_a_stay(self):
        """/review runs as a delegate thread whose prompt hook gets the main conversation's session_id and no agent
        fields. Within 15 minutes of a card it must not count as staying: the card's handoff and the main
        conversation's state stay as they were, and the switch still works."""
        path = self.conversation()
        self.assertEqual(shape(self.submit(path)), "block")
        before = (self.pending(), state.session(self.data, MAIN))
        payload = {"session_id": MAIN, "turn_id": "turn-review", "transcript_path": self.review_log(AWAY + 300),
                   "cwd": self.cwd, "hook_event_name": "UserPromptSubmit", "model": ASTRA,
                   "permission_mode": "default", "prompt": "Review the current code changes."}
        self.assertIsNone(hook.on_prompt_submit(payload, now=LAST + AWAY + 300, env=self.env))
        self.assertEqual((self.pending(), state.session(self.data, MAIN)), before)
        self.assertEqual(self.events(), ["offer"])
        ctx = self.start(at=AWAY + 600)
        self.assertEqual(ctx["hookSpecificOutput"]["additionalContext"], before[0][1])

    def test_only_a_non_empty_text_names_an_agent(self):
        for fields, agent in (({"agent_id": "a"}, True), ({"agent_type": "worker"}, True), ({"agent_id": ""}, False),
                              ({"agent_id": None, "agent_type": ""}, False), ({"agent_id": 7}, False), ({}, False)):
            with self.subTest(fields=fields):
                self.assertIs(hook._agent(dict({"session_id": MAIN}, **fields)), agent)
        self.assertEqual(shape(self.in_agent(self.conversation(), at=AWAY, agent_id="", agent_type="")), "block")


class RunningTurnTest(Base):
    """The conversation's latest turn is still running and has made model calls (Snapshot.turn_running): Codex calls
    the model again when its tool returns, whatever is sent now. The hook leaves the prompt alone and writes nothing."""

    def running(self, path):
        snap = rollout.snapshot(path)
        snap.turn_running = True
        return mock.patch.object(rollout, "snapshot", return_value=snap)

    def test_nothing_is_shown_or_written(self):
        path = self.conversation()
        with self.running(path):
            self.assertIsNone(self.submit(path))
        self.assertEqual(self.decisions(), [])
        self.assertIsNone(self.pending())
        self.assertEqual(state.session(self.data, MAIN), {})
        self.assertEqual(shape(self.submit(path)), "block")      # the same moment once the turn has ended: the card

    def test_a_prompt_soon_after_a_card_is_no_stay_while_a_turn_runs(self):
        path = self.conversation()
        self.submit(path)
        before = (self.pending(), state.session(self.data, MAIN))
        with self.running(path):
            self.assertIsNone(self.submit(path, at=AWAY + 60))
        self.assertEqual((self.pending(), state.session(self.data, MAIN)), before)
        self.assertEqual(self.events(), ["offer"])

    def test_the_triggers_still_answer(self):
        """The test says that a real prompt now would get nothing, as the hook leaves it alone."""
        path = self.conversation()
        with self.running(path):
            self.assertEqual(self.submit(path, prompt="switch-advisor demo")["reason"], SPEC_DEMO)
            out = self.submit(path, prompt="switch-advisor test")
        self.assertTrue(out["reason"].startswith("TEST · "))
        self.assertTrue(out["reason"].endswith("This was only a test: nothing was sent. After a real break you would "
                                               "get nothing here, because its latest turn hasn't ended, and a turn "
                                               "counts as running for 24 hours after its last record."))
        self.assertNotIn("systemMessage", out)

    def test_a_quiet_conversation_reads_no_log(self):
        path = self.conversation()
        self.submit(path)
        self.submit(path, at=AWAY + 60)                           # stay: quiet for 12 hours
        with mock.patch.object(rollout, "snapshot") as snap:
            self.assertIsNone(self.submit(path, at=AWAY + 2 * HOUR))
        snap.assert_not_called()

    def test_a_running_turn_from_the_log(self):
        """The same from a log, by the real clock: the latest turn made its model call 2 hours ago and hasn't ended
        (its tool is still running)."""
        now = time.time()
        r = fx.Rollout(self.home, MAIN, self.cwd)
        r.meta(t=now - 3 * HOUR)
        r.turn(ASTRA, 832_200, t=now - 3 * HOUR)
        r.user(FIRST, t=now - 3 * HOUR + 5)
        r.call(now - 2 * HOUR, 400_000, 390_000, 10_000)
        payload = {"session_id": MAIN, "transcript_path": r.write(), "cwd": self.cwd, "prompt": PROMPT}
        self.assertIsNone(hook.on_prompt_submit(payload, env=self.env))
        self.assertEqual(self.decisions(), [])
        r.complete(now - 2 * HOUR + 1, REPLY)                  # the turn ended: the same prompt gets the card
        r.write()
        self.assertEqual(shape(hook.on_prompt_submit(payload, env=self.env)), "block")

    def test_a_turn_a_stopped_app_left_open(self):
        """A turn left open three days ago (a model call, then the app was stopped) isn't running, also once the app
        has opened the conversation again: Codex then logs thread_settings_applied, outside any turn, and again just
        before the prompt's turn starts. The card shows."""
        r = fx.Rollout(self.home, MAIN, self.cwd)
        r.meta()
        r.turn(ASTRA, 832_200, t=LAST - 600)
        r.user(FIRST, t=LAST - 595)
        r.call(LAST, 400_000, 390_000, 10_000)                 # its tool started; no task_complete ever came
        settings = {"type": "thread_settings_applied", "thread_id": MAIN,
                    "thread_settings": {"model": ASTRA, "service_tier": "default", "cwd": self.cwd}}
        r.event(settings, LAST + 3 * DAY - 600)                # opened again 10 minutes ago
        r.event(settings, LAST + 3 * DAY - 1)                  # the prompt being sent
        path = r.write()
        self.assertFalse(rollout.snapshot(path, now=LAST + 3 * DAY).turn_running)
        self.assertEqual(shape(self.submit(path, at=3 * DAY)), "block")
        self.assertEqual(self.events(), ["offer"])


class RefreshRateTest(Base):
    NOW = T0 + 8 * HOUR

    def meter_log(self):
        """A weekly window whose meter rose from 20% to 30% while 12 requests of $1.00 each were made: $1.20 per 1%."""
        r = fx.Rollout(self.home, MAIN, self.cwd)
        r.meta()
        r.turn(ASTRA, 258_400, t=T0)
        r.meter(T0 + 60, 20.0, resets_at=T0 + 3 * DAY)
        for i in range(12):
            r.call(T0 + HOUR + i * 1200, 100_000, 0, 0)
        r.meter(T0 + 7 * HOUR, 30.0, resets_at=T0 + 3 * DAY)
        return r.write()

    def rate_json(self):
        return state.read_json(os.path.join(self.data, "rate.json"))

    def test_writes_rate_json(self):
        self.meter_log()
        self.assertIsNone(hook.refresh_rate(now=self.NOW, env=self.env))
        got = self.rate_json()
        self.assertEqual(got, rate.compute_rate(home=self.home, now=self.NOW))
        self.assertEqual(got["usd_per_pt"], 1.2)
        self.assertFalse(os.path.exists(os.path.join(self.data, "rate.lock")))

    def test_measured_at_most_twice_a_day(self):
        self.meter_log()
        real = rate.compute_rate
        with mock.patch.object(rate, "compute_rate", side_effect=real) as compute:
            hook.refresh_rate(now=self.NOW, env=self.env)
            hook.refresh_rate(now=self.NOW + 12 * HOUR - 1, env=self.env)
            self.assertEqual(compute.call_count, 1)
            hook.refresh_rate(now=self.NOW + 12 * HOUR, env=self.env)
            self.assertEqual(compute.call_count, 2)

    def test_no_window_yet_is_written_as_null(self):
        self.conversation()
        hook.refresh_rate(now=LAST + AWAY, env=self.env)
        self.assertEqual(self.rate_json(), {"usd_per_pt": None, "windows": 0, "computed_at": LAST + AWAY, "detail": []})
        self.assertNotIn("weekly limit", self.submit(self.conversation())["reason"])

    def test_nothing_without_a_data_folder(self):
        with mock.patch.object(rate, "compute_rate") as compute:
            self.assertIsNone(hook.refresh_rate(now=self.NOW, env={"CODEX_HOME": self.home}))
        compute.assert_not_called()
        self.assertFalse(os.path.exists(self.data))

    def test_a_running_refresh_is_left_alone(self):
        os.makedirs(self.data)
        lock = os.path.join(self.data, "rate.lock")
        open(lock, "w").close()
        os.utime(lock, (self.NOW - 60, self.NOW - 60))
        with mock.patch.object(rate, "compute_rate", return_value=None) as compute:
            hook.refresh_rate(now=self.NOW, env=self.env)
            compute.assert_not_called()
            os.utime(lock, (self.NOW - 700, self.NOW - 700))          # left by a refresh that was stopped
            hook.refresh_rate(now=self.NOW, env=self.env)
            compute.assert_called_once()
        self.assertFalse(os.path.exists(lock))

    def test_the_card_uses_the_rate(self):
        self.meter_log()
        hook.refresh_rate(now=self.NOW, env=self.env)
        out = self.submit(self.conversation())
        self.assertIn("about $7.84 if the cache expired (6.5% of your weekly limit)", out["reason"])     # $1.20 per 1%

    def test_it_deletes_handoffs_nobody_used(self):
        """A conversation resumed after a card's 30 minutes runs only this hook: the handoff text goes then, not at
        some later prompt or new conversation."""
        self.submit(self.conversation())
        self.assertIsNotNone(self.pending())
        hook.refresh_rate(now=LAST + AWAY + 1800, env=self.env)
        self.assertIsNotNone(self.pending())                # 30 minutes old is still young enough
        hook.refresh_rate(now=LAST + AWAY + 1801, env=self.env)
        self.assertIsNone(self.pending())
        self.assertEqual(self.events(), ["offer", "expired"])
        self.assertEqual(os.listdir(os.path.join(self.data, "pending")), [])


class ShapesTest(Base):
    def test_the_printed_json_is_ascii_and_strict(self):
        """hook.to_json, what the scripts print: ASCII only, and a lone surrogate (which only a log Codex didn't write
        holds) becomes "?", as Codex's JSON reader refuses the escape of one and would drop the whole answer."""
        text = hook.to_json({"decision": "block", "reason": "TEST · model gpt-\ud800x – café 🙂",
                             "systemMessage": ["a\udfffb", {"k\ud801": "v"}]})
        text.encode("ascii")                                      # strict
        self.assertIsNone(re.search(r"\\ud[89ab][0-9a-f]{2}(?!\\ud[c-f])|(?<!\\ud[89ab][0-9a-f]{2})\\ud[c-f][0-9a-f]{2}",
                                    text))                       # no escape of a lone surrogate
        self.assertEqual(json.loads(text), {"decision": "block", "reason": "TEST · model gpt-?x – café 🙂",
                                            "systemMessage": ["a?b", {"k?": "v"}]})

    def test_every_output_is_one_a_hook_may_give(self):
        """A sweep over breaks, sizes, triggers and folders: every output is one of the four shapes (shape() fails
        otherwise), and their texts never show None, nan, inf or $-."""
        seen = set()
        sizes = ((400_000, 390_000, 10_000), (195_000, 190_000, 5_000), (40_000, 35_000, 2_000))
        for i, last in enumerate(sizes):
            path = self.conversation(last=last, window=(832_200, 258_400, None)[i])
            for at in (0, 1800, 3600, 7200, AWAY, 2 * DAY):
                for prompt in (PROMPT, "ok", "", "switch-advisor test", "switch-advisor demo"):
                    seen.add(shape(self.submit(path, prompt=prompt, at=at, sid="s-%d-%d" % (i, at))))
                    seen.add(shape(self.start(sid="n-%d-%d" % (i, at), at=at + 60)))
        self.assertEqual(seen, {"none", "block", "note", "context"})
        self.decisions()


if __name__ == "__main__":
    unittest.main()
