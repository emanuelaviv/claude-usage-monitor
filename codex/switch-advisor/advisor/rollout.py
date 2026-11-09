"""Reading Codex's local session logs ("rollouts").

Codex keeps one log per conversation thread, CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl, with
one JSON record per line, and compresses older logs to .jsonl.zst. Python 3.14 reads those with compression.zstd;
older Pythons skip them (ZSTD_AVAILABLE says which).

Lines can be megabytes long (instructions, tool output, compaction history). Codex starts every line the same way,
{"timestamp":...,"ordinal":...,"type":...,"payload":{"type":..., so the readers tell a record's kind from its first
bytes and parse only the records they need. A line in any other layout is parsed whole; a line that doesn't parse
(the last one can be half-written) is skipped. Nothing here writes to Codex's folders or prints anything, and the
readers keep no more conversation text than they return.
"""
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # older Pythons, or a build without zstd: .zst rollouts are skipped
    _zstd = None
ZSTD_AVAILABLE = _zstd is not None

_ZSTD_ERROR = getattr(_zstd, "ZstdError", None)
_READ_ERRORS = (OSError, EOFError, ValueError) + ((_ZSTD_ERROR,) if isinstance(_ZSTD_ERROR, type) else ())

TEXT_MAX = 4_000          # characters kept per text (each request, the last reply)
TAIL_BYTES = 2_000_000    # what snapshot() reads from the end of a log
RUNNING_S = 15 * 60       # a subagent that wrote a record this recently and is mid-turn counts as running
TURN_STALE_S = 24 * 3600  # a turn still open whose last record is older than this was left by a stopped app
LAST_STEP = 65_536        # last_record_time() reads a log's end in steps from this size up ...
LAST_LIMIT = 64 * 2 ** 20  # ... to this far back
ZST_LAG_S = 2 * 86400     # a compressed log this Python can't open is taken to end within this of its modification
                          # time (the longest seen is 35 h)
QUESTIONS = 3             # Facts.open_questions keeps the newest 3 questions, each title and option up to TEXT_MAX
OPTIONS = 8               # ... and up to 8 options (rule.open_questions cuts them for the handoff, and marks the cut)

# Codex's line layout: timestamp, ordinal, type, then the payload, whose own "type" (when it has one) comes first.
_HEAD = re.compile(rb'\{"timestamp":"([^"]*)",(?:"ordinal":\d+,)?"type":"(\w+)"(?:,"payload":\{"type":"(\w+)")?')
_STAMP = re.compile(rb'\{"timestamp":"([^"]*)"')
_ROLE = re.compile(rb'"role":"(\w+)"')
_ISO = re.compile(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d:?\d\d)?$", re.IGNORECASE)
_THREAD_ID = re.compile(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\.jsonl(?:\.zst)?$")
_ASKS = re.compile(r"\?\s*(\*\*)?\s*$")   # the reply's last line ends with a question, as in the Claude version
_INPUT_ITEMS = ("function_call_output", "custom_tool_call_output")
_TURN_END = ("task_complete", "turn_aborted")
_LAST = {}                # (path, size, mtime) -> the time of a .jsonl's last record, read once per process
# Codex's own text in user-role messages, by the tag it starts with (lower case): in logs from before Codex marked
# typed text (no content_item_metadata) this tells it from a prompt. Found on real logs, and in Codex's source.
_MARKUP = frozenset(("environment_context", "external_codex_apps_open_page", "in-app-browser-context",
                     "send_user_message_question_reply", "recommended_plugins", "turn_aborted", "user_instructions",
                     "instructions", "user_shell_command", "subagent_notification", "skill"))
_MARKUP_TAG = re.compile(r"<([A-Za-z_][A-Za-z0-9_-]*)[\s/>]")


# ---- places --------------------------------------------------------------------------------------------------------

def codex_home():
    """Codex's folder: $CODEX_HOME, else ~/.codex."""
    home = os.environ.get("CODEX_HOME")
    return os.path.expanduser(home) if home else os.path.join(os.path.expanduser("~"), ".codex")


def sessions_dir(home=None):
    return os.path.join(home or codex_home(), "sessions")


def log_times(home=None, since=0.0, include_zst=True):
    """(when it was last written, path) of each rollout last written at or after `since` (epoch seconds), oldest
    first. "Last written" is written(): not just the modification time, which Codex can leave behind."""
    found = []
    for folder, _dirs, names in os.walk(sessions_dir(home)):
        for name in names:
            if not name.startswith("rollout-"):
                continue
            if not (name.endswith(".jsonl") or (include_zst and name.endswith(".jsonl.zst"))):
                continue
            path = os.path.join(folder, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            t = written(path, st)
            if t >= since:
                found.append((t, path))
    found.sort()
    return found


def rollout_files(home=None, since=0.0, include_zst=True):
    """Rollout files last written at or after `since` (epoch seconds), oldest first (see log_times)."""
    return [path for _t, path in log_times(home, since, include_zst)]


def written(path, st=None):
    """When a log was last written, as far as can be told without reading all of it.

    Codex on Windows can leave a log's modification time at the moment it created the log while it goes on appending
    to it for hours, so for a .jsonl the time of its last record, read from its end, counts too. A .jsonl.zst can't be
    read from its end, but Codex compresses a log after its last record: its creation time (or, where the system keeps
    none, its last status change) bounds it. 0.0 for a file that can't be read."""
    try:
        st = os.stat(path) if st is None else st
    except OSError:
        return 0.0
    if path.endswith(".zst"):
        created = getattr(st, "st_birthtime", None)
        return max(st.st_mtime, created if isinstance(created, (int, float)) else st.st_ctime)
    key = (path, st.st_size, st.st_mtime_ns)
    if key not in _LAST:
        _LAST[key] = last_record_time(path)
    last = _LAST[key]
    return st.st_mtime if last is None else max(st.st_mtime, last)


def zst_bound(path, t):
    """The latest a compressed log this Python can't open may hold a record, given `t`, its written() time: that
    bound is only its creation, which can be days after its last record, so it is taken to end within ZST_LAG_S of its
    modification time instead (rate.py does the same). `t` when the file can't be read."""
    try:
        return min(t, os.path.getmtime(path) + ZST_LAG_S)
    except OSError:
        return t


def last_record_time(path):
    """The time of the last record in a .jsonl log whose start can be read, from the end of the file; None when its
    last 64 MB hold none or the file can't be read. A half-written last line counts when its time is complete."""
    try:
        with open(path, "rb") as fh:
            end = os.fstat(fh.fileno()).st_size
            buf, start = b"", end
            while start > 0 and end - start < LAST_LIMIT:
                step = min(start, max(LAST_STEP, len(buf)))      # twice as far back each time
                start -= step
                fh.seek(start)
                buf = fh.read(step) + buf
                t = _last_time_in(buf, start == 0)
                if t is not None:
                    return t
    except OSError:
        return None
    return None


def _last_time_in(buf, whole):
    """The time of the last line in `buf` whose start is in it (whole: buf starts the file), or None."""
    stop = len(buf)
    while True:
        nl = buf.rfind(b"\n", 0, stop)
        if nl < 0 and not whole:
            return None              # the earliest line here starts before buf
        line = buf[nl + 1:stop]
        m = _STAMP.match(line)
        t = _time(m.group(1).decode("ascii", "replace")) if m else None
        if t is None and line.strip() and len(line) <= 1_000_000:
            rec = _loads(line)          # another layout: the record's own timestamp, wherever it is
            t = _time(rec.get("timestamp")) if rec is not None else None
        if t is not None:
            return t
        if nl < 0:
            return None
        stop = nl


def find_rollout(session_id, home=None):
    """The newest log whose name ends with -<session_id>.jsonl (or .jsonl.zst), or None."""
    if not session_id:
        return None
    ends = ("-%s.jsonl" % session_id, "-%s.jsonl.zst" % session_id)
    best = None
    for folder, _dirs, names in os.walk(sessions_dir(home)):
        for name in names:
            if not name.endswith(ends):
                continue
            path = os.path.join(folder, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, path)
    return best[1] if best else None


def titles(home=None):
    """Thread id -> conversation title, from CODEX_HOME/session_index.jsonl (a later line wins)."""
    out = {}
    try:
        fh = open(os.path.join(home or codex_home(), "session_index.jsonl"), encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            rec = _loads(line)
            if rec and isinstance(rec.get("id"), str) and isinstance(rec.get("thread_name"), str):
                out[rec["id"]] = rec["thread_name"]
    return out


# ---- lines and records ---------------------------------------------------------------------------------------------

def parse_ts(s):
    """Epoch seconds from an ISO-8601 time such as "2026-11-07T02:23:31.975Z" (a time without a zone is UTC).
    Raises ValueError for anything else."""
    if not isinstance(s, str):
        raise ValueError("not an ISO-8601 time")
    text = s.strip()
    try:
        d = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in ("Z", "z") else text)
    except ValueError:
        # Python 3.10 takes only 3 or 6 digits of a second's fraction: rebuild other forms
        m = _ISO.match(text)
        if not m:
            raise ValueError("not an ISO-8601 time") from None
        day, hms, frac, zone = m.groups()
        if not zone or zone.upper() == "Z":
            zone = "+00:00"
        elif ":" not in zone:
            zone = zone[:3] + ":" + zone[3:]
        d = datetime.fromisoformat("%sT%s.%s%s" % (day, hms, (frac or "0")[:6].ljust(6, "0"), zone))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _time(s):
    try:
        return parse_ts(s)
    except ValueError:
        return None


def _open(path):
    """A binary stream of a rollout, or None (a missing file, or .zst on a Python without zstd)."""
    try:
        if path.endswith(".zst"):
            return _zstd.open(path, "rb") if ZSTD_AVAILABLE and _zstd is not None else None
        return open(path, "rb")
    except _READ_ERRORS:
        return None


def _lines(path):
    """The raw lines of a rollout, as bytes; nothing for a file that can't be read. A read error ends the file."""
    fh = _open(path)
    if fh is None:
        return
    with fh:
        try:
            for raw in fh:
                yield raw
        except _READ_ERRORS:
            return


def _loads(raw):
    """The JSON object on one line (bytes or str), or None."""
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def iter_records(path):
    """Every record of a rollout (.jsonl, or .jsonl.zst when ZSTD_AVAILABLE) as a dict; lines that don't parse are
    skipped."""
    for raw in _lines(path):
        rec = _loads(raw)
        if rec is not None:
            yield rec


def _peek(raw):
    """(timestamp text, record type, payload type, record) of one line. With Codex's layout the first three come from
    the line's start and the record is None until it is needed; any other line is parsed here. None for a line that
    isn't a JSON object."""
    m = _HEAD.match(raw)
    if m:
        return (m.group(1).decode("ascii", "replace"), m.group(2).decode("ascii"),
                (m.group(3) or b"").decode("ascii"), None)
    rec = _loads(raw)
    if rec is None:
        return None
    ptype = _dict(rec.get("payload")).get("type")
    ts = rec.get("timestamp")
    return ((ts if isinstance(ts, str) else ""), str(rec.get("type") or ""),
            (ptype if isinstance(ptype, str) else ""), rec)


def _payload(raw, rec):
    """The payload of a line whose kind is known ({} when it has none), or None when the line doesn't parse."""
    if rec is None:
        rec = _loads(raw)
        if rec is None:
            return None
    p = rec.get("payload")
    return p if isinstance(p, dict) else {}


def _role(raw, rec):
    """A message's role, read from the raw line to spare parsing it: the first "role" key after "payload", which
    Codex writes before the message's content. A record can carry other objects with role keys after its payload
    (approval reviews' metadata names the messages they looked at), so a later one is no evidence; inside a JSON
    string a quote is escaped, so text can't fake one."""
    if rec is None:
        at = raw.find(b'"payload":{')
        m = _ROLE.search(raw, at if at >= 0 else 0)
        if m:
            return m.group(1).decode("ascii")
    return (_payload(raw, rec) or {}).get("role")


def _int(v, default=0):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    try:
        return int(v)
    except (ValueError, OverflowError):
        return default


def _str(v):
    return v if isinstance(v, str) and v else None


def _dict(v):
    return v if isinstance(v, dict) else {}


def _model_of(turn_context):
    settings = _dict(_dict(turn_context.get("collaboration_mode")).get("settings"))
    return _str(turn_context.get("model")) or _str(settings.get("model"))


def _markup(msg):
    """True when a user-role message is Codex's own text: its first text starts with the AGENTS.md heading or with a
    tag in _MARKUP (by the tag only)."""
    content = msg.get("content")
    for item in content if isinstance(content, list) else []:
        text = _dict(item).get("text")
        if isinstance(text, str) and text.strip():
            text = text.lstrip()
            if text.startswith("# AGENTS.md"):
                return True
            m = _MARKUP_TAG.match(text)
            return bool(m) and m.group(1).lower() in _MARKUP
    return False


def _sent(raw, rec):
    """Whether a user-role message is a prompt someone sent: one holding a typed item (provenance "user"), or, in logs
    from before Codex marked typed text (no content_item_metadata), one that isn't Codex's own markup. Codex adds its
    environment, AGENTS.md and app pages as user messages too, also in the middle of a turn after a tool. In Codex's
    own layout a message with metadata is told from its line, unparsed: inside a JSON string a quote is escaped, so
    text can't fake the provenance."""
    if rec is None and b'"content_item_metadata":[' in raw:
        return b'"provenance":{"type":"user"' in raw
    msg = _payload(raw, rec) or {}
    meta = _dict(msg.get("internal_chat_message_metadata_passthrough")).get("content_item_metadata")
    if isinstance(meta, list):
        return any(_dict(_dict(m).get("provenance")).get("type") == "user" for m in meta)
    return not _markup(msg)


# ---- threads and calls ---------------------------------------------------------------------------------------------

@dataclass
class Call:
    """One model response, from its token_usage_record (in older logs, from a token_count event)."""
    thread: str
    kind: str                         # "main" | "subagent"
    end: float                        # the record's timestamp: when the response had finished
    start: float | None = None        # the last input-side record before it (task_started, user message, tool
                                      # output), if that came after the previous call
    model: str | None = None
    tier: str | None = None           # service_tier ("default"; "priority" (Fast) and "ultrafast" cost more)
    window: int | None = None         # model_context_window at the time
    inp: int = 0                      # input tokens, cached ones included
    cached: int = 0
    out: int = 0                      # output tokens, reasoning included
    reasoning: int = 0
    after_compaction: bool = False    # the first call after a 'compacted' record
    turn: str | None = None
    response_id: str | None = None
    after_prompt: bool = False        # a new turn or a prompt came after the previous call: someone sent one (a call
                                      # after a tool's output alone, or after text Codex added itself, such as its
                                      # <environment_context>, is the model carrying on; see _sent)


@dataclass
class Meter:
    """One reading of the plan's limits (the "codex" limit of a token_count event); percentages are 0-100."""
    t: float
    plan: str | None = None
    weekly_pct: float | None = None
    weekly_resets: float | None = None     # epoch seconds
    five_h_pct: float | None = None
    five_h_resets: float | None = None


@dataclass
class Thread:
    id: str
    kind: str                                    # "main" | "subagent"
    parent: str | None = None                    # a subagent's parent thread
    cwd: str | None = None
    originator: str | None = None                # e.g. "Codex Desktop"
    branch: str | None = None
    started: float | None = None
    file: str = ""
    calls: list[Call] = field(default_factory=list)
    compactions: list[tuple[float, int]] = field(default_factory=list)   # (timestamp, input tokens of the last call
                                                                          # before it); one with no call before it in
                                                                          # this log isn't listed
    meters: list[Meter] = field(default_factory=list)


def _new_stats():
    return {"files": 0, "zst_skipped": 0, "bad_lines": 0, "fallback_calls": 0}


def _meter(t, rate_limits):
    """The plan's weekly and 5-hour readings from a token_count event's rate_limits, or None. Other limit ids are
    other products' limits; the weekly window (10,080 minutes) can be the primary or the secondary one."""
    rl = rate_limits
    if t is None or not isinstance(rl, dict) or rl.get("limit_id") not in (None, "codex"):
        return None
    weekly = five_h = None
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not isinstance(w, dict) or _int(w.get("used_percent"), None) is None:
            continue
        if w.get("window_minutes") == 10080:
            weekly = w
        elif w.get("window_minutes") == 300:
            five_h = w
    if weekly is None and five_h is None:
        return None
    return Meter(t=t, plan=_str(rl.get("plan_type")),
                 weekly_pct=_pct(weekly), weekly_resets=_resets(weekly),
                 five_h_pct=_pct(five_h), five_h_resets=_resets(five_h))


def _pct(w):
    return float(w["used_percent"]) if w else None


def _resets(w):
    return float(w["resets_at"]) if w and _int(w.get("resets_at"), None) is not None else None


def _thread_from_meta(meta, ts, path):
    source = meta.get("source")
    sub = isinstance(source, dict) and "subagent" in source
    spawn = _dict(_dict(source.get("subagent")).get("thread_spawn")) if sub else {}
    named = _THREAD_ID.search(os.path.basename(path))
    return Thread(
        id=_str(meta.get("id")) or (named.group(1) if named else os.path.basename(path)),
        kind="subagent" if sub else "main",
        parent=_str(meta.get("parent_thread_id")) or _str(spawn.get("parent_thread_id")),
        cwd=_str(meta.get("cwd")), originator=_str(meta.get("originator")),
        branch=_str(_dict(meta.get("git")).get("branch")),
        started=_time(meta.get("timestamp")) or _time(ts), file=path)


class _Settings:
    """What is in effect at the current line: model, tier, window, turn and the last input-side record."""

    def __init__(self):
        self.model = self.tier = self.window = self.turn = None
        self.input_at = -1          # line number of the last input-side record
        self.input_ts = None
        self.prompt_at = -1         # ... of the last task_started, user_message event or prompt (_sent)


class _Calls:
    """Calls from one source as they are read, with what depends on the call before each."""

    def __init__(self, thread):
        self.thread = thread
        self.calls = []
        self.compactions = []
        self.at = -1                # line number of the last call
        self.compacted = False      # a compaction since the last call

    def add(self, at, end, usage, now, turn, response_id):
        usage = _dict(usage)
        self.calls.append(Call(
            thread=self.thread.id, kind=self.thread.kind, end=end,
            start=_time(now.input_ts) if now.input_at > self.at else None,
            model=now.model, tier=now.tier, window=now.window,
            inp=_int(usage.get("input_tokens")), cached=_int(usage.get("cached_input_tokens")),
            out=_int(usage.get("output_tokens")), reasoning=_int(usage.get("reasoning_output_tokens")),
            after_compaction=self.compacted, turn=turn or now.turn, response_id=response_id,
            after_prompt=now.prompt_at > self.at))
        self.at = at
        self.compacted = False

    def compaction(self, t):
        if self.calls and t is not None:
            self.compactions.append((t, self.calls[-1].inp))
        self.compacted = True


def read_thread(path):
    """The thread in one rollout, or None when the file can't be read or doesn't start with session_meta."""
    return _read_thread(path, _new_stats())


def _read_thread(path, stats):
    th = None
    now = _Settings()
    records = fallback = None
    saw_records = False          # any token_usage_record: then token_count events are not calls
    prev_total = None
    seen = set()
    for at, raw in enumerate(_lines(path)):
        head = _peek(raw)
        if head is None:
            if raw.strip():
                stats["bad_lines"] += 1
            continue
        ts, rtype, ptype, rec = head
        if th is None:
            meta = _payload(raw, rec) if rtype == "session_meta" else None
            if meta is None:
                if rtype == "session_meta":
                    stats["bad_lines"] += 1
                return None
            th = _thread_from_meta(meta, ts, path)
            records, fallback = _Calls(th), _Calls(th)
            continue
        if rtype == "token_usage_record":
            saw_records = True
            p = _payload(raw, rec)
            if p is None:
                stats["bad_lines"] += 1
                continue
            if _str(p.get("thread_id")) not in (None, th.id):
                continue         # copied from another thread's history: counted in that thread's own log
            rid = _str(p.get("response_id"))
            end = _time(ts)
            if end is None or (rid is not None and rid in seen):
                continue
            if rid is not None:
                seen.add(rid)
            records.add(at, end, p.get("usage"), now, _str(p.get("turn_id")), rid)
        elif rtype == "event_msg" and ptype == "token_count":
            p = _payload(raw, rec)
            if p is None:
                stats["bad_lines"] += 1
                continue
            t = _time(ts)
            info = p.get("info")
            if isinstance(info, dict):
                now.window = _int(info.get("model_context_window"), None) or now.window
                total = info.get("total_token_usage")
                if total != prev_total:             # older logs repeat the same totals: not a new call
                    prev_total = total
                    if t is not None and isinstance(info.get("last_token_usage"), dict):
                        fallback.add(at, t, info["last_token_usage"], now, None, None)
            meter = _meter(t, p.get("rate_limits"))
            if meter is not None:
                th.meters.append(meter)
        elif rtype == "event_msg" and ptype in ("task_started", "thread_settings_applied", "user_message"):
            if ptype == "user_message":
                now.input_at, now.input_ts, now.prompt_at = at, ts, at
                continue
            p = _payload(raw, rec)
            if p is None:
                stats["bad_lines"] += 1
                continue
            if ptype == "task_started":
                now.window = _int(p.get("model_context_window"), None) or now.window
                now.turn = _str(p.get("turn_id")) or now.turn
                now.input_at, now.input_ts, now.prompt_at = at, ts, at
            else:
                s = _dict(p.get("thread_settings"))
                now.model = _str(s.get("model")) or now.model
                now.tier = _str(s.get("service_tier")) or now.tier
        elif rtype == "turn_context":
            p = _payload(raw, rec)
            if p is None:
                stats["bad_lines"] += 1
                continue
            now.model = _model_of(p) or now.model
            now.turn = _str(p.get("turn_id")) or now.turn
        elif rtype == "compacted":
            t = _time(ts)
            records.compaction(t)
            fallback.compaction(t)
        elif rtype == "response_item":
            if ptype in _INPUT_ITEMS:
                now.input_at, now.input_ts = at, ts
            elif ptype == "message":
                role = _role(raw, rec)
                if role != "assistant":
                    now.input_at, now.input_ts = at, ts
                    if role == "user" and _sent(raw, rec):
                        now.prompt_at = at
    if th is None:
        return None
    chosen = records if saw_records else fallback
    if not saw_records:
        stats["fallback_calls"] += len(fallback.calls)
    th.calls, th.compactions = chosen.calls, chosen.compactions
    return th


def load(home=None, since=0.0):
    """All threads from the rollouts last written since `since` (see written()), by thread id, and reading stats.
    Callers that want only what happened since then filter the calls by their own times.

    Calls are deduplicated by response_id across files (a fork can copy history). A thread kept in two logs (a
    compressed one and a newer one) is merged, and its `file` is the log holding its latest call. Stats: "files" read,
    "zst_skipped" (compressed logs this Python can't read that may hold records since `since`: those whose zst_bound
    is), "bad_lines" (lines that didn't parse, among those read), "fallback_calls" (calls taken from token_count events
    of older logs without token_usage_record)."""
    stats = _new_stats()
    threads = {}
    latest = {}                 # thread id -> (its latest call in a log, else that log's start; the log)
    seen = set()
    for t, path in log_times(home, since):
        if path.endswith(".zst") and not ZSTD_AVAILABLE:
            if zst_bound(path, t) >= since:
                stats["zst_skipped"] += 1
            continue
        stats["files"] += 1
        th = _read_thread(path, stats)
        if th is None:
            continue
        mark = max((c.end for c in th.calls), default=th.started or 0.0)
        if th.id not in latest or mark >= latest[th.id][0]:
            latest[th.id] = (mark, th.file)
        calls = []
        for c in th.calls:
            if c.response_id is not None:
                if c.response_id in seen:
                    continue
                seen.add(c.response_id)
            calls.append(c)
        th.calls = calls
        old = threads.get(th.id)
        if old is None:
            threads[th.id] = th
            continue
        # the same thread in two logs (e.g. a compressed one and a newer one): merge them
        old.calls = sorted(old.calls + th.calls, key=lambda c: c.end)
        old.compactions = sorted(old.compactions + th.compactions)
        old.meters = sorted(old.meters + th.meters, key=lambda m: m.t)
        starts = [t for t in (old.started, th.started) if t is not None]
        old.started = min(starts) if starts else None
    for tid, (_mark, path) in latest.items():
        threads[tid].file = path
    return threads, stats


# ---- the prompt hook's quick read ----------------------------------------------------------------------------------

@dataclass
class Snapshot:
    """What the prompt hook needs, read from the end of a log."""
    thread: str | None = None
    kind: str = "main"
    cwd: str | None = None
    model: str | None = None
    tier: str | None = None
    window: int | None = None
    last_call_end: float | None = None      # timestamp of the last token_usage_record (or, in older logs, the last
                                            # new token_count): the last model response, not the last record
    ctx: int = 0                            # what the next request resends: the last call's input + output
    last_input: int = 0
    last_cached: int = 0
    compacted_since_last_call: bool = False
    meter: Meter | None = None              # the latest reading of the plan's limits
    calls_in_tail: int = 0                  # calls in the part of the log that was read
    turn_running: bool = False              # the latest turn has made a model call and hasn't ended (no task_complete
                                            # or turn_aborted after its task_started), and its own last record is
                                            # under TURN_STALE_S old: Codex calls the model again when its tool
                                            # returns, whatever is sent now
    turn_id: str | None = None              # the latest task_started's turn id ...
    trigger: str | None = None              # ... and what started that turn (turn_attribution.turn_trigger:
                                            # "composer" in the desktop app, "remote_android" from the ChatGPT
                                            # Android app); Codex writes it before the prompt hook runs


class _Tail:
    """The snapshot's fields, gathered from consecutive lines (in order) of one log."""

    def __init__(self):
        self.n = 0
        self.model = self.tier = self.window = self.meter = None
        self.settings = False         # a thread_settings_applied was seen, so the tier is known
        self.call = None              # (line, end, usage) of the latest token_usage_record
        self.calls = 0
        self.fallback = None          # the same from token_count events, for logs without token_usage_record
        self.fallback_calls = 0
        self.prev_total = None
        self.compacted_at = -1
        self.started_at = -1          # line of the latest task_started
        self.turn_id = self.trigger = None    # ... its turn id and turn_attribution.turn_trigger
        self.ended_at = -1            # ... of the latest task_complete or turn_aborted
        self.turn_ts = None           # the timestamp of the last record other than thread_settings_applied, which
                                      # Codex also writes outside turns: when a conversation is opened again, and
                                      # just before a turn starts (so as the prompt hook runs)

    def feed(self, raw):
        self.n += 1
        head = _peek(raw)
        if head is None:
            return
        ts, rtype, ptype, rec = head
        if ts and not (rtype == "event_msg" and ptype == "thread_settings_applied"):
            self.turn_ts = ts
        if rtype == "compacted":
            self.compacted_at = self.n
            return
        if rtype not in ("token_usage_record", "turn_context", "event_msg"):
            return
        if rtype == "event_msg" and ptype not in ("token_count", "thread_settings_applied", "task_started"):
            if ptype in _TURN_END:
                self.ended_at = self.n
            return
        p = _payload(raw, rec)
        if p is None:
            return
        if rtype == "token_usage_record":
            end = _time(ts)
            if end is not None:
                self.call = (self.n, end, _dict(p.get("usage")))
                self.calls += 1
        elif rtype == "turn_context":
            self.model = _model_of(p) or self.model
        elif ptype == "thread_settings_applied":
            s = _dict(p.get("thread_settings"))
            self.model = _str(s.get("model")) or self.model
            self.tier = _str(s.get("service_tier")) or self.tier
            self.settings = True
        elif ptype == "task_started":
            self.window = _int(p.get("model_context_window"), None) or self.window
            self.started_at = self.n
            self.turn_id = _str(p.get("turn_id"))
            self.trigger = _str(_dict(p.get("turn_attribution")).get("turn_trigger"))
        else:  # token_count
            t = _time(ts)
            info = p.get("info")
            if isinstance(info, dict):
                self.window = _int(info.get("model_context_window"), None) or self.window
                total = info.get("total_token_usage")
                if total != self.prev_total:
                    self.prev_total = total
                    if t is not None and isinstance(info.get("last_token_usage"), dict):
                        self.fallback = (self.n, t, info["last_token_usage"])
                        self.fallback_calls += 1
            meter = _meter(t, p.get("rate_limits"))
            if meter is not None:
                self.meter = meter

    def incomplete(self):
        """True when the part read doesn't settle the snapshot, so the whole log has to be read."""
        return self.call is None or self.model is None or self.window is None or not self.settings

    def into(self, snap, now, whole):
        """Fills `snap`; `whole`: the lines fed were the whole log. A turn is still running when its task_started is
        the latest turn event and a call came after it; or, in a tail cut from a longer log, when the tail holds calls
        but no turn event at all (one long turn still going). Either only while the turn's own last record (turn_ts:
        a thread_settings_applied written since, when the app opened the conversation again, doesn't count) is under
        TURN_STALE_S old: a stopped app leaves its turn open."""
        last, snap.calls_in_tail = (self.call, self.calls) if self.call else (self.fallback, self.fallback_calls)
        snap.model, snap.tier, snap.window, snap.meter = self.model, self.tier, self.window, self.meter
        snap.turn_id, snap.trigger = self.turn_id, self.trigger
        if last is not None:
            at, end, usage = last
            snap.last_call_end = end
            snap.last_input = _int(usage.get("input_tokens"))
            snap.last_cached = _int(usage.get("cached_input_tokens"))
            snap.ctx = snap.last_input + _int(usage.get("output_tokens"))
            snap.compacted_since_last_call = self.compacted_at > at
            if self.started_at > self.ended_at:
                running = at > self.started_at
            else:
                running = self.started_at == self.ended_at == -1 and not whole
            t = _time(self.turn_ts) if running else None
            snap.turn_running = t is not None and now - t <= TURN_STALE_S
        return snap


def _meta_into(snap, first):
    rec = _loads(first) if first else None
    if rec is None or rec.get("type") != "session_meta":
        return
    meta = _dict(rec.get("payload"))
    source = meta.get("source")
    snap.thread = _str(meta.get("id"))
    snap.kind = "subagent" if isinstance(source, dict) and "subagent" in source else "main"
    snap.cwd = _str(meta.get("cwd"))


def _read_end(path, tail_bytes):
    """(first line, the complete lines of the last `tail_bytes`, whether those cover the whole log), or None when the
    file can't be read. A .zst log can't be read from its end: its lines come as a stream, all of them."""
    if path.endswith(".zst"):
        lines = _lines(path)
        first = next(lines, None)
        return (first, lines, True) if first is not None else None
    try:
        with open(path, "rb") as fh:
            first = fh.readline()
            size = os.fstat(fh.fileno()).st_size
            start = max(len(first), size - max(int(tail_bytes), 0))
            fh.seek(start)
            buf = fh.read()
    except OSError:
        return None
    whole = start <= len(first)
    if not whole:
        cut = buf.find(b"\n")     # the tail starts inside a line: drop that partial line
        buf = buf[cut + 1:] if cut >= 0 else b""
    return first, buf.split(b"\n"), whole


def snapshot(path, tail_bytes=TAIL_BYTES, now=None):
    """The prompt hook's view of a log, from its first line (thread id, kind, cwd) and its last `tail_bytes`.

    The idle time must be measured from the last model response: when the hook runs, the new turn's records
    (task_started, turn_context, world_state, the user message) may already be written, so last_call_end comes from
    the last token_usage_record, never from the last record. Model, tier and window come from the latest
    turn_context / thread_settings_applied / task_started (or token_count) in the tail; when the tail lacks any of
    them or holds no call, the whole log is read instead. turn_running is judged at `now` (epoch seconds, default the
    clock). An unreadable file gives an empty Snapshot."""
    snap = Snapshot()
    end = _read_end(path, tail_bytes)
    if end is None:
        return snap
    first, lines, whole = end
    _meta_into(snap, first)
    tail = _Tail()
    for raw in lines:
        tail.feed(raw)
    if not whole and tail.incomplete():
        tail, whole = _Tail(), True
        rest = _lines(path)
        next(rest, None)          # the first line is session_meta
        for raw in rest:
            tail.feed(raw)
    return tail.into(snap, time.time() if now is None else now, whole)


def latest_meter(home=None, logs=8, tail_bytes=262_144):
    """The newest reading of the plan's weekly meter in the logs written to most recently (.jsonl only; see
    written()). After a break, another conversation usually has a newer reading than this one's last, so "you're at
    N%" uses this. It reads only the ends of logs; None when none of these logs has a reading."""
    best = None
    paths = rollout_files(home, include_zst=False)
    for path in (paths[-int(logs):] if logs > 0 else []):
        end = _read_end(path, tail_bytes)
        if end is None:
            continue
        tail = _Tail()
        for raw in end[1]:
            tail.feed(raw)
        m = tail.meter
        if m is not None and m.weekly_pct is not None and (best is None or m.t > best.t):
            best = m
    return best


# ---- facts for the handoff and the quality flags -------------------------------------------------------------------

@dataclass
class Facts:
    """What a handoff carries and what the quality flags look at. Texts are cut to TEXT_MAX characters."""
    thread: str | None = None
    cwd: str | None = None
    branch: str | None = None
    first_request: str | None = None        # the first text the person typed in this thread (provenance "user")
    requests: list[str] = field(default_factory=list)   # every typed request in order; a compaction's retained
                                                        # messages fill in those no longer in the log
    last_reply: str | None = None           # the last task_complete.last_agent_message, else the last assistant
                                            # message (also when that came after the last task_complete)
    asks_you: bool = False                  # the last reply ends with a question, or a request_user_input call has no
                                            # answer yet
    agents_spawned: int = 0                 # spawn_agent calls in this thread
    agents_running: int = 0                 # this thread's subagents that are mid-turn and wrote a record in the last
                                            # 15 min
    last_reply_len: int = 0                 # characters of the whole last reply (last_reply keeps TEXT_MAX)
    open_questions: list = field(default_factory=list)  # the questions of the request_user_input calls still
                                            # waiting for an answer (see asks_you), oldest first, the newest
                                            # QUESTIONS at most: [{"title": str, "options": [str, ...]}, ...], each
                                            # text up to TEXT_MAX characters (rule.open_questions cuts them shorter)
    reply_asks: bool | None = None          # the whole last reply ends with a question (asks_you without the
                                            # request_user_input calls); None without a reply, and in Facts made by
                                            # hand (rule.quality_flags then goes by asks_you)
    open_question_count: int = 0            # how many questions are still waiting for an answer: more than
                                            # len(open_questions) when older ones were left out
    unread_calls: int | None = None         # request_user_input calls still waiting whose questions the plugin
                                            # can't read (JSON in a shape it doesn't know); None in Facts made by hand


def _typed_text(msg):
    """What the person typed in a user message: its content items whose provenance is "user". Codex adds AGENTS.md,
    the environment and app pages to user messages as other items ("agents_md", "harness", ...); those are not the
    person's request. Codex splits a typed text into items line by line, so they are joined as they are."""
    content = msg.get("content")
    meta = _dict(msg.get("internal_chat_message_metadata_passthrough")).get("content_item_metadata")
    if not isinstance(content, list) or not isinstance(meta, list):
        return ""
    parts = []
    for item, m in zip(content, meta):
        provenance = _dict(_dict(m).get("provenance"))
        if provenance.get("type") == "user" and isinstance(_dict(item).get("text"), str):
            parts.append(item["text"])
    return "".join(parts).strip()


def _assistant_text(msg):
    content = msg.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(x["text"] for x in content if isinstance(x, dict) and isinstance(x.get("text"), str)).strip()


def _fill_in(requests, retained):
    """Add a compaction's retained user messages that are not in the log as messages any more. A retained text matches
    a listed request when it is the same text, or when it is marked incomplete and the request starts with it; each
    listed request matches once, so a message typed twice stays twice."""
    items = [u for u in retained if isinstance(u, dict) and isinstance(u.get("text"), str)]
    items.sort(key=lambda u: _int(u.get("order"), 0))
    unmatched = Counter(requests)
    for u in items:
        text = u["text"].strip()[:TEXT_MAX]
        if not text:
            continue
        if unmatched[text] > 0:
            unmatched[text] -= 1
            continue
        if u.get("complete") is False:
            hit = next((r for r, n in unmatched.items() if n > 0 and r.startswith(text)), None)
            if hit is not None:
                unmatched[hit] -= 1
                continue
        requests.append(text)


def _first_payload(path):
    for raw in _lines(path):
        return _payload(raw, None)
    return None


def _turn_open(path):
    """True when a log's last turn has started and not ended (no task_complete or turn_aborted after its
    task_started). A tail with no turn event at all, cut from a longer log, is one long turn still going."""
    end = _read_end(path, TAIL_BYTES)
    if end is None:
        return False
    _first, lines, whole = end
    last = None
    for raw in lines:
        head = _peek(raw)
        if head is not None and head[1] == "event_msg" and head[2] in ("task_started", "task_complete", "turn_aborted"):
            last = head[2]
    return last == "task_started" or (last is None and not whole)


def _home_of(path):
    """The CODEX_HOME a log lives in (<home>/sessions/YYYY/MM/DD/<log>), else None."""
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(3):
        d = os.path.dirname(d)
    return os.path.dirname(d) if os.path.basename(d) == "sessions" else None


def _agents_running(thread_id, home, now):
    """This thread's subagents that wrote a record in the last 15 minutes (written(): a log's modification time can
    stay at its creation while an agent works) and whose last turn hasn't ended. Only logs that recent can count, so
    only their first line (session_meta, naming the parent) is read before the tail. A compressed log is finished."""
    if not thread_id:
        return 0
    running = 0
    for t, path in log_times(home, since=now - RUNNING_S, include_zst=False):
        if now - t >= RUNNING_S:
            continue
        meta = _first_payload(path) or {}
        spawn = _dict(_dict(_dict(meta.get("source")).get("subagent")).get("thread_spawn"))
        if thread_id in (meta.get("parent_thread_id"), spawn.get("parent_thread_id")) and _turn_open(path):
            running += 1
    return running


def _reply(at, text):
    """(line, the text cut to TEXT_MAX, its whole length, whether it ends with a question) for one reply."""
    text = text.strip() if isinstance(text, str) else ""
    return at, text[:TEXT_MAX], len(text), bool(_ASKS.search(text))


_TITLE_KEYS = ("title", "question", "text", "prompt", "header")
_LABEL_KEYS = ("label", "title", "text", "value", "name")


def _first_text(d, keys, limit):
    """The first non-empty text among d's `keys`, stripped and cut to `limit` characters, else ""."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:limit]
    return ""


def _questions(arguments):
    """The call's last QUESTIONS questions (_all_questions); [] for arguments that aren't JSON."""
    return (_all_questions(arguments) or [])[-QUESTIONS:]


def _refused(output):
    """Whether the immediate output of a request_user_input_async call says Codex refused it. Codex answers a call it
    accepted with {"accepted": true}, and one it refused (arguments it can't parse or that miss a field, no question,
    an empty title, no option) with an error message, so the question panel showed nothing. A JSON object counts as
    accepted unless it says otherwise ("accepted": false, or an "error"); an output of another shape (none, a list of
    content items) keeps the call asked."""
    if isinstance(output, list):
        output = "".join(i.get("text", "") for i in output if isinstance(i, dict) and isinstance(i.get("text"), str))
    if not isinstance(output, str):
        return False
    try:
        value = json.loads(output)
    except (ValueError, RecursionError):
        return True                     # plain text: Codex's error message
    return not (isinstance(value, dict) and value.get("accepted") is not False and "error" not in value)


def _all_questions(arguments):
    """The questions of a request_user_input call, from its arguments (JSON text, as Codex logs it; on the real logs
    {"questions": [{"title": str, "options": [str, ...]}]}): [{"title", "options"}, ...], each with up to OPTIONS
    options, every text cut to TEXT_MAX only: rule.open_questions cuts a title or an option shorter for the handoff,
    and can mark the cut only when it sees the whole. An option may also be an object with a label-like key; a
    question with neither a title nor an option is left out, and JSON in another shape gives none. None when the
    arguments aren't JSON at all: Codex then answers the call with an error and shows nothing. Never raises."""
    try:
        args = json.loads(arguments) if isinstance(arguments, (str, bytes)) else arguments
    except (ValueError, RecursionError):
        return None
    found = _dict(args).get("questions")
    out = []
    for q in found if isinstance(found, list) else []:
        if not isinstance(q, dict):
            continue
        options = []
        listed = q.get("options")
        for o in listed if isinstance(listed, list) else []:
            text = (o.strip()[:TEXT_MAX] if isinstance(o, str) else
                    _first_text(o, _LABEL_KEYS, TEXT_MAX) if isinstance(o, dict) else "")
            if text:
                options.append(text)
            if len(options) == OPTIONS:
                break
        title = _first_text(q, _TITLE_KEYS, TEXT_MAX)
        if title or options:
            out.append({"title": title, "options": options})
    return out


def facts(path, home=None, now=None):
    """Facts about one log, read whole (only the records it needs are parsed). `home` is the CODEX_HOME searched
    for running subagents (by default the one the log lives in, else codex_home()); `now` (epoch seconds) is when.
    An unreadable file gives empty Facts.

    A question asked with request_user_input is answered by the call's output. One asked with
    request_user_input_async gets an output at once, {"accepted": true}, or Codex's error message when it refused the
    call, which then asks nothing (_refused); a call whose arguments aren't JSON at all asks nothing either. The answer
    comes later as a user message naming the call, and the question counts as answered once a model response follows
    such a message (or a typed one: the person answered there or moved on). Either kind is also no longer asked once a later turn has
    started and got a model response: the person moved on, whether or not the log marks typed text (Codex didn't
    before late October 2026). Until then it is still asking, also when the prompt hook runs and Codex has already
    logged the prompt being sent, which may be that answer: that turn has no response yet."""
    f = Facts()
    now = time.time() if now is None else now
    if home is None:
        home = _home_of(path) or codex_home()
    asked = {}                      # call id -> (its last QUESTIONS questions, how many it asked), of the calls
                                    # below, in the order asked
    asking = set()                  # call ids of request_user_input calls without an output yet
    waiting = set()                 # ... of request_user_input_async calls not answered yet
    heard = set()                   # ... of those a user message has answered since the last model response
    passed = set()                  # ... of either kind asked before a turn that has no model response yet
    completed = assistant = None    # _reply() of the last task_complete message and of the last assistant message
    for at, raw in enumerate(_lines(path)):
        head = _peek(raw)
        if head is None:
            continue
        _ts, rtype, ptype, rec = head
        if at == 0 and rtype == "session_meta":
            meta = _payload(raw, rec) or {}
            f.thread, f.cwd = _str(meta.get("id")), _str(meta.get("cwd"))
            f.branch = _str(_dict(meta.get("git")).get("branch"))
        elif rtype == "response_item" and ptype == "message":
            role = _role(raw, rec)
            if role == "user":
                text = _typed_text(_payload(raw, rec) or {})
                if text:
                    f.requests.append(text[:TEXT_MAX])
                if waiting:
                    heard.update(waiting if text else (c for c in waiting if c.encode("utf-8", "replace") in raw))
            elif role == "assistant":
                text = _assistant_text(_payload(raw, rec) or {})
                if text:
                    assistant = _reply(at, text)
        elif rtype == "token_usage_record":
            done = heard | passed           # the model has seen the answers, or a later turn moved past them
            if done:
                waiting -= done
                asking -= done
                heard.clear()
                passed.clear()
                for call_id in done:
                    asked.pop(call_id, None)
        elif rtype == "event_msg" and ptype == "task_started":
            passed |= waiting | asking
        elif rtype == "response_item" and ptype == "function_call":
            p = _payload(raw, rec) or {}
            name = p.get("name") if isinstance(p.get("name"), str) else ""
            if name == "spawn_agent":
                f.agents_spawned += 1
            elif name.startswith("request_user_input") and _str(p.get("call_id")):
                call_id = p["call_id"]
                qs = _all_questions(p.get("arguments"))
                asked.pop(call_id, None)
                if qs is None:              # arguments that aren't JSON: Codex rejects the call, so nothing is asked
                    waiting.discard(call_id)
                    asking.discard(call_id)
                    continue
                (waiting if name.endswith("_async") else asking).add(call_id)
                asked[call_id] = (qs[-QUESTIONS:], len(qs))     # only the newest QUESTIONS of all the calls are kept
        elif rtype == "response_item" and ptype == "function_call_output" and (asking or waiting):
            out = _payload(raw, rec) or {}
            call_id = out.get("call_id")
            if isinstance(call_id, str) and (call_id in asking or call_id in waiting and _refused(out.get("output"))):
                # a sync call's output is its answer; an async call that Codex refused asked nothing
                asking.discard(call_id)
                waiting.discard(call_id)
                heard.discard(call_id)
                passed.discard(call_id)
                asked.pop(call_id, None)
        elif rtype == "event_msg" and ptype == "task_complete":
            completed = _reply(at, (_payload(raw, rec) or {}).get("last_agent_message"))
        elif rtype == "compacted":
            retained = _dict((_payload(raw, rec) or {}).get("retained_context")).get("user_messages")
            if isinstance(retained, list):
                _fill_in(f.requests, retained)
    # the last task_complete's message, unless it had none or an assistant message came after it (a turn that
    # was stopped or is still going)
    reply = completed
    if assistant is not None and (reply is None or not reply[1] or assistant[0] > reply[0]):
        reply = assistant
    if reply is not None and reply[1]:
        _at, f.last_reply, f.last_reply_len, f.reply_asks = reply
    f.asks_you = bool(f.reply_asks) or bool(asking) or bool(waiting)
    still = [v for call_id, v in asked.items() if call_id in asking or call_id in waiting]
    f.open_questions = [q for qs, _n in still for q in qs][-QUESTIONS:]
    f.open_question_count = sum(n for _qs, n in still)
    f.unread_calls = sum(1 for _qs, n in still if n == 0)
    f.first_request = f.requests[0] if f.requests else None
    f.agents_running = _agents_running(f.thread, home, now)
    return f
