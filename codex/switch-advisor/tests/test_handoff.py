"""advisor.handoff: the handoff's parts and limits, leaving out the held-back prompt, and git status."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from advisor import handoff, rollout, rule
from advisor.rollout import TEXT_MAX, Facts
from tests import fixtures as fx

CWD = os.path.join(os.sep, "work", "demo-project")      # a made-up folder; build() only prints it
FILES = [" M src/parser.py", "?? docs/"]
SPEC_INTRO = ("This conversation continues work from an earlier Codex conversation in this project. These notes were "
              "built on this computer from that conversation's log, with no model call: check them against the files, "
              "and ask the user when something is unclear.")


def facts(**kw):
    """Facts of a made-up conversation: five typed requests, a last reply that asks something, two agents."""
    args = dict(thread="t-1", cwd=CWD, branch="feature/parser",
                requests=["Build a streaming parser for the logs.", "Add tests for quoted fields.",
                          "Make it skip bad lines.", "Run the tests again.", "Now write the docs."],
                last_reply="The docs are written. Should I also update the changelog?", asks_you=True,
                agents_spawned=2)
    args.update(kw)
    if "first_request" not in kw:
        args["first_request"] = args["requests"][0] if args["requests"] else None
    return Facts(**args)


FIRST = "The first request (first 1,500 characters):"
LATEST = "The latest requests, newest last (up to 3, each up to 600 characters):"
REPLY = "Where it stopped, its last reply (first 1,500 characters):"
CHANGES = "Uncommitted changes (git status, up to 12 files):"


def part(text, header):
    """The body of the part headed `header` (up to the next blank line), or None."""
    for p in text.split("\n\n"):
        if p.startswith(header + "\n"):
            return p[len(header) + 1:]
    return None


class BuildTest(unittest.TestCase):
    def test_every_part_in_order(self):
        self.assertEqual(handoff.build(facts(), FILES), "\n\n".join([
            SPEC_INTRO,
            "Project: %s (branch feature/parser)" % CWD,
            "The first request (first 1,500 characters):\nBuild a streaming parser for the logs.",
            "The latest requests, newest last (up to 3, each up to 600 characters):\n"
            "- Make it skip bad lines.\n- Run the tests again.\n- Now write the docs.",
            "Where it stopped, its last reply (first 1,500 characters):\n"
            "The docs are written. Should I also update the changelog?",
            "Uncommitted changes (git status, up to 12 files):\n-  M src/parser.py\n- ?? docs/",
            "The earlier conversation started 2 agents; their reports stay in that conversation.",
        ]))

    def test_missing_parts_are_left_out(self):
        self.assertEqual(handoff.build(Facts(), []), SPEC_INTRO)
        self.assertEqual(handoff.build(Facts(cwd=CWD), None), SPEC_INTRO + "\n\nProject: " + CWD)
        for branch in (None, "", "HEAD"):
            with self.subTest(branch=branch):
                self.assertIn("\n\nProject: %s\n\n" % CWD, handoff.build(facts(branch=branch), []))
        text = handoff.build(facts(requests=[], last_reply=None, agents_spawned=0), [])
        self.assertEqual(text, SPEC_INTRO + "\n\nProject: %s (branch feature/parser)" % CWD)

    def test_the_latest_requests_leave_out_the_first(self):
        self.assertIsNone(part(handoff.build(facts(requests=["Only one."]), []), LATEST))
        two = handoff.build(facts(requests=["First.", "Second."]), [])
        self.assertEqual(part(two, FIRST), "First.")
        self.assertEqual(part(two, LATEST),
                         "- Second.")

    def test_a_first_request_without_the_list(self):
        text = handoff.build(Facts(first_request="Just this.", requests=[]), [])
        self.assertEqual(part(text, FIRST), "Just this.")

    def test_agents(self):
        self.assertNotIn("agent", handoff.build(facts(agents_spawned=0), []))
        self.assertTrue(handoff.build(facts(agents_spawned=1), []).endswith(
            "The earlier conversation started 1 agent; its report stays in that conversation."))
        self.assertTrue(handoff.build(facts(agents_spawned=3), []).endswith(
            "The earlier conversation started 3 agents; their reports stay in that conversation."))

    def test_the_text_holds_no_time(self):
        self.assertEqual(handoff.build(facts(), FILES, now=1.0), handoff.build(facts(), FILES, now=2e9))


class HeldBackPromptTest(unittest.TestCase):
    PROMPT = "Please carry on with the CSV case\nand keep the interface."

    def test_left_out_when_the_log_already_has_it(self):
        f = facts(requests=["Build it.", "Add tests.", self.PROMPT])
        text = handoff.build(f, [], prompt=self.PROMPT)
        self.assertNotIn("carry on with the CSV", text)
        self.assertEqual(part(text, LATEST),
                         "- Add tests.")
        self.assertIn("carry on with the CSV", handoff.build(f, []))          # without prompt=, it is a request

    def test_the_same_words_with_other_spacing(self):
        f = facts(requests=["Build it.", "Please carry on with the CSV case and keep the interface."])
        self.assertEqual(handoff.requests(f, "  Please carry on with the CSV case\r\n and keep  the interface. "),
                         ["Build it."])

    def test_repeated_attempts_at_the_end_all_go(self):
        f = facts(requests=["Build it.", "Go on.", "Add tests.", "Go on.", "Go on."])
        self.assertEqual(handoff.requests(f, "Go on."), ["Build it.", "Go on.", "Add tests."])

    def test_a_long_prompt_the_log_cut(self):
        long = "word " * 1500                         # 7,500 characters; the log keeps the first 4,000
        f = facts(requests=["Build it.", long.strip()[:TEXT_MAX]])
        self.assertEqual(handoff.requests(f, long), ["Build it."])

    def test_when_it_is_the_only_request(self):
        f = facts(requests=[self.PROMPT])
        self.assertEqual(handoff.requests(f, self.PROMPT), [])
        self.assertIsNone(part(handoff.build(f, [], prompt=self.PROMPT), FIRST))


class LimitsTest(unittest.TestCase):
    def test_each_part_is_cut(self):
        f = facts(requests=["a" * 5000, "b" * 900, "c" * 900, "d" * 900], last_reply="e" * 3000)
        text = handoff.build(f, [" M f%02d.py" % i for i in range(20)])
        first = part(text, FIRST)
        self.assertEqual(first, "a" * 1500 + handoff.CUT)
        latest = part(text, LATEST).split("\n")
        self.assertEqual(latest, ["- " + ch * 600 + handoff.CUT for ch in "bcd"])
        self.assertEqual(part(text, REPLY),
                         "e" * 1500 + handoff.CUT)
        files = part(text, CHANGES).split("\n")
        self.assertEqual(files, ["-  M f%02d.py" % i for i in range(12)] + ["- and 8 more"])

    def test_a_cut_ends_at_a_line_break_near_the_end(self):
        reply = "\n".join("line %03d of the reply" % i for i in range(200))
        body = part(handoff.build(facts(last_reply=reply), []), REPLY)
        self.assertTrue(body.endswith(" of the reply" + handoff.CUT))
        self.assertLessEqual(len(body), 1500 + len(handoff.CUT))
        self.assertGreater(len(body), 1400)

    def test_never_over_8000_characters(self):
        f = facts(cwd="C:\\" + "deep\\" * 400, branch="b" * 500, requests=["r" * 5000] * 6, last_reply="x" * 5000,
                  agents_spawned=1_000_000)
        text = handoff.build(f, ["?? " + "p" * 1000 + str(i) for i in range(500)])
        self.assertLessEqual(len(text), handoff.MAX_CHARS)
        self.assertTrue(text.startswith(SPEC_INTRO))

    def test_multi_line_requests_stay_inside_their_item(self):
        text = handoff.build(facts(requests=["First.", "line one\nline two\n\n\n\nline three"]), [])
        self.assertEqual(part(text, LATEST), "- line one\n  line two\n  \n  line three")

    def test_blank_runs_are_shortened(self):
        text = handoff.build(facts(last_reply="Done.\n\n\n\n\nNext?", agents_spawned=0), [])
        self.assertTrue(text.endswith(REPLY + "\nDone.\n\nNext?"))

    def test_git_lines_keep_their_status_columns(self):
        text = handoff.build(Facts(), [" M a.py", "M  b.py", "MM c.py", "", "   "])
        self.assertEqual(part(text, CHANGES), "-  M a.py\n- M  b.py\n- MM c.py")

    def test_the_text_always_encodes(self):
        f = facts(requests=["\U0001F600" * 2000, "half \ud83d of a pair"], last_reply="ok \udc00 done",
                  cwd=CWD + "\ud800")
        text = handoff.build(f, ["?? caf\udce9.txt"])
        text.encode("utf-8")                                   # strict: no lone surrogate left
        self.assertEqual(part(text, FIRST), "\U0001F600" * 1500 + handoff.CUT)
        self.assertIn("- half ? of a pair", text)


QUESTION = {"title": "Which database should the service use?", "options": ["Postgres", "SQLite"]}
ASKED = "Waiting for the user's answer to:"


def asking(questions, **kw):
    """facts() with open_questions (set on the object, so this works with a Facts from before the field too)."""
    f = facts(**kw)
    f.open_questions = questions
    return f


class OpenQuestionsTest(unittest.TestCase):
    """Questions the conversation is still waiting for the user to answer (Codex's question panel): the handoff carries
    them, with their options, right after the last reply, and never cuts them."""

    def test_after_the_last_reply(self):
        text = handoff.build(asking([QUESTION], last_reply="I sent you a question in the panel."), FILES)
        parts = text.split("\n\n")
        at = next(i for i, p in enumerate(parts) if p.startswith(REPLY))
        self.assertEqual(parts[at + 1], ASKED + "\n- Which database should the service use?\n  Options: Postgres; SQLite")
        self.assertTrue(parts[at + 2].startswith(CHANGES))
        self.assertEqual(text.replace("\n\n" + parts[at + 1], ""),
                         handoff.build(facts(last_reply="I sent you a question in the panel."), FILES))
        no_reply = handoff.build(asking([QUESTION], last_reply=None), [])
        self.assertTrue(no_reply.split("\n\n")[-2].startswith(ASKED))      # before the agents line

    def test_each_question_with_its_options(self):
        f = asking([{"title": "First?\r\nIts second line.", "options": []}, {"title": "", "options": ["a", "b"]},
                    QUESTION])
        self.assertEqual(part(handoff.build(f, []), ASKED),
                         "- First?\n  Its second line.\n- Options: a; b\n"
                         "- Which database should the service use?\n  Options: Postgres; SQLite")

    def test_questions_the_plugin_cant_read_are_counted(self):
        """rollout.facts counts the open question calls it can't read (unread_calls): the handoff says a question in
        the panel isn't shown, after the ones it carries, or alone."""
        for questions, unread, line in (
                ([QUESTION], 1, "(Another question in the question panel couldn't be read here, so it is not shown.)"),
                ([QUESTION], 2, "(2 more questions in the question panel couldn't be read here, so they are not "
                                "shown.)"),
                ([], 1, "(A question in the question panel couldn't be read here, so it is not shown.)"),
                ([], 3, "(3 questions in the question panel couldn't be read here, so they are not shown.)")):
            with self.subTest(questions=len(questions), unread=unread):
                f = asking(questions, last_reply="Asked.")
                f.unread_calls = unread
                body = part(handoff.build(f, []), ASKED)
                self.assertTrue(body.endswith("\n" + line) or body == line, body)
        for unread in (0, None, True, "1"):
            with self.subTest(unread=unread):
                f = asking([], last_reply="Asked.")
                f.unread_calls = unread
                self.assertNotIn(ASKED, handoff.build(f, []))

    def test_older_questions_left_out_are_counted(self):
        """rollout.facts keeps the newest 3 open questions and counts them all (open_question_count): the handoff says
        how many older ones it leaves out, and says nothing when it carries them all."""
        three = [{"title": "Q%d?" % i, "options": ["a", "b"]} for i in (3, 4, 5)]
        for count, line in ((5, "(2 earlier questions, asked before these, are not shown.)"),
                            (4, "(1 earlier question, asked before these, is not shown.)")):
            with self.subTest(count=count):
                f = asking(three, last_reply="Asked.")
                f.open_question_count = count
                self.assertEqual(part(handoff.build(f, []), ASKED), "- Q3?\n  Options: a; b\n- Q4?\n  Options: a; b\n"
                                                                   "- Q5?\n  Options: a; b\n" + line)
        for count in (3, 0, None, "9", True):
            with self.subTest(count=count):
                f = asking(three, last_reply="Asked.")
                f.open_question_count = count
                self.assertNotIn("earlier question", handoff.build(f, []))
        f = facts()
        f.open_question_count = 4                            # a count without a question it can carry: no section
        self.assertNotIn(ASKED, handoff.build(f, []))

    def test_without_open_questions_nothing_changes(self):
        for questions in (None, [], [{"title": "", "options": []}], ["x"], "x"):
            with self.subTest(questions=questions):
                f = facts() if questions is None else asking(questions)
                self.assertEqual(handoff.build(f, FILES), handoff.build(facts(), FILES))

    def test_the_caps(self):
        """The newest 3 questions, each title up to 500 characters, at most 8 options of up to 120 characters."""
        questions = [{"title": "q%d " % i + "t" * 600, "options": ["o%d " % j + "p" * 200 for j in range(10)]}
                     for i in range(5)]
        body = part(handoff.build(asking(questions, last_reply="Asked."), []), ASKED)
        body, left = body.rsplit("\n", 1)
        self.assertEqual(left, "(2 earlier questions, asked before these, are not shown.)")
        items = body.split("\n- ")
        self.assertEqual(len(items), 3)
        for i, item in zip((2, 3, 4), items):
            title, options = item.lstrip("- ").split("\n  Options: ")
            self.assertEqual(title, ("q%d " % i + "t" * 600)[:500] + handoff.CUT)
            self.assertEqual(options.split("; "), [("o%d " % j + "p" * 200)[:120] + handoff.CUT for j in range(8)])
        self.assertEqual(rule.QUESTIONS_KEPT, 3)
        self.assertEqual(rule._CUT, handoff.CUT)
        self.assertEqual(rule.REPLY_KEPT, handoff.REPLY_CHARS)

    def test_a_long_question_from_the_log_is_whole_or_marked(self):
        """From a log: rollout.facts keeps a question's title and options whole, so the handoff carries a title of 435
        characters whole, the question at its end included, and a cut one (a title over 500 characters, an option
        over 120) ends with the cut mark."""
        ask = "Which one should the service use from now on?"
        whole = "I compared the two storage layouts on the benchmark you gave me. " * 6 + ask
        long_title = "Some background for the second choice. " * 20 + "Which one?"
        option = "Keep the row layout, and add an index on the timestamp column so the nightly report stays fast " * 2
        with tempfile.TemporaryDirectory() as tmp:
            home = fx.make_home(tmp)
            r = fx.Rollout(home, fx.new_id(), os.path.join(tmp, "project"))
            r.meta(t=fx.T0)
            r.turn("gpt-6-astra", 832_200, t=fx.T0)
            r.user("Benchmark the storage layouts.", t=fx.T0 + 1)
            r.call(fx.T0 + 10, 400_000, 0, 10_000)
            r.ask(fx.T0 + 11, [{"title": whole, "options": [option, "Switch to the column layout"]},
                               {"title": long_title, "options": []}])
            r.complete(fx.T0 + 12, "I sent you two questions in the panel.")
            f = rollout.facts(r.write(), home=home, now=fx.T0 + 8 * fx.HOUR)
        self.assertEqual((len(whole), len(option.strip())), (435, 189))
        self.assertEqual(part(handoff.build(f, [], prompt="The column one."), ASKED),
                         "- " + whole + "\n  Options: " + option.strip()[:120].rstrip() + handoff.CUT
                         + "; Switch to the column layout\n- " + long_title[:500].rstrip() + handoff.CUT)

    def big_questions(self):
        return [{"title": "q%d " % i + "t" * 600, "options": ["o%d " % j + "p" * 200 for j in range(10)]}
                for i in range(3)]

    def test_the_reply_makes_room_first(self):
        f = asking(self.big_questions(), requests=["a" * 2000] + ["r%d " % i + "b" * 300 for i in range(3)],
                   last_reply="e" * 3000, agents_spawned=0, cwd=None)
        whole = handoff.build(f, [])
        self.assertLessEqual(len(whole), handoff.MAX_CHARS)
        self.assertIn(handoff._asked(f), whole)                   # the questions whole
        without = handoff.build(asking([], **{k: getattr(f, k) for k in ("requests", "last_reply", "agents_spawned",
                                                                         "cwd")}), [])
        self.assertEqual(part(whole, FIRST), part(without, FIRST))    # the requests as they were
        self.assertEqual(part(whole, LATEST), part(without, LATEST))
        header = next(p for p in whole.split("\n\n") if p.startswith("Where it stopped")).split("\n")[0]
        kept = int(header.split("(first ")[1].split(" characters")[0].replace(",", ""))
        self.assertLess(kept, 1500)
        self.assertGreaterEqual(kept, handoff.REPLY_MIN)
        self.assertEqual(part(whole, header), "e" * kept + handoff.CUT)
        self.assertGreater(len(whole), handoff.MAX_CHARS - 50)        # the room the reply gave is used

    def test_the_questions_are_never_cut(self):
        f = asking(self.big_questions(), cwd="C:\\" + "deep\\" * 400, branch="b" * 500, requests=["r" * 5000] * 6,
                   last_reply="x" * 5000, agents_spawned=1_000_000)
        text = handoff.build(f, ["?? " + "p" * 1000 + str(i) for i in range(500)])
        self.assertLessEqual(len(text), handoff.MAX_CHARS)
        self.assertTrue(text.startswith(SPEC_INTRO))
        self.assertTrue(text.endswith("\n\n" + handoff._asked(f)))
        self.assertNotIn("Where it stopped", text)                    # the reply went first


class GitStatusTest(unittest.TestCase):
    def test_no_folder_no_lines(self):
        self.assertEqual(handoff.git_status(os.path.join(tempfile.gettempdir(), "no-such-folder-0b1c2d3e")), [])
        self.assertEqual(handoff.git_status(""), [])
        self.assertEqual(handoff.git_status(None), [])

    def test_the_command(self):
        done = subprocess.CompletedProcess([], 0, stdout=" M a.py\r\n?? caf\xc3\xa9.txt\n\n".encode("latin-1"))
        git = os.path.join(tempfile.gettempdir(), "tools", "git")
        with mock.patch.object(handoff, "git_program", return_value=git), \
                mock.patch("subprocess.run", return_value=done) as run:
            self.assertEqual(handoff.git_status(tempfile.gettempdir()), [" M a.py", "?? café.txt"])
        args, kw = run.call_args
        self.assertEqual(args[0], [git, "--no-optional-locks", "-C", tempfile.gettempdir(), "-c", "core.quotepath=off",
                                   "-c", "core.fsmonitor=false", "-c", "safe.bareRepository=explicit", "status",
                                   "--porcelain=v1"])
        self.assertEqual(kw["timeout"], 3.0)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_git_is_never_started_by_its_bare_name(self):
        """The hook runs in the project's folder, and Windows looks there first for a program named by its bare name:
        a git.exe planted in the project would run with your rights. git comes from an absolute PATH folder only."""
        name = "git.exe" if os.name == "nt" else "git"
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            project, tools = os.path.join(tmp, "project"), os.path.join(tmp, "tools")
            for folder in (project, tools, os.path.join(project, "bin")):
                os.makedirs(folder)
                with open(os.path.join(folder, name), "wb") as fh:
                    fh.write(b"#!/bin/sh\n")
                os.chmod(os.path.join(folder, name), 0o755)
            os.chdir(project)
            try:
                self.assertEqual(handoff.git_program(os.pathsep.join([".", "", "bin", tools])),
                                 os.path.join(tools, name))
                self.assertIsNone(handoff.git_program(os.pathsep.join([".", "", "bin", '"."'])))
                done = subprocess.CompletedProcess([], 0, stdout=b"")
                with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([".", tools])}), \
                        mock.patch("subprocess.run", return_value=done) as run:
                    handoff.git_status(project)
                self.assertEqual(run.call_args[0][0][0], os.path.join(tools, name))
                with mock.patch.dict(os.environ, {"PATH": "."}), mock.patch("subprocess.run") as run:
                    self.assertEqual(handoff.git_status(project), [])
                run.assert_not_called()
            finally:
                os.chdir(here)

    @unittest.skipIf(handoff.git_program() is None, "git is not installed")
    def test_a_repositorys_own_fsmonitor_command_is_not_run(self):
        """core.fsmonitor in a repository's config names a command `git status` runs; an agent working in the project
        can write that config. git_status turns it off."""
        git = handoff.git_program()
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            subprocess.run([git, "init", "-q", repo], check=True, capture_output=True)
            marker = os.path.join(tmp, "fsmonitor-ran.txt")
            command = os.path.join(tmp, "fsmonitor-command")
            with open(command, "w", newline="\n") as fh:
                fh.write("#!/bin/sh\necho ran > '%s'\nexit 1\n" % marker.replace("\\", "/"))
            os.chmod(command, 0o755)
            subprocess.run([git, "-C", repo, "config", "core.fsmonitor", command.replace("\\", "/")], check=True,
                           capture_output=True)
            with open(os.path.join(repo, "notes.txt"), "w") as fh:
                fh.write("synthetic\n")
            subprocess.run([git, "-C", repo, "status", "--porcelain=v1"], capture_output=True)
            if not os.path.exists(marker):
                self.skipTest("this git doesn't run a core.fsmonitor command")
            os.remove(marker)
            self.assertEqual(handoff.git_status(repo), ["?? notes.txt"])
            self.assertFalse(os.path.exists(marker))

    def test_any_failure_gives_no_lines(self):
        here = tempfile.gettempdir()
        for failure in (subprocess.TimeoutExpired(["git"], 3.0), FileNotFoundError("git"), OSError("denied"),
                        ValueError("bad")):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch("subprocess.run", side_effect=failure):
                    self.assertEqual(handoff.git_status(here), [])
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 128, stdout=b"fatal")):
            self.assertEqual(handoff.git_status(here), [])

    @unittest.skipIf(handoff.git_program() is None, "git is not installed")
    def test_a_real_repository(self):
        git = handoff.git_program()
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run([git, "init", "-q", repo], check=True, capture_output=True)
            for name in ("staged.txt", "café.txt", "notes.md"):
                with open(os.path.join(repo, name), "w", encoding="utf-8") as fh:
                    fh.write("synthetic\n")
            subprocess.run([git, "-C", repo, "add", "staged.txt"], check=True, capture_output=True)
            lines = handoff.git_status(repo)
            self.assertEqual(sorted(lines), ["?? café.txt", "?? notes.md", "A  staged.txt"])
            self.assertFalse(os.path.exists(os.path.join(repo, ".git", "index.lock")))

    @unittest.skipIf(handoff.git_program() is None, "git is not installed")
    def test_outside_a_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            inside = subprocess.run([handoff.git_program(), "-C", folder, "rev-parse", "--is-inside-work-tree"],
                                    capture_output=True)
            if inside.returncode == 0:
                self.skipTest("the temporary folder is inside a git repository")
            self.assertEqual(handoff.git_status(folder), [])


if __name__ == "__main__":
    unittest.main()
