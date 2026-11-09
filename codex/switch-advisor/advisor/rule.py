"""The after-break rule for Codex: back at a large conversation after a break, is a new conversation started from a
handoff cheaper than re-sending this one? A port of afterBreak and qualityFlags in mod/switch-advisor/hooks/rule.ts,
with Codex's economics:

- No cache-write premium. A request whose prompt cache missed pays the full input price on everything but the
  shared prefix (instructions and tools), which stays cached.
- The cache doesn't expire at a fixed time: the chance that it missed grows with the idle time (miss_curve, measured
  on real logs). Sending here therefore costs an expectation: the cold price with that chance, the warm one otherwise.
- Codex compacts a conversation by itself near the end of its window, which caps how long a large one stays large.

All $ are API-equivalent list prices (prices.py); on a ChatGPT plan they measure how much of your limits a request
uses. Costs cover the resent context only: output and new content cost the same either way. Pure: no files, no
model calls. The prompt hook and the monitor both decide with it.
"""
import math
import re
from dataclasses import dataclass, field

from .prices import rates

REPLY_KEPT = 1500     # the handoff keeps the first 1,500 characters of the last reply (handoff.py)
FILES_LISTED = 12     # ... and lists up to 12 files with uncommitted changes
TEXT_KEPT = 4000      # rollout.facts keeps at most 4,000 characters of a text (rollout.TEXT_MAX), so without its
                      # last_reply_len a longer reply reads as "at least 4,000"
QUESTIONS_KEPT = 3    # the handoff carries the newest 3 questions still waiting for an answer (open_questions) ...
QUESTION_CHARS = 500  # ... each title up to 500 characters (real ones reach about 480, and often end with the
                      # question itself) ...
OPTIONS_KEPT = 8      # ... with up to 8 of its options ...
OPTION_CHARS = 120    # ... each up to 120 characters
_CUT = " […]"         # ends a text that was cut (handoff.CUT)
_ASKS = re.compile(r"\?\s*(\*\*)?\s*$")   # a reply that ends with a question (rollout._ASKS)


@dataclass
class Params:
    base_tokens: int = 27_000            # B: a new conversation's first request
    base_shared: int = 20_000            # of B (and of any conversation) that stays cached even after a miss
    reread_tokens: int = 80_000          # R: files a new conversation reads again (Claude's measured allowance; unverified for Codex)
    reread_worst: int = 150_000
    handoff_tokens: int = 2_000
    calls_between_returns: tuple = (22, 50)   # (p25, p50)
    growth_per_call: int = 900
    compact_share: float = 0.86          # auto-compaction point as a share of model_context_window
    compacted_tokens: int = 39_000
    miss_curve: tuple = ((300, 0.0), (900, 0.07), (1800, 0.11), (2700, 0.19), (3600, 0.19), (5400, 0.25),
                         (7200, 0.39), (14400, 0.58), (21600, 0.64), (43200, 0.76), (86400, 0.92), (float("inf"), 1.0))
                                         # (idle upper bound in s, P(miss)); keep it non-decreasing
    min_idle_s: int = 3600               # never a card or note before an hour idle
    min_miss: float = 0.5                # a card needs P(miss) >= this (about 2 h and longer)
    bar_low: float = 2.0                 # typical saving needed, low quality risk ($, API-equivalent)
    bar_medium: float = 5.0              # ... medium risk (high risk never blocks)
    floor_min: float = 0.5               # the worst case must still save this much
    note_min_usd: float = 0.5            # a note needs the expected re-read to cost at least this
    note_min_miss: float = 0.25
    quiet_s: int = 12 * 3600             # after "send again to stay", no card or note in that conversation for this long
    resend_window_s: int = 900           # a prompt within 15 min of a card counts as "stay here"
    handoff_ttl_s: int = 1800            # a pending handoff is used by a new conversation started within 30 min
    usd_per_pt: float | None = None      # $ per 1% of the weekly limit (from rate.json); None: dollars only


def _num(v):
    """A number a params file may hold: int or float, finite, not a bool."""
    try:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    except OverflowError:                # an int too large for a float
        return False


_COUNTS = ("base_tokens", "base_shared", "reread_tokens", "reread_worst", "handoff_tokens", "growth_per_call",
           "compacted_tokens", "min_idle_s", "quiet_s", "resend_window_s", "handoff_ttl_s")   # tokens or s, >= 0
_DOLLARS = ("bar_low", "bar_medium", "floor_min", "note_min_usd")                              # $, >= 0
_CHANCES = ("min_miss", "note_min_miss")                                                       # 0..1


def params_from(d):
    """Params from params.json (written by `python -m monitor.codex replay --write`): each field it names replaces the
    default. Unknown keys and invalid values are ignored, so a bad file can't stop the hook."""
    P = Params()
    if not isinstance(d, dict):
        return P
    for name in _COUNTS:
        v = d.get(name)
        if _num(v) and v >= 0:
            setattr(P, name, int(round(v)))
    for name in _DOLLARS:
        v = d.get(name)
        if _num(v) and v >= 0:
            setattr(P, name, float(v))
    for name in _CHANCES:
        v = d.get(name)
        if _num(v) and 0 <= v <= 1:
            setattr(P, name, float(v))
    v = d.get("compact_share")
    if _num(v) and 0 < v <= 1:
        P.compact_share = float(v)
    v = d.get("calls_between_returns")
    if isinstance(v, (list, tuple)) and len(v) >= 2 and all(_num(x) and x >= 0 for x in v[:2]):
        P.calls_between_returns = tuple(sorted(v[:2]))
    curve = _curve(d.get("miss_curve"))
    if curve:
        P.miss_curve = curve
    v = d.get("usd_per_pt")
    if _num(v) and v > 0:
        P.usd_per_pt = float(v)
    return P


def _curve(v):
    """miss_curve from JSON, a list of [idle upper bound in s, P(miss)] pairs; the open-ended last bound may be null or
    Infinity. Sorted by bound and made non-decreasing (a longer break never makes a miss less likely). None if any
    pair is malformed or two share a bound."""
    if not isinstance(v, (list, tuple)) or not v:
        return None
    pairs = []
    for item in v:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        upper, p = item
        if upper is None or (isinstance(upper, float) and upper == math.inf):
            upper = math.inf
        elif not (_num(upper) and upper > 0):
            return None
        if not (_num(p) and 0 <= p <= 1):
            return None
        pairs.append((float(upper), float(p)))
    pairs.sort()
    if len({upper for upper, _ in pairs}) < len(pairs):
        return None
    curve, top = [], 0.0
    for upper, p in pairs:
        top = max(top, p)
        curve.append((upper, top))
    return tuple(curve)


def p_miss(idle_s, P):
    """The chance that the prompt cache missed after `idle_s` seconds without a model response: the value of the first
    bucket whose upper bound is above it (beyond every bound, the last bucket's)."""
    for upper, p in P.miss_curve:
        if idle_s < upper:
            return p
    return P.miss_curve[-1][1] if P.miss_curve else 1.0


# ---- what a handoff can't carry -------------------------------------------------------------------------------

@dataclass
class Flag:
    key: str
    level: str           # "high" | "medium" | "info"
    text: str            # user-facing, completes "It may miss: ..."


# The same patterns as rule.ts. re.A makes \w and \b ASCII, as they are in JavaScript.
SHORT_REPLY = re.compile(r"^\s*(yes|ok|okay|sure|continue|go on|go ahead|proceed|do it|next)\b", re.I | re.A)
NAMES_FILE = re.compile(r"[\w./\\-]+\.(md|txt|json|ya?ml|py|kt|kts|ts|tsx|js|sql|gradle|csv|html|docx|xlsx|pptx|pdf)\b",
                        re.I | re.A)


def open_questions(f):
    """The questions the conversation is still waiting for you to answer, as the handoff carries them: rollout.facts'
    open_questions (asked with request_user_input_async, or with request_user_input that has no output yet) as
    [(title, [option, ...]), ...], oldest first. At most the newest 3, each title cut to 500 characters, and at most 8
    options of 120 characters on one line each; a text that was cut ends with " […]" (rollout.facts keeps them whole
    up to 4,000 characters, so the cut shows). Entries with neither a title nor an option are left out, and so is
    anything that isn't a {"title": str, "options": [str, ...]} dict; [] for facts without the field."""
    return _all_open(f)[-QUESTIONS_KEPT:]


def open_question_total(f):
    """How many questions the conversation is still waiting for you to answer: rollout.facts' open_question_count,
    which also counts the older ones its open_questions leaves out; at least as many as open_questions holds."""
    n = getattr(f, "open_question_count", None)
    held = len(_all_open(f))
    return n if isinstance(n, int) and not isinstance(n, bool) and n > held else held


def _all_open(f):
    """open_questions(f) before the newest QUESTIONS_KEPT are taken."""
    asked = getattr(f, "open_questions", None)
    if not isinstance(asked, (list, tuple)):
        return []
    out = []
    for q in asked:
        if not isinstance(q, dict):
            continue
        title = q.get("title")
        title = _clip(title.strip(), QUESTION_CHARS) if isinstance(title, str) else ""
        given = q.get("options")
        options = []
        for o in (given if isinstance(given, (list, tuple)) else []):
            if len(options) == OPTIONS_KEPT:
                break
            if isinstance(o, str) and o.strip():
                options.append(_clip(" ".join(o.split()), OPTION_CHARS))
        if title or options:
            out.append((title, options))
    return out


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit].rstrip() + _CUT


def _awaiting(questions, total=0):
    """The awaiting-answer flag's text. With open questions the handoff carries them, and their options: the newest
    QUESTIONS_KEPT when `total`, the number still open, is larger."""
    if not questions:
        return "your message answers its last reply; the handoff carries that reply, not what was said before it"
    options = any(opts for _title, opts in questions)
    left = total - len(questions)
    if left > 0:
        if len(questions) == 1:
            carried = "the newest one and its options" if options else "the newest one"
        else:
            carried = f"the newest {len(questions)} and their options" if options else f"the newest {len(questions)}"
        earlier = "the earlier one" if left == 1 else f"the {left} earlier ones"
        return (f"your message answers the {total} questions it asked you; the handoff carries {carried}, not "
                f"{earlier} or what was said before them")
    if len(questions) == 1:
        carried = "that question and its options" if options else "that question"
        return (f"your message answers the question it asked you; the handoff carries {carried}, not what was said "
                f"before it")
    carried = "those questions and their options" if options else "those questions"
    return (f"your message answers the {len(questions)} questions it asked you; the handoff carries {carried}, not "
            f"what was said before them")


def quality_flags(f, prompt, uncommitted):
    """What a new conversation might miss, from the conversation's facts (rollout.facts: asks_you, reply_asks,
    open_questions, last_reply, last_reply_len, agents_running, agents_spawned), the prompt being sent and the number
    of files with uncommitted changes.

    A long reply is a high risk only when the reply itself asks something: the handoff keeps its first 1,500
    characters, and the question may come after them. A question asked in Codex's question panel is carried whole
    (open_questions), so with one of those and a long reply that asks nothing, the flag is the medium awaiting-answer
    one, which says the handoff carries it. A question in the panel that the plugin can't read (rollout.facts'
    unread_calls: arguments in a shape it doesn't know) is a high risk, whatever the reply or the other questions say:
    the handoff can't carry it. For Facts made by hand, without unread_calls, that is a conversation that asks you
    something (asks_you) with neither a question the handoff can carry nor a reply that asks it."""
    reply = (getattr(f, "last_reply", None) or "").strip()
    whole = getattr(f, "last_reply_len", None)      # the whole reply's length: last_reply keeps TEXT_KEPT of it
    exact = _num(whole) and whole >= len(reply)
    length = int(whole) if exact else len(reply)
    questions = open_questions(f)
    asked = bool(getattr(f, "asks_you", False))
    asks = asked or bool(questions)
    reply_asks = getattr(f, "reply_asks", None)
    if not isinstance(reply_asks, bool):
        # not measured (Facts made by hand, or no reply): asks_you is the reply's own question, unless there is no
        # reply, or open questions explain it and the reply doesn't end with one
        reply_asks = asked and bool(reply) and (not questions or bool(_ASKS.search(reply)))
    unread_calls = getattr(f, "unread_calls", None)
    if isinstance(unread_calls, int) and not isinstance(unread_calls, bool):
        unread = unread_calls > 0
    else:
        unread = asked and not questions and not reply_asks
    running = getattr(f, "agents_running", 0) or 0
    spawned = getattr(f, "agents_spawned", 0) or 0
    prompt = prompt or ""
    uncommitted = uncommitted or 0
    flags = []
    if running > 0:
        flags.append(Flag("agents-running", "high", "agents started in this conversation are still running, and their "
                                                    "results come back here, not to the new conversation"))
    if reply_asks and length > REPLY_KEPT:
        size = f"{length:,}" if exact or length < TEXT_KEPT else f"at least {length:,}"
        flags.append(Flag("long-question", "high", f"its last reply asks you something and runs {size} characters, "
                                                   f"and the handoff keeps the first {REPLY_KEPT:,}"))
    elif unread:
        flags.append(Flag("unread-question", "high", "it asked you something in the question panel that the handoff "
                                                     "can't read, so the new conversation wouldn't see the question"))
    elif asks or (not NAMES_FILE.search(prompt) and (len(prompt.strip()) < 100 or SHORT_REPLY.search(prompt))):
        flags.append(Flag("awaiting-answer", "medium", _awaiting(questions, open_question_total(f))))
    if spawned > 0 and running == 0:
        if spawned == 1:
            text = ("this conversation coordinated 1 agent; its report is only in this conversation, and the handoff "
                    "keeps the last reply")
        else:
            text = (f"this conversation coordinated {spawned} agents; their reports are only in this conversation, and "
                    f"the handoff keeps its last reply")
        flags.append(Flag("agents-used", "medium", text))
    if uncommitted > FILES_LISTED:
        flags.append(Flag("many-files", "medium", f"{uncommitted} files have uncommitted changes, and the handoff "
                                                  f"lists {FILES_LISTED}"))
    elif uncommitted == 1:
        flags.append(Flag("uncommitted", "info", "1 file has uncommitted changes; the new conversation sees it with "
                                                 "git status"))
    elif uncommitted > 1:
        flags.append(Flag("uncommitted", "info", f"{uncommitted} files have uncommitted changes; the new conversation "
                                                 f"sees them with git status"))
    return flags


def level_of(flags):
    levels = {f.level for f in flags}
    return "high" if "high" in levels else "medium" if "medium" in levels else "low"


# ---- the card after a break -----------------------------------------------------------------------------------

@dataclass
class Card:
    show: bool                # block the prompt with `text`
    text: str                 # the block reason (plain text, paragraphs separated by blank lines)
    note: str                 # a one-line systemMessage when no card is shown ("" for nothing)
    risk: str = "low"
    flags: list = field(default_factory=list)      # flag keys
    numbers: dict = field(default_factory=dict)    # see after_break
    raw: dict = field(default_factory=dict)        # numbers' figures unrounded (not logged; see after_break)
    failed: list = field(default_factory=list)     # the conditions for a card that failed (see after_break)


def _cold(tokens, pin, pc, P):
    """One request whose prompt cache missed: the shared prefix is still read from cache, the rest pays full input."""
    shared = min(P.base_shared, tokens)
    return shared * pc + (tokens - shared) * pin


def after_break(ctx, idle_s, flags, P, model, window=None, tier=None, meter_pct=None, label=""):
    """Back after `idle_s` seconds to a conversation whose next request resends `ctx` tokens: block the prompt with a
    card (`show`), tell its cost in a one-line `note`, or say nothing.

    A card needs a break of at least an hour, a likely cache miss, a saving even in the worst case (now and by the
    next break), and a typical saving above the bar for the quality risk. High risk never blocks: the note then says
    what a switch would save. `text` is built whenever the request has a price, so `switch-advisor test` can show it;
    only `show` blocks. `label` ("TEST · " or "DEMO · ") goes before the first sentence, and a labelled card that
    wouldn't show says so. The card and the note both give what this message costs if the cache missed, with the
    chance of that; the card adds the average over both cases, which its saving is counted from.

    numbers ($ rounded to 4 decimals): resend_here (expected), resend_cold (if the cache missed), resend_warm,
    new_conv, new_worst, save_now, typical [p25, p50 calls], floor (the worst case now), floor_typical (the worst
    case by the next break: the new conversation re-read reread_worst, so its later calls are that much larger),
    per_call, after_call (per call once Codex has compacted this conversation), calls_left (None: no window known),
    p_miss, ctx, idle_s, model.
    raw: the same keys, unrounded (calls_left is None without a window here too), for a reader that formats the
    figures itself; it is not logged and may hold values JSON can't.
    failed: the conditions for a card that failed, from the unrounded figures, in this order: "idle" (idle_s <
    min_idle_s), "miss" (p_miss < min_miss), "floor" (floor < floor_min), "floor_typical" (min(floor_typical) <
    floor_min), "typical" (min(typical) < the bar for the risk); then "risk" when the rest held but the risk is high.
    Empty when the card shows.
    numbers, raw and failed are empty when there is nothing to price (ctx <= 0, or no known price for the model)."""
    flags = list(flags or [])
    risk, keys, label = level_of(flags), [f.key for f in flags], label or ""
    r = rates(model, ctx, tier) if _num(ctx) and ctx > 0 and _num(idle_s) else None
    if r is None:
        return Card(False, "", "", risk, keys, {})
    q = p_miss(idle_s, P)
    pin, pc = r[:2]
    warm = ctx * pc
    cold = _cold(ctx, pin, pc, P)
    here = q * cold + (1 - q) * warm                                  # expected: the cache missed with chance q
    n_new = P.base_tokens + P.reread_tokens + P.handoff_tokens        # N: a new conversation once it has re-read
    n_worst = P.base_tokens + P.reread_worst + P.handoff_tokens       # NW: ... if it re-reads the worst case
    pin_n, pc_n = rates(model, n_new, tier)[:2]                       # every request at its own size's rates
    pin_w, pc_w = rates(model, n_worst, tier)[:2]
    new_conv = _cold(n_new, pin_n, pc_n, P)
    new_worst = _cold(n_worst, pin_w, pc_w, P)
    save_now = here - new_conv
    floor = here - new_worst
    left = math.inf                                                   # calls until Codex compacts this conversation
    if _num(window) and window > 0:
        left = max(0.0, (P.compact_share * window - ctx) / max(P.growth_per_call, 1))
    per_call = warm - n_new * pc_n                # what each later call re-reads from cache, here minus there
    pc_c = rates(model, P.compacted_tokens, tier)[1]
    after_call = P.compacted_tokens * pc_c - n_new * pc_n             # ... once Codex has compacted this one, which
    # then is the smaller of the two: those calls count against the switch (the compaction's own request is left out,
    # which keeps the estimate on the cautious side)
    typical = [save_now + min(n, left) * per_call + max(0.0, n - left) * after_call for n in P.calls_between_returns]
    # the worst case over the same calls: the new conversation re-read reread_worst, and every later call re-reads it
    per_worst = warm - n_worst * pc_w
    after_worst = P.compacted_tokens * pc_c - n_worst * pc_w
    floor_typical = [floor + min(n, left) * per_worst + max(0.0, n - left) * after_worst
                     for n in P.calls_between_returns]
    bar = P.bar_low if risk == "low" else P.bar_medium
    # min: past the compaction point more calls mean less saving, so neither end of a range may fall short. A card
    # pays when every condition holds; `not x >= y` rather than `x < y` keeps a NaN from passing one.
    failed = [name for name, held in (("idle", idle_s >= P.min_idle_s), ("miss", q >= P.min_miss),
                                      ("floor", floor >= P.floor_min),
                                      ("floor_typical", min(floor_typical) >= P.floor_min),
                                      ("typical", min(typical) >= bar)) if not held]
    pays = not failed
    show = pays and risk != "high"
    if pays and not show:
        failed.append("risk")
    capped = left < P.calls_between_returns[-1]       # compaction comes before the next break would end

    lead = label
    if label and not show:
        lead += ("A switch would pay here, but the quality risk is too high for a card; this is what it would look "
                 "like. " if pays else "A switch wouldn't pay here; this is what the card would look like. ")
    expired = "has most likely expired" if q >= 0.5 else "may have expired"
    # the cost of a miss, as the note gives it, then the average over both cases, which the saving is counted from
    average = "" if _cents(here) == _cents(cold) else f", so about {_usd(here)} on average"
    opening = (f"{lead}Switch advisor: you were away {_hours(idle_s)}, so this conversation's prompt cache {expired} "
               f"(it had after {q * 100:.0f}% of breaks this long). Sending here re-reads about {_tokens(ctx)} tokens: "
               f"about {_usd(cold)} if the cache expired{_weekly(cold, P, meter_pct)}, against {_usd(warm)} with a "
               f"warm cache{average}.")
    switch = (f"A new conversation starts from a handoff built from this one on your computer, with no model call, and "
              f"with about {_tokens(n_new)} tokens once it has re-read its files: about {_usd(new_conv)}. "
              f"{_savings(save_now, typical)}{_compaction(left, after_call, P)} "
              f"{_worst(floor, floor_typical, capped, P)}")
    how = (f"To switch: start a new conversation in this project folder and send your prompt there within "
           f"{_duration(P.handoff_ttl_s)}; it starts from the handoff. To stay: send your prompt here again within "
           f"{_duration(P.resend_window_s)}, and this conversation won't be checked again for {_duration(P.quiet_s)}.")
    text = "\n\n".join([opening, switch, _quality(risk, flags), how])

    note = ""
    if not show and idle_s >= P.min_idle_s and (pays or (q >= P.note_min_miss and here >= P.note_min_usd)):
        # what a miss costs, next to its chance: the expected cost (here) would read as if it were the miss's
        likely = "likely re-reads" if q >= 0.5 else "may re-read"
        note = (f"{label}Switch advisor: back after {_hours(idle_s)}, this message {likely} about {_tokens(ctx)} tokens "
                f"without the cache (it had expired after {q * 100:.0f}% of breaks this long): about {_usd(cold)}"
                f"{_weekly(cold, P, meter_pct)}, against {_usd(warm)} with a warm cache.")
        if pays:                          # only the quality risk kept the card away
            first_high = next(f.text for f in flags if f.level == "high")
            note += (f" A new conversation would save about {_usd(save_now)} on average, but the quality risk is high: "
                     f"{first_high}.")

    numbers = {
        "resend_here": _r4(here), "resend_cold": _r4(cold), "resend_warm": _r4(warm), "new_conv": _r4(new_conv),
        "new_worst": _r4(new_worst), "save_now": _r4(save_now), "typical": [_r4(t) for t in typical],
        "floor": _r4(floor), "floor_typical": [_r4(t) for t in floor_typical], "per_call": _r4(per_call),
        "after_call": _r4(after_call), "calls_left": None if left == math.inf else _r4(left),
        "p_miss": _r4(q), "ctx": int(ctx), "idle_s": _r4(idle_s), "model": model,
    }
    raw = {
        "resend_here": here, "resend_cold": cold, "resend_warm": warm, "new_conv": new_conv, "new_worst": new_worst,
        "save_now": save_now, "typical": list(typical), "floor": floor, "floor_typical": list(floor_typical),
        "per_call": per_call, "after_call": after_call, "calls_left": None if left == math.inf else left,
        "p_miss": q, "ctx": ctx, "idle_s": idle_s, "model": model,
    }
    return Card(show, text, note, risk, keys, numbers, raw, failed)


CARRIES = "carries your first and latest requests, the last reply and the uncommitted files"


def _quality(risk, flags):
    """The quality paragraph; info flags follow in parentheses."""
    info = "".join(f" ({f.text}.)" for f in flags if f.level == "info")
    if risk == "low":
        return (f"Quality risk: low. Its last reply doesn't ask you anything, no agents are running, and the handoff "
                f"{CARRIES}.{info}")
    misses = "; ".join(f.text for f in flags if f.level != "info")
    return f"Quality risk: {risk}. The handoff {CARRIES}. It may miss: {misses}.{info}"


def _savings(now, typical):
    """'That saves about $5.25 now and typically $20.89–$40.80 by your next break.' A labelled test card can show a
    switch that costs more, so a negative figure reads as a cost, never as '$-0.30'."""
    lo, hi = sorted(typical)
    if _cents(now) >= 0 and _cents(lo) >= 0:
        return f"That saves about {_usd(now)} now and typically {_span(lo, hi)} by your next break."
    if _cents(now) < 0 and _cents(hi) < 0:
        return f"That costs about {_usd(now)} more now and typically {_span(hi, lo)} more by your next break."
    return (f"Against sending here, that is about {_more_less(now)} now and typically {_more_less(lo)} to "
            f"{_more_less(hi)} by your next break.")


def _compaction(left, after_call, P):
    """Why the typical saving stops growing: Codex compacts this conversation before the next break would end, and
    from then on it is usually the smaller of the two."""
    if left >= P.calls_between_returns[-1]:
        return ""
    n = max(1, round(left))
    calls = "1 call" if n == 1 else f"{n} calls"
    if _cents(after_call) < 0:
        return (f" Codex compacts this conversation by itself within about {calls}; after that it is smaller than the "
                f"new one, so the typical figure counts the later calls against the switch.")
    return (f" Codex compacts this conversation by itself within about {calls}; the typical figure counts the later "
            f"calls at its compacted size.")


def _worst(now, later, capped, P):
    """'Even if it re-reads 150K tokens of files, it saves about $4.55.' When Codex compacts this conversation before
    the next break, the typical figure can fall below the saving now, so the worst case says where it ends by then
    too (a larger new conversation re-reads more on each of those calls)."""
    head = f"Even if it re-reads {_tokens(P.reread_worst)} tokens of files, "
    if not capped:
        return head + f"it {_change(now)}."
    lo, hi = sorted(later)
    if _cents(now) >= 0 and _cents(lo) >= 0:
        return head + f"it saves about {_usd(now)} now and typically {_span(lo, hi)} by your next break."
    if _cents(now) < 0 and _cents(hi) < 0:
        return head + f"it costs about {_usd(now)} more now and typically {_span(hi, lo)} more by your next break."
    return head + (f"against sending here it is about {_more_less(now)} now and typically {_more_less(lo)} to "
                   f"{_more_less(hi)} by your next break.")


def _change(x):
    return f"saves about {_usd(x)}" if _cents(x) >= 0 else f"costs about {_usd(x)} more"


def _more_less(x):
    return f"{_usd(x)} less" if _cents(x) >= 0 else f"{_usd(x)} more"


def _span(a, b):
    """'$20.89–$40.80' between two amounts of the same sign; one amount when they round alike."""
    return _usd(a) if _cents(a) == _cents(b) else f"{_usd(a)}–{_usd(b)}"


def _weekly(cost, P, meter_pct):
    """' (1.2% of your weekly limit; you're at 64%)' with whichever part is known; '' when neither is."""
    share = cost / P.usd_per_pt if _num(P.usd_per_pt) and P.usd_per_pt > 0 else None
    at = f"{meter_pct:.0f}%" if _num(meter_pct) and meter_pct >= 0 else None
    if share is not None:
        part = "<0.1%" if share < 0.1 else f"{share:.1f}%"
        return f" ({part} of your weekly limit; you're at {at})" if at else f" ({part} of your weekly limit)"
    return f" (you're at {at} of your weekly limit)" if at else ""


def _cents(x):
    return round(x, 2) + 0.0              # + 0.0 turns -0.0 into 0.0


def _usd(x):
    """$5.25 for an amount of either sign: the words around it say which way it goes."""
    return f"${abs(_cents(x)):.2f}"


def _tokens(n):
    """410K; the exact count under 1,000."""
    return f"{int(n):,}" if n < 1000 else f"{int(n / 1000 + 0.5):,}K"


def _hours(s):
    return f"{s / 3600:.1f} h"


def _duration(s):
    """A time the card states, never longer than `s` seconds: the hook keeps to `s` exactly. In seconds under 2 minutes
    ('90 seconds'), else in whole minutes rounded down ('61 minutes'); in hours only when they are exact ('1 hour',
    '12 hours', '1.5 hours')."""
    s = max(0, int(s))
    if s < 120:
        return "1 second" if s == 1 else f"{s} seconds"
    if s >= 3600 and s % 1800 == 0:
        h = f"{s // 3600}" if s % 3600 == 0 else f"{s / 3600:.1f}"
        return "1 hour" if h == "1" else f"{h} hours"
    return f"{s // 60} minutes"


def _r4(x):
    return round(x, 4)
