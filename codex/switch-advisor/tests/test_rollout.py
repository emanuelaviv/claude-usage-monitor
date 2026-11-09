"""advisor.rollout on synthetic logs (tests/fixtures.py): finding logs, threads and calls, the prompt hook's snapshot,
and the facts a handoff carries."""
import calendar
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from advisor import rollout, rule
from tests import fixtures as fx

T0, HOUR, DAY = fx.T0, fx.HOUR, fx.DAY
MAIN = "0a1b2c3d-0000-4000-8000-000000000001"
OTHER = "0a1b2c3d-0000-4000-8000-000000000002"
SUB = "0a1b2c3d-0000-4000-8000-0000000000a1"


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = fx.make_home(self._tmp.name)
        self.cwd = os.path.join(self._tmp.name, "project")

    def tearDown(self):
        self._tmp.cleanup()

    def rollout(self, thread_id=None, **kw):
        return fx.Rollout(self.home, thread_id or fx.new_id(), self.cwd, **kw)

    def basic(self, thread_id=MAIN, model="gpt-6-astra", window=258_400):
        """A main thread: one turn, a typed request, one call, a reply."""
        r = self.rollout(thread_id)
        r.meta()
        r.turn(model, window)
        r.user("Fix the parser", t=T0 + 10)
        r.call(T0 + 60, 120_000, 100_000, 2_000)
        r.complete(T0 + 61, "Done.")
        return r


class PlacesTest(Base):
    def test_codex_home_from_env(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": self.home}):
            self.assertEqual(rollout.codex_home(), self.home)
            self.assertEqual(rollout.sessions_dir(), os.path.join(self.home, "sessions"))
        with mock.patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(rollout.codex_home(), os.path.join(os.path.expanduser("~"), ".codex"))
        self.assertEqual(rollout.sessions_dir(self.home), os.path.join(self.home, "sessions"))

    def test_rollout_files_since_oldest_first(self):
        """By when each log was last written: the later of its modification time and its last record's time. A
        compressed log can't be read from its end; it was made (here: now) after its last record."""
        a = self.basic(MAIN).write(mtime=T0 + 100)     # last record T0 + 61, modified T0 + 100
        b = self.basic(OTHER).write(mtime=T0 + 50)     # modified T0 + 50, its last record T0 + 61
        z = os.path.join(os.path.dirname(a), "rollout-2026-11-07T08-00-00-%s.jsonl.zst" % SUB)
        with open(z, "wb") as fh:
            fh.write(b"not really compressed")
        os.utime(z, (T0 + 200, T0 + 200))
        with open(os.path.join(os.path.dirname(a), "notes.jsonl"), "w") as fh:
            fh.write("{}\n")
        self.assertEqual(rollout.rollout_files(self.home), [b, a, z])
        self.assertEqual(rollout.rollout_files(self.home, since=T0 + 62), [a, z])
        self.assertEqual(rollout.rollout_files(self.home, include_zst=False), [b, a])
        self.assertEqual(rollout.log_times(self.home, include_zst=False), [(T0 + 61, b), (T0 + 100, a)])
        self.assertGreaterEqual(rollout.written(z), max(T0 + 200, os.stat(z).st_mtime))
        self.assertEqual(rollout.rollout_files(os.path.join(self._tmp.name, "nowhere")), [])

    def test_find_rollout_takes_the_newest(self):
        old = fx.Rollout(self.home, MAIN, self.cwd, day=(2026, 11, 5))
        old.call(T0 - 2 * DAY, 1_000, 0, 10)
        old_path = old.write(mtime=T0 - 2 * DAY)
        new = self.basic(MAIN)
        new_path = new.write(mtime=T0 + 100)
        self.assertNotEqual(old_path, new_path)
        self.assertEqual(rollout.find_rollout(MAIN, self.home), new_path)
        os.utime(old_path, (T0 + 500, T0 + 500))
        self.assertEqual(rollout.find_rollout(MAIN, self.home), old_path)
        self.assertIsNone(rollout.find_rollout(OTHER, self.home))
        self.assertIsNone(rollout.find_rollout("", self.home))

    def test_titles_last_one_wins(self):
        self.assertEqual(rollout.titles(self.home), {})
        fx.add_title(self.home, MAIN, "First name")
        with open(os.path.join(self.home, "session_index.jsonl"), "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        fx.add_title(self.home, OTHER, "Ünïcode title")
        fx.add_title(self.home, MAIN, "Renamed")
        self.assertEqual(rollout.titles(self.home), {MAIN: "Renamed", OTHER: "Ünïcode title"})


class ParseTest(Base):
    def test_parse_ts(self):
        base = calendar.timegm((2026, 11, 7, 2, 23, 31))
        self.assertAlmostEqual(rollout.parse_ts("2026-11-07T02:23:31.975Z"), base + 0.975, places=6)
        self.assertEqual(rollout.parse_ts("2026-11-07T02:23:31Z"), base)
        self.assertEqual(rollout.parse_ts("2026-11-07T02:23:31"), base)       # no zone: UTC
        self.assertAlmostEqual(rollout.parse_ts("2026-11-07T02:23:31.5Z"), base + 0.5, places=6)
        self.assertAlmostEqual(rollout.parse_ts("2026-11-07T02:23:31.123456789Z"), base + 0.123456, places=6)
        self.assertEqual(rollout.parse_ts("2026-11-07T04:23:31+02:00"), base)
        self.assertEqual(rollout.parse_ts("2026-11-07T04:23:31+0200"), base)
        for bad in ("", "yesterday", "2026-13-40T99:99:99Z", None, 1794013806):
            with self.assertRaises(ValueError):
                rollout.parse_ts(bad)

    def test_iter_records_skips_bad_lines(self):
        r = self.basic()
        r.raw("{not json")
        r.raw("[1, 2, 3]")
        r.raw("")
        r.raw('{"timestamp":"2026-11-07T09:05:00.000Z","ordinal":99,"type":"token_usage_record","payload":{"thr')
        recs = list(rollout.iter_records(r.write()))
        self.assertTrue(all(isinstance(x, dict) for x in recs))
        self.assertEqual(recs[0]["type"], "session_meta")
        self.assertEqual(len(recs), len([1 for kind, _ in r._lines if kind == "record"]))
        self.assertEqual(list(rollout.iter_records(os.path.join(self.home, "missing.jsonl"))), [])


class ReadThreadTest(Base):
    def test_thread_and_calls(self):
        r = self.rollout(MAIN)
        r.meta(branch="feature/parser")
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.user("Fix the parser", t=T0 + 10)
        r.call(T0 + 20, 30_000, 20_000, 500, reasoning=200)
        r.tool_output(T0 + 30)
        r.call(T0 + 40, 32_000, 30_000, 400)
        r.call(T0 + 50, 33_000, 31_000, 300)             # no input since the previous call
        r.complete(T0 + 51, "Fixed.")
        r.turn("gpt-6.1-sol", 475_000, tier="fast", t=T0 + HOUR)
        r.harness("<environment_context>synthetic</environment_context>", t=T0 + HOUR + 1)
        r.user("Now the tests", t=T0 + HOUR + 2)
        r.call(T0 + HOUR + 30, 40_000, 21_000, 900)
        th = rollout.read_thread(r.write())
        self.assertEqual((th.id, th.kind, th.parent, th.cwd), (MAIN, "main", None, self.cwd))
        self.assertEqual((th.originator, th.branch, th.started), ("Codex Desktop", "feature/parser", T0))
        self.assertEqual(th.file, r.path)
        c = th.calls
        self.assertEqual([x.end for x in c], [T0 + 20, T0 + 40, T0 + 50, T0 + HOUR + 30])
        self.assertEqual([x.start for x in c], [T0 + 10, T0 + 30, None, T0 + HOUR + 2])
        self.assertEqual([(x.model, x.tier, x.window) for x in c],
                         [("gpt-6-astra", "default", 258_400)] * 3 + [("gpt-6.1-sol", "fast", 475_000)])
        self.assertEqual((c[0].inp, c[0].cached, c[0].out, c[0].reasoning), (30_000, 20_000, 500, 200))
        self.assertEqual({x.kind for x in c}, {"main"})
        self.assertEqual({x.thread for x in c}, {MAIN})
        self.assertEqual(len({x.response_id for x in c}), 4)
        self.assertEqual(c[0].turn, c[2].turn)
        self.assertNotEqual(c[0].turn, c[3].turn)
        self.assertFalse(any(x.after_compaction for x in c))

    def test_subagent(self):
        r = self.rollout(SUB, kind="subagent", parent=MAIN)
        r.meta()
        r.turn("codex-auto-review", 258_400)
        r.call(T0 + 5, 10_000, 0, 50)
        th = rollout.read_thread(r.write())
        self.assertEqual((th.kind, th.parent), ("subagent", MAIN))
        self.assertEqual((th.calls[0].kind, th.calls[0].model), ("subagent", "codex-auto-review"))

    def test_compactions(self):
        r = self.rollout(SUB, kind="subagent", parent=MAIN)
        r.meta()
        r.compacted(T0 + 1, ["The task from the parent"])     # no call before it in this log: not listed
        r.turn("gpt-6-astra", 258_400)
        r.call(T0 + 10, 55_000, 50_000, 100)
        r.call(T0 + 20, 221_000, 200_000, 4_000)
        r.compacted(T0 + 30, [])
        r.call(T0 + 40, 39_000, 20_000, 300)
        r.call(T0 + 50, 41_000, 39_000, 300)
        th = rollout.read_thread(r.write())
        self.assertEqual(th.compactions, [(T0 + 30, 221_000)])
        self.assertEqual([c.after_compaction for c in th.calls], [True, False, True, False])

    def test_meters(self):
        r = self.rollout(MAIN)
        r.meta()
        r.meter(T0 + 1, 20.0, resets_at=T0 + 5 * DAY)                                  # weekly as primary
        r.meter(T0 + 2, 21.5, resets_at=T0 + 5 * DAY, five_h=(3.0, T0 + 4 * HOUR))      # weekly as secondary
        r.meter(T0 + 3, 99.0, resets_at=T0 + 5 * DAY, limit_id="premium")             # another limit: ignored
        r.meter(T0 + 4, None, resets_at=0, five_h=4.0, plan="team")                   # 5-hour window only
        r.call(T0 + 5, 1_000, 0, 10)                                                  # rate_limits null
        th = rollout.read_thread(r.write())
        self.assertEqual(th.meters, [
            rollout.Meter(T0 + 1, "plus", 20.0, T0 + 5 * DAY, None, None),
            rollout.Meter(T0 + 2, "plus", 21.5, T0 + 5 * DAY, 3.0, T0 + 4 * HOUR),
            rollout.Meter(T0 + 4, "team", None, None, 4.0, T0 + 4 + 5 * HOUR)])

    def test_older_logs_use_token_count(self):
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400)
        r.user("Old style", t=T0 + 5)
        r.call(T0 + 10, 50_000, 40_000, 500, legacy=True)
        r.repeat(T0 + 11)
        r.meter(T0 + 12, 30.0, resets_at=T0 + DAY)        # a meter reading repeats the totals too
        r.call(T0 + 20, 60_000, 50_000, 700, legacy=True)
        r.repeat(T0 + 21)
        r.repeat(T0 + 22)
        path = r.write()
        th = rollout.read_thread(path)
        self.assertEqual([(c.end, c.inp, c.cached, c.out) for c in th.calls],
                         [(T0 + 10, 50_000, 40_000, 500), (T0 + 20, 60_000, 50_000, 700)])
        self.assertEqual([c.start for c in th.calls], [T0 + 5, None])
        self.assertEqual({(c.response_id, c.model, c.window) for c in th.calls}, {(None, "gpt-6-astra", 258_400)})
        self.assertEqual(len(th.meters), 1)
        _threads, stats = rollout.load(self.home)
        self.assertEqual(stats["fallback_calls"], 2)

    def test_token_count_ignored_when_records_exist(self):
        th = rollout.read_thread(self.basic().write())
        self.assertEqual(len(th.calls), 1)
        self.assertIsNotNone(th.calls[0].response_id)

    def test_record_copied_from_another_thread_is_skipped(self):
        r = self.rollout(SUB, kind="subagent", parent=MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400)
        r.add("token_usage_record", {"thread_id": MAIN, "turn_id": "turn-copied", "session_id": MAIN,
                                     "response_id": "resp-copied", "usage": fx._usage(90_000, 0, 10)}, T0 + 1)
        r.call(T0 + 10, 20_000, 0, 10)
        th = rollout.read_thread(r.write())
        self.assertEqual([c.inp for c in th.calls], [20_000])

    def test_other_line_layout_is_still_read(self):
        r = self.basic()
        rec = {"payload": {"usage": fx._usage(70_000, 60_000, 800), "response_id": "resp-spaced",
                           "thread_id": MAIN}, "type": "token_usage_record", "timestamp": fx.iso(T0 + 90)}
        r.raw(json.dumps(rec))      # spaces after separators, payload first
        r.raw(json.dumps({"timestamp": fx.iso(T0 + 95), "type": "compacted", "payload": {"window_number": 1}}))
        th = rollout.read_thread(r.write())
        self.assertEqual([c.inp for c in th.calls], [120_000, 70_000])
        self.assertEqual(th.compactions, [(T0 + 95, 70_000)])
        snap = rollout.snapshot(r.path)
        self.assertEqual((snap.last_call_end, snap.ctx, snap.compacted_since_last_call), (T0 + 90, 70_800, True))

    def test_not_a_rollout(self):
        r = self.rollout(MAIN)
        r.user("No session_meta first")
        path = r.write(meta=False)
        self.assertIsNone(rollout.read_thread(path))
        self.assertIsNone(rollout.read_thread(os.path.join(self.home, "missing.jsonl")))


class LoadTest(Base):
    def test_dedupe_and_merge_across_files(self):
        a = fx.Rollout(self.home, MAIN, self.cwd, day=(2026, 11, 6))
        a.meta()
        a.turn("gpt-6-astra", 258_400)
        a.call(T0 - DAY + 10, 1_000, 0, 10, response_id="resp-shared")
        a.call(T0 - DAY + 20, 2_000, 0, 10, response_id="resp-a")
        a.write(mtime=T0 - DAY + 100)
        b = self.rollout(MAIN)
        b.meta()
        b.turn("gpt-6-astra", 258_400)
        b.call(T0 + 10, 1_000, 0, 10, response_id="resp-shared")
        b.call(T0 + 20, 3_000, 0, 10, response_id="resp-b")
        b.write()
        sub = self.rollout(SUB, kind="subagent", parent=MAIN)
        sub.call(T0 + 30, 4_000, 0, 10)
        sub.write()
        threads, stats = rollout.load(self.home)
        self.assertEqual(set(threads), {MAIN, SUB})
        main = threads[MAIN]
        self.assertEqual([c.response_id for c in main.calls], ["resp-shared", "resp-a", "resp-b"])
        self.assertEqual(main.file, b.path)
        self.assertEqual(main.started, T0 - DAY)
        self.assertEqual(stats, {"files": 3, "zst_skipped": 0, "bad_lines": 0, "fallback_calls": 0})

    def test_since_and_bad_lines(self):
        old = fx.Rollout(self.home, OTHER, self.cwd, day=(2026, 10, 28))
        old.meta()
        old.turn("gpt-6-astra", 258_400)
        old.call(old.start + 60, 1_000, 0, 10)
        old.write()
        r = self.basic(MAIN)
        r.raw("{not json")
        r.raw('{"timestamp":"2026-11-07T09:05:00.000Z","ordinal":99,"type":"token_usage_record","payload":{"thr')
        r.write()
        threads, stats = rollout.load(self.home, since=T0 - DAY)
        self.assertEqual(list(threads), [MAIN])
        self.assertEqual((stats["files"], stats["bad_lines"]), (1, 2))


class SnapshotTest(Base):
    def test_anchors_on_the_last_model_response(self):
        r = self.basic()
        r.meter(T0 + 62, 40.0, resets_at=T0 + 3 * DAY)
        before = rollout.snapshot(r.write())
        # what Codex may already have written when the prompt hook runs, hours later
        r.task_started(T0 + 3 * HOUR)
        r.turn_context("gpt-6-astra")
        r.world_state()
        r.user("Back again", t=T0 + 3 * HOUR + 1)
        after = rollout.snapshot(r.write())
        self.assertEqual(before.last_call_end, T0 + 60)
        self.assertEqual(after.last_call_end, T0 + 60)
        self.assertEqual((after.ctx, after.last_input, after.last_cached), (122_000, 120_000, 100_000))
        self.assertEqual((after.thread, after.kind, after.cwd), (MAIN, "main", self.cwd))
        self.assertEqual((after.model, after.tier, after.window), ("gpt-6-astra", "default", 258_400))
        self.assertFalse(after.compacted_since_last_call)
        self.assertEqual(after.meter.weekly_pct, 40.0)
        self.assertEqual(after.calls_in_tail, 1)

    def test_the_latest_turn_start_and_what_started_it(self):
        """turn_id and trigger come from the latest task_started (turn_attribution.turn_trigger), which Codex writes
        before the prompt hook runs; one without turn_attribution (older Codex) gives no trigger."""
        r = self.basic()
        snap = rollout.snapshot(r.write())
        self.assertEqual((snap.turn_id, snap.trigger), (r.turn_id, None))
        r.task_started(T0 + 3 * HOUR, trigger="composer", turn_id="turn-desk")
        r.call(T0 + 3 * HOUR + 30, 125_000, 120_000, 1_000)
        r.task_started(T0 + 9 * HOUR, trigger="remote_android", turn_id="turn-phone")
        r.turn_context("gpt-6-astra")
        snap = rollout.snapshot(r.write())
        self.assertEqual((snap.turn_id, snap.trigger), ("turn-phone", "remote_android"))
        self.assertEqual(snap.last_call_end, T0 + 3 * HOUR + 30)
        self.assertFalse(snap.turn_running)
        r.task_started(T0 + 10 * HOUR)
        snap = rollout.snapshot(r.write())
        self.assertEqual(snap.trigger, None)
        self.assertNotEqual(snap.turn_id, "turn-phone")
        self.assertEqual((rollout.Snapshot().turn_id, rollout.Snapshot().trigger), (None, None))

    def test_latest_turn_and_meter_win(self):
        r = self.basic()
        r.meter(T0 + 62, 40.0, resets_at=T0 + 3 * DAY)
        r.turn("gpt-6.1-sol", 475_000, tier="fast", t=T0 + 100)
        r.call(T0 + 110, 50_000, 45_000, 100)
        r.meter(T0 + 111, 41.0, resets_at=T0 + 3 * DAY, five_h=12.0)
        snap = rollout.snapshot(r.write())
        self.assertEqual((snap.model, snap.tier, snap.window), ("gpt-6.1-sol", "fast", 475_000))
        self.assertEqual((snap.meter.weekly_pct, snap.meter.five_h_pct), (41.0, 12.0))
        self.assertEqual((snap.last_call_end, snap.ctx, snap.calls_in_tail), (T0 + 110, 50_100, 2))

    def test_compaction_flag(self):
        r = self.basic()
        r.compacted(T0 + 70, ["Fix the parser"])
        self.assertTrue(rollout.snapshot(r.write()).compacted_since_last_call)
        r.call(T0 + 80, 39_000, 20_000, 200)
        snap = rollout.snapshot(r.write())
        self.assertFalse(snap.compacted_since_last_call)
        self.assertEqual(snap.ctx, 39_200)

    def test_subagent_kind(self):
        r = self.rollout(SUB, kind="subagent", parent=MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400)
        r.call(T0 + 5, 10_000, 0, 50)
        snap = rollout.snapshot(r.write())
        self.assertEqual((snap.thread, snap.kind), (SUB, "subagent"))

    def big_log(self):
        """Over 3 MB, with a 1.2 MB first line: an early turn, 2 MB of tool output, then the latest turn."""
        r = self.rollout(MAIN)
        r.meta(instructions="Synthetic instructions. " * 50_000)
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.user("Start", t=T0 + 1)
        r.call(T0 + 10, 100_000, 90_000, 1_000)
        for i in range(20):
            r.tool_output(T0 + 20 + i, size=100_000)
        r.turn("gpt-6.1-sol", 475_000, tier="fast", t=T0 + 100)
        r.user("Continue", t=T0 + 101)
        for i in range(4):
            r.tool_output(T0 + 110 + i, size=100_000)
        r.call(T0 + 200, 300_000, 290_000, 3_000)
        r.tool_output(T0 + 201, size=100_000)
        path = r.write()
        self.assertGreater(os.path.getsize(path), 3_000_000)
        with open(path, "rb") as fh:
            self.assertGreater(len(fh.readline()), 1_000_000)
        return path

    def test_reads_only_the_tail_of_a_big_log(self):
        path = self.big_log()
        with mock.patch.object(rollout, "_lines", side_effect=AssertionError("read the whole log")):
            snap = rollout.snapshot(path)
        self.assertEqual((snap.thread, snap.kind, snap.cwd), (MAIN, "main", self.cwd))
        self.assertEqual((snap.model, snap.tier, snap.window), ("gpt-6.1-sol", "fast", 475_000))
        self.assertEqual((snap.last_call_end, snap.ctx, snap.calls_in_tail), (T0 + 200, 303_000, 1))

    def test_reads_the_whole_log_when_the_tail_lacks_the_turn(self):
        path = self.big_log()
        with mock.patch.object(rollout, "_lines", wraps=rollout._lines) as whole:
            snap = rollout.snapshot(path, tail_bytes=150_000)
        self.assertTrue(whole.called)
        self.assertEqual((snap.model, snap.tier, snap.window), ("gpt-6.1-sol", "fast", 475_000))
        self.assertEqual((snap.last_call_end, snap.ctx), (T0 + 200, 303_000))

    def test_tail_cut_inside_a_line(self):
        r = self.basic()
        for i in range(30):
            r.turn("gpt-6-astra", 258_400, t=T0 + 100 + i * 10)
            r.call(T0 + 105 + i * 10, 100_000 + i, 90_000, 1_000)
        path = r.write()
        full = rollout.snapshot(path, tail_bytes=10 ** 9)
        for tail in (1_000, 1_500, 2_345, 5_000):
            snap = rollout.snapshot(path, tail_bytes=tail)
            self.assertEqual((snap.last_call_end, snap.ctx, snap.model, snap.window),
                             (full.last_call_end, full.ctx, full.model, full.window), tail)

    def test_older_log_falls_back_to_token_count(self):
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400)
        r.call(T0 + 10, 50_000, 40_000, 500, legacy=True)
        r.call(T0 + 20, 60_000, 50_000, 700, legacy=True)
        r.repeat(T0 + 21)
        r.user("Later", t=T0 + 2 * HOUR)
        snap = rollout.snapshot(r.write())
        self.assertEqual((snap.last_call_end, snap.ctx, snap.last_cached), (T0 + 20, 60_700, 50_000))

    def test_a_turn_still_running(self):
        """A turn that has made a model call and hasn't ended (no task_complete or turn_aborted yet): Codex calls the
        model again when its tool returns, whatever is sent now. Not once the turn ends, nor for a new turn without a
        call yet (the prompt being sent), nor when its last record is over 24 h old (an app that was stopped)."""
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.user("Run the slow tests and wait for them.", t=T0 + 1)
        r.call(T0 + 10, 40_000, 30_000, 200)              # then a tool runs for hours
        path = r.write()
        for now, running in ((T0 + 2 * HOUR, True), (T0 + 10 + DAY, True), (T0 + 11 + DAY, False)):
            self.assertEqual(rollout.snapshot(path, now=now).turn_running, running, now - T0)
        with mock.patch.object(rollout.time, "time", return_value=T0 + HOUR):
            self.assertTrue(rollout.snapshot(path).turn_running)       # by default, at the clock's time
        r.user("Steer: skip the slow ones.", t=T0 + 3 * HOUR)          # typed while the turn runs: still that turn
        self.assertTrue(rollout.snapshot(r.write(), now=T0 + 3 * HOUR).turn_running)
        r.call(T0 + 3 * HOUR + 10, 41_000, 40_000, 100)
        r.complete(T0 + 3 * HOUR + 11, "The fast tests pass.")
        self.assertFalse(rollout.snapshot(r.write(), now=T0 + 4 * HOUR).turn_running)
        r.turn("gpt-6-astra", 258_400, t=T0 + 8 * HOUR)               # the prompt being sent, already logged
        r.user("Now the slow ones.", t=T0 + 8 * HOUR + 1)
        snap = rollout.snapshot(r.write(), now=T0 + 8 * HOUR + 2)
        self.assertEqual((snap.turn_running, snap.last_call_end), (False, T0 + 3 * HOUR + 10))
        r.call(T0 + 8 * HOUR + 10, 42_000, 41_000, 100)
        self.assertTrue(rollout.snapshot(r.write(), now=T0 + 9 * HOUR).turn_running)
        r.aborted(T0 + 9 * HOUR)                                       # stopped by the person
        self.assertFalse(rollout.snapshot(r.write(), now=T0 + 9 * HOUR).turn_running)

    def test_a_turn_is_judged_by_its_own_last_record(self):
        """Codex writes thread_settings_applied outside turns too: when the app opens a conversation again, and just
        before a turn starts, so as the prompt hook runs. Those don't make a turn a stopped app left open look recent;
        the turn's own records do."""
        settings = {"type": "thread_settings_applied", "thread_id": MAIN,
                    "thread_settings": {"model": "gpt-6-astra", "service_tier": "default"}}
        now = T0 + 3 * DAY
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.user("Run the slow suite.", t=T0 + 1)
        r.call(T0 + 30, 40_000, 30_000, 200)               # its tool started, then the app was stopped
        r.event(settings, now - 600)                       # opened again 10 minutes ago
        r.event(settings, now - 1)                         # the prompt being sent
        path = r.write()
        self.assertFalse(rollout.snapshot(path, now=now).turn_running)
        r.tool_output(now - 300)                           # but its tool did return 5 minutes ago: running
        self.assertTrue(rollout.snapshot(r.write(), now=now).turn_running)
        r = self.rollout(OTHER)                            # a tool running for 2 hours, the prompt being sent now
        r.meta()
        r.turn("gpt-6-astra", 258_400, t=now - 3 * HOUR)
        r.user("Run the slow suite.", t=now - 3 * HOUR + 1)
        r.call(now - 2 * HOUR, 40_000, 30_000, 200)
        r.event(dict(settings, thread_id=OTHER), now - 1)
        self.assertTrue(rollout.snapshot(r.write(), now=now).turn_running)

    def test_a_turn_running_past_the_tail(self):
        """A tail cut from a longer log with calls but no turn event at all is one long turn still going (here its
        settings were applied again within it, so the tail settles the snapshot); the whole log of a turn that ended
        is not running."""
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.user("Start", t=T0 + 1)
        r.call(T0 + 10, 100_000, 90_000, 1_000)
        for i in range(10):
            r.tool_output(T0 + 20 + i, size=100_000)
        r.event({"type": "thread_settings_applied", "thread_id": MAIN,
                 "thread_settings": {"model": "gpt-6-astra", "service_tier": "default"}}, T0 + 40)
        r.turn_context("gpt-6-astra", t=T0 + 41)
        r.call(T0 + 50, 200_000, 190_000, 1_000)
        path = r.write()
        with mock.patch.object(rollout, "_lines", side_effect=AssertionError("read the whole log")):
            snap = rollout.snapshot(path, tail_bytes=50_000, now=T0 + HOUR)
        self.assertEqual((snap.ctx, snap.turn_running), (201_000, True))
        self.assertTrue(rollout.snapshot(path, now=T0 + HOUR).turn_running)            # the whole log: same turn
        r.complete(T0 + 60, "Done.")
        self.assertFalse(rollout.snapshot(r.write(), tail_bytes=50_000, now=T0 + HOUR).turn_running)
        old = self.rollout(OTHER)                       # an older log without any turn event: no running turn known
        old.meta()
        old.turn_context("gpt-6-astra", t=T0)
        old.event({"type": "thread_settings_applied", "thread_id": OTHER, "thread_settings": {"model": "gpt-6-astra"}})
        old.call(T0 + 10, 50_000, 40_000, 500)
        self.assertFalse(rollout.snapshot(old.write(), now=T0 + HOUR).turn_running)

    def test_no_calls_and_missing_file(self):
        r = self.rollout(MAIN)
        r.meta()
        r.user("Hello")
        snap = rollout.snapshot(r.write())
        self.assertEqual((snap.thread, snap.last_call_end, snap.ctx, snap.calls_in_tail), (MAIN, None, 0, 0))
        self.assertEqual(rollout.snapshot(os.path.join(self.home, "missing.jsonl")), rollout.Snapshot())
        empty = os.path.join(self.home, "sessions", "rollout-empty.jsonl")
        open(empty, "wb").close()
        self.assertEqual(rollout.snapshot(empty), rollout.Snapshot())

    def test_speed_on_a_20mb_log(self):
        """Generous bounds (the spec's targets are 0.3 s and 1.5 s); they catch a reader that parses everything."""
        r = self.rollout(MAIN)
        r.meta()
        for turn in range(40):
            r.turn("gpt-6-astra", 258_400, t=T0 + turn * 100)
            r.user("Request %d" % turn, t=T0 + turn * 100 + 1)
            for k in range(10):
                r.tool_output(T0 + turn * 100 + 2 + k, size=50_000)
                r.call(T0 + turn * 100 + 3 + k, 100_000 + turn * 1_000 + k, 90_000, 500)
            r.complete(T0 + turn * 100 + 50, "Turn %d done." % turn)
        path = r.write()
        self.assertGreater(os.path.getsize(path), 20_000_000)
        began = time.perf_counter()
        snap = rollout.snapshot(path)
        took_snapshot = time.perf_counter() - began
        began = time.perf_counter()
        f = rollout.facts(path, home=self.home, now=T0 + 5_000)
        took_facts = time.perf_counter() - began
        self.assertEqual(snap.ctx, 100_000 + 39_000 + 9 + 500)
        self.assertEqual(len(f.requests), 40)
        self.assertLess(took_snapshot, 1.0)
        self.assertLess(took_facts, 4.0)


class LatestMeterTest(Base):
    def log(self, t, weekly=None, five_h=None):
        r = self.rollout()
        r.turn("gpt-6-astra", 258_400, t=t - 60)
        r.call(t - 1, 50_000, 40_000, 1_000)
        r.meter(t, weekly, resets_at=T0 + 3 * DAY, five_h=five_h)
        return r.write()

    def test_the_newest_weekly_reading_of_the_recent_logs(self):
        self.log(T0, weekly=20.0)
        self.log(T0 + HOUR, weekly=35.0)
        self.log(T0 + 2 * HOUR, weekly=None, five_h=50.0)      # the newest log has only a 5-hour reading
        m = rollout.latest_meter(self.home)
        self.assertEqual((m.weekly_pct, m.t), (35.0, T0 + HOUR))
        self.assertIsNone(rollout.latest_meter(self.home, logs=1))    # only the newest log, which has no weekly one
        self.assertIsNone(rollout.latest_meter(self.home, logs=0))

    def test_no_logs_or_no_readings(self):
        self.assertIsNone(rollout.latest_meter(self.home))
        self.basic().write()
        self.assertIsNone(rollout.latest_meter(self.home))


class FactsTest(Base):
    def facts(self, r, now=T0 + DAY):
        return rollout.facts(r.write(), home=self.home, now=now)

    def test_only_typed_text_is_a_request(self):
        r = self.rollout(MAIN)
        r.meta(branch="main")
        r.turn("gpt-6-astra", 258_400)
        r.user("# AGENTS.md instructions\nAlways run the tests.", provenance="agents_md")
        r.harness("<environment_context>cwd and shell</environment_context>")
        r.message([("<app_page>an open page</app_page>\n", "harness"), ("Fix the parser\n", "user"),
                   ("so that it reads .zst too", "user")])
        r.call(T0 + 10, 30_000, 20_000, 100)
        r.user("Then add tests")
        r.user("   ")
        f = self.facts(r)
        self.assertEqual((f.thread, f.cwd, f.branch), (MAIN, self.cwd, "main"))
        self.assertEqual(f.requests, ["Fix the parser\nso that it reads .zst too", "Then add tests"])
        self.assertEqual(f.first_request, "Fix the parser\nso that it reads .zst too")

    def test_retained_messages_fill_in_after_a_compaction(self):
        r = self.rollout(MAIN)
        r.meta()
        r.compacted(T0 + 1, [{"text": "Second", "order": 1}, {"text": "First task", "order": 0}])
        r.turn("gpt-6-astra", 258_400)
        r.user("Third")
        f = self.facts(r)
        self.assertEqual(f.requests, ["First task", "Second", "Third"])
        self.assertEqual(f.first_request, "First task")

    def test_retained_messages_already_in_the_log_are_not_repeated(self):
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400)
        r.user("A long first request that the compaction keeps only in part")
        r.user("yes")
        r.user("yes")
        r.call(T0 + 10, 221_000, 200_000, 4_000)
        r.compacted(T0 + 20, [{"text": "A long first", "complete": False}, "yes", "yes", "Dropped from the log"])
        r.user("yes")
        f = self.facts(r)
        self.assertEqual(f.requests, ["A long first request that the compaction keeps only in part", "yes", "yes",
                                      "Dropped from the log", "yes"])

    def test_last_reply(self):
        r = self.basic()
        self.assertEqual(self.facts(r).last_reply, "Done.")
        r.turn("gpt-6-astra", 258_400, t=T0 + 100)
        r.assistant("Working on it: reading the logs.", t=T0 + 110, phase="commentary", encrypted=True)
        r.complete(T0 + 120, None)                  # a turn that ended without a final message
        f = self.facts(r)
        self.assertEqual(f.last_reply, "Working on it: reading the logs.")
        r.turn("gpt-6-astra", 258_400, t=T0 + 200)
        r.complete(T0 + 210, "All tests pass.")
        r.turn("gpt-6-astra", 258_400, t=T0 + 300)
        r.assistant("Stopped halfway, should I go on?", t=T0 + 310, phase="commentary")
        r.aborted(T0 + 320)                         # stopped after the assistant had written
        f = self.facts(r)
        self.assertEqual(f.last_reply, "Stopped halfway, should I go on?")
        self.assertTrue(f.asks_you)

    def test_asks_you(self):
        for reply, asks in (("Done.", False), ("Shall I commit?", True), ("Ready.\n\n**Shall I commit?**", True),
                            ("Is this right? I went ahead anyway.", False)):
            r = self.rollout(MAIN)
            r.meta()
            r.complete(T0 + 5, reply)
            self.assertEqual(self.facts(r).asks_you, asks, reply)
        r = self.basic()
        r.request_input(T0 + 70)                     # asked; its immediate output only says the question was sent
        self.assertTrue(self.facts(r).asks_you)
        r = self.basic()
        r.request_input(T0 + 70, answered=True)
        self.assertTrue(self.facts(r).asks_you)      # no model response has seen the answer yet
        r.call(T0 + 90, 31_000, 30_000, 100)
        self.assertFalse(self.facts(r).asks_you)
        r = self.basic()
        call_id = r.request_input(T0 + 70, sync=True)
        self.assertTrue(self.facts(r).asks_you)
        r.answer(T0 + 80, call_id)                   # request_user_input's output is the answer
        self.assertFalse(self.facts(r).asks_you)

    def test_an_async_question_stays_open_until_answered(self):
        """Codex asks with request_user_input_async, carries on, and ends its turn with a summary that asks nothing:
        the question is still open. It is answered by a user message naming the call (or one the person typed) once
        a model response follows it. A user message about something else (an agent's report) doesn't answer it."""
        r = self.basic()
        r.turn("gpt-6-astra", 258_400, t=T0 + 100)
        r.user("Set up the release.", t=T0 + 101)
        r.call(T0 + 110, 40_000, 30_000, 200)
        call_id = r.request_input(T0 + 111)
        r.call(T0 + 120, 41_000, 40_000, 300)       # the model carries on while the question waits
        r.complete(T0 + 130, "The branch is ready; I asked you which version to tag.")
        f = self.facts(r)
        self.assertEqual((f.asks_you, f.last_reply), (True, "The branch is ready; I asked you which version to tag."))
        r.item({"type": "message", "id": "msg-report", "role": "user",
                "content": [{"type": "input_text", "text": "A report from a helper agent."}]}, T0 + 140)
        r.call(T0 + 150, 42_000, 41_000, 100)
        self.assertTrue(self.facts(r).asks_you)
        r.turn("gpt-6-astra", 258_400, t=T0 + 200)
        r.async_answer(T0 + 201, call_id)
        self.assertTrue(self.facts(r).asks_you)      # what the prompt hook sees when the answer is the prompt sent
        r.call(T0 + 210, 43_000, 42_000, 100)
        r.complete(T0 + 211, "Tagged.")
        self.assertFalse(self.facts(r).asks_you)
        typed = self.basic(OTHER)
        typed.request_input(T0 + 70)
        typed.turn("gpt-6-astra", 258_400, t=T0 + 200)
        typed.user("Tag it 2.0 and go on.", t=T0 + 201)
        typed.call(T0 + 210, 31_000, 30_000, 100)
        typed.complete(T0 + 211, "Tagged 2.0.")
        self.assertFalse(self.facts(typed).asks_you)

    def asked_in_an_older_log(self, sync=False):
        """A log in the format Codex wrote before late October 2026, whose typed prompts carry no
        content_item_metadata: a request, a response, then a question, and the turn ends."""
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.plain_user("Set up the release.", t=T0 + 1)
        r.call(T0 + 10, 40_000, 30_000, 200)
        r.ask(T0 + 11, [{"title": "Which version should I tag?", "options": ["2.0", "1.9.1"]}], sync=sync)
        r.complete(T0 + 21, "The branch is ready.")
        return r

    def test_an_older_log_moves_past_a_question(self):
        """In an older log no user message reads as typed, so none answers a question by itself: a question is no
        longer asked once a later turn has started and got a model response. The turn being submitted has none yet,
        so when the prompt hook runs its question still counts, whatever the prompt is."""
        r = self.asked_in_an_older_log()
        self.assertTrue(self.facts(r).asks_you)
        r.turn("gpt-6-astra", 258_400, t=T0 + HOUR)
        r.plain_user("Tag it 2.0 and go on.", t=T0 + HOUR + 1)        # answered in a plain prompt ...
        f = self.facts(r)
        self.assertEqual((f.asks_you, len(f.open_questions)), (True, 1))  # ... being submitted: no response yet
        r.call(T0 + HOUR + 10, 42_000, 41_000, 100)
        r.complete(T0 + HOUR + 11, "Tagged 2.0.")
        f = self.facts(r)
        self.assertEqual((f.asks_you, f.open_questions), (False, []))

        r = self.asked_in_an_older_log()                                 # moved on: the question is never named
        for i, (text, reply) in enumerate((("Forget the release; fix the parser instead.", "Fixed the parser."),
                                           ("Now the tests.", "Added the tests. " + "Details. " * 200))):
            r.turn("gpt-6-astra", 258_400, t=T0 + (i + 1) * HOUR)
            r.plain_user(text, t=T0 + (i + 1) * HOUR + 1)
            r.call(T0 + (i + 1) * HOUR + 10, 42_000 + i, 41_000, 100)
            r.complete(T0 + (i + 1) * HOUR + 11, reply)
        f = self.facts(r)
        self.assertEqual((f.asks_you, f.open_questions), (False, []))
        self.assertGreater(f.last_reply_len, rule.REPLY_KEPT)
        self.assertEqual(rule.quality_flags(f, "test " * 25, 0), [])    # no "long-question" for a reply asking nothing

        r = self.asked_in_an_older_log(sync=True)                        # a sync question no output ever answered
        self.assertTrue(self.facts(r).asks_you)
        r.turn("gpt-6-astra", 258_400, t=T0 + HOUR)
        r.plain_user("Never mind, fix the parser.", t=T0 + HOUR + 1)
        r.call(T0 + HOUR + 10, 42_000, 41_000, 100)
        self.assertFalse(self.facts(r).asks_you)

    def test_a_question_of_this_turn_stays_open_while_the_model_carries_on(self):
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.plain_user("Set up the release.", t=T0 + 1)
        r.call(T0 + 10, 40_000, 30_000, 200)
        r.ask(T0 + 11, [{"title": "Which version?"}])
        r.call(T0 + 20, 41_000, 40_000, 300)                              # same turn: nobody moved past it
        r.complete(T0 + 21, "I asked you which version to tag.")
        f = self.facts(r)
        self.assertEqual((f.asks_you, f.open_questions), (True, [{"title": "Which version?", "options": []}]))

    def test_open_questions(self):
        """The questions still waiting for an answer, for the handoff, from the calls' arguments ({"questions":
        [{"title", "options"}]}): in the order asked, the newest 3, without those answered or moved past."""
        r = self.basic()
        r.turn("gpt-6-astra", 258_400, t=T0 + 100)
        r.user("Set up the release.", t=T0 + 101)
        r.call(T0 + 110, 40_000, 30_000, 200)
        first = r.ask(T0 + 111, [{"title": "Which version?", "options": ["2.0", "1.9.1"]}])
        r.ask(T0 + 112, [{"title": "Tag it now?"}, {"title": "Push the tag?", "options": ["yes", "no"]}])
        r.call(T0 + 120, 41_000, 40_000, 300)
        r.complete(T0 + 130, "I asked you three things.")
        self.assertEqual(self.facts(r).open_questions, [
            {"title": "Which version?", "options": ["2.0", "1.9.1"]}, {"title": "Tag it now?", "options": []},
            {"title": "Push the tag?", "options": ["yes", "no"]}])
        self.assertEqual(self.facts(r).open_question_count, 3)
        r.ask(T0 + 131, [{"title": "Write release notes?", "options": ["short", "long"]}])
        f = self.facts(r)
        self.assertEqual([q["title"] for q in f.open_questions], ["Tag it now?", "Push the tag?",
                                                                   "Write release notes?"])     # the newest 3
        self.assertEqual(f.open_question_count, 4)                       # ... of the 4 still open
        r.async_answer(T0 + 140, first)                                  # answered while the turn runs
        r.call(T0 + 150, 42_000, 41_000, 100)
        f = self.facts(r)
        self.assertEqual((len(f.open_questions), f.open_question_count), (3, 3))
        r.turn("gpt-6-astra", 258_400, t=T0 + 200)
        r.user("Tag it 2.0.", t=T0 + 201)                                # a typed prompt answers the rest
        r.call(T0 + 210, 43_000, 42_000, 100)
        r.complete(T0 + 211, "Tagged.")
        f = self.facts(r)
        self.assertEqual((f.asks_you, f.open_questions, f.open_question_count), (False, [], 0))
        self.assertEqual((rollout.Facts().open_questions, rollout.Facts().open_question_count), ([], 0))

    def test_open_questions_shapes_and_caps(self):
        """Titles and options are kept whole up to 4,000 characters (rule.open_questions cuts them for the handoff,
        and can mark the cut only when it sees the whole), a question keeps 8 options, and a call its last 3
        questions. An option may be an object with a label (request_user_input's own shape); malformed arguments give
        nothing and never raise."""
        r = self.basic()
        options = ["o" * 200] + ["option %d" % i for i in range(10)]
        r.ask(T0 + 70, [{"title": "  " + "T" * 400 + "  ", "options": options}])
        sync = r.ask(T0 + 71, [{"header": "Mode", "question": "Which mode?", "options": [
            {"label": "Fast", "description": "Costs more."}, {"label": "  "}, {"description": "no label"}, 5, None,
            " Standard "]}], sync=True)
        for arguments in ("{not json", json.dumps({"questions": "Which?"}), json.dumps([1, 2]), "",
                          json.dumps({"questions": ["Which?", {"title": 5}, {}, None]}), json.dumps({"other": 1})):
            r.ask(T0 + 72, [], arguments=arguments)
        r.call(T0 + 80, 31_000, 30_000, 100)
        f = self.facts(r)
        self.assertTrue(f.asks_you)
        self.assertEqual(f.open_questions, [
            {"title": "T" * 400, "options": ["o" * 200] + ["option %d" % i for i in range(7)]},
            {"title": "Which mode?", "options": ["Fast", "Standard"]}])
        # the 4 calls in JSON of another shape wait with questions the plugin can't read; the 2 whose arguments aren't
        # JSON at all ask nothing, as Codex answers them with an error and shows nothing
        self.assertEqual(f.unread_calls, 4)
        self.assertEqual((rollout._all_questions("{not json"), rollout._all_questions(""),
                          rollout._questions("{not json")), (None, None, []))
        self.assertEqual(rollout._all_questions(json.dumps({"other": 1})), [])
        self.assertIsNone(rollout.Facts().unread_calls)          # Facts made by hand: not counted
        r.answer(T0 + 90, sync, '{"answers": {"mode": "Fast"}}')
        self.assertEqual([q["title"] for q in self.facts(r).open_questions], ["T" * 400])
        r.ask(T0 + 91, [{"title": "Q%d " % i + "x" * 5000, "options": [{"label": "y" * 5000}]} for i in range(5)])
        f = self.facts(r)
        asked = f.open_questions
        self.assertEqual([q["title"][:3] for q in asked], ["Q2 ", "Q3 ", "Q4 "])
        self.assertEqual(f.open_question_count, 6)          # the 400-character one and these 5; malformed calls none
        self.assertEqual({(len(q["title"]), len(q["options"][0])) for q in asked},
                         {(rollout.TEXT_MAX, rollout.TEXT_MAX)})
        many = json.dumps({"questions": [{"title": "Q%d" % i} for i in range(9)]})     # a call keeps only its last 3
        self.assertEqual(rollout._questions(many), [{"title": "Q%d" % i, "options": []} for i in (6, 7, 8)])

    def test_a_call_codex_refused_asks_nothing_and_the_re_ask_counts_once(self):
        """Codex answers a request_user_input_async call it accepted with {"accepted":true} and one it refused with an
        error message: the panel showed nothing for the refused one, so only the model's corrected re-ask is open, as
        one question, whether the refused call was readable or not."""
        for name, first in (("a shape the plugin can't read", {"arguments": json.dumps({"questions": [{"id": "db"}]}),
                                                               "refused": "failed to parse function arguments: "
                                                                          "missing field `title`"}),
                            ("a readable question with no option", {"refused": "options must contain at least one "
                                                                               "non-empty answer"})):
            with self.subTest(name):
                r = self.basic()
                r.ask(T0 + 70, [{"title": "Which database?", "options": []}], **first)
                r.ask(T0 + 71, [{"title": "Which database?", "options": ["Postgres", "SQLite"]}])
                r.call(T0 + 80, 31_000, 30_000, 100)
                r.complete(T0 + 81, "I asked you in the panel.")
                f = self.facts(r)
                self.assertEqual((f.asks_you, f.open_question_count, f.unread_calls), (True, 1, 0))
                self.assertEqual(f.open_questions, [{"title": "Which database?", "options": ["Postgres", "SQLite"]}])
        r = self.basic()
        r.ask(T0 + 70, [{"title": "Which database?"}], refused="questions must not be empty")
        r.complete(T0 + 81, "Something went wrong with the question panel.")
        f = self.facts(r)
        self.assertEqual((f.asks_you, f.open_questions, f.unread_calls), (False, [], 0))

    def test_what_counts_as_refused(self):
        for output, refused in (('{"accepted":true}', False), ('{"accepted": true, "id": "q1"}', False),
                                ('{"queued": true}', False), ('{"accepted":false}', True), ('{"error": "x"}', True),
                                ("failed to parse function arguments: x", True), ("", True), ('"text"', True),
                                ("[1, 2]", True), (None, False), (5, False), ({"accepted": True}, False),
                                ([{"type": "input_text", "text": '{"accepted":true}'}], False),
                                ([{"type": "input_text", "text": "question titles must not be empty"}], True)):
            with self.subTest(output=output):
                self.assertIs(rollout._refused(output), refused)

    def test_a_rejected_question_call_asks_nothing(self):
        """A request_user_input call whose arguments aren't JSON: Codex answers it with an error, and the model asks
        again. Only the second call waits for an answer; with no second call, nothing is asked."""
        r = self.basic()
        r.ask(T0 + 70, [], arguments='{"questions": [{"title": "Which data')            # cut off
        r.ask(T0 + 71, [{"title": "Which database?", "options": ["Postgres", "SQLite"]}])
        r.call(T0 + 80, 31_000, 30_000, 100)
        r.complete(T0 + 81, "I asked you in the panel.")
        f = self.facts(r)
        self.assertEqual((f.asks_you, f.open_question_count, f.unread_calls), (True, 1, 0))
        self.assertEqual([q["title"] for q in f.open_questions], ["Which database?"])
        r2 = self.basic()
        r2.ask(T0 + 70, [], arguments="not json at all")
        r2.call(T0 + 80, 31_000, 30_000, 100)
        r2.complete(T0 + 81, "Something went wrong with the question.")
        f = self.facts(r2)
        self.assertEqual((f.asks_you, f.open_questions, f.open_question_count, f.unread_calls), (False, [], 0, 0))

    def test_agents_spawned_and_running(self):
        now = T0 + HOUR
        r = self.basic()
        r.spawn_agent(T0 + 62)
        r.spawn_agent(T0 + 63)
        r.complete(T0 + 64, "Two helpers are working.")
        r.write()

        def helper(thread_id, parent, finished, age_s):
            h = fx.Rollout(self.home, thread_id, self.cwd, kind="subagent", parent=parent)
            h.meta(t=T0 + 62)
            h.turn("gpt-6-astra", 258_400, t=T0 + 62)
            h.call(now - age_s - 5, 20_000, 0, 100)
            if finished:
                h.complete(now - age_s, "Report.")
            h.write(mtime=now - age_s)

        helper("0a1b2c3d-0000-4000-8000-0000000000b1", MAIN, finished=False, age_s=60)       # running
        helper("0a1b2c3d-0000-4000-8000-0000000000b2", MAIN, finished=True, age_s=60)        # done
        helper("0a1b2c3d-0000-4000-8000-0000000000b3", MAIN, finished=False, age_s=20 * 60)  # quiet for 20 min
        helper("0a1b2c3d-0000-4000-8000-0000000000b4", OTHER, finished=False, age_s=60)      # someone else's
        f = rollout.facts(r.path, home=self.home, now=now)
        self.assertEqual((f.agents_spawned, f.agents_running), (2, 1))
        f = rollout.facts(r.path, now=now)                  # home: the one the log lives in
        self.assertEqual(f.agents_running, 1)
        self.assertEqual(rollout.facts(r.path, home=self.home, now=now + HOUR).agents_running, 0)

    def test_texts_are_cut(self):
        r = self.rollout(MAIN)
        r.meta()
        r.user("x" * 10_000)
        r.complete(T0 + 5, "y" * 5_999 + "?")
        f = self.facts(r)
        self.assertEqual(len(f.requests[0]), rollout.TEXT_MAX)
        self.assertEqual((len(f.last_reply), f.last_reply_len, f.asks_you), (rollout.TEXT_MAX, 6_000, True))

    def test_whether_the_reply_itself_asks(self):
        """reply_asks: the whole last reply ends with a question, also past the 4,000 characters kept; asks_you also
        counts the questions asked in the panel. None without a reply. The quality flags rest on it: a long reply that
        asks nothing itself, with a question open in the panel, isn't a long question."""
        question = [{"title": "Which database should the service use?", "options": ["Postgres", "SQLite"]}]
        for reply, ask, asks in (("Done.", False, False), ("y" * 5_999 + "?", False, True),
                                 ("Which one? " + "y" * 5_000, True, False),
                                 ("y" * 3_999 + "? " + "z" * 2_000, True, False), (None, True, None)):
            with self.subTest(reply=(reply or "")[-12:], ask=ask):
                r = self.rollout(MAIN)
                r.meta()
                r.turn("gpt-6-astra", 258_400, t=T0)
                r.user("Plan the migration.", t=T0 + 1)
                if ask:
                    r.ask(T0 + 2, question)
                r.call(T0 + 3, 40_000, 30_000, 200)
                r.complete(T0 + 5, reply)
                f = self.facts(r)
                self.assertEqual((f.reply_asks, f.asks_you), (asks, bool(asks) or ask))
                flags = [x.key for x in rule.quality_flags(f, "Postgres", 0)]
                self.assertEqual("long-question" in flags, bool(asks) and len(reply) > rule.REPLY_KEPT)

    def test_missing_file(self):
        self.assertEqual(rollout.facts(os.path.join(self.home, "missing.jsonl"), home=self.home),
                         rollout.Facts(unread_calls=0))            # read, with nothing found: counted as none

    def test_the_role_is_the_messages_own(self):
        """Approval reviews' user messages carry a record-level metadata object after the payload, whose ids name
        other messages and their roles: the message is still the user's."""
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.call(T0 + 10, 30_000, 0, 100)
        rec = {"timestamp": fx.iso(T0 + 20), "ordinal": 99, "type": "response_item",
               "payload": {"type": "message", "id": "msg-review", "role": "user",
                           "content": [{"type": "input_text", "text": "Review this command."}],
                           "internal_chat_message_metadata_passthrough": {
                               "content_item_metadata": [{"provenance": {"type": "user"}}]}},
               "metadata": {"guardian_sources": [{"id": {"message_id": "m1", "turn_id": "t1", "role": "assistant"}}],
                            "retained_source": {"id": {"message_id": "m2", "turn_id": "t1", "role": "assistant"}}}}
        r.raw(json.dumps(rec, separators=(",", ":")))
        r.call(T0 + 30, 31_000, 30_000, 100)
        th = rollout.read_thread(r.write())
        self.assertEqual([c.start for c in th.calls], [T0, T0 + 20])
        f = rollout.facts(r.path, home=self.home, now=T0 + DAY)
        self.assertEqual((f.requests, f.last_reply), (["Review this command."], None))


class StoppedMtimeTest(Base):
    """Codex on Windows can leave a log's modification time at the moment it created the log while it goes on
    appending: logs are picked and ordered by their last record (rollout.written), not by that time."""

    def long_log(self, thread_id=MAIN):
        """Written from T0 to T0 + 5 h, its modification time left at T0."""
        r = self.rollout(thread_id)
        r.meta(t=T0)
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.user("Fix the parser", t=T0 + 10)
        r.call(T0 + 60, 120_000, 100_000, 2_000)
        r.turn("gpt-6-astra", 258_400, t=T0 + 5 * HOUR)
        r.user("Now the tests", t=T0 + 5 * HOUR + 10)
        r.call(T0 + 5 * HOUR + 60, 130_000, 120_000, 1_000)
        r.meter(T0 + 5 * HOUR + 61, 50.0, resets_at=T0 + 3 * DAY)
        r.complete(T0 + 5 * HOUR + 62, "Done.")
        return r.write(mtime=T0)

    def test_files_and_load_go_by_the_last_record(self):
        path = self.long_log()
        other = self.basic(OTHER).write(mtime=T0 + HOUR)     # its last record is at T0 + 61
        self.assertEqual(rollout.written(path), T0 + 5 * HOUR + 62)
        self.assertEqual(rollout.written(other), T0 + HOUR)
        self.assertEqual(rollout.rollout_files(self.home), [other, path])
        self.assertEqual(rollout.rollout_files(self.home, since=T0 + 2 * HOUR), [path])
        threads, _stats = rollout.load(self.home, since=T0 + 2 * HOUR)
        self.assertEqual(list(threads), [MAIN])
        self.assertEqual([c.end for c in threads[MAIN].calls], [T0 + 60, T0 + 5 * HOUR + 60])

    def test_the_last_record_is_found_past_long_lines(self):
        r = self.basic()
        r.tool_output(T0 + 500, size=300_000)                # a last line longer than one read from the end
        path = r.write(mtime=T0)
        self.assertEqual(rollout.last_record_time(path), T0 + 500)
        r.raw('{"timestamp":"%s","ordinal":99,"type":"event_msg","payload":{"ty' % fx.iso(T0 + 600))  # half-written
        self.assertEqual(rollout.last_record_time(r.write(mtime=T0)), T0 + 600)
        r.raw(json.dumps({"payload": {"type": "token_count"}, "type": "event_msg", "timestamp": fx.iso(T0 + 700)}))
        self.assertEqual(rollout.last_record_time(r.write(mtime=T0)), T0 + 700)     # another layout
        empty = os.path.join(os.path.dirname(path), "rollout-empty.jsonl")
        open(empty, "wb").close()
        os.utime(empty, (T0, T0))
        self.assertIsNone(rollout.last_record_time(empty))
        self.assertEqual(rollout.written(empty), T0)
        self.assertEqual(rollout.written(os.path.join(self.home, "missing.jsonl")), 0.0)

    def test_compressed_logs_are_bounded_by_when_they_were_made(self):
        r = self.basic()
        path = r.write(compress=True, mtime=T0 - DAY)        # skips this test on a Python without zstd
        self.assertGreaterEqual(rollout.written(path), os.stat(path).st_mtime)
        self.assertGreaterEqual(rollout.written(path), T0 + 61)   # made (now) after its last record
        threads, _stats = rollout.load(self.home, since=T0)
        self.assertEqual([c.end for c in threads[MAIN].calls], [T0 + 60])

    def test_load_keeps_the_log_with_the_latest_call(self):
        """A thread in two logs: the one that sorts last isn't always the one written to last."""
        old = fx.Rollout(self.home, MAIN, self.cwd, day=(2026, 11, 5))
        old.meta()
        old.turn("gpt-6-astra", 258_400)
        old.call(old.start + 60, 1_000, 0, 10, response_id="resp-older")
        old_path = old.write(mtime=T0 + DAY)                  # touched later, say by a copy
        new_path = self.basic(MAIN).write()                    # its calls are the latest
        self.assertEqual(rollout.rollout_files(self.home), [new_path, old_path])
        threads, _stats = rollout.load(self.home)
        self.assertEqual(threads[MAIN].file, new_path)
        self.assertEqual(len(threads[MAIN].calls), 2)

    def test_the_newest_meter_reading(self):
        for i in range(8):                                     # 8 logs modified after it, with older readings
            r = self.rollout()
            r.turn("gpt-6-astra", 258_400, t=T0 + i * 60)
            r.call(T0 + i * 60 + 1, 50_000, 40_000, 1_000)
            r.meter(T0 + i * 60 + 2, 20.0 + i, resets_at=T0 + 3 * DAY)
            r.write(mtime=T0 + HOUR + i)
        self.long_log()
        m = rollout.latest_meter(self.home)
        self.assertEqual((m.weekly_pct, m.t), (50.0, T0 + 5 * HOUR + 61))

    def test_a_running_agent_whose_log_time_stayed(self):
        now = T0 + HOUR
        r = self.basic()
        r.spawn_agent(T0 + 62)
        r.complete(T0 + 64, "A helper is working.")
        r.write()
        h = fx.Rollout(self.home, SUB, self.cwd, kind="subagent", parent=MAIN)
        h.meta(t=T0 + 62)
        h.turn("gpt-6-astra", 258_400, t=T0 + 62)
        h.call(now - 300, 20_000, 0, 100)                     # it wrote 5 minutes ago ...
        h.write(mtime=T0 + 62)                                # ... its log's modification time stayed at its start
        self.assertEqual(rollout.facts(r.path, home=self.home, now=now).agents_running, 1)
        self.assertEqual(rollout.facts(r.path, home=self.home, now=now + 900).agents_running, 0)


class AfterPromptTest(Base):
    def test_a_call_after_a_tool_alone_follows_no_prompt(self):
        """A model that waited 4 h on a tool (agents it started) carries on without anyone sending a prompt."""
        r = self.rollout(MAIN)
        r.meta()
        r.turn("gpt-6-astra", 258_400, t=T0)
        r.user("Start the helpers", t=T0 + 1)
        r.call(T0 + 10, 30_000, 20_000, 100)
        r.tool_output(T0 + 4 * HOUR)
        r.call(T0 + 4 * HOUR + 10, 31_000, 20_000, 100)
        r.turn("gpt-6-astra", 258_400, t=T0 + 6 * HOUR)
        r.call(T0 + 6 * HOUR + 10, 32_000, 20_000, 100)      # a new turn
        r.user("Steer: also check the docs", t=T0 + 6 * HOUR + 20)
        r.call(T0 + 6 * HOUR + 30, 33_000, 32_000, 100)      # a message typed while the turn ran
        th = rollout.read_thread(r.write())
        self.assertEqual([c.after_prompt for c in th.calls], [True, False, True, True])

    def test_text_codex_adds_after_a_tool_is_no_prompt(self):
        """When a tool returns, Codex can add text of its own as a user message, such as its <environment_context>,
        with no content_item_metadata (as on the real logs) or marked as its own: nobody sent a prompt. In a log from
        before Codex marked typed text, a user message without metadata that isn't Codex's markup is a prompt."""
        cases = (("<environment_context>synthetic</environment_context>", "plain", False),
                 ("<environment_context>synthetic</environment_context>", "harness", False),
                 ("# AGENTS.md instructions for the project\n\n<INSTRUCTIONS>\nRun the tests.\n</INSTRUCTIONS>",
                  "plain", False),
                 ('<in-app-browser-context url="https://example.invalid/">a page</in-app-browser-context>', "plain",
                  False),
                 ("<external_codex_apps_open_page>a page</external_codex_apps_open_page>", "plain", False),
                 ("<send_user_message_question_reply>2.0</send_user_message_question_reply>", "plain", False),
                 ("<recommended_plugins>none</recommended_plugins>", "plain", False),
                 ("<user_shell_command>ls</user_shell_command>", "plain", False),
                 ("  Steer: also check the docs", "plain", True),
                 ("<div> in the page header is misaligned; fix it", "plain", True),
                 ("Steer: also check the docs", "user", True))
        for text, kind, sent in cases:
            with self.subTest(text=text[:40], kind=kind):
                r = self.rollout(MAIN)
                r.meta()
                r.turn("gpt-6-astra", 258_400, t=T0)
                r.user("Start the helpers and wait for them.", t=T0 + 1)
                r.call(T0 + 30, 150_000, 0, 1_000)
                r.tool_output(T0 + 1.2 * HOUR)                 # the wait ends
                {"plain": r.plain_user, "harness": r.harness, "user": r.user}[kind](text)
                r.world_state()
                r.call(T0 + 1.2 * HOUR + 10, 152_000, 20_000, 1_000)
                th = rollout.read_thread(r.write())
                self.assertEqual([c.after_prompt for c in th.calls], [True, sent])
                self.assertEqual(th.calls[1].start, T0 + 1.2 * HOUR)     # still an input before the call


class ZstTest(Base):
    def test_compressed_logs_are_read_when_zstd_exists(self):
        r = self.basic()
        r.meter(T0 + 62, 40.0, resets_at=T0 + 3 * DAY)
        path = r.write(compress=True)            # skips this test on a Python without zstd
        self.assertTrue(path.endswith(".jsonl.zst"))
        self.assertTrue(rollout.ZSTD_AVAILABLE)
        self.assertEqual(rollout.find_rollout(MAIN, self.home), path)
        self.assertEqual(next(rollout.iter_records(path))["type"], "session_meta")
        self.assertEqual([c.inp for c in rollout.read_thread(path).calls], [120_000])
        snap = rollout.snapshot(path)
        self.assertEqual((snap.thread, snap.last_call_end, snap.ctx, snap.meter.weekly_pct),
                         (MAIN, T0 + 60, 122_000, 40.0))
        self.assertEqual(rollout.facts(path, home=self.home, now=T0 + DAY).requests, ["Fix the parser"])
        threads, stats = rollout.load(self.home)
        self.assertEqual((list(threads), stats["files"], stats["zst_skipped"]), ([MAIN], 1, 0))

    def test_compressed_logs_are_skipped_without_zstd(self):
        r = self.basic()
        if fx.ZSTD:
            path = r.write(compress=True)
        else:
            path = r.path + ".zst"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(b"\x28\xb5\x2f\xfd not a real frame")
        self.basic(OTHER).write()
        with mock.patch.object(rollout, "ZSTD_AVAILABLE", False):
            self.assertEqual(list(rollout.iter_records(path)), [])
            self.assertIsNone(rollout.read_thread(path))
            self.assertEqual(rollout.snapshot(path), rollout.Snapshot())
            self.assertEqual(rollout.facts(path, home=self.home), rollout.Facts(unread_calls=0))
            threads, stats = rollout.load(self.home)
        self.assertEqual(list(threads), [OTHER])
        self.assertEqual((stats["files"], stats["zst_skipped"]), (1, 1))

    def test_only_compressed_logs_that_may_hold_records_since_count_as_left_out(self):
        """Without zstd a compressed log's bound is its creation, which can be days after its last record: it is
        taken to end within ZST_LAG_S of its modification time, so one made now from a log last written 10 days ago
        can't hold a record of the last 7 days and isn't counted as left out of them."""
        folder = os.path.join(self.home, "sessions", "2026", "10", "28")
        os.makedirs(folder)
        for mtime in (T0 - 10 * DAY, T0 - DAY):
            path = os.path.join(folder, "rollout-2026-10-28T09-00-00-%s.jsonl.zst" % fx.new_id())
            with open(path, "wb") as fh:
                fh.write(b"\x28\xb5\x2f\xfd not a real frame")
            os.utime(path, (mtime, mtime))                 # created now, by the test
        since = T0 - 7 * DAY
        self.assertEqual(len(rollout.rollout_files(self.home, since=since)), 2)    # both picked by their creation
        with mock.patch.object(rollout, "ZSTD_AVAILABLE", False):
            _threads, stats = rollout.load(self.home, since=since)
            self.assertEqual((stats["files"], stats["zst_skipped"]), (0, 1))
            _threads, stats = rollout.load(self.home)
            self.assertEqual(stats["zst_skipped"], 2)
        self.assertEqual(rollout.zst_bound(path, T0 + DAY), T0 + DAY)
        self.assertEqual(rollout.zst_bound(path, T0 + 5 * DAY), T0 - DAY + rollout.ZST_LAG_S)
        self.assertEqual(rollout.zst_bound(os.path.join(folder, "missing.jsonl.zst"), 5.0), 5.0)


if __name__ == "__main__":
    unittest.main()
