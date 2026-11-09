"""Read Claude Code and Cowork transcripts into deduplicated API calls and human prompts (read-only).

The rules come from DESIGN.md section 4.1:
- One API call is one message.id. Its content blocks are separate records with the same usage.
  Dedupe globally and keep the copy with the most output tokens.
- Sum main, subagent and workflow files.
- Cowork transcripts sit under hidden .claude folders, so walk them with os.walk.
- Prompt text is never kept. A prompt keeps only its length and the PROMPT_*.md stem it names.
"""
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime

CODE_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")
APP_DATA = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude-Data")    # the desktop app's, on Windows


def claude_data_dirs():
    """The desktop app's per-account folders (Claude-Data/claude_<id>), most recently used first."""
    try:
        dirs = [os.path.join(APP_DATA, d) for d in os.listdir(APP_DATA) if d.startswith("claude_")]
    except OSError:
        return []
    return sorted((d for d in dirs if os.path.isdir(d)), key=os.path.getmtime, reverse=True)


COWORK_ROOTS = [os.path.join(d, "local-agent-mode-sessions") for d in claude_data_dirs()]

_SKIP_DIRS = {"outputs", "uploads", "uploads-tmp", "security", "skills-plugin", "file-history", "shell-snapshots"}
_TAGS = ("<task-notification", "<scheduled-task", "<command-", "<local-command", "<system-reminder",
         "[Request interrupted", "<local-command-caveat")
_STEM = re.compile(r"PROMPT_([A-Za-z0-9_.\-]+?)\.md", re.IGNORECASE)
# Trailing rework suffixes only: '_eN' (an errata packet's own number) stays part of the packet name.
_REWORK = re.compile(r"(_fix(es)?|_r\d+|_amend\d*|_revision\d*|_errata\d*|_repair|_followup|_carry_fixes)$",
                     re.IGNORECASE)


@dataclass
class Call:
    id: str
    surface: str        # code | cowork
    session: str
    kind: str           # main | subagent | workflow
    start: float        # epoch seconds of the response's first record
    end: float          # epoch seconds of the response's last record
    model: str
    effort: str | None
    inp: int
    out: int
    cr: int
    cw5: int
    cw1h: int
    think: int

    @property
    def prompt_tokens(self):
        """Tokens this request sent: uncached input + cache reads + cache writes."""
        return self.inp + self.cr + self.cw5 + self.cw1h

    @property
    def cw(self):
        return self.cw5 + self.cw1h


@dataclass
class Prompt:
    uuid: str
    surface: str
    session: str
    ts: float
    stem: str | None    # first PROMPT_<stem>.md the prompt names, if any
    length: int
    cwd: str | None = None


def root_of(stem):
    """The packet a handoff stem belongs to: the stem without a rework suffix."""
    if not stem:
        return None
    s = stem
    while True:
        t = _REWORK.sub("", s)
        if t == s:
            return s.lower()
        s = t


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _kind(path):
    p = path.replace("\\", "/")
    if "/subagents/workflows/" in p:
        return "workflow"
    if "/subagents/" in p:
        return "subagent"
    return "main"


def iter_files(surfaces=("code", "cowork")):
    """Yield (surface, path) for every transcript file."""
    if "code" in surfaces:
        for dp, dn, fns in os.walk(CODE_ROOT):
            for fn in fns:
                if fn.endswith(".jsonl") and fn != "journal.jsonl":
                    yield "code", os.path.join(dp, fn)
    if "cowork" in surfaces:
        marker = os.sep + ".claude" + os.sep + "projects"
        for root in COWORK_ROOTS:
            for dp, dn, fns in os.walk(root):
                dn[:] = [d for d in dn if d not in _SKIP_DIRS]
                if marker not in dp:
                    continue
                for fn in fns:
                    if fn.endswith(".jsonl"):
                        yield "cowork", os.path.join(dp, fn)


def _human_text(d):
    """The prompt text if this user record is something the person typed, else None."""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    content = (d.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    elif isinstance(content, str):
        text = content
    else:
        return None
    origin = d.get("origin")
    okind = origin.get("kind") if isinstance(origin, dict) else None
    if okind == "human":
        return None if text.lstrip().startswith("<scheduled-task") else text
    if okind is not None or d.get("promptSource") not in (None, "sdk", "typed", "queued"):
        return None
    if text.lstrip().startswith(_TAGS):
        return None
    return text


def load(surfaces=("code", "cowork"), since=0.0):
    """Return (calls, prompts, titles, stats): deduplicated calls, human prompts in main threads, session titles."""
    best = {}        # message id -> (out, fields) from the copy with the most output tokens
    timing = {}      # message id -> (min_ts, max_ts, surface, session, kind, path) from the earliest file
    per_file = {}    # (message id, path) -> [min_ts, max_ts]
    prompts, titles = [], {}
    stats = {"files": 0, "cowork_files": 0, "cowork_found": 0, "bad_lines": 0}
    for surface, path in iter_files(surfaces):
        stats["cowork_found"] += surface == "cowork"
        try:
            if os.path.getmtime(path) < since:
                continue
        except OSError:
            continue
        stats["files"] += 1
        stats["cowork_files"] += surface == "cowork"
        kind = _kind(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"assistant"' not in line and '"user"' not in line and "itle\"" not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    stats["bad_lines"] += 1     # a half-written last line
                    continue
                t = d.get("type")
                if t == "assistant":
                    m = d.get("message") or {}
                    mid, model = m.get("id"), m.get("model")
                    if not mid or not model or model == "<synthetic>" or not d.get("timestamp"):
                        continue
                    u = m.get("usage") or {}
                    cc = u.get("cache_creation") or {}
                    cw5 = cc.get("ephemeral_5m_input_tokens") or 0
                    cw1h = cc.get("ephemeral_1h_input_tokens") or 0
                    total_cw = u.get("cache_creation_input_tokens") or 0
                    if cw5 + cw1h == 0 and total_cw:   # older records lack the TTL split
                        if surface == "cowork" or kind == "main":
                            cw1h = total_cw
                        else:
                            cw5 = total_cw
                    out = u.get("output_tokens") or 0
                    ts = _ts(d["timestamp"])
                    k = (mid, path)
                    pf = per_file.get(k)
                    if pf is None:
                        per_file[k] = [ts, ts]
                    else:
                        pf[0], pf[1] = min(pf[0], ts), max(pf[1], ts)
                    tm = timing.get(mid)
                    if tm is None or ts < tm[0]:
                        timing[mid] = (ts, ts, surface, d.get("sessionId") or "", kind, path)
                    if mid not in best or out > best[mid][0]:
                        details = u.get("output_tokens_details") or {}
                        best[mid] = (out, model, d.get("effort") or d.get("perTurnEffort"),
                                     u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0,
                                     cw5, cw1h, details.get("thinking_tokens") or 0)
                elif t == "user" and kind == "main":
                    text = _human_text(d)
                    if text is None or not d.get("timestamp"):
                        continue
                    m = _STEM.search(text)
                    prompts.append(Prompt(d.get("uuid") or "", surface, d.get("sessionId") or "",
                                          _ts(d["timestamp"]), m.group(1) if m else None, len(text), d.get("cwd")))
                elif t == "custom-title" and d.get("customTitle"):
                    titles[d.get("sessionId")] = ("custom", d["customTitle"])
                elif t == "ai-title" and d.get("aiTitle"):
                    if titles.get(d.get("sessionId"), ("", ""))[0] != "custom":
                        titles[d.get("sessionId")] = ("ai", d["aiTitle"])
    calls = []
    for mid, (out, model, effort, inp, cr, cw5, cw1h, think) in best.items():
        _, _, surface, session, kind, path = timing[mid]
        start, end = per_file[(mid, path)]
        calls.append(Call(mid, surface, session, kind, start, end, model, effort, inp, out, cr, cw5, cw1h, think))
    calls.sort(key=lambda c: c.start)
    seen, uniq = set(), []
    for p in sorted(prompts, key=lambda p: p.ts):    # forks copy prompt records into later files
        if p.uuid and p.uuid in seen:
            continue
        seen.add(p.uuid)
        uniq.append(p)
    if "cowork" in surfaces and stats["cowork_found"] == 0 and not _warned:
        _warned.append(True)
        print(f"note: no Cowork transcripts under {APP_DATA}; Code only.", file=sys.stderr)
    return calls, uniq, {s: v[1] for s, v in titles.items()}, stats


_warned = []
