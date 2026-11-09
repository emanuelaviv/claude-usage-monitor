"""The entry scripts in hooks/, run the way Codex runs them: a separate Python process with the hook's JSON on stdin,
the session's folder as its working directory, and PLUGIN_ROOT, PLUGIN_DATA and CODEX_HOME set. Whatever they are
given, each prints nothing or exactly one JSON object and exits with code 0. Also: hooks.json and plugin.json are
valid, and a prompt sent without a break is answered in under half a second, Python's start-up included."""
import importlib.util
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from advisor import VERSION, state
from tests import fixtures as fx

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS = os.path.join(PLUGIN, "hooks")
SCRIPTS = ("prompt_submit.py", "session_start.py", "refresh_rate.py")
MAIN = "0c1d2e3f-0000-4000-8000-000000000001"
NEW = "0c1d2e3f-0000-4000-8000-000000000002"
ASTRA = "gpt-6-astra"
# Made-up text in Hebrew and accented Latin, with an emoji outside the BMP and a dash: the scripts must read and
# write UTF-8 whatever the console's code page is.
PROMPT = ("המשך בבקשה עם המפענח: טפל בשדות במירכאות שנמשכים על פני כמה שורות, ושמור על הממשק הציבורי כפי שהוא. "
          "Ünïcödé café 🙂 – marker qv-5150.")
MARKER = "qv-5150"
GARBAGE = (b"", b"   \n", b"not json", b"\xff\xfe\x00\x81 garbage", b"[1, 2, 3]", b"null", b'{"prompt": 5}',
           b"[" * 100_000)
FAST_LIMIT_S = 0.5


def environment(data, home):
    """This environment without anything that would change how the scripts run, plus what Codex sets for a plugin's
    hook command."""
    drop = ("PLUGIN_ROOT", "PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA", "CODEX_HOME", "PYTHONIOENCODING",
            "PYTHONUTF8", "PYTHONPATH")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(PLUGIN_ROOT=PLUGIN, PLUGIN_DATA=data, CODEX_HOME=home)
    return env


def run(script, stdin, env, cwd, hooks=HOOKS):
    """(exit code, stdout, stderr, wall-clock seconds) of one hook script."""
    began = time.perf_counter()
    done = subprocess.run([sys.executable, os.path.join(hooks, script)], input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, cwd=cwd, timeout=120)
    return done.returncode, done.stdout, done.stderr, time.perf_counter() - began


def output(stdout):
    """None for empty stdout, else the one JSON object it holds; fails on anything else."""
    text = stdout.decode("utf-8")
    if not text.strip():
        return None
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise AssertionError("not a JSON object: %r" % text[:100])
    return obj


class EntryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.home = fx.make_home(self.tmp)
        self.data = os.path.join(self.tmp, "plugin-data")
        self.cwd = os.path.join(self.tmp, "project")
        os.makedirs(self.cwd)
        self.env = environment(self.data, self.home)

    def log(self, idle, size=0, then=None):
        """A main conversation whose last model response was `idle` seconds ago by the real clock (the scripts use
        it), with `size` bytes of tool output before its last turn. then: a prompt already logged as the new turn."""
        now = time.time()
        began = now - idle - 3 * 3600
        r = fx.Rollout(self.home, MAIN, self.cwd)
        r.meta(t=began)
        r.turn(ASTRA, 832_200, t=began)
        r.user("Build a streaming parser for the log files.")
        r.call(began + 60, 150_000, 140_000, 3_000)
        for _ in range(size // 100_000):
            r.tool_output(size=100_000)
        r.turn(ASTRA, 832_200, t=now - idle - 120)
        r.user("Now handle the quoted fields.")
        r.call(now - idle, 400_000, 390_000, 10_000)
        r.complete(now - idle + 1, "Done: quoted fields work now.")
        if then is not None:
            r.turn(ASTRA, 832_200, t=now - 1)
            r.user(then)
        return r.write()

    def payload(self, path, prompt=PROMPT):
        return json.dumps({"session_id": MAIN, "transcript_path": path, "cwd": self.cwd,
                           "hook_event_name": "UserPromptSubmit", "model": ASTRA, "turn_id": "turn-new",
                           "permission_mode": "default", "prompt": prompt}, ensure_ascii=False).encode("utf-8")

    def test_manifest_and_hooks_are_valid_json(self):
        with open(os.path.join(PLUGIN, ".codex-plugin", "plugin.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual((manifest["name"], manifest["version"], manifest["hooks"]),
                         ("switch-advisor", VERSION, "./hooks/hooks.json"))
        with open(os.path.join(PLUGIN, manifest["hooks"]), encoding="utf-8") as fh:
            hooks = json.load(fh)
        self.assertEqual(sorted(hooks["hooks"]), ["SessionStart", "UserPromptSubmit"])
        named = set()
        for groups in hooks["hooks"].values():
            for group in groups:
                for h in group["hooks"]:
                    self.assertEqual(h["type"], "command")
                    self.assertNotIn("suppressOutput", h)
                    self.assertTrue(h["command"].startswith('python3 "${PLUGIN_ROOT}/hooks/'))
                    script = h["command"].split("${PLUGIN_ROOT}/hooks/", 1)[1].rstrip('"')
                    self.assertTrue(os.path.isfile(os.path.join(HOOKS, script)), script)
                    named.add(script)
                    # one plain command, which parses in PowerShell (Codex's hook shell on Windows) and cmd.exe alike;
                    # the script's path takes backslashes, which every form of the plugin's path accepts (see
                    # test_the_windows_commands_as_codex_runs_them)
                    self.assertEqual(h["commandWindows"], 'python "${PLUGIN_ROOT}\\hooks\\' + script + '"')
        self.assertEqual(named, set(SCRIPTS))
        start = hooks["hooks"]["SessionStart"]
        # the CLI's /clear starts a new conversation with source "clear"; resume, compact and fork continue one
        self.assertEqual([g["matcher"] for g in start], ["startup|clear", "startup|resume|clear"])
        self.assertEqual(start[0]["hooks"][0]["additionalContextLimit"], 6000)
        self.assertIs(start[1]["hooks"][0]["async"], True)

    def test_a_card_for_a_non_ascii_prompt(self):
        path = self.log(idle=8 * 3600, then=PROMPT)
        code, out, err, _s = run("prompt_submit.py", self.payload(path), self.env, self.cwd)
        self.assertEqual((code, err), (0, b""))
        got = output(out)
        self.assertEqual(set(got), {"decision", "reason"})
        self.assertTrue(got["reason"].startswith("Switch advisor: you were away 8.0 h"))
        self.assertEqual(len(os.listdir(os.path.join(self.data, "pending"))), 2)
        self.assertFalse(os.path.exists(os.path.join(self.data, "errors.log")))
        for folder, _dirs, names in os.walk(self.data):
            for name in names:
                with open(os.path.join(folder, name), "rb") as fh:
                    blob = fh.read()
                self.assertNotIn(PROMPT.encode("utf-8"), blob, name)
                self.assertNotIn(MARKER.encode("utf-8"), blob, name)

    def test_the_demo_prints_ascii_json(self):
        """Only ASCII on stdout, other characters as \\u escapes: on Windows the output passes through PowerShell or
        cmd.exe, which can recode text that isn't ASCII."""
        code, out, err, _s = run("prompt_submit.py", self.payload(None, "switch-advisor demo"), self.env, self.cwd)
        self.assertEqual((code, err), (0, b""))
        reason = output(out)["reason"]
        self.assertTrue(reason.startswith("DEMO · Switch advisor: you were away 8.2 h"))
        self.assertIn("$20.89–$40.80", reason)
        out.decode("ascii")                                       # strict
        self.assertIn(b"DEMO \\u00b7 ", out)
        start = json.dumps({"session_id": NEW, "transcript_path": None, "cwd": self.cwd, "source": "startup"})
        state.put_pending(self.data, self.cwd, MAIN, "שלום – café", {}, time.time() - 60)
        code, out, err, _s = run("session_start.py", start.encode("utf-8"), self.env, self.cwd)
        self.assertEqual((code, err), (0, b""))
        out.decode("ascii")
        self.assertEqual(output(out)["hookSpecificOutput"]["additionalContext"], "שלום – café")

    def test_a_byte_order_mark_before_the_json_is_skipped(self):
        stdin = b"\xef\xbb\xbf" + self.payload(None, "switch-advisor demo")
        code, out, err, _s = run("prompt_submit.py", stdin, self.env, self.cwd)
        self.assertEqual((code, err), (0, b""))
        self.assertTrue(output(out)["reason"].startswith("DEMO · Switch advisor:"))

    def test_session_start_gives_the_handoff(self):
        text = "Notes for the new conversation: שלום, café, 🙂 and a – dash."
        state.put_pending(self.data, self.cwd, MAIN, text, {"ctx": 410_000}, time.time() - 60)
        payload = json.dumps({"session_id": NEW, "transcript_path": None, "cwd": self.cwd,
                              "hook_event_name": "SessionStart", "model": ASTRA, "permission_mode": "default",
                              "source": "startup"}).encode("utf-8")
        code, out, err, _s = run("session_start.py", payload, self.env, self.cwd)
        self.assertEqual((code, err), (0, b""))
        self.assertEqual(output(out), {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                              "additionalContext": text}})
        self.assertEqual(os.listdir(os.path.join(self.data, "pending")), [])

    def test_refresh_rate_prints_nothing(self):
        self.log(idle=3600)
        code, out, err, _s = run("refresh_rate.py", b'{"session_id": "x", "source": "startup"}', self.env, self.cwd)
        self.assertEqual((code, out, err), (0, b"", b""))
        self.assertIsNone(state.read_json(os.path.join(self.data, "rate.json"))["usd_per_pt"])   # no meter yet

    def test_garbage_in_nothing_out(self):
        for script in SCRIPTS:
            for stdin in GARBAGE:
                with self.subTest(script=script, stdin=stdin[:12]):
                    code, out, err, _s = run(script, stdin, self.env, self.cwd)
                    self.assertEqual((code, out, err), (0, b"", b""))

    def test_a_broken_package_still_lets_prompts_through(self):
        """As on a Python older than 3.10, where importing the package fails."""
        plugin = os.path.join(self.tmp, "broken-plugin")
        os.makedirs(os.path.join(plugin, "advisor"))
        shutil.copytree(HOOKS, os.path.join(plugin, "hooks"))
        with open(os.path.join(plugin, "advisor", "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write('raise TypeError("unsupported operand type(s) for |")\n')
        for script in SCRIPTS:
            with self.subTest(script=script):
                code, out, err, _s = run(script, self.payload(None, "switch-advisor demo"), self.env, self.cwd,
                                         hooks=os.path.join(plugin, "hooks"))
                self.assertEqual((code, out, err), (0, b"", b""))

    def test_the_plugin_folder_is_never_written(self):
        """The scripts add no advisor/__pycache__ to the installed copy. Codex's fingerprint of a plugin's files skips
        __pycache__ and .pyc on its main branch, so this is a precaution: the copy stays exactly as approved even if a
        build hashes them. A clean copy of the plugin gets a card (which loads the handoff module), a new conversation
        and a rate refresh, with nothing in the environment that would stop Python writing bytecode."""
        plugin = os.path.join(self.tmp, "plugins", "cache", "switch-advisor", VERSION)
        for part in ("advisor", "hooks", ".codex-plugin"):
            shutil.copytree(os.path.join(PLUGIN, part), os.path.join(plugin, part),
                            ignore=shutil.ignore_patterns("__pycache__"))
        before = sorted(os.path.relpath(os.path.join(f, n), plugin) for f, _d, ns in os.walk(plugin) for n in ns)
        env = {k: v for k, v in self.env.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
        env["PLUGIN_ROOT"] = plugin
        hooks = os.path.join(plugin, "hooks")
        code, out, _err, _s = run("prompt_submit.py", self.payload(self.log(idle=8 * 3600)), env, self.cwd, hooks)
        self.assertEqual((code, set(output(out))), (0, {"decision", "reason"}))
        start = json.dumps({"session_id": NEW, "transcript_path": None, "cwd": self.cwd, "source": "startup"})
        for script in ("session_start.py", "refresh_rate.py"):
            self.assertEqual(run(script, start.encode("utf-8"), env, self.cwd, hooks)[0], 0)
        self.assertTrue(os.path.exists(os.path.join(self.data, "rate.json")))
        after = sorted(os.path.relpath(os.path.join(f, n), plugin) for f, _d, ns in os.walk(plugin) for n in ns)
        self.assertEqual(after, before)

    def test_a_git_program_in_the_project_is_never_run(self):
        """A program named git in the project folder, the hooks' working directory (on Windows a git.exe there is
        found before PATH; elsewhere, when PATH holds "."). It must not run: its output would become the handoff's
        list of uncommitted files, and it would run with your rights."""
        env = {k: v for k, v in self.env.items() if k.upper() != "NODEFAULTCURRENTDIRECTORYINEXEPATH"}
        if os.name == "nt":
            shutil.copy(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"),
                        os.path.join(self.cwd, "git.exe"))       # it prints its banner and stops, stdin being closed
            planted = "Microsoft"
        else:
            with open(os.path.join(self.cwd, "git"), "w", newline="\n") as fh:
                fh.write("#!/bin/sh\necho ' M PLANTED-PROGRAM-OUTPUT.txt'\n")
            os.chmod(os.path.join(self.cwd, "git"), 0o755)
            env["PATH"] = "." + os.pathsep + env.get("PATH", "")
            planted = "PLANTED-PROGRAM-OUTPUT"
        code, out, _err, _s = run("prompt_submit.py", self.payload(self.log(idle=8 * 3600)), env, self.cwd)
        self.assertEqual((code, set(output(out))), (0, {"decision", "reason"}))
        with open(os.path.join(self.data, "pending", state.cwd_key(self.cwd) + ".txt"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn(planted, text)
        self.assertNotIn(planted, output(out)["reason"])

    @unittest.skipUnless(os.name == "nt", "Codex runs commandWindows on Windows only")
    def test_the_windows_commands_as_codex_runs_them(self):
        r"""Each commandWindows string, run as Codex runs it on Windows (codex-rs, hooks/src/engine/command_runner.rs
        build_command and core/src/session/mod.rs build_hooks_config): in the conversation's shell, which on Windows is
        PowerShell (pwsh, else Windows PowerShell), as `<powershell> -NoProfile -Command <command>` with the command as
        one argument; or, when the conversation has no single local environment or no PowerShell is found, as
        `"%COMSPEC%" /C "<command>"`, a raw command line. ${PLUGIN_ROOT} becomes the plugin's folder, the working
        directory is the conversation's project, and stdin carries the hook's JSON.

        With a PATH of absolute folders, PowerShell doesn't look for a program in its current folder, so a python.bat or
        python.cmd in the project (from a cloned repository, or written by an agent) doesn't run in Python's place, in
        any of these forms of the plugin's path: with a space, &, (), ^ and ', as a \\?\ path, with forward slashes, or
        over 260 characters. (A $ followed by a letter, { or (, or a backtick, in the path, PowerShell expands inside the
        quotes: the hooks then fail and let prompts through, a limit the README states.) The script's path takes
        backslashes, as a \\?\ path reads a forward slash as part of a name. cmd.exe does look in its current folder
        first, so the fallback is run here only in a project without such a program, after a control check that the
        planted ones are found that way: the case the plugin can't guard, as no command string is safe in both
        shells."""
        if os.path.basename(sys.executable).lower() != "python.exe":
            self.skipTest("this Python isn't a python.exe that `python` on PATH could find")

        def copy_plugin(dest):
            for part in ("advisor", "hooks", ".codex-plugin"):
                shutil.copytree(os.path.join(PLUGIN, part), os.path.join(dest, part),
                                ignore=shutil.ignore_patterns("__pycache__"))

        # a space, &, (), ^ and ', which cmd.exe or PowerShell read specially outside quotes, and a non-ASCII letter
        plugin = os.path.join(self.tmp, "plugins", "cache", "switch advisor & (x) ^ o'café", VERSION)
        copy_plugin(plugin)
        roots = {"the plugin's folder": plugin, "its \\\\?\\ form": "\\\\?\\" + plugin,
                 "with forward slashes": plugin.replace("\\", "/")}
        deep = os.path.join(self.tmp, "plugins", "cache", *["a folder that makes the path long"] * 8, VERSION)
        try:
            copy_plugin(deep)                   # where Windows long paths are turned on
            roots["over 260 characters"] = deep
        except OSError:
            pass
        with open(os.path.join(plugin, "hooks", "hooks.json"), encoding="utf-8") as fh:
            groups = json.load(fh)["hooks"]
        commands = {h["command"].split("${PLUGIN_ROOT}/hooks/", 1)[1].rstrip('"'): h["commandWindows"]
                    for event in groups.values() for group in event for h in group["hooks"]}
        self.assertEqual(set(commands), set(SCRIPTS))

        system = os.environ.get("SystemRoot") or r"C:\Windows"
        shells = {"Windows PowerShell": os.path.join(system, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")}
        pwsh = shutil.which("pwsh") or next((p for p in (
            r"C:\Program Files\PowerShell\7\pwsh.exe",
            os.path.expanduser(r"~\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"))
            if os.path.isfile(p)), None)
        if pwsh:
            shells["PowerShell 7"] = pwsh          # Codex's own runtime ships one
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        env = {k: v for k, v in self.env.items() if k.upper() != "NODEFAULTCURRENTDIRECTORYINEXEPATH"}
        python, rest = os.path.dirname(sys.executable), env.get("PATH", "")
        layouts = {"Python's folder first": python + ";" + rest, "an empty entry first": ";" + python + ";" + rest}

        def codex_runs(shell, script, stdin, path, root, cwd):
            command = commands[script].replace("${PLUGIN_ROOT}", root)
            if shell == "cmd.exe":
                line = '"%s" /C "%s"' % (comspec, command)
            else:                                   # Rust's Command::arg quotes as list2cmdline does
                line = subprocess.list2cmdline([shells[shell], "-NoProfile", "-Command", command])
            done = subprocess.run(line, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=dict(env, PATH=path, PLUGIN_ROOT=root), cwd=cwd, timeout=120)
            self.assertEqual((done.returncode, done.stderr), (0, b""), (shell, script))
            return done.stdout

        def prompt(cwd):
            return json.dumps({"session_id": MAIN, "transcript_path": None, "cwd": cwd,
                               "hook_event_name": "UserPromptSubmit", "model": ASTRA, "turn_id": "turn-new",
                               "permission_mode": "default", "prompt": "switch-advisor demo"}).encode("utf-8")

        def start(cwd):
            return json.dumps({"session_id": NEW, "transcript_path": None, "cwd": cwd, "hook_event_name": "SessionStart",
                               "model": ASTRA, "permission_mode": "default", "source": "startup"}).encode("utf-8")

        planted = os.path.join(self.tmp, "a cloned project")
        os.makedirs(planted)
        marker = os.path.join(self.tmp, "planted-program-ran.txt")
        for plant in ("python.bat", "python.cmd"):
            with open(os.path.join(planted, plant), "w", newline="\r\n") as fh:
                fh.write('@echo off\necho ran>"%s"\necho {"decision":"block","reason":"PLANTED"}\n' % marker)
        # control: cmd.exe, which searches its current folder first, runs the planted program for a bare `python`
        control = subprocess.run('"%s" /C "python "%s""' % (comspec, os.path.join(plugin, "hooks", "prompt_submit.py")),
                                 input=prompt(planted), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=dict(env, PATH=layouts["Python's folder first"]), cwd=planted, timeout=120)
        self.assertIn(b"PLANTED", control.stdout)
        os.remove(marker)

        # every shell with every form of the plugin's path; PowerShell in the planted project with both PATH layouts
        cases = [(shell, "Python's folder first", form) for shell in shells for form in roots]
        cases += [(shell, "an empty entry first", "the plugin's folder") for shell in shells]
        cases += [("cmd.exe", "Python's folder first", form) for form in roots]
        rate = os.path.join(self.data, "rate.json")
        for shell, layout, form in cases:
            with self.subTest(shell=shell, PATH=layout, plugin=form):
                path, root = layouts[layout], roots[form]
                cwd = self.cwd if shell == "cmd.exe" else planted
                out = codex_runs(shell, "prompt_submit.py", prompt(cwd), path, root, cwd)
                self.assertNotIn(b"PLANTED", out)
                self.assertTrue(output(out)["reason"].startswith("DEMO · Switch advisor: you were away 8.2 h"))
                if form == "the plugin's folder" and layout == "Python's folder first":
                    text = "Notes for the new conversation: שלום, café."
                    state.put_pending(self.data, cwd, MAIN, text, {}, time.time() - 60)
                    out = codex_runs(shell, "session_start.py", start(cwd), path, root, cwd)
                    self.assertEqual(output(out), {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                                          "additionalContext": text}})
                    if os.path.exists(rate):
                        os.remove(rate)
                    self.assertEqual(codex_runs(shell, "refresh_rate.py", start(cwd), path, root, cwd), b"")
                    self.assertTrue(os.path.exists(rate))          # the real refresh ran
                self.assertFalse(os.path.exists(marker))

    def test_the_fast_path_is_quick(self):
        """A prompt 10 minutes after the last response, on a log of about 5 MB: no output, in under 0.5 s of wall
        clock including Python's start-up. The median of 5 runs after one that warms the disk cache is reported."""
        path = self.log(idle=600, size=5_000_000)
        size_mb = os.path.getsize(path) / 1e6
        self.assertGreater(size_mb, 4.5)
        stdin = self.payload(path)
        run("prompt_submit.py", stdin, self.env, self.cwd)
        times = []
        for _ in range(5):
            code, out, err, seconds = run("prompt_submit.py", stdin, self.env, self.cwd)
            self.assertEqual((code, out, err), (0, b"", b""))
            times.append(seconds)
        median = statistics.median(times)
        sys.stderr.write("\n  fast path, %.1f MB log: median %.3f s (runs: %s) ... "
                         % (size_mb, median, ", ".join("%.3f" % t for t in times)))
        self.assertLess(median, FAST_LIMIT_S)
        self.assertFalse(os.path.exists(os.path.join(self.data, "errors.log")))    # it ran, it didn't fail
        self.assertEqual(os.listdir(self.data), [])                              # and it wrote nothing


class TooOldTest(unittest.TestCase):
    """On a Python older than 3.10 the prompt hook does nothing, except that the two trigger prompts are held back with
    the reason, so that trying one shows why no card comes instead of sending it to the model."""

    def main(self, prompt):
        spec = importlib.util.spec_from_file_location("prompt_submit_too_old", os.path.join(HOOKS, "prompt_submit.py"))
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        stdin = io.StringIO(json.dumps({"prompt": prompt, "session_id": MAIN, "cwd": "/project"}))
        stdout = io.StringIO()
        with mock.patch.object(sys, "version_info", (3, 9, 18, "final", 0)), mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", stdout), mock.patch.dict(sys.modules):
            sys.modules.pop("advisor.hook", None)
            script.main()
            self.assertNotIn("advisor.hook", sys.modules)         # the package, which needs 3.10, is never loaded
        return stdout.getvalue()

    def test_the_triggers_say_why_nothing_happens(self):
        for prompt in ("switch-advisor demo", "  Switch-Advisor   TEST "):
            with self.subTest(prompt=prompt):
                out = self.main(prompt)
                self.assertTrue(out.isascii())
                obj = json.loads(out)
                self.assertEqual(set(obj), {"decision", "reason"})
                self.assertEqual(obj["decision"], "block")
                self.assertTrue(obj["reason"].startswith("Switch advisor needs Python 3.10 or later, and its hooks run "
                                                         "Python 3.9 ("))

    def test_every_other_prompt_is_left_alone(self):
        for prompt in ("Please refactor the parser.", "switch-advisor", "switch-advisor demo please"):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.main(prompt), "")

    def test_the_console_code_page_doesnt_matter(self):
        """On Windows an old Python reads piped stdin in the ANSI code page; the hook reads the bytes as UTF-8, so a
        folder name in Hebrew (whose UTF-8 has bytes cp1252 doesn't define) still gets the reason."""
        spec = importlib.util.spec_from_file_location("prompt_submit_cp", os.path.join(HOOKS, "prompt_submit.py"))
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        data = json.dumps({"prompt": "switch-advisor test", "session_id": MAIN, "cwd": "C:\\אבא\\קוד"},
                          ensure_ascii=False).encode("utf-8")
        self.assertIn(b"\x90", data)                     # א is D7 90 in UTF-8, and cp1252 has no 0x90
        stdin = io.TextIOWrapper(io.BytesIO(b"\xef\xbb\xbf" + data), encoding="cp1252", errors="strict")
        stdout = io.StringIO()
        with mock.patch.object(sys, "version_info", (3, 9, 18, "final", 0)), mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", stdout):
            script.main()
        self.assertEqual(json.loads(stdout.getvalue())["decision"], "block")

    def test_the_script_is_ascii_so_any_python_can_compile_it(self):
        """A Python 2 compiles the whole file before its version check runs, and refuses non-ASCII bytes without a
        coding line; then Codex would send the trigger to the model."""
        with open(os.path.join(HOOKS, "prompt_submit.py"), "rb") as fh:
            raw = fh.read()
        self.assertEqual([b for b in raw if b > 127], [])


if __name__ == "__main__":
    unittest.main()
