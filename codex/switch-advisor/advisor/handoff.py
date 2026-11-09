"""The handoff: notes for a new conversation on where an earlier one stopped, built on this computer from the earlier
conversation's log (rollout.facts) and git status, with no model call.

The prompt hook keeps it in the data folder when it holds a prompt back (state.put_pending), and the SessionStart
hook gives it, as developer context, to the next new conversation started in the same project folder. It never holds
the prompt that was held back: you send that yourself, in the new conversation.
"""
import os
import re
import subprocess

from .rollout import TEXT_MAX
from .rule import open_question_total, open_questions

MAX_CHARS = 8_000        # the whole handoff (SessionStart's additionalContextLimit is 6,000 tokens)
FIRST_CHARS = 1_500      # the first request
LATEST = 3               # the latest requests ...
REQUEST_CHARS = 600      # ... each up to this
REPLY_CHARS = 1_500      # the last reply (the quality flags count on this: rule.REPLY_KEPT)
REPLY_MIN = 100          # a reply cut shorter than this to make room for open questions is left out instead
FILES = 12               # files with uncommitted changes listed (rule.FILES_LISTED)
LINE_CHARS = 200         # one git status line
PATH_CHARS = 300         # the project folder
BRANCH_CHARS = 100
CUT = " […]"             # ends a text that was cut

INTRO = ("This conversation continues work from an earlier Codex conversation in this project. These notes were "
         "built on this computer from that conversation's log, with no model call: check them against the files, and "
         "ask the user when something is unclear.")
ASKED = "Waiting for the user's answer to:"


def build(f, uncommitted, now=None, prompt=None):
    """The handoff for rollout.Facts `f` and the git status lines `uncommitted`, at most 8,000 characters; parts whose
    data is missing are left out.

    Questions the conversation is still waiting for the user to answer (rule.open_questions: the newest 3, with their
    options) follow the last reply, and are never cut: when the whole is too long, the reply is shortened first (or
    left out), then the other parts are cut from the end, and the questions follow what is left of them.

    prompt: the prompt being held back. Codex may write the new turn to the log before the hook runs, so when the log's
    latest requests are that prompt they are left out. now: not used (the text holds no time); callers may pass it."""
    parts = [INTRO]
    cwd, branch = _line(getattr(f, "cwd", None)), _line(getattr(f, "branch", None))
    if cwd:
        on = " (branch %s)" % _cut(branch, BRANCH_CHARS) if branch and branch != "HEAD" else ""
        parts.append("Project: %s%s" % (_cut(cwd, PATH_CHARS), on))
    reqs = requests(f, prompt)
    if reqs:
        parts.append("The first request (first %s characters):\n%s" % (format(FIRST_CHARS, ","),
                                                                      _cut(reqs[0], FIRST_CHARS)))
    latest = reqs[1:][-LATEST:]
    if latest:
        parts.append("The latest requests, newest last (up to %d, each up to %s characters):\n%s"
                     % (LATEST, format(REQUEST_CHARS, ","), "\n".join(_bullet(_cut(r, REQUEST_CHARS)) for r in latest)))
    reply = _text(getattr(f, "last_reply", None))
    at_reply = None
    if reply:
        at_reply = len(parts)
        parts.append(_reply(reply, REPLY_CHARS))
    asked = _asked(f)
    if asked:
        parts.append(asked)
    files = [line for line in (uncommitted or []) if isinstance(line, str) and line.strip()]
    if files:
        lines = ["- " + _clip(_clean(line.rstrip()), LINE_CHARS) for line in files[:FILES]]
        if len(files) > FILES:
            lines.append("- and %d more" % (len(files) - FILES))
        parts.append("Uncommitted changes (git status, up to %d files):\n%s" % (FILES, "\n".join(lines)))
    agents = getattr(f, "agents_spawned", 0) or 0
    if agents == 1:
        parts.append("The earlier conversation started 1 agent; its report stays in that conversation.")
    elif agents > 1:
        parts.append("The earlier conversation started %d agents; their reports stay in that conversation." % agents)
    text = "\n\n".join(parts)
    if len(text) <= MAX_CHARS:
        return text
    if not asked:
        return _cut(text, MAX_CHARS - len(CUT))
    # Too long, with open questions: the reply makes room first, ...
    if at_reply is not None:
        keep = len(_cut(reply, REPLY_CHARS)) - (len(text) - MAX_CHARS) - len(CUT)
        if keep >= REPLY_MIN:
            parts[at_reply] = _reply(reply, keep)
        else:
            del parts[at_reply]
        text = "\n\n".join(parts)
        if len(text) <= MAX_CHARS:
            return text
    # ... then the other parts are cut from the end, never the questions, which then come last
    rest = "\n\n".join(p for p in parts if p is not asked)
    return _cut(rest, MAX_CHARS - len(asked) - 2 - len(CUT)) + "\n\n" + asked


def _reply(reply, limit):
    return "Where it stopped, its last reply (first %s characters):\n%s" % (format(limit, ","), _cut(reply, limit))


def _asked(f):
    """'Waiting for the user's answer to:' and each open question, with its options on one line, then how many older
    ones are left out, and how many asked in a shape the plugin can't read (rollout.facts' unread_calls), if any; None
    without any of these."""
    items = []
    questions = open_questions(f)
    for title, options in questions:
        title = re.sub(r"\n{3,}", "\n\n", _clean(title).replace("\r\n", "\n"))
        line = "Options: " + "; ".join(_clean(o) for o in options) if options else ""
        if title and line:
            items.append(_bullet(title) + "\n  " + line)
        else:
            items.append(_bullet(title or line))
    left = open_question_total(f) - len(questions)
    if items and left > 0:
        items.append("(%s, asked before these, %s not shown.)" % (
            "1 earlier question" if left == 1 else "%d earlier questions" % left, "is" if left == 1 else "are"))
    unread = getattr(f, "unread_calls", None)
    if isinstance(unread, int) and not isinstance(unread, bool) and unread > 0:
        if unread == 1:
            what = "Another question" if items else "A question"
        else:
            what = ("%d more questions" if items else "%d questions") % unread
        items.append("(%s in the question panel couldn't be read here, so %s not shown.)" % (
            what, "it is" if unread == 1 else "they are"))
    return ASKED + "\n" + "\n".join(items) if items else None


def requests(f, prompt=None):
    """The typed requests a handoff carries, oldest first (the first is the first request): f.requests, or
    f.first_request alone, without the held-back prompt at the end. Empty: the handoff carries no request."""
    reqs = [r for r in (getattr(f, "requests", None) or []) if _text(r)]
    first = _text(getattr(f, "first_request", None))
    if not reqs and first:
        reqs = [first]
    if _text(prompt):
        while reqs and _same(reqs[-1], prompt):
            reqs.pop()
    return reqs


def _same(request, prompt):
    """Whether a logged request is this prompt: the same words, or the start of it when the log kept only the
    first TEXT_MAX characters."""
    a, b = " ".join(request.split()), " ".join(prompt.split())
    return a == b or (len(request) >= TEXT_MAX and b.startswith(a))


def _text(v):
    return v if isinstance(v, str) and v.strip() else None


def _line(v):
    """A folder or branch name on one line (line breaks become spaces; nothing else changes), or None."""
    return " ".join(v.splitlines()).strip() if _text(v) else None


def _clean(text):
    """Text that encodes as UTF-8: a lone surrogate (half of a pair, as broken JSON can hold) becomes '?'."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit].rstrip() + CUT


def _cut(text, limit):
    """A text's first `limit` characters at most, ending at a line break when one is near the end, marked when
    cut. Runs of blank lines are shortened to one."""
    t = re.sub(r"\n{3,}", "\n\n", _clean(text).replace("\r\n", "\n").strip())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    nl = cut.rfind("\n")
    if nl > limit * 0.6:
        cut = cut[:nl]
    return cut.rstrip() + CUT


def _bullet(text):
    """'- text', its later lines indented to stay inside the item."""
    return "- " + text.replace("\n", "\n  ")


def git_program(path=None):
    """git's full path, found in the absolute folders of PATH (`path`, default the PATH variable), or None.

    Codex starts a hook in the project's folder, and on Windows a program started by its bare name is looked for in
    the current folder before PATH (by CreateProcess, as subprocess starts it): a git.exe in the project (from a cloned
    repository, an archive, or written by an agent) would run with your rights. So git is never started by its bare
    name, and PATH entries that aren't absolute (".", "", "bin") are skipped. On Windows a folder must name its drive
    or server, as CreateProcess looks only for git.exe."""
    name = "git.exe" if os.name == "nt" else "git"
    for folder in (os.environ.get("PATH", "") if path is None else path).split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or not os.path.isabs(folder) or (os.name == "nt" and not os.path.splitdrive(folder)[0]):
            continue
        program = os.path.join(folder, name)
        if os.path.isfile(program) and os.access(program, os.X_OK):
            return program
    return None


def git_status(cwd, timeout=3.0):
    """The project's uncommitted changes as `git status --porcelain=v1` prints them, one "XY path" line per file.
    [] outside a repository, without git, after `timeout` seconds or on any other error; never raises.

    git is started by its full path (git_program). --no-optional-locks keeps git from refreshing the index, so the hook
    never writes to the repository or waits on a lock Codex holds; core.fsmonitor=false turns off the fsmonitor
    command a repository's own config may name, and safe.bareRepository=explicit keeps a bare repository inside the
    project from being taken for it (as Codex runs git itself); core.quotepath=off prints non-ASCII names as they are.
    The repository's config can still make status run a command: a clean or process filter (filter.<name>.clean or
    .process, mapped to files by .gitattributes) runs for a tracked file whose stat data changed, with your rights, as
    it does in Codex's own git calls. A filter that hangs is stopped by `timeout`. On Windows no console window opens."""
    try:
        if not cwd or not os.path.isdir(cwd):
            return []
        git = git_program()
        if git is None:
            return []
        done = subprocess.run(
            [git, "--no-optional-locks", "-C", cwd, "-c", "core.quotepath=off", "-c", "core.fsmonitor=false",
             "-c", "safe.bareRepository=explicit", "status", "--porcelain=v1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if done.returncode != 0:
            return []
        return [line.rstrip("\r") for line in done.stdout.decode("utf-8", "replace").split("\n") if line.strip()]
    except Exception:
        return []
