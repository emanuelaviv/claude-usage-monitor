"""What the hooks do. Codex runs hooks/prompt_submit.py when you send a prompt, hooks/session_start.py when a new
conversation starts (also with the CLI's /clear), and hooks/refresh_rate.py in the background when one starts or
resumes; each script calls one function here and prints what it returns.

- on_prompt_submit: after a break of an hour or more in a main conversation, it holds the prompt back with the card
  when a new conversation clearly pays (and keeps the handoff for one), or lets the prompt through with a one-line
  note of what it is likely to cost, or says nothing. Sending a prompt again within 15 minutes of a card means
  "stay here": that conversation isn't checked again for 12 hours. A project folder keeps one handoff at a time, so
  while another conversation's card's handoff there is waiting, a card that would replace it is shown as the note
  instead. Only a prompt typed at this computer (in the Codex app's composer or the CLI) is ever held back: one from
  the ChatGPT phone app, an automation, a retry or the question panel gets the note, as nobody may see the card.
  Prompts in an agent's thread or a delegate's (such as /review's), and prompts sent while a turn is still running,
  are left alone.
- on_session_start: a new conversation started in the same project folder within 30 minutes of a card gets the
  handoff as developer context.
- refresh_rate: at most twice a day, measures $ per 1% of the weekly limit from the logs (rate.py).

A handoff nobody used is deleted by the first of these hooks to run after its 30 minutes (each prunes the data
folder), or by the monitor's nightly replay.

A hook must never break a prompt. Any exception means no output, so the prompt is sent; errors.log in the data folder
gets the step and the exception's type, never its message, which could quote a prompt or the log.
"""
import json
import math
import os
import re
import time
from dataclasses import replace

from . import rollout, rule, state

# handoff (it loads subprocess) and rate (statistics) are imported where they are used: most prompts come without a
# break, and that path has to finish, Python's start-up included, well inside half a second.

DEMO = "switch-advisor demo"
TEST = "switch-advisor test"
TEST_IDLE_S = 3 * 3600          # the test card pretends you were away this long
TEST_PROMPT = "test " * 25      # what the test card's flags weigh instead of the trigger's two words: a prompt that
                                # is neither short nor a reply, and names no file
EXAMPLE = "These are example numbers, not this conversation's."
FLAG_PROMPT_CHARS = 2_000       # the quality flags look at this much of the prompt (see _flags)
RATE_FILE = "rate.json"
RATE_MAX_AGE_S = 12 * 3600      # rate.json is measured again when it is older than this
RATE_LOCK_S = 600               # a refresh lock older than this was left by a refresh that was stopped


# ---- UserPromptSubmit ----------------------------------------------------------------------------------------------

def on_prompt_submit(payload, now=None, env=os.environ):
    """The UserPromptSubmit hook: {"decision": "block", "reason": <card>} holds the prompt back, {"systemMessage":
    <note>} lets it through with a note, None lets it through quietly. `env` supplies PLUGIN_DATA and CODEX_HOME."""
    d, step, said = None, "read", None
    try:
        now = time.time() if now is None else float(now)
        if not isinstance(payload, dict):
            return None
        prompt, sid, cwd = payload.get("prompt"), payload.get("session_id"), payload.get("cwd")
        if not isinstance(prompt, str) or not _text(sid) or not _text(cwd):
            return None
        said = " ".join(prompt.split()).lower()
        step = "state"
        d = state.data_dir(env)
        P = rule.params_from(state.load_params(d))
        if d:
            state.prune(d, now, P.handoff_ttl_s)
        if said == DEMO:
            step = "demo"
            return _demo(d, sid, now)
        if said == TEST:
            step = "test"
            return _test(d, sid, cwd, _transcript(payload, env), prompt, P, now, _home(env), agent=_agent(payload),
                         turn_id=payload.get("turn_id"))
        if _agent(payload):
            return None           # typed in an agent's thread: its session_id is the main conversation's
        path = _transcript(payload, env)
        if not path:
            return None
        step = "session"
        s = state.session(d, sid) if d else {}
        stay = _num(s.get("offered_at")) and 0 <= now - s["offered_at"] <= P.resend_window_s
        if not stay and _num(s.get("quiet_until")) and s["quiet_until"] > now:
            return None
        step = "snapshot"
        snap = rollout.snapshot(path, now=now)      # turn_running is judged at the hook's time
        if snap.kind == "subagent":
            return None           # a delegate's thread, such as /review's: Codex sends the main conversation's
                                  # session_id and no agent fields, so it must not count as staying after a card
        if getattr(snap, "turn_running", False) is True:
            return None           # mid-turn: Codex calls the model again when its tool returns, whatever is sent now
        if stay:
            step = "session"
            return _kept(d, sid, cwd, s, P, now)
        if snap.last_call_end is None or snap.compacted_since_last_call:
            return None
        idle = now - snap.last_call_end
        if idle < P.min_idle_s:
            return None

        step = "facts"            # a break: from here on, time matters less
        from . import handoff
        _add_rate(P, d)
        f = rollout.facts(path, now=now)
        files = handoff.git_status(cwd)
        flags = _flags(f, prompt, files)
        pct = _meter_pct(_newest(snap.meter, rollout.latest_meter(_home(env))), now)
        card = rule.after_break(snap.ctx, idle, flags, P, snap.model, snap.window, snap.tier, pct)
        trigger = _trigger(snap, payload.get("turn_id"))
        if (card.show and d and _typed_here(trigger) and not state.guardrail_off(d)
                and handoff.requests(f, prompt) and not _waiting(d, cwd, sid, now, P)):
            step = "offer"
            return _offer(d, sid, cwd, handoff.build(f, files, now, prompt=prompt), card, snap.model, now, trigger)
        if card.show:
            # No data folder to keep a handoff in, the prompt wasn't typed at this computer (from the phone app, which
            # may not show the card, so a held-back prompt would look like a message that went nowhere; or from an
            # automation, a retry or the question panel, where nobody may be there to answer it), the guardrail turned
            # the card off, the log holds no typed request for the handoff to carry, or another conversation's card's
            # handoff is waiting in this folder (this card would replace it, and that card's switch would start from
            # this conversation's notes): the same rule with the card turned off gives just the note.
            card = rule.after_break(snap.ctx, idle, flags, _no_card(P), snap.model, snap.window, snap.tier, pct)
        if not card.note:
            return None
        step = "note"
        if d:
            state.log(d, "note", t=now, session=sid, model=snap.model, numbers=card.numbers, **_known(trigger))
        return {"systemMessage": card.note}
    except Exception as e:
        _error(d, "prompt_submit/" + step, e)
        if said in (DEMO, TEST):    # a trigger never needs the model: say what failed instead of sending it there
            return _block("Switch advisor: \"%s\" stopped at step \"%s\" (%s), so there is no card to show. Nothing was "
                          "sent." % (said, step, type(e).__name__))
        return None


def _kept(d, sid, cwd, s, P, now):
    """A prompt sent again soon after a card: the person chose to stay. Its handoff goes, and this conversation gets
    no card or note for quiet_s. "kept" is logged only when that handoff was still waiting: after a switch (a new
    conversation took it), a prompt here is not a choice to stay, but the quiet period spares it a second card. A
    handoff `switch-advisor test` made here since (once the card's was taken) isn't the card's: it stays, as the test
    promised, and "kept" isn't logged."""
    s.pop("offered_at", None)
    s["quiet_until"] = now + P.quiet_s
    state.save_session(d, sid, s)
    if state.drop_pending(d, cwd, from_session=sid, test=False):
        state.log(d, "kept", t=now, session=sid)
    return None


_TRIGGER = re.compile(r"[A-Za-z0-9_.-]{1,40}")


def _trigger(snap, turn_id):
    """What started this prompt's turn: the trigger of the log's latest task_started (rollout.Snapshot), which Codex
    writes before the prompt hook runs, when that record is this turn's (its turn_id is the hook's). "composer" for a
    prompt typed in the desktop app, "user" in the CLI, "remote_android" from the ChatGPT Android app; the app's
    automations, retries and question-panel answers have their own. None when it can't be told (older Codex logs no
    trigger), and "other" for a trigger that isn't a short identifier (it goes into the decision log)."""
    t = getattr(snap, "trigger", None)
    if not (isinstance(turn_id, str) and turn_id and getattr(snap, "turn_id", None) == turn_id):
        return None
    if not isinstance(t, str) or not t:
        return None
    return t if _TRIGGER.fullmatch(t) else "other"     # one we can't name still isn't typed here


def _known(trigger):
    """{"trigger": trigger} for the decision log, or {} when it is unknown (older Codex logs no trigger)."""
    return {"trigger": trigger} if trigger else {}


TYPED_HERE = ("composer", "user")     # the Codex app's composer, the Codex CLI


def _typed_here(trigger):
    """A prompt typed at this computer, the only kind the card may hold back: its trigger is the app's composer's or
    the CLI's, or none was recorded. Any other (the ChatGPT phone app's "remote_android", an automation's, a retry's, an
    answer from the question panel, one Codex adds later) gets the note: nobody may see the card."""
    return trigger is None or trigger in TYPED_HERE


def _not_typed_why(trigger):
    """Why a prompt that wasn't typed here gets the note, as the test card says it."""
    if trigger.startswith("remote"):
        return "it came from your phone, which may not show the card"
    if trigger == "other":
        return "Codex doesn't mark it as typed at this computer, and nobody may be there to answer the card"
    return "Codex marks it %s, not typed at this computer, and nobody may be there to answer the card" % trigger


def _offer(d, sid, cwd, text, card, model, now, trigger=None):
    """Holds the prompt back with the card: keeps the handoff for a new conversation in this folder, and remembers the
    offer, so that sending a prompt here again means "stay"."""
    key = state.cwd_key(cwd)
    state.put_pending(d, cwd, sid, text, card.numbers, now)
    try:
        s = state.session(d, sid)
        s.update(offered_at=now, numbers=card.numbers, cwd_key=key)
        state.save_session(d, sid, s)
    except Exception:
        state.drop_pending(d, cwd, from_session=sid)     # no handoff without the offer it belongs to
        raise
    state.log(d, "offer", t=now, session=sid, model=model, risk=card.risk, flags=card.flags, numbers=card.numbers,
              cwd_key=key, **_known(trigger))
    return _block(card.text)


def _waiting(d, cwd, sid, now, P):
    """True when a handoff another conversation's card made for this project folder is still waiting for a new
    conversation. A folder keeps one handoff, so making another would break that one's card. A test's handoff doesn't
    count: a real card may replace it."""
    rec = _card_waiting(d, cwd, now, P)
    return rec is not None and rec.get("from_session") != sid


def _card_waiting(d, cwd, now, P):
    """The record of the handoff a card made for this project folder, whichever conversation showed it, while it is
    still waiting for a new conversation (not stale: younger than handoff_ttl_s, and not made more than a minute
    ahead of the clock); None otherwise, and for a test's handoff."""
    rec = state.peek_pending(d, cwd)
    if rec is None or rec.get("test") is True:
        return None
    return rec if not _num(rec.get("at")) or not state.stale(rec["at"], now, P.handoff_ttl_s) else None


def _agent(payload):
    """Whether the prompt was typed in an agent's thread: Codex then sends the main conversation's session_id, with
    the agent's agent_id and agent_type."""
    return any(isinstance(payload.get(k), str) and payload[k] != "" for k in ("agent_id", "agent_type"))


def _flags(f, prompt, files):
    """The quality flags for this prompt. They are given only its first 2,000 characters, which settle whether it is
    short, starts with a short reply or names a file: the file-name pattern takes quadratic time on a long run
    without spaces (a pasted blob), and could outlast the hook's timeout on the whole prompt."""
    return rule.quality_flags(f, prompt[:FLAG_PROMPT_CHARS], len(files))


def _no_card(P):
    """Params under which no saving is enough for a card, so after_break gives only its note."""
    return replace(P, bar_low=math.inf, bar_medium=math.inf)


def _meter_pct(m, now):
    """How much of the weekly limit is used, from a meter reading. None when unknown, or when that week has ended
    since (the meter has reset)."""
    if m is None or not _num(m.weekly_pct):
        return None
    if _num(m.weekly_resets) and m.weekly_resets <= now:
        return None
    return m.weekly_pct


def _newest(own, other):
    """The newer of this conversation's last meter reading and the newest one in the recent logs: after a break,
    other conversations have usually moved the meter since."""
    if own is None or (other is not None and _num(other.t) and _num(own.t) and other.t > own.t):
        return other
    return own


def _add_rate(P, d):
    """$ per 1% of the weekly limit, from rate.json (refresh_rate), when it has one."""
    r = state.read_json(os.path.join(d, RATE_FILE)) if d else None
    v = r.get("usd_per_pt") if r else None
    if _num(v) and v > 0:
        P.usd_per_pt = float(v)


def _transcript(payload, env):
    """The conversation's log: transcript_path when that file exists, else the newest log named after the session."""
    path = payload.get("transcript_path")
    if _text(path) and os.path.isfile(path):
        return path
    return rollout.find_rollout(payload.get("session_id"), _home(env))


def _home(env):
    """CODEX_HOME from `env`; None lets rollout use its default (~/.codex)."""
    home = env.get("CODEX_HOME")
    return os.path.expanduser(home) if home else None


# ---- the triggers --------------------------------------------------------------------------------------------------

def _demo(d, sid, now):
    """`switch-advisor demo`: the card with example numbers (410K tokens after 8.2 h on gpt-6-astra with an 832,200
    window, one medium flag, $5 per 1% of the weekly limit, the meter at 64%), for screenshots. It keeps no handoff
    and reads nothing of this conversation."""
    P = rule.Params(usd_per_pt=5.0)
    flags = rule.quality_flags(rollout.Facts(), DEMO, 0)     # the short prompt: "your message answers its last reply"
    card = rule.after_break(410_000, 29_520, flags, P, "gpt-6-astra", window=832_200, meter_pct=64,
                            label="DEMO · ")
    if d:
        state.log(d, "demo", t=now, session=sid)
    return _block(card.text + "\n\n" + EXAMPLE)


def _test(d, sid, cwd, path, prompt, P, now, home=None, agent=False, turn_id=None):
    """`switch-advisor test`: this conversation's card as if you came back after 3 hours, shown even when the rule
    wouldn't show one (the card then says so, and when the rule would give the one-line note instead, that note comes
    with it as a systemMessage). It is held back also when sent from the phone app, so you can see whether the phone
    shows a card. With a data folder it also keeps a real handoff, so the switch can be tried, unless a card's handoff
    is waiting in this folder (this conversation's own card's too): a test never replaces one, though it does replace
    an earlier test's. It never sets offered_at: the next prompt here is checked as usual."""
    label = "TEST · "
    if agent:
        return _block(label + "Switch advisor: this is an agent's thread; only your own conversations are checked.")
    if not path:
        return _block(label + "Switch advisor: there is no log of this conversation yet, so there is nothing to check.")
    snap = rollout.snapshot(path, now=now)
    if snap.kind == "subagent":
        return _block(label + "Switch advisor: this is an agent's thread; only your own conversations are checked.")
    if snap.last_call_end is None or snap.ctx <= 0:
        return _block(label + "Switch advisor: this conversation has no model response yet, so there is nothing to "
                              "compare.")
    if snap.compacted_since_last_call:
        return _block(label + "Switch advisor: Codex compacted this conversation after its last response, so the next "
                              "message sends only the compacted context, and a new conversation can't save anything.")
    from . import handoff
    _add_rate(P, d)
    f = rollout.facts(path, now=now)
    files = handoff.git_status(cwd)
    flags = rule.quality_flags(f, TEST_PROMPT, len(files))
    pct = _meter_pct(_newest(snap.meter, rollout.latest_meter(home)), now)
    card = rule.after_break(snap.ctx, TEST_IDLE_S, flags, P, snap.model, snap.window, snap.tier, pct, label=label)
    if not card.text:
        if snap.model:
            why = "there is no known price for this conversation's model (%s)" % snap.model
        else:
            why = "this conversation's log doesn't name its model"
        return _block(label + "Switch advisor: %s, so there is nothing to compare." % why)
    # What a real break like this would give instead of the card: the note when the rule shows no card, and also when
    # the prompt hook would turn the card into the note (on_prompt_submit): no data folder, a prompt not typed at this
    # computer, the guardrail, no typed request for the handoff to carry, or another conversation's card's handoff
    # waiting in this folder.
    trigger = _trigger(snap, turn_id)
    why = None
    if card.show and not _typed_here(trigger):
        why = _not_typed_why(trigger)
    elif card.show and d:
        if state.guardrail_off(d):
            why = "the monitor's guardrail has turned the card off"
        elif not handoff.requests(f, prompt):
            why = "this conversation's log holds no request you typed for a handoff to carry"
    demoted = card.show and (not d or why is not None or _waiting(d, cwd, sid, now, P))
    note = (rule.after_break(snap.ctx, TEST_IDLE_S, flags, _no_card(P), snap.model, snap.window, snap.tier, pct,
                             label=label).note if demoted else "" if card.show else card.note)
    # ... and nothing at all where the prompt hook stays silent: during the quiet period after you chose to stay, and
    # while the conversation's latest turn counts as running
    quiet = (state.session(d, sid) if d else {}).get("quiet_until")
    if _num(quiet) and quiet > now:
        silent = "you chose to stay in this conversation, so it isn't checked for %s" % _left(quiet - now)
    elif getattr(snap, "turn_running", False) is True:
        silent = ("its latest turn hasn't ended, and a turn counts as running for %s after its last record"
                  % _duration(rollout.TURN_STALE_S))
    else:
        silent = None
    if silent:
        note = ""
    waiting = _card_waiting(d, cwd, now, P) if d else None
    if waiting is not None:
        state.log(d, "test", t=now, session=sid, **_known(trigger))
        whose = ("the handoff from this conversation's card is" if waiting.get("from_session") == sid
                 else "a handoff from another conversation in this project is")
        end = ("This was only a test: nothing was sent, and no handoff was made, because %s still waiting for a new "
               "conversation, and a test doesn't replace it." % whose)
    elif d:
        state.put_pending(d, cwd, sid, handoff.build(f, files, now, prompt=prompt), card.numbers, now, test=True)
        state.log(d, "test", t=now, session=sid, **_known(trigger))
        end = ("To try the switch: start a new conversation in this project folder and send a prompt there within %s; "
               "it starts from the handoff made just now. This was only a test: nothing was sent%s."
               % (_duration(P.handoff_ttl_s), "" if silent else ", and this conversation is checked as usual"))
    else:
        end = ("This was only a test: nothing was sent, and no handoff was made, because Codex gave the plugin no data "
               "folder to keep it in.")
    if silent:
        end += " After a real break you would get nothing here, because %s." % silent
    elif note:
        end += " After a real break you would get the one-line note shown with this card instead"
        end += ", because %s." % why if why else "."
    elif demoted:
        end += (" After a real break this card wouldn't show%s, and the rule gives no note for it either."
                % (", because %s" % why if why else ""))
    paras = card.text.split("\n\n")
    if paras[-1].startswith("To switch:"):      # "To stay: send your prompt here again" isn't true of a test
        paras[-1] = end
    else:
        paras.append(end)
    out = _block("\n\n".join(paras))
    if note:
        out["systemMessage"] = note    # Codex shows it and still holds the prompt back
    return out


# ---- SessionStart --------------------------------------------------------------------------------------------------

def on_session_start(payload, now=None, env=os.environ):
    """The SessionStart hook for a new conversation (source "startup", or "clear": the CLI's /clear starts a new
    conversation too): {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": <handoff>}} when
    a card kept a handoff for this project folder in the last handoff_ttl_s, from another conversation. A handoff past
    its use (state.stale: older, or made more than a minute ahead of the clock) is deleted and logged "expired".
    Otherwise None."""
    d, step = None, "read"
    try:
        now = time.time() if now is None else float(now)
        if not isinstance(payload, dict) or payload.get("source") not in ("startup", "clear"):
            return None
        sid, cwd = payload.get("session_id"), payload.get("cwd")
        if not _text(sid) or not _text(cwd):
            return None
        d = state.data_dir(env)
        if not d:
            return None
        step = "thread"
        path = payload.get("transcript_path")
        if _text(path) and os.path.isfile(path) and rollout.snapshot(path).kind == "subagent":
            return None           # an agent's thread: the handoff is for a conversation you start
        step = "pending"
        P = rule.params_from(state.load_params(d))
        state.prune(d, now, P.handoff_ttl_s)
        rec = state.take_pending(d, cwd)
        if rec is None:
            return None
        key = state.cwd_key(cwd)
        if rec.get("from_session") == sid:         # not a new conversation but the one that made it: put it back
            state.put_pending(d, cwd, sid, rec.get("text") or "", rec.get("numbers"), rec.get("at"),
                              test=bool(rec.get("test")))
            return None
        if not _num(rec.get("at")) or state.stale(rec["at"], now, P.handoff_ttl_s):
            state.log(d, "expired", t=now, from_session=rec.get("from_session"), cwd_key=key)
            return None
        text = rec.get("text")
        if not _text(text):
            return None
        fields = {"from_session": rec.get("from_session"), "numbers": rec.get("numbers"), "cwd_key": key}
        if rec.get("test"):
            fields["test"] = True               # made by `switch-advisor test`: the replay can leave it out
        state.log(d, "switched", t=now, session=sid, **fields)
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    except Exception as e:
        _error(d, "session_start/" + step, e)
        return None


# ---- the rate refresh ----------------------------------------------------------------------------------------------

def refresh_rate(now=None, env=os.environ):
    """The SessionStart hook that runs in the background, when a conversation starts (also with /clear) or resumes:
    deletes expired handoffs (state.prune), then measures $ per 1% of the weekly limit (rate.compute_rate) and writes
    rate.json, unless rate.json is younger than 12 h or another refresh is running. When no weekly window qualifies
    yet, rate.json says "usd_per_pt": null, so the logs aren't read again at every start. Returns None."""
    d, step = None, "read"
    try:
        now = time.time() if now is None else float(now)
        d = state.data_dir(env)
        if not d:
            return None
        step = "prune"
        state.prune(d, now, rule.params_from(state.load_params(d)).handoff_ttl_s)
        step = "read"
        path = os.path.join(d, RATE_FILE)
        old = state.read_json(path)
        if old and _num(old.get("computed_at")) and 0 <= now - old["computed_at"] < RATE_MAX_AGE_S:
            return None
        step = "lock"
        lock = os.path.join(d, "rate.lock")
        if not _lock(lock, now):
            return None
        try:
            step = "compute"
            from . import rate
            r = rate.compute_rate(home=_home(env), now=now)
            step = "write"
            state.write_json(path, r or {"usd_per_pt": None, "windows": 0, "computed_at": now, "detail": []})
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
    except Exception as e:
        _error(d, "refresh_rate/" + step, e)
    return None


def _lock(path, now):
    """Takes the refresh lock: a file that exists while a refresh runs, so that Codex starting several conversations
    at once reads the logs once. A lock older than RATE_LOCK_S was left by a refresh that was stopped: it is taken
    over."""
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return True
        except FileExistsError:
            try:
                if now - os.path.getmtime(path) < RATE_LOCK_S:
                    return False
                os.remove(path)
            except OSError:
                return False
    return False


# ---- small things --------------------------------------------------------------------------------------------------

def _block(reason):
    return {"decision": "block", "reason": reason}


def _error(d, where, e):
    """One line in errors.log: the time, the hook and step, and the exception's type, never its message. The file is
    started again after 100 KB."""
    if not d:
        return
    try:
        path = os.path.join(d, "errors.log")
        try:
            if os.path.getsize(path) > 100_000:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("%s %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), where, type(e).__name__))
    except Exception:
        pass


def _duration(s):
    """'30 minutes', '1 hour', '90 seconds': as the card states it (rule._duration), never longer than `s`."""
    return rule._duration(s)


def _left(s):
    """'11 more hours', '1 more hour', '25 more minutes': how much of a period is left, rounded down."""
    if s >= 3600:
        h = int(s // 3600)
        return "%d more hour%s" % (h, "" if h == 1 else "s")
    m = max(1, int(s // 60))
    return "%d more minute%s" % (m, "" if m == 1 else "s")


def to_json(out):
    """What a hook script prints: one line of JSON, ASCII only (other characters as \\u escapes), as on Windows the
    output passes through PowerShell or cmd.exe, which can recode other text; a lone surrogate (from a log Codex
    didn't write) becomes "?", as Codex's JSON reader refuses its escape and would drop the whole answer."""
    return json.dumps(_scrub(out), ensure_ascii=True)


def _scrub(x):
    if isinstance(x, str):
        return x.encode("utf-8", "replace").decode("utf-8")
    if isinstance(x, dict):
        return {_scrub(k): _scrub(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_scrub(v) for v in x]
    return x


def _text(v):
    return isinstance(v, str) and bool(v.strip())


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
