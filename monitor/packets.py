"""Is the next packet a good place to start a new conversation?

A packet is a PROMPT_<stem>.md handoff file that your prompt names. Its first lines say who it is written for:
- some are addressed to "the Claude Code session that built ..." with no fallback;
- some allow "a new Claude Code session" if that session can't be reached;
- some stand on their own.
Reading only those first lines (in memory, never stored), the packet gets one of three risk classes:

- low: a different family from this conversation's work, not written for this conversation;
- medium: the header allows a new session, or the packet builds from files on disk in the same family;
- high: written for this conversation with no fallback, a correction/follow-up round, a stale file, or a re-reference.

Only low and medium packets ever get a "start a new conversation" suggestion.
"""
import os
import re
import time

from .ingest import root_of

_SELF = re.compile(r"\(the Claude Code (session|chat|conversation) that (built|wrote|ran|did)", re.I)
_FALLBACK = re.compile(r"(if you can.t reach it|otherwise|or)\W+a new Claude Code (session|chat|conversation)", re.I)
_YOUR = re.compile(r"\b(you built|you ran|you wrote|your (own )?(report|design|spec|rehearsal|driver|clone|fixes|"
                   r"feasibility note|note|reading|plan|revised design|open items|closure build report|sharpenings|"
                   r"rejection))\b", re.I)
_SKIP = {".git", "node_modules", "build", ".gradle", "dist", "venv", ".venv", "__pycache__", ".idea", "target", "out"}
STALE_S = 6 * 3600          # a file unchanged this long before the prompt is a reference, not a new packet
_index = {}                 # root dir -> {lower-case file name: path}
_index_time = {}


def family(stem):
    """The work stream a packet belongs to: its numbered stream plus the component, e.g. 'step7_web'."""
    s = re.sub(r"^claude_", "", (stem or "").lower())
    m = re.match(r"([a-z]+\d+)(_(android|backend|ios|web))?", s)
    if m:
        return m.group(1) + (m.group(2) or "")
    return s.split("_")[0]


def _repo_roots(cwd):
    """Where to look, in order: the checkout cwd is in (a worktree has its own files), then its main repository."""
    d = os.path.abspath(cwd)
    while True:
        git = os.path.join(d, ".git")
        if os.path.exists(git):
            roots = [d]
            if os.path.isfile(git):     # a worktree: its '.git' file points at the main repository
                try:
                    line = open(git, encoding="utf-8").read().strip()
                    if line.startswith("gitdir:"):
                        main = re.split(r"[\\/]\.git[\\/]", line.split(":", 1)[1].strip())[0]
                        if os.path.isdir(main):
                            roots.append(os.path.normpath(main))
                except OSError:
                    pass
            return roots
        parent = os.path.dirname(d)
        if parent == d:
            return [os.path.abspath(cwd)]
        d = parent


def _packet_index(root):
    if root not in _index or time.time() - _index_time[root] > 300:
        idx = {}
        for dp, dn, fns in os.walk(root):
            dn[:] = [x for x in dn if x not in _SKIP and not x.startswith(".")]   # skips .tmp snapshots too
            for fn in fns:
                if fn.startswith("PROMPT_") and fn.endswith(".md"):
                    idx.setdefault(fn.lower(), os.path.normpath(os.path.join(dp, fn)))
        _index[root], _index_time[root] = idx, time.time()
    return _index[root]


def find_packet(stem, cwd):
    """Path of PROMPT_<stem>.md in the checkout of cwd (or its main repository), or None."""
    if not stem or not cwd or not os.path.isdir(cwd):
        return None
    for root in _repo_roots(cwd):
        hit = _packet_index(root).get(f"prompt_{stem}.md".lower())
        if hit:
            return hit
    return None


def classify(stem, cwd, prompt_ts, earlier_stems):
    """Return (risk, reason). earlier_stems: the PROMPT stems this conversation named before."""
    earlier_stems = [s for s in earlier_stems if s]
    if stem in earlier_stems:
        return "high", "this conversation already worked on this file"
    path = find_packet(stem, cwd)
    if path is None:
        return "high", "the prompt file was not found, so it can't be checked"
    try:
        head_lines = open(path, encoding="utf-8", errors="replace").read().splitlines()[:40]
        mtime = os.path.getmtime(path)
    except OSError:
        return "high", "the prompt file could not be read"
    if prompt_ts - mtime > STALE_S:
        return "high", f"the file was last changed {int((prompt_ts - mtime) / 3600)} h earlier, so it is a reference"
    head = "\n".join(head_lines)
    first = head_lines[0] if head_lines else ""
    same_family = family(stem) in {family(s) for s in earlier_stems}
    rework = root_of(stem) != (stem or "").lower()
    if _FALLBACK.search(head):
        return "medium", "the packet allows a new session, but it has to re-read the spec first"
    if _SELF.search(first) or len(_YOUR.findall(head)) >= 2:
        return "high", "the packet is written for this conversation and builds on its work"
    if same_family and rework:
        return "high", "a correction or follow-up round of this conversation's work"
    if same_family:
        return "medium", "same work stream; it builds from files on disk, which a new session must re-read"
    return "low", "a separate piece of work that is not addressed to this conversation"
