"""The plugin's data folder: what the hooks remember between prompts. Codex gives a plugin's hooks a writable folder
of their own (PLUGIN_DATA); nothing here writes anywhere else.

    sessions/<session id>.json     offered_at, numbers, cwd_key, quiet_until
    pending/<cwd key>.json         the handoff waiting for a new conversation in that project folder: from_session,
    pending/<cwd key>.txt          at, numbers; and its text, deleted when it is used, or after 30 minutes by the
                                   first hook to run then (any prompt or conversation start) or the monitor's nightly
                                   replay
    decisions/<YYYY-MM>.jsonl      one line per decision, for the monitor's replay
    rate.json                      $ per 1% of the weekly limit (hook.refresh_rate)
    params.json, guardrail.json    written by the monitor
    errors.log                     the step and exception type of a hook that failed

The pending .txt is the only file with conversation text in it (and, for the moment it is written, its temporary
copy). Everything else holds numbers, flag keys and ids; project folders are named by a hash of their path
(cwd_key), so no path is stored either. Files are replaced whole (a temporary file, then os.replace), so a hook that
is stopped halfway never leaves half a file.
"""
import hashlib
import json
import math
import os
import re
import time

from . import VERSION

HANDOFF_TTL_S = 1800             # a pending handoff is used by a new conversation started within this (rule.Params)
AHEAD_S = 60                     # a handoff made more than this ahead of the clock counts as expired (stale)
SESSION_KEEP_S = 7 * 86400       # session files untouched this long are deleted
LEFTOVER_S = 3600                # temporary files a stopped hook left behind are deleted after this

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


# ---- the folder and names ----------------------------------------------------------------------------------------

def data_dir(env=os.environ):
    """The plugin's data folder, created if needed: PLUGIN_DATA, else CLAUDE_PLUGIN_DATA. None when neither is set or
    the folder can't be created; then the hooks never hold a prompt back, since they couldn't keep the handoff."""
    path = env.get("PLUGIN_DATA") or env.get("CLAUDE_PLUGIN_DATA")
    if not path:
        return None
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    return path


def cwd_key(cwd):
    """A project folder's name in the data folder: the first 16 hex digits of the sha1 of its real path, so
    C:\\Work\\Proj, c:/work/proj/, \\\\?\\C:\\Work\\Proj, its 8.3 short name and a junction or symbolic link to it share
    one key, and the conversation that showed a card and the one started to switch find the same handoff. A network
    path (\\\\server\\share\\...) is only normalized: resolving one whose server doesn't answer takes seconds."""
    path = _plain(cwd)
    network = os.name == "nt" and path[:2] in ("\\\\", "//")
    path = os.path.normcase(os.path.abspath(path) if network else os.path.realpath(path))
    return hashlib.sha1(path.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _plain(path):
    """A path without the \\\\?\\ prefix Windows programs may add (abspath would mangle it)."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _safe(name):
    """An id as a file name: only A-Z, a-z, 0-9, _ and -, so it can't point outside its folder."""
    return _UNSAFE.sub("_", str(name))[:120] or "_"


# ---- files ---------------------------------------------------------------------------------------------------------

def read_json(path):
    """The JSON object in a file, or None (missing, unreadable, not JSON, or not an object). A file you edited by hand
    may start with a byte order mark (Windows PowerShell's Set-Content -Encoding UTF8 writes one): that is read too."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            obj = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def write_json(path, obj):
    """Writes an object as JSON, replacing the file whole. Non-finite numbers are written as null."""
    _write(path, json.dumps(_clean(obj), indent=1).encode("utf-8"))


def _write(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        _replace(tmp, path)
    except BaseException:
        _remove(tmp)
        raise


def _replace(src, dst):
    """os.replace, tried three times: on Windows it fails while another process has the target open."""
    for attempt in range(3):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 2:
                raise
            time.sleep(0.02)


def _remove(*paths):
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def _read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _clean(x):
    """A copy that json can write as strict JSON: NaN and infinity become None."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


def _entries(folder):
    try:
        return list(os.scandir(folder))
    except OSError:
        return []


def _mtime(entry):
    try:
        return entry.stat().st_mtime
    except OSError:
        return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


# ---- each conversation's state -----------------------------------------------------------------------------------

def _session_path(d, sid):
    return os.path.join(d, "sessions", _safe(sid) + ".json")


def session(d, sid):
    """What the hooks remember about one conversation: {} at first."""
    return read_json(_session_path(d, sid)) or {}


def save_session(d, sid, obj):
    write_json(_session_path(d, sid), obj)


# ---- the pending handoff -------------------------------------------------------------------------------------------

def _pending_paths(d, key):
    base = os.path.join(d, "pending", key)
    return base + ".json", base + ".txt"


def peek_pending(d, cwd):
    """The pending handoff's record for `cwd` ({"from_session", "at", "numbers", ...}, without its text), left in
    place; None when there is none."""
    return read_json(_pending_paths(d, cwd_key(cwd))[0])


def put_pending(d, cwd, from_session, text, numbers, now, test=False):
    """Keeps a handoff for the next new conversation in `cwd`, replacing any earlier one for that folder (the hook
    checks with peek_pending that it isn't another conversation's, still waiting). The text is written first, so
    whoever finds the .json finds the text too. test: made by `switch-advisor test`."""
    meta, body = _pending_paths(d, cwd_key(cwd))
    _write(body, (text or "").encode("utf-8", "replace"))
    rec = {"from_session": from_session, "at": now, "numbers": numbers}
    if test:
        rec["test"] = True
    write_json(meta, rec)


def take_pending(d, cwd):
    """The pending handoff for `cwd`, removed from the folder: {"from_session", "at", "numbers", "text", ...}, or
    None. It is first renamed to a name of this process's own, so two conversations starting at once can't both
    take it."""
    meta, body = _pending_paths(d, cwd_key(cwd))
    claim = "%s.%d.claim" % (meta, os.getpid())
    try:
        _replace(meta, claim)
    except OSError:          # no pending handoff, or another process took it
        return None
    rec = read_json(claim)
    text = _read_text(body)
    _remove(claim, body)
    if rec is None:
        return None
    rec["text"] = text
    return rec


def stale(at, now, ttl_s=HANDOFF_TTL_S):
    """Whether a handoff made at `at` (epoch seconds) is past its use at `now`: older than ttl_s, or more than a minute
    ahead of the clock. One made while the clock ran fast (a dual-boot clock offset, a clock set back since) would
    otherwise wait, and hold its project folder, for as long as the clock was off plus ttl_s."""
    return now - at > ttl_s or at - now > AHEAD_S


def drop_pending(d, cwd, from_session=None, test=None):
    """Deletes the pending handoff for `cwd`; with from_session, only if that conversation made it; with test True or
    False, only if it is (or isn't) one `switch-advisor test` made. True if one was deleted."""
    meta, body = _pending_paths(d, cwd_key(cwd))
    rec = read_json(meta)
    if rec is None or (from_session is not None and rec.get("from_session") != from_session):
        return False
    if test is not None and (rec.get("test") is True) != bool(test):
        return False
    _remove(meta, body)
    return True


# ---- the decision log ----------------------------------------------------------------------------------------------

def log(d, event, **fields):
    """Appends one decision to decisions/<YYYY-MM>.jsonl (the UTC month of its time): "t" (fields' t, else now),
    "event", "session" (None when no conversation is involved), "version", then the other fields as given. Numbers,
    flag keys and ids only, never text. Never raises: a log line is never worth a failed hook."""
    try:
        t = fields.pop("t", None)
        t = time.time() if t is None else float(t)
        rec = {"t": round(t, 3), "event": event, "session": fields.pop("session", None), "version": VERSION}
        rec.update(fields)
        line = json.dumps(_clean(rec), separators=(",", ":"))
        folder = os.path.join(d, "decisions")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, time.strftime("%Y-%m", time.gmtime(t)) + ".jsonl"), "a",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ---- files the monitor writes --------------------------------------------------------------------------------------

def load_params(d):
    """params.json, the inputs `python -m monitor.codex replay --write` measured from your logs, or None."""
    return read_json(os.path.join(d, "params.json")) if d else None


def guardrail_off(d):
    """True when the monitor's guardrail has turned the after-break card off: guardrail.json says
    {"after_break_enabled": false}, because switches went back to the old conversation too often."""
    g = read_json(os.path.join(d, "guardrail.json")) if d else None
    return g is not None and g.get("after_break_enabled") is False


# ---- clean-up ------------------------------------------------------------------------------------------------------

def prune(d, now, ttl_s=HANDOFF_TTL_S):
    """Deletes pending handoffs past their use (stale: older than ttl_s, or made more than a minute ahead of the
    clock), logging "expired": nobody used them; session files untouched for 7 days; and temporary files a stopped
    hook left behind: those beside a handoff (a .tmp may be a copy of its text) after ttl_s as well, or when written
    more than a minute ahead of the clock, the others after an hour. The hooks call it each time they run, and so does
    the monitor's replay --write; nothing deletes a handoff at the very moment its time is up."""
    leftover = min(ttl_s, LEFTOVER_S)
    for entry in _entries(os.path.join(d, "pending")):
        name, written = entry.name, _mtime(entry)
        age = now - written if written is not None else 0.0
        if name.endswith(".json"):
            rec = read_json(entry.path) or {}
            at = rec.get("at") if _num(rec.get("at")) else now - age       # made at: its "at", else when written
            if stale(at, now, ttl_s):
                key = name[:-len(".json")]
                claim = "%s.%d.claim" % (entry.path, os.getpid())
                try:
                    _replace(entry.path, claim)      # so that of two hooks pruning at once, one logs it
                except OSError:
                    continue
                _remove(claim, os.path.join(d, "pending", key + ".txt"))
                log(d, "expired", t=now, from_session=rec.get("from_session"), cwd_key=key)
        elif name.endswith(".txt"):
            if (age > ttl_s or -age > AHEAD_S) and not os.path.exists(entry.path[:-len(".txt")] + ".json"):
                _remove(entry.path)         # a text whose .json is gone
        elif name.endswith((".tmp", ".claim")) and (age > leftover or -age > AHEAD_S):
            _remove(entry.path)
    for entry in _entries(os.path.join(d, "sessions")):
        written = _mtime(entry)
        age = now - written if written is not None else 0.0
        if (entry.name.endswith(".json") and age > SESSION_KEEP_S) or (entry.name.endswith(".tmp") and age > LEFTOVER_S):
            _remove(entry.path)
