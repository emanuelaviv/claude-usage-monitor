"""advisor.state: the data folder's files (sessions, the pending handoff, the decision log, the monitor's files) and
their clean-up."""
import json
import os
import tempfile
import unittest
from unittest import mock

from advisor import VERSION, state
from tests import fixtures as fx

T0, DAY = fx.T0, fx.DAY


def _short_name(path):
    """A folder's 8.3 short name on Windows, or None (another system, or short names are off on that drive)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024):
            return buf.value
    except Exception:
        pass
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self._tmp.name, "data")
        os.makedirs(self.d)
        self.cwd = os.path.join(self._tmp.name, "project")

    def tearDown(self):
        self._tmp.cleanup()

    def path(self, *parts):
        return os.path.join(self.d, *parts)

    def lines(self):
        """Every decision line written, in file order."""
        folder = self.path("decisions")
        out = []
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                out.extend(json.loads(line) for line in fh if line.strip())
        return out

    def names(self, *folder):
        p = self.path(*folder)
        return sorted(os.listdir(p)) if os.path.isdir(p) else []

    def age(self, path, seconds, now=T0):
        """Makes a file look `seconds` old at `now`."""
        os.utime(path, (now - seconds, now - seconds))


class DataDirTest(Base):
    def test_plugin_data_first_and_created(self):
        a, b = self.path("a"), self.path("b")
        self.assertEqual(state.data_dir({"PLUGIN_DATA": a, "CLAUDE_PLUGIN_DATA": b}), a)
        self.assertTrue(os.path.isdir(a))
        self.assertFalse(os.path.exists(b))

    def test_claude_plugin_data_when_plugin_data_is_unset(self):
        b = self.path("b")
        self.assertEqual(state.data_dir({"CLAUDE_PLUGIN_DATA": b}), b)
        self.assertEqual(state.data_dir({"PLUGIN_DATA": "", "CLAUDE_PLUGIN_DATA": b}), b)

    def test_none_without_a_usable_folder(self):
        self.assertIsNone(state.data_dir({}))
        self.assertIsNone(state.data_dir({"PLUGIN_DATA": ""}))
        blocker = self.path("a-file")
        with open(blocker, "w") as fh:
            fh.write("x")
        self.assertIsNone(state.data_dir({"PLUGIN_DATA": os.path.join(blocker, "data")}))


class CwdKeyTest(Base):
    def test_sixteen_hex_digits(self):
        key = state.cwd_key(self.cwd)
        self.assertRegex(key, r"^[0-9a-f]{16}$")

    def test_one_folder_one_key(self):
        key = state.cwd_key(self.cwd)
        self.assertEqual(state.cwd_key(self.cwd + os.sep), key)
        self.assertEqual(state.cwd_key(os.path.join(self.cwd, "sub", "..")), key)
        self.assertNotEqual(state.cwd_key(self.cwd + "-other"), key)

    @unittest.skipUnless(os.name == "nt", "Windows paths")
    def test_windows_spellings(self):
        key = state.cwd_key(r"C:\Work\Proj")
        for same in ("c:/work/proj", "C:/Work/Proj/", r"c:\WORK\proj", "\\\\?\\C:\\Work\\Proj"):
            with self.subTest(same=same):
                self.assertEqual(state.cwd_key(same), key)
        self.assertEqual(state.cwd_key("\\\\?\\UNC\\server\\share\\x"), state.cwd_key(r"\\server\share\x"))

    def test_a_link_or_a_short_name_shares_the_key(self):
        """The same folder opened through a junction or symbolic link to it, or by its 8.3 short name (Windows)."""
        real = os.path.join(self._tmp.name, "my project folder")
        os.makedirs(real)
        key = state.cwd_key(real)
        tried = 0
        link = os.path.join(self._tmp.name, "project-link")
        if fx.link_folder(real, link):
            tried += 1
            self.assertEqual(state.cwd_key(link), key)
            self.assertEqual(state.cwd_key(os.path.join(link, "")), key)
        short = _short_name(real)
        if short and os.path.normcase(short) != os.path.normcase(real):
            tried += 1
            self.assertEqual(state.cwd_key(short), key)
        if not tried:
            self.skipTest("this system makes neither links to folders nor short names")


class FilesTest(Base):
    def test_round_trip_leaves_no_temporary_file(self):
        p = self.path("sub", "x.json")
        state.write_json(p, {"a": 1, "b": [1.5, None], "c": "ok"})
        self.assertEqual(state.read_json(p), {"a": 1, "b": [1.5, None], "c": "ok"})
        state.write_json(p, {"a": 2})
        self.assertEqual(state.read_json(p), {"a": 2})
        self.assertEqual(self.names("sub"), ["x.json"])

    def test_bad_files_read_as_none(self):
        self.assertIsNone(state.read_json(self.path("missing.json")))
        for i, data in enumerate((b"", b"{not json", b"[1, 2]", b"42", b"\xff\xfe\x00", b"[" * 100_000)):
            p = self.path("bad%d.json" % i)
            with open(p, "wb") as fh:
                fh.write(data)
            with self.subTest(data=data[:10]):
                self.assertIsNone(state.read_json(p))

    def test_a_hand_edited_file_with_a_byte_order_mark_reads_as_usual(self):
        """Windows PowerShell's Set-Content -Encoding UTF8 starts the file with a BOM; Out-File writes UTF-16, which
        stays unreadable (None)."""
        p = self.path("guardrail.json")
        with open(p, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + b'{"after_break_enabled": true}\r\n')
        self.assertEqual(state.read_json(p), {"after_break_enabled": True})
        with open(p, "wb") as fh:
            fh.write('{"after_break_enabled": true}'.encode("utf-16"))
        self.assertIsNone(state.read_json(p))

    def test_non_finite_numbers_are_written_as_null(self):
        p = self.path("x.json")
        state.write_json(p, {"a": float("nan"), "b": [float("inf"), -float("inf")], "c": {"d": float("nan")}})

        def strict(name):
            raise ValueError(name)
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.read(), parse_constant=strict), {"a": None, "b": [None, None], "c": {"d": None}})

    def test_failed_write_keeps_the_old_file(self):
        p = self.path("x.json")
        state.write_json(p, {"a": 1})
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                state.write_json(p, {"a": 2})
        self.assertEqual(state.read_json(p), {"a": 1})
        self.assertEqual(self.names(), ["x.json"])


class SessionTest(Base):
    def test_empty_at_first_then_saved(self):
        self.assertEqual(state.session(self.d, "s-1"), {})
        state.save_session(self.d, "s-1", {"offered_at": T0, "cwd_key": "abc"})
        self.assertEqual(state.session(self.d, "s-1"), {"offered_at": T0, "cwd_key": "abc"})
        self.assertEqual(self.names("sessions"), ["s-1.json"])

    def test_ids_cannot_point_outside_the_folder(self):
        for sid in ("../../evil", "..\\..\\evil", "a/b:c*d", "", "x" * 500):
            state.save_session(self.d, sid, {"n": 1})
            with self.subTest(sid=sid[:20]):
                self.assertEqual(state.session(self.d, sid), {"n": 1})
        self.assertEqual(sorted(os.listdir(self._tmp.name)), ["data"])
        self.assertEqual(self.names(), ["sessions"])
        for name in self.names("sessions"):
            self.assertRegex(name, r"^[A-Za-z0-9_-]+\.json$")


class PendingTest(Base):
    def test_put_then_take(self):
        state.put_pending(self.d, self.cwd, "s-1", "the handoff", {"ctx": 410_000}, T0)
        key = state.cwd_key(self.cwd)
        self.assertEqual(self.names("pending"), [key + ".json", key + ".txt"])
        rec = state.take_pending(self.d, self.cwd)
        self.assertEqual(rec, {"from_session": "s-1", "at": T0, "numbers": {"ctx": 410_000}, "text": "the handoff"})
        self.assertEqual(self.names("pending"), [])
        self.assertIsNone(state.take_pending(self.d, self.cwd))

    def test_one_handoff_per_folder(self):
        state.put_pending(self.d, self.cwd, "s-1", "first", {}, T0)
        state.put_pending(self.d, self.cwd, "s-2", "second", {}, T0 + 60)
        self.assertIsNone(state.take_pending(self.d, self.cwd + "-other"))
        rec = state.take_pending(self.d, self.cwd)
        self.assertEqual((rec["from_session"], rec["text"]), ("s-2", "second"))

    def test_drop_only_by_the_conversation_that_made_it(self):
        state.put_pending(self.d, self.cwd, "s-1", "text", {}, T0)
        self.assertFalse(state.drop_pending(self.d, self.cwd, from_session="s-2"))
        self.assertEqual(len(self.names("pending")), 2)
        self.assertTrue(state.drop_pending(self.d, self.cwd, from_session="s-1"))
        self.assertEqual(self.names("pending"), [])
        state.put_pending(self.d, self.cwd, "s-1", "text", {}, T0)
        self.assertTrue(state.drop_pending(self.d, self.cwd))
        self.assertFalse(state.drop_pending(self.d, self.cwd))

    def test_peek_leaves_it_in_place(self):
        self.assertIsNone(state.peek_pending(self.d, self.cwd))
        state.put_pending(self.d, self.cwd, "s-1", "the handoff", {"ctx": 410_000}, T0)
        self.assertEqual(state.peek_pending(self.d, self.cwd), {"from_session": "s-1", "at": T0,
                                                                "numbers": {"ctx": 410_000}})
        self.assertEqual(state.take_pending(self.d, self.cwd)["text"], "the handoff")

    def test_test_handoffs_are_marked(self):
        state.put_pending(self.d, self.cwd, "s-1", "text", {}, T0, test=True)
        self.assertIs(state.take_pending(self.d, self.cwd)["test"], True)
        state.put_pending(self.d, self.cwd, "s-1", "text", {}, T0)
        self.assertNotIn("test", state.take_pending(self.d, self.cwd))

    def test_text_round_trips(self):
        text = "Hebrew שלום, accents café, an emoji 🙂, and a lone half of a pair \ud83d."
        state.put_pending(self.d, self.cwd, "s-1", text, {}, T0)
        got = state.take_pending(self.d, self.cwd)["text"]
        self.assertEqual(got, text.replace("\ud83d", "?"))
        got.encode("utf-8")           # strict: no lone surrogate survives

    def test_a_handoff_is_taken_once(self):
        state.put_pending(self.d, self.cwd, "s-1", "text", {}, T0)
        meta = self.path("pending", state.cwd_key(self.cwd) + ".json")
        real = os.replace

        def another_process_first(src, dst):
            if src == meta:                 # another conversation renamed it a moment earlier
                real(src, dst + ".other")
                raise FileNotFoundError(src)
            return real(src, dst)
        with mock.patch("os.replace", side_effect=another_process_first):
            self.assertIsNone(state.take_pending(self.d, self.cwd))


class LogTest(Base):
    def test_one_line_per_decision_in_the_month_file(self):
        state.log(self.d, "offer", t=T0, session="s-1", model="gpt-6-astra", risk="low", flags=["uncommitted"],
                  numbers={"ctx": 410_000}, cwd_key="abc")
        state.log(self.d, "kept", t=T0 + 60, session="s-1")
        self.assertEqual(self.names("decisions"), ["2026-11.jsonl"])
        offer, kept = self.lines()
        self.assertEqual(offer, {"t": T0, "event": "offer", "session": "s-1", "version": VERSION, "model": "gpt-6-astra",
                                 "risk": "low", "flags": ["uncommitted"], "numbers": {"ctx": 410_000}, "cwd_key": "abc"})
        self.assertEqual(kept, {"t": T0 + 60, "event": "kept", "session": "s-1", "version": VERSION})

    def test_session_is_always_there(self):
        state.log(self.d, "expired", t=T0, from_session="s-1", cwd_key="abc")
        self.assertEqual(self.lines(), [{"t": T0, "event": "expired", "session": None, "version": VERSION,
                                         "from_session": "s-1", "cwd_key": "abc"}])

    def test_month_comes_from_t(self):
        state.log(self.d, "demo", t=T0 + 30 * DAY, session="s-1")
        self.assertEqual(self.names("decisions"), ["2026-12.jsonl"])

    def test_strict_json(self):
        state.log(self.d, "note", t=T0, session="s-1", model=None, numbers={"x": float("nan"), "y": float("inf")})

        def strict(name):
            raise ValueError(name)
        with open(self.path("decisions", "2026-11.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(json.loads(fh.readline(), parse_constant=strict)["numbers"], {"x": None, "y": None})

    def test_never_raises(self):
        blocker = self.path("a-file")
        with open(blocker, "w") as fh:
            fh.write("x")
        state.log(blocker, "offer", t=T0, session="s-1")        # a data folder that is a file
        state.log(self.d, "offer", t="not a time", session="s-1")
        self.assertEqual(self.lines(), [])


class MonitorFilesTest(Base):
    def test_params(self):
        self.assertIsNone(state.load_params(self.d))
        self.assertIsNone(state.load_params(None))
        state.write_json(self.path("params.json"), {"base_tokens": 30_000})
        self.assertEqual(state.load_params(self.d), {"base_tokens": 30_000})

    def test_guardrail(self):
        self.assertFalse(state.guardrail_off(self.d))
        self.assertFalse(state.guardrail_off(None))
        for value, off in ((False, True), (True, False), ("false", False), (0, False), (None, False)):
            state.write_json(self.path("guardrail.json"), {"after_break_enabled": value, "reason": "test"})
            with self.subTest(value=value):
                self.assertEqual(state.guardrail_off(self.d), off)
        with open(self.path("guardrail.json"), "w") as fh:
            fh.write("{broken")
        self.assertFalse(state.guardrail_off(self.d))


class PruneTest(Base):
    def test_expired_handoff_is_deleted_and_logged(self):
        state.put_pending(self.d, self.cwd, "s-1", "text", {"ctx": 1}, T0)
        state.prune(self.d, T0 + 1800)
        self.assertEqual(len(self.names("pending")), 2)       # 30 minutes old is still young enough
        self.assertEqual(self.lines(), [])
        state.prune(self.d, T0 + 1801)
        self.assertEqual(self.names("pending"), [])
        self.assertEqual(self.lines(), [{"t": T0 + 1801, "event": "expired", "session": None, "version": VERSION,
                                         "from_session": "s-1", "cwd_key": state.cwd_key(self.cwd)}])

    def test_an_expired_handoff_is_logged_once(self):
        state.put_pending(self.d, self.cwd, "s-1", "text", {}, T0)
        meta = self.path("pending", state.cwd_key(self.cwd) + ".json")
        real = os.replace

        def another_hook_first(src, dst):
            if src == meta:                 # another hook pruning at the same moment renamed it first
                real(src, dst + ".other")
                raise FileNotFoundError(src)
            return real(src, dst)
        with mock.patch("os.replace", side_effect=another_hook_first):
            state.prune(self.d, T0 + 1801)
        self.assertEqual(self.lines(), [])

    def test_a_handoff_ahead_of_the_clock_is_expired(self):
        """Made while the clock ran fast (set right since): more than a minute ahead, it is deleted and logged, so it
        never waits for as long as the clock was off."""
        key = state.cwd_key(self.cwd)
        for ahead, kept in ((60, True), (61, False), (3 * 3600, False)):
            with self.subTest(ahead=ahead):
                state.put_pending(self.d, self.cwd, "s-1", "text", {}, T0 + ahead)
                state.prune(self.d, T0)
                self.assertEqual(self.names("pending"), [key + ".json", key + ".txt"] if kept else [])
        self.assertEqual(self.lines(), [{"t": T0, "event": "expired", "session": None, "version": VERSION,
                                         "from_session": "s-1", "cwd_key": key}] * 2)

    def test_stale(self):
        self.assertEqual([state.stale(T0 + d, T0, 1800) for d in (-1801, -1800, 0, 60, 61)],
                         [True, False, False, False, True])

    def test_files_written_ahead_of_the_clock_go_too(self):
        """A handoff without its time ages by its file, and a text left without its .json or a temporary copy by
        theirs: written more than a minute ahead of the clock, they go as well."""
        os.makedirs(self.path("pending"))
        files = [self.path("pending", "0123456789abcdef.json"), self.path("pending", "orphan.txt"),
                 self.path("pending", "k.txt.12.tmp"), self.path("pending", "n.json.13.claim")]
        state.write_json(files[0], {"from_session": "s-1"})
        for p in files[1:]:
            with open(p, "w") as fh:
                fh.write("x")
        recent = self.path("pending", "recent.txt")
        with open(recent, "w") as fh:
            fh.write("x")
        for p in files:
            self.age(p, -3 * 3600)              # 3 hours ahead
        self.age(recent, -30)                   # half a minute ahead: a clock that differs a little
        state.prune(self.d, T0)
        self.assertEqual(self.names("pending"), ["recent.txt"])
        self.assertEqual([e["event"] for e in self.lines()], ["expired"])

    def test_its_own_lifetime(self):
        state.put_pending(self.d, self.cwd, "s-1", "text", {}, T0)
        state.prune(self.d, T0 + 700, ttl_s=600)
        self.assertEqual(self.names("pending"), [])

    def test_a_handoff_without_its_time_ages_by_the_file(self):
        p = self.path("pending", "0123456789abcdef.json")
        state.write_json(p, {"from_session": "s-1"})
        self.age(p, 1700)
        state.prune(self.d, T0)
        self.assertEqual(self.names("pending"), ["0123456789abcdef.json"])
        self.age(p, 1900)
        state.prune(self.d, T0)
        self.assertEqual(self.names("pending"), [])

    def test_old_sessions_are_deleted(self):
        state.save_session(self.d, "old", {"n": 1})
        state.save_session(self.d, "recent", {"n": 2})
        self.age(self.path("sessions", "old.json"), 8 * DAY)
        self.age(self.path("sessions", "recent.json"), 6 * DAY)
        state.prune(self.d, T0)
        self.assertEqual(self.names("sessions"), ["recent.json"])

    def test_leftovers_are_deleted(self):
        os.makedirs(self.path("pending"))
        os.makedirs(self.path("sessions"))
        old = [self.path("pending", "k.json.12.tmp"), self.path("pending", "k.json.12.claim"),
               self.path("sessions", "s.json.12.tmp"), self.path("pending", "orphan.txt")]
        new = [self.path("pending", "n.json.13.tmp"), self.path("sessions", "n.json.13.tmp")]
        for p in old + new:
            with open(p, "w") as fh:
                fh.write("x")
        for p in old:
            self.age(p, 2 * 3600)
        for p in new:
            self.age(p, 60)
        state.prune(self.d, T0)
        self.assertEqual(self.names("pending"), ["n.json.13.tmp"])
        self.assertEqual(self.names("sessions"), ["n.json.13.tmp"])

    def test_a_temporary_copy_of_a_handoff_lives_no_longer_than_the_handoff(self):
        """A hook stopped between writing a handoff's text to a temporary file and renaming it leaves that text
        behind: it goes after the handoff's 30 minutes, like the handoff itself."""
        os.makedirs(self.path("pending"))
        os.makedirs(self.path("sessions"))
        copy, claim, session = (self.path("pending", "k.txt.12.tmp"), self.path("pending", "k.json.12.claim"),
                                self.path("sessions", "s.json.12.tmp"))
        for p in (copy, claim, session):
            with open(p, "w") as fh:
                fh.write("x")
            self.age(p, 1801)
        state.prune(self.d, T0)
        self.assertEqual(self.names("pending"), [])
        self.assertEqual(self.names("sessions"), ["s.json.12.tmp"])      # no text: the hour still applies

    def test_an_empty_folder_is_fine(self):
        state.prune(self.d, T0)
        state.prune(self.path("missing"), T0)
        self.assertEqual(self.names(), [])


if __name__ == "__main__":
    unittest.main()
