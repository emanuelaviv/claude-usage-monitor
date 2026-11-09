"""Synthetic Codex homes and rollouts for the tests. Nothing here comes from a real log: ids, paths, texts and
numbers are made up.

    from tests import fixtures as fx
    home = fx.make_home(tmp)                 # <tmp>/codex-home with an empty sessions/ folder; returns its path
    r = fx.Rollout(home, "thread-a", cwd)    # kind="main" | "subagent" (then parent=<the parent's thread id>)
    r.meta()                                 # session_meta (write() puts one first if you don't)
    r.turn("gpt-6-astra", 258_400)           # a new turn: task_started, turn_context, thread_settings_applied
    r.user("Fix the parser")                 # a typed request (provenance "user"); harness(text) for injected text
    r.call(fx.T0 + 60, 120_000, 100_000, 2_000)                # a response: token_usage_record + token_count
    r.meter(fx.T0 + 61, 42.0, resets_at=fx.T0 + 3 * fx.DAY)    # the weekly limit at 42% (token_count rate_limits)
    r.complete(fx.T0 + 62, "Done. Shall I commit?")             # the reply: assistant message + task_complete
    path = r.write()                         # sessions/2026/11/07/rollout-2026-11-07T09-00-00-thread-a.jsonl

Times are epoch seconds, written as ISO-8601 with Z. T0 is 09:00 UTC on 2026-11-07 (Rollout.start is 09:00 UTC on
the Rollout's own day). A method without a time uses the time of the record before it; the first record's is start.
Lines are compact JSON in Codex's order (timestamp, ordinal, type, payload), so the readers' fast path is the one
tested; raw(text) adds any line as is (bad JSON, a half-written line, another layout).

Rollout methods, in the order a log usually has them:
    meta(t=None, instructions=..., originator="Codex Desktop", branch="main")
    turn(model, window, tier=None, t=None)       task_started + turn_context + thread_settings_applied (service_tier
                                                 is tier, or "default")
    task_started(t)                              a new turn id; model_context_window = the last turn()'s window
    turn_context(model, t=None)  world_state(t=None)
    user(text, provenance="user", t=None)        a user-role message, one content item per line as Codex writes it
    harness(text, t=None)                        a user-role message Codex injected (provenance "harness")
    message(items, t=None)                       a user-role message from [(text, provenance), ...]
    assistant(text, t=None, phase="final_answer", encrypted=False)
    tool_output(t=None, size=1_000)              a custom_tool_call_output of `size` characters (to make logs big)
    call(t, inp, cached, out, reasoning=0, legacy=False, response_id=None) -> response_id
                                                 token_usage_record + token_count; legacy=True writes only the
                                                 token_count, as logs before token_usage_record did
    repeat(t)                                    a token_count repeating the previous totals (older logs do this)
    meter(t, weekly_pct, resets_at, plan="plus", five_h=None, limit_id="codex")
                                                 a token_count with rate_limits. five_h: None, a percentage or
                                                 (percentage, resets_at); with it the 5-hour window is primary and
                                                 the weekly one secondary, else the weekly one is primary.
                                                 weekly_pct=None leaves the weekly window out.
    complete(t, last_agent_message)              the assistant message (when a string) + task_complete
    aborted(t)                                   turn_aborted
    compacted(t, retained_user_messages)         a compaction; items are texts or dicts merged into each retained
                                                 message ({"text", "order", "complete", ...})
    spawn_agent(t) -> call_id                    a spawn_agent function call and its output
    request_input(t, answered=False, sync=False) -> call_id
                                                 request_user_input_async with its immediate output; answered=True
                                                 adds the answer (async_answer). sync=True: request_user_input, whose
                                                 output is the answer; answer(t, call_id) answers it later
    async_answer(t, call_id)                     the answer to an async question: a user message naming the call
    ask(t, questions, sync=False, arguments=None) -> call_id
                                                 request_user_input_async (sync=True: request_user_input) with the
                                                 arguments' real shape, {"questions": questions}, e.g. [{"title":
                                                 "Which?", "options": ["A", "B"]}]; arguments: a text written as the
                                                 call's arguments as is (malformed ones). Async: its immediate output.
    plain_user(text, t=None)                     a user-role message without content_item_metadata, as Codex logged
                                                 typed prompts (and its own text, such as <environment_context>)
                                                 before it marked typed text
    add(rtype, payload, t=None)                  any record
    raw(text)                                    a line as is
    write(compress=False, mtime=None, meta=True) -> path
                                                 writes the log (again, after more records, to grow it). Its
                                                 modification time is the last record's time unless mtime is given.
                                                 compress=True writes .jsonl.zst and skips the test without zstd.
                                                 Without a session_meta record it puts one first (meta=False: not).
Attributes: thread_id, session_id, cwd, kind, parent, start, t (the clock), turn_id, window, path.

Also: make_home(tmpdir), iso(t), new_id(), add_title(home, thread_id, name), link_folder(target, link) (a junction
on Windows, else a symbolic link; False when it can't be made), ZSTD (this Python writes .zst), T0, HOUR, DAY.
"""
import calendar
import json
import os
import subprocess
import unittest
import uuid
from datetime import datetime, timezone

try:
    from compression import zstd as _zstd
except ImportError:
    _zstd = None
ZSTD = _zstd is not None

HOUR = 3600
DAY = 86400
ACCEPTED = '{"accepted":true}'      # Codex's output for a request_user_input_async call it accepted (the real logs')
T0 = calendar.timegm((2026, 11, 7, 9, 0, 0))


def make_home(tmpdir):
    """A CODEX_HOME inside tmpdir with an empty sessions/ folder."""
    home = os.path.join(tmpdir, "codex-home")
    os.makedirs(os.path.join(home, "sessions"), exist_ok=True)
    return home


def iso(t):
    """Epoch seconds as Codex writes them: 2026-11-07T09:00:00.000Z."""
    d = datetime.fromtimestamp(t, timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def new_id():
    return str(uuid.uuid4())


def link_folder(target, link):
    """Makes `link` a second name for the folder `target`: a junction on Windows (no special rights needed), else a
    symbolic link. False when this system can't make one."""
    os.makedirs(target, exist_ok=True)
    if os.name == "nt":
        done = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", link, target], stdin=subprocess.DEVNULL,
                              capture_output=True)
        return done.returncode == 0 and os.path.isdir(link)
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return os.path.isdir(link)


def add_title(home, thread_id, name, updated_at=None):
    """Appends a conversation title to CODEX_HOME/session_index.jsonl."""
    with open(os.path.join(home, "session_index.jsonl"), "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"id": thread_id, "thread_name": name, "updated_at": updated_at or iso(T0)},
                            ensure_ascii=False) + "\n")


def _usage(inp, cached, out, reasoning=0):
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
            "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


class Rollout:
    """One synthetic thread log; see the module docstring."""

    def __init__(self, home, thread_id, cwd, kind="main", parent=None, day=(2026, 11, 7)):
        self.home, self.thread_id, self.cwd, self.kind, self.parent = home, thread_id, cwd, kind, parent
        self.session_id = parent if (kind == "subagent" and parent) else thread_id
        self.day = tuple(day)
        self.start = calendar.timegm(self.day + (9, 0, 0))
        self.t = self.start
        self.turn_id = "turn-%s-0" % thread_id
        self.window = None
        self._lines = []              # ("record", dict) or ("raw", str)
        self._n = 0                   # ids handed out
        self._total = _usage(0, 0, 0)
        self._last_info = None
        self._last_record = None
        self._windows = 0
        stamp = datetime.fromtimestamp(self.start, timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        self.path = os.path.join(home, "sessions", "%04d" % day[0], "%02d" % day[1], "%02d" % day[2],
                                 "rollout-%s-%s.jsonl" % (stamp, thread_id))

    # ---- building blocks ----

    def _id(self, prefix):
        self._n += 1
        return "%s-%s-%d" % (prefix, self.thread_id, self._n)

    def add(self, rtype, payload, t=None):
        if t is not None:
            self.t = t
        self._lines.append(("record", {"timestamp": iso(self.t), "type": rtype, "payload": payload}))

    def raw(self, text):
        self._lines.append(("raw", text))

    def event(self, payload, t=None):
        self.add("event_msg", payload, t)

    def item(self, payload, t=None):
        self.add("response_item", payload, t)

    # ---- records ----

    def meta(self, t=None, instructions="You are a coding agent working in a test project.",
             originator="Codex Desktop", branch="main"):
        self.add("session_meta", self._meta_payload(self.t if t is None else t, instructions, originator, branch), t)

    def _meta_payload(self, t, instructions, originator, branch):
        if self.kind == "subagent":
            source = {"subagent": {"thread_spawn": {"parent_thread_id": self.parent, "depth": 1,
                                                    "agent_path": "/root/helper", "agent_nickname": "Helper",
                                                    "agent_role": None}}}
        else:
            source = "vscode"
        payload = {"session_id": self.session_id, "id": self.thread_id}
        if self.parent:
            payload["parent_thread_id"] = self.parent
        payload.update({
            "timestamp": iso(t), "cwd": self.cwd, "originator": originator,
            "cli_version": "0.0.0-test", "source": source, "model_provider": "openai",
            "base_instructions": {"text": instructions, "provenance": {"type": "model"}},
            "git": {"commit_hash": "0" * 40, "branch": branch, "repository_url": "https://example.invalid/repo.git"}})
        return payload

    def task_started(self, t, trigger=None, turn_id=None):
        """trigger: what started the turn ("composer" in the desktop app, "remote_android" from the ChatGPT Android
        app), written as Codex writes it, in turn_attribution; turn_id: the turn's id instead of a new one."""
        self.turn_id = turn_id or self._id("turn")
        payload = {"type": "task_started", "turn_id": self.turn_id, "started_at": int(t),
                   "model_context_window": self.window, "collaboration_mode_kind": "default"}
        if trigger is not None:
            payload["turn_attribution"] = {"turn_id": self.turn_id, "turn_trigger": trigger, "parent_turn_id": None,
                                           "initiating_agent_path": None, "root_turn_id": self.turn_id}
        self.event(payload, t)

    def turn_context(self, model, t=None):
        self.add("turn_context", {
            "turn_id": self.turn_id, "cwd": self.cwd, "approval_policy": "on-request", "model": model,
            "collaboration_mode": {"mode": "default", "settings": {"model": model, "reasoning_effort": "medium",
                                                                   "developer_instructions": None}},
            "effort": "medium"}, t)

    def turn(self, model, window, tier=None, t=None, trigger=None, turn_id=None):
        self.window = window
        self.task_started(self.t if t is None else t, trigger=trigger, turn_id=turn_id)
        self.turn_context(model)
        self.event({"type": "thread_settings_applied", "thread_id": self.thread_id,
                    "thread_settings": {"model": model, "service_tier": tier or "default", "cwd": self.cwd,
                                        "reasoning_effort": "medium"}})

    def world_state(self, t=None):
        self.add("world_state", {"full": True, "state": {"environments": {"environments": {
            "local": {"cwd": self.cwd, "status": "available"}}}}}, t)

    def message(self, items, t=None):
        content, meta = [], []
        for text, provenance in items:
            for line in text.splitlines(keepends=True) or [text]:
                content.append({"type": "input_text", "text": line})
                meta.append({"provenance": {"type": provenance}})
        self.item({"type": "message", "id": self._id("msg"), "role": "user", "content": content,
                   "internal_chat_message_metadata_passthrough": {
                       "turn_id": self.turn_id, "create_time": float(self.t if t is None else t),
                       "content_item_kinds": ["user.text"] * len(content), "content_item_metadata": meta}}, t)

    def user(self, text, provenance="user", t=None):
        self.message([(text, provenance)], t)

    def harness(self, text, t=None):
        self.message([(text, "harness")], t)

    def assistant(self, text, t=None, phase="final_answer", encrypted=False):
        payload = {"type": "message"}
        if encrypted:
            payload["encrypted_content"] = "c3ludGhldGlj"
        payload.update({"status": "completed", "id": self._id("msg"), "role": "assistant",
                        "content": [{"type": "output_text", "text": text}], "phase": phase,
                        "internal_chat_message_metadata_passthrough": {"turn_id": self.turn_id}})
        self.item(payload, t)

    def tool_output(self, t=None, size=1_000):
        line = "synthetic tool output line\n"
        self.item({"type": "custom_tool_call_output", "id": self._id("out"), "call_id": self._id("call"),
                   "output": (line * (size // len(line) + 1))[:size]}, t)

    def call(self, t, inp, cached, out, reasoning=0, legacy=False, response_id=None):
        usage = _usage(inp, cached, out, reasoning)
        self._total = {k: self._total[k] + usage[k] for k in usage}
        rid = None
        if not legacy:
            rid = response_id or self._id("resp")
            self._last_record = {"thread_id": self.thread_id, "turn_id": self.turn_id,
                                 "session_id": self.session_id, "response_id": rid, "usage": usage,
                                 "turn_token_usage": usage, "thread_token_usage": dict(self._total)}
            self.add("token_usage_record", self._last_record, t)
        self._last_info = {"total_token_usage": dict(self._total), "last_token_usage": usage,
                           "last_usage_includes_reasoning": True, "model_context_window": self.window}
        self.event({"type": "token_count", "info": self._last_info, "rate_limits": None}, t)
        return rid

    def repeat(self, t):
        self.event({"type": "token_count", "info": self._last_info, "rate_limits": None}, t)

    def meter(self, t, weekly_pct, resets_at, plan="plus", five_h=None, limit_id="codex"):
        weekly = None
        if weekly_pct is not None:
            weekly = {"used_percent": float(weekly_pct), "window_minutes": 10080, "resets_at": int(resets_at)}
        if five_h is None:
            primary, secondary = weekly, None
        else:
            pct, resets = five_h if isinstance(five_h, (tuple, list)) else (five_h, t + 5 * HOUR)
            primary = {"used_percent": float(pct), "window_minutes": 300, "resets_at": int(resets)}
            secondary = weekly
        self.event({"type": "token_count", "info": self._last_info, "rate_limits": {
            "limit_id": limit_id, "limit_name": None, "primary": primary, "secondary": secondary,
            "credits": None, "plan_type": plan, "rate_limit_reached_type": None}}, t)

    def complete(self, t, last_agent_message):
        if isinstance(last_agent_message, str):
            self.assistant(last_agent_message, t)
        self.event({"type": "task_complete", "turn_id": self.turn_id, "last_agent_message": last_agent_message,
                    "started_at": int(t), "completed_at": int(t), "duration_ms": 1000}, t)

    def aborted(self, t):
        self.event({"type": "turn_aborted", "turn_id": self.turn_id, "reason": "interrupted",
                    "started_at": int(t)}, t)

    def compacted(self, t, retained_user_messages):
        retained = []
        for i, x in enumerate(retained_user_messages):
            m = {"message_id": "kept-%d" % i, "turn_id": self.turn_id, "order": i, "revision": 0, "complete": True}
            m.update(x if isinstance(x, dict) else {"text": x})
            retained.append(m)
        self._windows += 1
        self.add("compacted", {
            "message": "", "replacement_history": [{"type": "compaction", "encrypted_content": "c3ludGhldGlj"}],
            "replacement_history_metadata": [{"client_authored": False}],
            "retained_context": {"verified_answers": [], "incomplete": False, "user_messages": retained,
                                 "user_messages_incomplete": False, "assistant_messages": [],
                                 "assistant_messages_incomplete": False, "next_order": len(retained)},
            "window_number": self._windows, "window_id": "window-%d" % self._windows,
            "compaction_response_id": (self._last_record or {}).get("response_id"),
            "latest_token_usage_record": self._last_record}, t)
        self.item({"type": "compaction", "id": self._id("cmp"), "encrypted_content": "c3ludGhldGlj",
                   "internal_chat_message_metadata_passthrough": {"turn_id": self.turn_id}})

    def _function_call(self, t, name, arguments):
        call_id = self._id("call")
        self.item({"type": "function_call", "status": "completed", "id": self._id("fc"), "name": name,
                   "namespace": "test", "arguments": json.dumps(arguments), "call_id": call_id,
                   "internal_chat_message_metadata_passthrough": {"turn_id": self.turn_id}}, t)
        return call_id

    def answer(self, t, call_id, output="ok"):
        self.item({"type": "function_call_output", "id": self._id("fco"), "call_id": call_id, "output": output,
                   "internal_chat_message_metadata_passthrough": {"turn_id": self.turn_id}}, t)

    def spawn_agent(self, t):
        call_id = self._function_call(t, "spawn_agent", {"task": "A synthetic task for a helper agent."})
        self.answer(t, call_id, '{"agent": "helper"}')
        return call_id

    def request_input(self, t, answered=False, sync=False):
        """A question for the person, as Codex asks it: request_user_input_async, whose output comes at once and only
        says the question was sent; answered=True then adds the answer, a user message naming the call. sync=True:
        request_user_input, whose output is the answer (answered=True writes it)."""
        name = "request_user_input" if sync else "request_user_input_async"
        call_id = self._function_call(t, name, {"questions": [{"question": "Which option?"}]})
        if not sync:
            self.answer(t, call_id, ACCEPTED)
        if answered:
            (self.answer if sync else self.async_answer)(t, call_id)
        return call_id

    def async_answer(self, t, call_id, text="The first one."):
        """The answer to an async question: a user message naming the call, without provenance (as Codex writes it)."""
        self.item({"type": "message", "id": self._id("msg"), "role": "user",
                   "content": [{"type": "input_text", "text": "Answer to %s: %s" % (call_id, text)}]}, t)

    def ask(self, t, questions, sync=False, arguments=None, refused=None):
        """A question for the person with the arguments' real shape, {"questions": [{"title", "options"}, ...]}:
        request_user_input_async and its immediate output, {"accepted":true} as Codex writes it (sync=True:
        request_user_input, answered by answer()). arguments: a text written as the call's arguments instead (to write
        malformed ones); refused: Codex's error message as the output instead, for a call it refused."""
        call_id = self._id("call")
        self.item({"type": "function_call", "status": "completed", "id": self._id("fc"),
                   "name": "request_user_input" if sync else "request_user_input_async", "namespace": "test",
                   "arguments": json.dumps({"questions": questions}) if arguments is None else arguments,
                   "call_id": call_id, "internal_chat_message_metadata_passthrough": {"turn_id": self.turn_id}}, t)
        if not sync:
            self.answer(t, call_id, ACCEPTED if refused is None else refused)
        return call_id

    def plain_user(self, text, t=None):
        """A user-role message without content_item_metadata: how Codex logged a typed prompt before it marked typed
        text, and how it still logs some text of its own (<environment_context>, AGENTS.md, app pages)."""
        self.item({"type": "message", "id": self._id("msg"), "role": "user",
                   "content": [{"type": "input_text", "text": text}]}, t)

    # ---- the file ----

    def _text(self, meta):
        if meta and not any(kind == "record" and obj["type"] == "session_meta" for kind, obj in self._lines):
            payload = self._meta_payload(self.start, "You are a coding agent working in a test project.",
                                         "Codex Desktop", "main")
            self._lines.insert(0, ("record", {"timestamp": iso(self.start), "type": "session_meta",
                                              "payload": payload}))
        out, ordinal = [], 0
        for kind, obj in self._lines:
            if kind == "raw":
                out.append(obj)
                continue
            rec = {"timestamp": obj["timestamp"], "ordinal": ordinal, "type": obj["type"], "payload": obj["payload"]}
            ordinal += 1
            out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        return "".join(line + "\n" for line in out)

    def write(self, compress=False, mtime=None, meta=True):
        data = self._text(meta).encode("utf-8")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        path = self.path
        if compress:
            if not ZSTD:
                raise unittest.SkipTest("writing .zst needs Python 3.14+ (compression.zstd)")
            path += ".zst"
            with _zstd.open(path, "wb") as fh:
                fh.write(data)
        else:
            with open(path, "wb") as fh:
                fh.write(data)
        stamp = self.t if mtime is None else mtime
        os.utime(path, (stamp, stamp))
        return path
