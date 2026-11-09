"""Codex in the monitor: your weekly limit, what your Codex conversations cost, and the switch advisor's rule replayed
on your own Codex logs. Local; no model calls. Only `replay --write` writes anything (see below).

Usage: python -m monitor.codex [now|spend|replay|handoff] [options]        (default: now)

  now [--hours 24] [--hide-titles]
      Your weekly limit, then each main conversation with a request in the last hours: its size, idle time, what the
      next message re-reads, and what the switch advisor's rule says now.
  spend [--days 7]
      API-equivalent $ per day for main conversations, subagents and approval reviews, and the Codex credits.
  replay [--write]
      Every return to a main conversation after a break of 1 h or more: the rule's verdict then, its cost prediction
      against what the request cost, and what a switch would have saved by the next return. It also measures the
      rule's inputs from your logs and reads the plugin's decision logs. --write saves the inputs to
      data/codex_params.json in this checkout and to params.json in the plugin's data folder, turns the plugin's
      cards off (guardrail.json) once 4 or more switches were made and half or more of them went back to the old
      conversation within 24 h, and deletes handoffs in the plugin's data folder that nobody used in time. To turn
      the cards on again, set after_break_enabled to true in guardrail.json: the replay then leaves them on.
      Deleting the file instead lets it turn them off again while the switches still meet that rule.
  handoff [--session ID]
      Prints the handoff a new conversation would start from, for the newest main conversation or the given thread
      id. It prints your conversation's text to the terminal and saves nothing.

The logs are read with the plugin's own code (codex/switch-advisor/advisor), so parsing, prices and the rule have one
source. $ are API list prices: on a ChatGPT plan they measure how much of your limits a request uses.
"""
import argparse
import glob
import json
import math
import os
import statistics
import sys
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(HERE, "codex", "switch-advisor")
if PLUGIN not in sys.path:
    sys.path.insert(0, PLUGIN)

from advisor import hook, prices, rate, rollout, rule, state  # noqa: E402  (the plugin package, from this checkout)

DATA = os.path.join(HERE, "data")   # this checkout's local data folder (never published)

RETURN_S = 3600                     # replay: a return is a request after a break of at least 1 h
LONG_BREAK_S = 2 * 3600             # calls_between_returns counts from returns after 2 h or more
MISS_IDLE_S = 6 * 3600              # base_shared: requests after 6 h or more whose cache missed
MIN_INPUT = 20_000                  # growth and cache samples: requests at least this large
MIN_SAMPLES = 5                     # a measured input needs this many samples, else the plugin's default stays
MIN_BUCKET = 10                     # a miss-curve bucket needs this many requests, else its default stays
RECENT_DAYS = 14                    # inputs that follow Codex's own behaviour are measured on this many recent days
FOLLOWS_CODEX = ("base_tokens", "base_shared", "compact_share", "compacted_tokens")
NEAR = 15                           # replay: a return's new conversation compacts as these many compactions nearest
                                    # in time did
CREDITS_PER_USD = 25                # Codex credits per $ of API price
GUARD_SWITCHES = 4                  # the guardrail needs at least this many switches
WENT_BACK_S = 24 * 3600             # ...and counts a switch as gone back when the old thread got a request this soon
ASSUMED = rule.Flag("assumed-medium", "medium", "the replay can't see the prompt or the last reply")


# ---- small helpers -------------------------------------------------------------------------------------------------

def q(xs, p):
    """The p-quantile of xs as the plugin's default inputs were measured (the lower nearest rank), or None."""
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))] if xs else None


def usd(x):
    """$1,234.56; a negative amount reads -$0.27."""
    x = round(x, 2) + 0.0
    return ("-$" if x < 0 else "$") + f"{abs(x):,.2f}"


def tokens(n):
    """410K; the exact count under 1,000."""
    return f"{n / 1000:,.0f}K" if n >= 1000 else f"{n:,}"


def span(s):
    """An idle time: '42 min', '3.1 h'."""
    return f"{s / 60:.0f} min" if s < 3600 else f"{s / 3600:.1f} h"


def at(t, fmt="%a %H:%M"):
    """A local time."""
    return datetime.fromtimestamp(t).strftime(fmt)


def of_week(x, P):
    """'1.2% of your weekly limit' at P.usd_per_pt, or '' while the rate isn't known."""
    if not P.usd_per_pt:
        return ""
    share = x / P.usd_per_pt
    return ("<0.1%" if share < 0.1 else f"{share:.1f}%") + " of your weekly limit"


def plural(n, word, many=None):
    """'1 card', '3 cards'; `many` for a plural that isn't word + s."""
    return f"{n:,} {word}" if n == 1 else f"{n:,} {many or word + 's'}"


def _skipped(stats):
    """Says once, on stderr, that compressed logs were left out."""
    n = stats.get("zst_skipped", 0)
    if n:
        print(f"Left out {plural(n, 'compressed Codex log')} (.jsonl.zst) that may hold records from the days "
              f"read: reading {'it' if n == 1 else 'them'} needs Python 3.14 or later.", file=sys.stderr)


def _handoff():
    """The plugin's handoff module (handoff text and git status), or None while it isn't there."""
    try:
        from advisor import handoff
    except ImportError:
        return None
    return handoff


# ---- the plugin's data folder ----------------------------------------------------------------------------------------

def plugin_dirs(home=None):
    """The switch-advisor plugin's data folders under CODEX_HOME/plugins/data/. Codex names the folder itself, so this
    takes every folder whose name contains switch-advisor."""
    base = glob.escape(os.path.join(home or rollout.codex_home(), "plugins", "data"))
    return sorted(p for p in glob.glob(os.path.join(base, "*switch-advisor*")) if os.path.isdir(p))


def saved_params(home=None, data=None):
    """(the params the plugin reads, where they came from): params.json in its data folder, else data/codex_params.json
    in this checkout, else {} (the plugin's defaults)."""
    for d in plugin_dirs(home):
        p = state.load_params(d)
        if p is not None:
            return p, "params.json in the plugin's data folder"
    p = state.read_json(os.path.join(data or DATA, "codex_params.json"))
    if p is not None:
        return p, "data/codex_params.json"
    return {}, "the plugin's defaults"


def cards_off(home=None):
    """True when the guardrail has turned the plugin's cards off (guardrail.json in its data folder)."""
    return any(state.guardrail_off(d) for d in plugin_dirs(home))


def cards_kept_on(home=None):
    """True when you set after_break_enabled to true in the plugin's guardrail.json by hand (the replay only ever
    writes false): the guardrail then leaves the cards on."""
    return any((state.read_json(os.path.join(d, "guardrail.json")) or {}).get("after_break_enabled") is True
               for d in plugin_dirs(home))


def handoff_waiting(home, cwd, sid, now, P):
    """True when another conversation's card's handoff for the folder `cwd` is waiting in the plugin's data folder, by
    the prompt hook's own test (hook._waiting: a test's handoff doesn't count, nor one past its use, state.stale): the
    prompt hook then gives the note, since a card would replace that handoff."""
    return bool(cwd) and any(hook._waiting(d, cwd, sid, now, P) for d in plugin_dirs(home))


def session_held(home, sid, now):
    """Why the prompt hook doesn't check conversation `sid` now, from what the plugin remembers of it
    (sessions/<id>.json in each data folder, read only), in words; '' when nothing holds it. A card was shown within
    resend_window_s, so a prompt now counts as staying; or you chose to stay, so it isn't checked until quiet_until.
    The hook checks these before anything else."""
    for d in plugin_dirs(home) if sid else []:
        s = state.session(d, sid)
        offered, quiet = s.get("offered_at"), s.get("quiet_until")
        if _number(offered) and 0 <= now - offered <= rule.params_from(state.load_params(d)).resend_window_s:
            return f"a card was shown here at {at(offered)}, so a prompt now counts as staying"
        if _number(quiet) and quiet > now:
            return f"you chose to stay, so this conversation isn't checked until {at(quiet)}"
    return ""


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _epoch(v):
    """A decision's time: epoch seconds (milliseconds are recognised) or an ISO-8601 text; None otherwise."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000 if v > 1e11 else float(v)
    try:
        return rollout.parse_ts(v)
    except ValueError:
        return None


def read_decisions(dirs):
    """Every event in the plugin's decision logs (decisions/*.jsonl in its data folders), oldest first. The logs hold
    numbers, flag keys and ids only."""
    events = []
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(glob.escape(d), "decisions", "*.jsonl"))):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            for line in lines:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t = _epoch(e.get("t")) if isinstance(e, dict) else None
                if t is not None and isinstance(e.get("event"), str):
                    e["t"] = t
                    events.append(e)
    events.sort(key=lambda e: e["t"])
    return events


def went_back(events, threads):
    """(switches, how many of them went back): a switch went back when its old thread got a new request within 24 h.
    A switch tried after `switch-advisor test` ("test": true) isn't counted: going back after a test is what a test
    is for, and it must not turn the cards off."""
    switches = [e for e in events if e["event"] == "switched" and e.get("test") is not True]
    back = 0
    for e in switches:
        old = threads.get(e.get("from_session")) if isinstance(e.get("from_session"), str) else None
        if old is not None and any(e["t"] < (c.start if c.start is not None else c.end) <= e["t"] + WENT_BACK_S
                                   for c in old.calls):
            back += 1
    return len(switches), back


# ---- now -----------------------------------------------------------------------------------------------------------

def limit_line(m, now):
    """'Codex weekly limit: 41% used, resets Thu 14:05.' from the newest reading, with the 5-hour window when the plan
    has one. A window that has reset since the reading says so."""
    if m is None:
        return "Codex weekly limit: no reading in these logs."
    parts = []
    for name, pct, resets in (("weekly limit", m.weekly_pct, m.weekly_resets),
                              ("5-hour limit", m.five_h_pct, m.five_h_resets)):
        if pct is None:
            continue
        if resets is not None and resets <= now:
            parts.append(f"{name}: {pct:.0f}% used at the last reading, but it has reset since ({at(resets)})")
        elif resets is not None:
            parts.append(f"{name}: {pct:.0f}% used, resets {at(resets)}")
        else:
            parts.append(f"{name}: {pct:.0f}% used")
    if not parts:
        return "Codex weekly limit: no reading in these logs."
    return "Codex " + ". ".join(parts) + f". (Last reading: {at(m.t)}.)"


def why_no_card(card, P, idle):
    """The rule's conditions a card failed, in words: card.failed, the list rule.after_break keeps of them (computed
    from its unrounded figures). A card without that list is checked here the same way, on card.raw (unrounded) or
    else card.numbers (rounded to 4 decimals); when no condition shows as failed there, a figure missed its threshold
    by less than the rounding, and the reason says so."""
    failed = getattr(card, "failed", None)
    if not (isinstance(failed, (list, tuple)) and failed):
        n = getattr(card, "raw", None) or card.numbers
        bar = P.bar_low if card.risk == "low" else P.bar_medium
        failed = [key for key, fails in (
            ("idle", idle < P.min_idle_s), ("miss", n["p_miss"] < P.min_miss), ("floor", n["floor"] < P.floor_min),
            ("floor_typical", min(n["floor_typical"]) < P.floor_min), ("typical", min(n["typical"]) < bar),
            ("risk", card.risk == "high")) if fails]
    out = []
    if "idle" in failed:
        out.append(f"the break is under {span(P.min_idle_s)}")
    if "miss" in failed:
        out.append(f"P(cache missed) is under {P.min_miss:.2f}")
    worst = f"if it re-reads {tokens(P.reread_worst)} tokens of files, it saves under {usd(P.floor_min)}"
    if "floor" in failed:
        out.append(worst)
    elif "floor_typical" in failed:
        out.append(worst + " by your next break")
    if "typical" in failed:
        bar = P.bar_low if card.risk == "low" else P.bar_medium
        out.append(f"the typical saving is under the {usd(bar)} bar for {card.risk} risk")
    if "risk" in failed or card.risk == "high":
        out.append("the quality risk is high")
    if not out:
        out.append("one of its figures falls short of what a card needs by less than a hundredth of a cent")
    return out


def _range(lo, hi):
    """'$4.67 to $6.63'; one figure when both round alike."""
    return usd(lo) if usd(lo) == usd(hi) else f"{usd(lo)} to {usd(hi)}"


def show_thread(th, title, P, now, home, ho, off):
    """One conversation's lines for `now`, in the prompt hook's terms: its snapshot, facts and flags, and what the
    plugin remembers of it. The figures are the card's own: unrounded (card.raw) when the rule gives them, then
    rounded to cents once, as the card rounds them."""
    snap = rollout.snapshot(th.file, now=now)
    print(f"\nCodex {th.id[:8]}" + (f" “{title}”" if title else ""))
    if snap.last_call_end is None:
        print("  No model response in its log yet.")
        return
    idle = max(0.0, now - snap.last_call_end)
    window = f" (window {tokens(snap.window)})" if snap.window else ""
    print(f"  {tokens(snap.ctx)} tokens of context{window} · last reply {at(snap.last_call_end)}, {span(idle)} ago · "
          f"{snap.model or 'model unknown'}" + (f" ({snap.tier})" if snap.tier and snap.tier != "default" else ""))
    if snap.compacted_since_last_call:
        print("  Codex compacted it after its last reply, so the next message starts small: the switch advisor stays "
              "quiet.")
        return
    if getattr(snap, "turn_running", False):
        print("  Its latest turn is still running: Codex calls the model again when its tool returns, whatever you "
              "send, so the switch advisor stays quiet.")
        return
    f = rollout.facts(th.file, home=home, now=now)
    cwd = f.cwd or th.cwd
    files = ho.git_status(cwd) if ho is not None and cwd else []
    flags = rule.quality_flags(f, "", len(files))
    card = rule.after_break(snap.ctx, idle, flags, P, snap.model, snap.window, snap.tier)
    if not card.numbers:
        print("  The switch advisor can't price it: " + ("no request yet." if snap.ctx <= 0 else
                                                          f"no price is known for {snap.model or 'its model'}."))
        return
    n = getattr(card, "raw", None) or card.numbers
    week = of_week(n["resend_here"], P)
    print(f"  Your next message here re-reads it: about {usd(n['resend_here'])} expected"
          + (f" ({week})" if week else "") + f", with P(cache missed) {n['p_miss']:.2f} after a break this long "
          f"({usd(n['resend_cold'])} if it missed); {usd(n['resend_warm'])} with a warm cache.")
    lo, hi = sorted(n["typical"])          # past Codex's compaction point, more calls can mean less
    typical = _range(lo, hi)
    worst = f"if it re-reads {tokens(P.reread_worst)} tokens of files"
    figures = [n["save_now"], lo, n["floor"]]
    if n["calls_left"] is not None and n["calls_left"] < P.calls_between_returns[-1]:
        # Codex compacts it before the next break: the worst case by then, as the card says it
        wlo, whi = sorted(n["floor_typical"])
        worst = f"{usd(n['floor'])} now and {_range(wlo, whi)} by your next break {worst}"
        figures.append(wlo)
    else:
        worst = f"{usd(n['floor'])} {worst}"
    if min(round(x, 2) + 0.0 for x in figures) >= 0:      # the sign of what is shown: -$0.0025 reads $0.00
        saving = (f"A new conversation would save {usd(n['save_now'])} now and typically {typical} by your next "
                  f"break; {worst}.")
    else:
        saving = (f"What a new conversation would save (negative: it costs more): {usd(n['save_now'])} now, typically "
                  f"{typical} by your next break, {worst}.")
    stay = session_held(home, th.id, now)      # the hook checks these first, and then says nothing
    if card.show:
        verdict = "a card: it holds your prompt back and offers a new conversation"
        held = ("" if stay else "the guardrail has turned the cards off" if off else
                "its log holds no typed request for the handoff to carry" if ho is not None and not ho.requests(f)
                else "another conversation's handoff is waiting in this project folder"
                if handoff_waiting(home, cwd, th.id, now, P) else "")
        if stay:
            verdict += f", but {stay}"
        elif held:    # the prompt hook then gives what the rule says with the card turned off
            noted = rule.after_break(snap.ctx, idle, flags, replace(P, bar_low=math.inf, bar_medium=math.inf),
                                     snap.model, snap.window, snap.tier).note
            verdict += (f", but {held}, so " +
                        ("your prompt goes through with the note instead" if noted else "nothing is shown"))
    elif card.note:
        verdict = "a note: your prompt goes through, with what it re-reads" + (f", but {stay}" if stay else "")
    else:
        verdict = "nothing"
    reasons = [] if card.show else why_no_card(card, P, idle)
    print(f"  The rule now: {verdict}." + (f" No card, because {'; '.join(reasons)}." if reasons else ""))
    print(f"  {saving}")
    risk = f"  Quality risk: {card.risk}" + (f" ({', '.join(card.flags)})." if card.flags else ".")
    if "awaiting-answer" in card.flags and not f.asks_you:
        risk += " The card also weighs your prompt: here it is taken as a short reply to the last one."
    print(risk)


def newest_meter(home, threads):
    """The newest weekly-limit reading: from the logs written to last (rollout.latest_meter, as the prompt hook
    reads it) or from these threads, whichever is newer; else these threads' newest reading of any kind."""
    loaded = sorted((m for th in threads.values() for m in th.meters), key=lambda m: m.t)
    weekly = [m for m in (rollout.latest_meter(home), next((m for m in reversed(loaded) if m.weekly_pct is not None),
                                                           None)) if m is not None]
    if weekly:
        return max(weekly, key=lambda m: m.t)
    return loaded[-1] if loaded else None


def show_now(hours=24.0, hide_titles=False, home=None, now=None):
    """`now`: the weekly limit, from its newest reading however old (the week may still be on), and each main
    conversation with a request in the last `hours`."""
    home = home or rollout.codex_home()
    now = time.time() if now is None else now
    since = now - hours * 3600
    threads, stats = rollout.load(home, since=since)
    _skipped(stats)
    print(limit_line(newest_meter(home, threads), now))
    measured = rate.compute_rate(home, now)
    if measured:
        print(f"1% of it is about {usd(measured['usd_per_pt'])} at API prices (from "
              f"{plural(measured['windows'], 'recent weekly window')}).")
    else:
        print("1% of it: not measured yet. That needs a weekly window whose readings span 6 h or more and rise 10 "
              "points or more, with its requests on models of known price.")
    saved, source = saved_params(home)
    P = rule.params_from(saved)
    if measured:
        P.usd_per_pt = measured["usd_per_pt"]
    off = cards_off(home)
    print(f"The rule's inputs: {source}." + (" The guardrail has turned the cards off." if off else ""))
    ho = _handoff()
    if ho is None:
        print("(Files with uncommitted changes aren't counted: the plugin's advisor/handoff.py is missing.)")
    rows = []
    for th in threads.values():
        if th.kind == "main" and th.calls:
            last = max(c.end for c in th.calls)
            if last >= since:
                rows.append((last, th))
    if not rows:
        print(f"\nNo requests in main Codex conversations in the last {hours:g} h.")
        return
    names = {} if hide_titles else rollout.titles(home)
    for _last, th in sorted(rows, key=lambda row: -row[0]):
        show_thread(th, names.get(th.id, ""), P, now, home, ho, off)


# ---- spend ---------------------------------------------------------------------------------------------------------

def show_spend(days=7, home=None, now=None):
    """`spend`: API-equivalent $ per local day, by main threads, subagents and approval reviews."""
    home = home or rollout.codex_home()
    now = time.time() if now is None else now
    since = now - days * 86400
    threads, stats = rollout.load(home, since=since)
    _skipped(stats)
    calls = [c for th in threads.values() for c in th.calls if c.end >= since]
    # the approval reviewer has no published price: it is priced like the most-used priced model of these days
    used = Counter(c.model for c in calls if c.model and c.model != rate.AUTO_REVIEW and prices.rates(c.model))
    stand_in = used.most_common(1)[0][0] if used else None
    by_day = defaultdict(lambda: [0.0, 0.0, 0.0])    # main, subagents, reviews
    unknown, reviews = Counter(), 0
    for c in calls:
        review = c.model == rate.AUTO_REVIEW
        cost = prices.request_cost(stand_in if review else c.model, c.inp, c.cached, c.out, c.tier)
        if cost is None:
            unknown[c.model or "no model"] += 1
            continue
        reviews += review
        by_day[at(c.end, "%Y-%m-%d %a")][2 if review else 0 if c.kind == "main" else 1] += cost
    if not by_day and not unknown:
        print(f"No Codex requests in the last {plural(days, 'day')}.")
        return
    print(f"Codex at API prices, per day (last {plural(days, 'day')}):")
    print(f"{'day':<16}{'main':>11}{'subagents':>11}{'reviews*':>11}{'total':>12}{'credits':>10}")
    sums = [0.0, 0.0, 0.0]
    for day in sorted(by_day):
        row = by_day[day]
        sums = [a + b for a, b in zip(sums, row)]
        print(f"{day:<16}{usd(row[0]):>11}{usd(row[1]):>11}{usd(row[2]):>11}{usd(sum(row)):>12}"
              f"{sum(row) * CREDITS_PER_USD:>10,.0f}")
    print(f"{'total':<16}{usd(sums[0]):>11}{usd(sums[1]):>11}{usd(sums[2]):>11}{usd(sum(sums)):>12}"
          f"{sum(sums) * CREDITS_PER_USD:>10,.0f}")
    if reviews:
        print(f"* Approval reviews ({rate.AUTO_REVIEW}) have no published price: they are priced like {stand_in}, "
              f"the most-used priced model in these days, so that column is an estimate.")
    else:
        print("* Approval reviews: none in these days.")
    print(f"Credits: Codex counts {CREDITS_PER_USD} credits per $ of API price.")
    if unknown:
        n = sum(unknown.values())
        models = ", ".join(f"{m} {k:,}" for m, k in unknown.most_common())
        what = "1 call on a model without a known price is" if n == 1 else \
            f"{n:,} calls on models without a known price are"
        print(f"({what} left out: {models}.)")


# ---- replay --------------------------------------------------------------------------------------------------------

def _main_calls(threads):
    """Each main thread with its calls in time order."""
    return [(th, sorted(th.calls, key=lambda c: c.end)) for th in threads.values() if th.kind == "main" and th.calls]


def _gaps(cs):
    """(index, previous call, call, idle s) for each call after the first. The idle time runs from the previous
    response to this request's last input-side record (your prompt, or a tool output), as the prompt hook sees it."""
    for i in range(1, len(cs)):
        a, b = cs[i - 1], cs[i]
        start = b.start if b.start is not None and b.start >= a.end else a.end
        yield i, a, b, start - a.end


def _missed(c):
    """The prompt cache missed: less than half of the request was read from it."""
    return c.inp > 0 and c.cached / c.inp < 0.5


def measure(threads, now=None, newest=None):
    """The rule's inputs measured from main threads, as the plugin's defaults were (p50 unless noted):
        base_tokens            a thread's first request
        base_shared            cached tokens on requests after 6 h or more idle whose cache missed
        calls_between_returns  p25 and p50 of the calls from one return after 2 h or more to the next (or the end); a
                               return is a prompt you sent, not a tool the model waited on
        growth_per_call        input growth from one call to the next, on requests of 20K or more (growth only)
        compact_share          the input before an auto-compaction / the window of the call before it
        compacted_tokens       the first request after a compaction
        miss_curve             P(cache missed: under half the request cached) by idle time, on requests of 20K or more
                               with the model of the call before; each bucket of the default curve with 10 requests
                               or more is measured, the others keep the default, then the curve is made non-decreasing
    The four that follow Codex's own behaviour (FOLLOWS_CODEX: its instructions and tools, where it compacts and what
    it keeps), which changes with its versions, are measured on the last RECENT_DAYS before `now` when those hold
    MIN_SAMPLES samples or more, else on the newest MIN_SAMPLES samples (all of them when there are no more), so that
    a break from Codex never brings back an older Codex's behaviour; the others always on all the logs. Requests right
    after a compaction count only for compacted_tokens. Returns (values, samples, requests per miss-curve bucket, the
    names measured on recent days); a value is None when there is nothing to measure. `newest`, a dict, gets
    {name: (time of the first sample, of the last)} for the inputs measured on their newest samples."""
    now = time.time() if now is None else now
    newest = {} if newest is None else newest
    growth, between = [], []
    default_curve = rule.Params().miss_curve
    buckets = [[0, 0] for _ in default_curve]          # [requests, misses] per bucket
    for th, cs in _main_calls(threads):
        returns = []
        for i, a, b, idle in _gaps(cs):
            if b.after_compaction:                       # small, and uncached by necessity: no sample of the rest
                continue
            if b.inp >= MIN_INPUT and b.inp >= a.inp:
                growth.append(b.inp - a.inp)
            if idle >= LONG_BREAK_S and b.after_prompt:
                returns.append(i)
            if b.inp >= MIN_INPUT and b.model == a.model:    # another model never reads this one's cache
                k = next((j for j, (upper, _p) in enumerate(default_curve) if idle < upper), len(default_curve) - 1)
                buckets[k][0] += 1
                buckets[k][1] += _missed(b)
        between += [nxt - i for i, nxt in zip(returns, returns[1:] + [len(cs)])]
    recent = set()

    def pick(name, timed):
        """The samples of the last RECENT_DAYS when there are enough of them, else the newest MIN_SAMPLES (timed is
        in time order)."""
        new = [v for t, v in timed if t >= now - RECENT_DAYS * 86400]
        if len(new) >= MIN_SAMPLES:
            recent.add(name)
            return new
        if len(timed) > MIN_SAMPLES:
            timed = timed[-MIN_SAMPLES:]
            newest[name] = (timed[0][0], timed[-1][0])
        return [v for _t, v in timed]
    timed = codex_timed(threads)
    firsts, shared = pick("base_tokens", timed["base_tokens"]), pick("base_shared", timed["base_shared"])
    shares, after = pick("compact_share", timed["compact_share"]), pick("compacted_tokens", timed["compacted_tokens"])
    curve, top = [], 0.0
    for (upper, default), (n, misses) in zip(default_curve, buckets):
        top = max(top, misses / n if n >= MIN_BUCKET else default)    # a longer break never misses less often
        curve.append((upper, round(top, 3)))
    values = {
        "base_tokens": q(firsts, 0.5),
        "base_shared": q(shared, 0.5),
        "calls_between_returns": [q(between, 0.25), q(between, 0.5)] if between else None,
        "growth_per_call": q(growth, 0.5),
        "compact_share": round(q(shares, 0.5), 3) if shares else None,
        "compacted_tokens": q(after, 0.5),
        "miss_curve": curve,
    }
    samples = {"base_tokens": len(firsts), "base_shared": len(shared), "calls_between_returns": len(between),
               "growth_per_call": len(growth), "compact_share": len(shares), "compacted_tokens": len(after)}
    return values, samples, [n for n, _misses in buckets], recent


def codex_timed(threads):
    """The samples of the inputs that follow Codex's own behaviour (FOLLOWS_CODEX), from main threads, each a list of
    (time, value) by time (see measure): base_tokens, base_shared, compact_share, compacted_tokens."""
    out = {name: [] for name in FOLLOWS_CODEX}
    for th, cs in _main_calls(threads):
        if not cs[0].after_compaction:
            out["base_tokens"].append((cs[0].end, cs[0].inp))
        for _i, a, b, idle in _gaps(cs):
            if b.after_compaction:
                out["compacted_tokens"].append((b.end, b.inp))
            elif b.inp >= MIN_INPUT and b.model == a.model and idle >= MISS_IDLE_S and _missed(b):
                out["base_shared"].append((b.end, b.cached))
        ends = [c.end for c in cs]
        for t, inp in th.compactions:
            j = bisect_right(ends, t) - 1                # the last call before the compaction
            if j >= 0 and cs[j].window:
                out["compact_share"].append((t, inp / cs[j].window))
    return {name: sorted(v) for name, v in out.items()}


def _then(P, timed, t):
    """P with the inputs that follow Codex's own behaviour as they were around time `t`: each the p50 of its NEAR
    samples closest to it, when there are MIN_SAMPLES or more. Codex's instructions, tools and compaction point change
    between versions: a new conversation started at a past return would have started and compacted as Codex did
    then."""
    out = {}
    for name in FOLLOWS_CODEX:
        near = sorted(timed[name], key=lambda x: abs(x[0] - t))[:NEAR]
        if len(near) >= MIN_SAMPLES:
            out[name] = q([v for _t, v in near], 0.5)
    return replace(P, **out) if out else P


def fresh_params(values, samples, curve_n):
    """The measured inputs that have enough samples, as params.json holds them (the open-ended miss-curve bound is
    null)."""
    out = {k: v for k, v in values.items() if k in samples and v is not None and samples[k] >= MIN_SAMPLES}
    if any(n >= MIN_BUCKET for n in curve_n):
        out["miss_curve"] = [[None if upper == math.inf else upper, p] for upper, p in values["miss_curve"]]
    return out


def _switch_saving(cs, i, end, ctx, P):
    """What a new conversation started at return i would have saved by the next return (calls i..end-1): each call's
    real cost against the same call in a new conversation. That one starts at N tokens (a new conversation once it
    has re-read its files) plus what the return request added, and grows by the same tokens; it compacts at its own
    point (P's: the caller gives the inputs that follow Codex as they were then, _then). Its first request is cold
    except the shared prefix; later ones read from the cache all but the real call's new tokens, and miss when the
    real call missed. Returns ($ or None when nothing could be priced, calls left unpriced)."""
    n_new = P.base_tokens + P.reread_tokens + P.handoff_tokens
    total, priced, unpriced, size = 0.0, 0, 0, 0
    for j in range(i, end):
        c = cs[j]
        if j == i:
            size = n_new + max(c.inp - ctx, 0)
            cached = min(P.base_shared, size)
        else:
            prev = cs[j - 1]
            if c.after_compaction:              # the real one compacted; the new one carries on, warm
                size = max(size + prev.out, P.base_tokens)
                cached = max(size - prev.out, 0)
            else:
                size = max(size + c.inp - prev.inp, P.base_tokens)
                if c.window and size >= P.compact_share * c.window:
                    size = P.compacted_tokens   # the new conversation reaches its own compaction point
                    cached = min(P.base_shared, size)
                elif c.cached < 0.5 * prev.inp:  # the real call missed the cache: so would the new one
                    cached = min(P.base_shared, size)
                else:
                    cached = max(size - (c.inp - c.cached), 0)
        real = prices.request_cost(c.model, c.inp, c.cached, c.out, c.tier)
        new = prices.request_cost(c.model, size, cached, c.out, c.tier)
        if real is None or new is None:
            unpriced += 1
            continue
        total += real - new
        priced += 1
    return (total if priced else None), unpriced


def replay_returns(threads, P, savings=True):
    """Every return after a break of at least 1 h in a main thread, replayed with the rule (oldest first). A return
    is a prompt sent after the break (a new turn or a prompt before the request, rollout.Call.after_prompt): when the
    model only waited that long on a tool, such as agents it started, nobody sent a prompt and the prompt hook never
    ran. The replay can't see the prompt or the last reply, so the verdict takes the quality risk as medium, the usual
    case for a short prompt; "card if low risk" marks a card only the low-risk bar would show. The verdict uses P, the
    inputs given: replay() gives the ones it measured, which the plugin uses once they are written (--write), and,
    when the plugin uses others until then, those too. What a switch would have saved (left out when `savings` is
    false) uses the inputs that follow Codex as they were then (_then)."""
    rows = []
    timed = codex_timed(threads) if savings else None
    for th, cs in _main_calls(threads):
        found = [(i, idle) for i, _a, b, idle in _gaps(cs) if idle >= RETURN_S and b.after_prompt]
        ends = [i for i, _idle in found[1:]] + [len(cs)]
        for (i, idle), end in zip(found, ends):
            rows.append(_replay_one(th, cs, i, end, idle, P, _then(P, timed, cs[i].end) if savings else None,
                                    savings))
    rows.sort(key=lambda row: row["at"])
    return rows


def _replay_one(th, cs, i, end, idle, P, then=None, savings=True):
    prev, c = cs[i - 1], cs[i]
    ctx = prev.inp + prev.out                  # what the prompt hook would have seen: the last call's input + output
    model, tier, window = c.model or prev.model, c.tier or prev.tier, c.window or prev.window
    row = {"thread": th.id, "at": c.start if c.start is not None else c.end, "idle": idle, "ctx": ctx,
           "model": model, "calls": end - i, "verdict": "nothing", "expected": None, "realized": None,
           "missed": _missed(c), "p_miss": None, "saving": None, "unpriced": 0, "compacted": c.after_compaction}
    if c.after_compaction:                     # Codex compacted it before this request: nothing to weigh
        row["verdict"] = "compacted"
        return row
    low = rule.after_break(ctx, idle, [], P, model, window, tier)
    if not low.numbers:
        row["verdict"] = "unpriced"
        return row
    medium = rule.after_break(ctx, idle, [ASSUMED], P, model, window, tier)
    row["verdict"] = ("card" if medium.show else "card if low risk" if low.show else
                      "note" if medium.note else "nothing")
    # what re-sending the conversation really cost: its cached part at the cached price, the rest at full price
    pin, pc = prices.rates(model, ctx, tier)[:2]
    resent = min(ctx, c.inp)
    hit = min(c.cached, resent)
    row["realized"] = hit * pc + (resent - hit) * pin
    row["expected"] = low.numbers["resend_here"]
    row["p_miss"] = low.numbers["p_miss"]
    if savings:
        row["saving"], row["unpriced"] = _switch_saving(cs, i, end, ctx, then or P)
    return row


def _print_returns(rows):
    print("Returns to a main conversation after a break of 1 h or more (quality risk taken as medium):")
    if not rows:
        print("  none")
        return
    print(f"  {'when':<17}{'thread':<10}{'idle':>7}{'context':>9}{'P(miss)':>9}{'expected':>10}{'realized':>10}"
          f"  {'missed':<7}{'verdict':<18}{'switch saves':>13}{'calls':>7}")
    for r in rows:
        when, thread = at(r["at"], "%Y-%m-%d %H:%M"), r["thread"][:8]
        head = f"  {when:<17}{thread:<10}{span(r['idle']):>7}{tokens(r['ctx']):>9}"
        if r["expected"] is None:
            print(f"{head}  {r['verdict']}")
            continue
        missed = "yes" if r["missed"] else "no"
        saves = usd(r["saving"]) if r["saving"] is not None else "unpriced"
        print(f"{head}{r['p_miss']:>9.2f}{usd(r['expected']):>10}{usd(r['realized']):>10}  {missed:<7}"
              f"{r['verdict']:<18}{saves:>13}{r['calls']:>7}")


def _counts(v):
    """'1 card, 0 more if the quality risk is low, 1 note, 0 with nothing shown' from a Counter of verdicts."""
    return (f"{plural(v['card'], 'card')}, {v['card if low risk']} more if the quality risk is low, "
            f"{plural(v['note'], 'note')}, {v['nothing']} with nothing shown")


def _summary(rows, n_threads, measured=False, before=None, source="the plugin's defaults"):
    """The summary of the returns. rows: replayed with the inputs measured from the logs where there were enough
    samples (`measured`: some were), else the ones the plugin uses (`source`); before: the verdicts with the inputs
    the plugin uses until --write saves the measured ones, when those differ."""
    priced = [r for r in rows if r["expected"] is not None]
    verdicts = Counter(r["verdict"] for r in rows)
    with_returns = len({r["thread"] for r in rows})
    print(f"\n{plural(len(rows), 'return')} after a break of 1 h or more in {with_returns} of "
          f"{plural(n_threads, 'main thread')}"
          + (f" ({verdicts['compacted']} right after a compaction and {verdicts['unpriced']} on models without a "
             f"price are left out)" if verdicts["compacted"] or verdicts["unpriced"] else "") + ".")
    if before is not None:
        print(f"The rule's verdicts, with the inputs measured below, which the plugin uses after --write: "
              f"{_counts(verdicts)}.")
        print(f"With the inputs it uses until then ({source}): {_counts(before)}.")
    elif measured:
        print(f"The rule's verdicts, with the inputs measured below, which the plugin already uses: "
              f"{_counts(verdicts)}.")
    else:
        print(f"The rule's verdicts, with the inputs the plugin uses ({source}): {_counts(verdicts)}.")
    groups = (("each card", [r for r in priced if r["verdict"] == "card"]),
              ("each card, low risk included", [r for r in priced if r["verdict"] in ("card", "card if low risk")]),
              ("every return (the most a switch could do, losses included)", priced))
    for label, group in groups:
        s = [r["saving"] for r in group if r["saving"] is not None]
        if s:
            losing = sum(1 for x in s if x < 0)
            print(f"A switch at {label}: {usd(sum(s))} saved in all by the next return, median "
                  f"{usd(statistics.median(s))} over {plural(len(s), 'return')}; {losing} would have cost more.")
        elif group is not priced:
            print(f"A switch at {label}: none, since no such card would have been shown.")
    errors = [abs(r["realized"] - r["expected"]) / r["realized"] for r in priced if r["realized"] > 0]
    if errors:
        print(f"Prediction: the expected cost of re-sending was off by {statistics.median(errors):.0%} at the median "
              f"(|realized - expected| / realized, {plural(len(errors), 'return')}); in all, "
              f"{usd(sum(r['realized'] for r in priced))} realized against {usd(sum(r['expected'] for r in priced))} "
              f"expected.")
        print(f"The cache missed on {sum(r['missed'] for r in priced)} of these returns; the miss curve expected "
              f"{sum(r['p_miss'] for r in priced):.1f}.")
    return verdicts


MEASURED = (
    ("base_tokens", "a new conversation's first request", "thread"),
    ("base_shared", "still cached on a request after 6 h or more whose cache missed", "request"),
    ("calls_between_returns", "calls from one return after 2 h or more to the next (p25, p50)", "return"),
    ("growth_per_call", "input growth per call, requests of 20K or more", "pair"),
    ("compact_share", "input before an auto-compaction, as a share of the window", "compaction"),
    ("compacted_tokens", "the first request after a compaction", "compaction"),
)


def _value(name, v):
    if name == "calls_between_returns":
        return ", ".join(f"{x:,}" for x in v)
    return f"{v:.3f}" if name == "compact_share" else f"{v:,}"


def _print_params(values, samples, curve_n, recent=(), newest=None):
    print(f"\nThe rule's inputs measured from your main threads (p50 unless noted; with fewer than {MIN_SAMPLES} "
          f"samples the plugin's default stays). Those that follow Codex's own behaviour use the last {RECENT_DAYS} "
          f"days when these hold {MIN_SAMPLES} samples or more, else their newest {MIN_SAMPLES}; the others all your "
          f"logs:")
    defaults = rule.Params()
    newest = newest or {}
    for name, what, unit in MEASURED:
        v, n = values[name], samples[name]
        shown = _value(name, v) if v is not None else "-"
        keep = "" if n >= MIN_SAMPLES else f"; default {_value(name, getattr(defaults, name))} stays"
        if name in recent:
            period = f", last {RECENT_DAYS} days"
        elif name in newest:
            period = f", the newest: {at(newest[name][0], '%Y-%m-%d')} to {at(newest[name][1], '%Y-%m-%d')}"
        else:
            period = ", all logs"
        print(f"  {name:<22}{shown:>10}  {what} ({plural(n, unit)}{period}{keep})")
    print(f"  miss_curve: P(cache missed) by the idle time before a request of 20K tokens or more (buckets with fewer "
          f"than {MIN_BUCKET} requests keep the default, then the curve never falls):")
    lower = 0
    for (upper, p), n in zip(values["miss_curve"], curve_n):
        if upper == math.inf:
            label = f"over {lower / 3600:g} h"
        elif upper <= 3600:
            label = f"{lower / 60:g}-{upper / 60:g} min"
        else:
            label = f"{lower / 3600:g}-{upper / 3600:g} h"
        print(f"    {label:<12}{p:>6.2f}  ({plural(n, 'request')}{'' if n >= MIN_BUCKET else ', default'})")
        lower = upper


def replay(write=False, home=None, now=None, out_dir=None):
    """`replay`: the rule replayed on every return, the measured inputs and the plugin's decisions. With `write`, saves
    the inputs (data/codex_params.json in `out_dir`, default this checkout's data folder, and params.json in the
    plugin's data folder) and, when the guardrail rule says so, guardrail.json. Returns a summary for the nightly
    log."""
    home = home or rollout.codex_home()
    now = time.time() if now is None else now
    out_dir = out_dir or DATA
    threads, stats = rollout.load(home)
    _skipped(stats)
    newest = {}
    values, samples, curve_n, recent = measure(threads, now, newest)
    fresh = fresh_params(values, samples, curve_n)
    saved, source = saved_params(home, out_dir)
    P = rule.params_from(dict(saved, **fresh))      # the inputs the plugin will use after --write
    today = rule.params_from(saved)                 # ... and the ones it uses until then
    rows = replay_returns(threads, P)
    _print_returns(rows)
    before = None if today == P else Counter(r["verdict"] for r in replay_returns(threads, today, savings=False))
    verdicts = _summary(rows, sum(1 for th in threads.values() if th.kind == "main" and th.calls), bool(fresh),
                        before, source)
    _print_params(values, samples, curve_n, recent, newest)

    dirs = plugin_dirs(home)
    events = read_decisions(dirs)
    switches, back = went_back(events, threads)
    turn_off = switches >= GUARD_SWITCHES and 2 * back >= switches
    already_off = cards_off(home)
    kept_on = not already_off and cards_kept_on(home)
    if not dirs:
        print("\nNo plugin data folder under CODEX_HOME/plugins/data (the plugin isn't installed or hasn't run yet), "
              "so there are no decisions to read.")
    else:
        e = Counter(x["event"] for x in events)
        tried = e["switched"] - switches        # switches tried after `switch-advisor test`
        print(f"\nThe plugin's decisions: {plural(e['offer'], 'card')} shown, {plural(e['kept'], 'stay')}, "
              f"{plural(switches, 'switch', 'switches')}, {plural(e['note'], 'note')}, "
              f"{plural(e['expired'], 'unused handoff')}, {e['test'] + e['demo']} tests and demos"
              + (f" ({plural(tried, 'switch', 'switches')} after a test, which the guardrail leaves out)" if tried
                 else "") + ".")
        if switches:
            print(f"{back} of the {plural(switches, 'switch', 'switches')} went back to the old conversation within "
                  f"24 h.")
        if already_off:
            print("Guardrail: the cards are turned off (guardrail.json); set after_break_enabled to true in that file "
                  "to turn them on again.")
        elif kept_on:
            print("Guardrail: you turned the cards back on (after_break_enabled is true in guardrail.json), so it "
                  "leaves them on" + (", though half or more of the switches went back." if turn_off else "."))
        elif turn_off and write:
            print("Guardrail: half or more of the switches went back, so the cards are now turned off "
                  "(guardrail.json); set after_break_enabled to true in that file to turn them on again.")
        elif turn_off:
            print("Guardrail: half or more of the switches went back, so the cards should be turned off; run with "
                  "--write to do it.")
        else:
            print(f"Guardrail: the cards stay on. They are turned off once {GUARD_SWITCHES} or more switches were made "
                  f"and half or more of them went back.")

    written, unreadable = [], False
    if write:
        for d in dirs:      # handoffs nobody used: the hooks delete them only when they next run
            state.prune(d, now, rule.params_from(state.load_params(d)).handoff_ttl_s)
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")
        record = dict(fresh, measured_at=stamp, samples=samples, miss_curve_samples=curve_n,
                      recent_days=RECENT_DAYS, measured_recent=sorted(recent),
                      measured_newest={k: [round(lo), round(hi)] for k, (lo, hi) in sorted(newest.items())})
        path = os.path.join(out_dir, "codex_params.json")
        state.write_json(path, record)
        written.append(path)
        for d in dirs:      # replaced whole (temporary file, then rename): the hook may read it at any moment
            path = os.path.join(d, "params.json")
            state.write_json(path, dict(state.read_json(path) or {}, **record))   # keeps fields set by hand
            written.append(path)
            path = os.path.join(d, "guardrail.json")
            if os.path.exists(path) and state.read_json(path) is None:
                # there but unreadable (UTF-16 from PowerShell's Out-File, say): it may be your hand edit, so it stays
                print(f"\n{path} can't be read as JSON, so the guardrail leaves it as it is.")
                unreadable = True
            elif turn_off and not already_off and not kept_on:
                state.write_json(path, {"after_break_enabled": False,
                                        "reason": f"{back} of {switches} switches went back to the old "
                                                  f"conversation within 24 h.",
                                        "switches": switches, "went_back": back, "updated": stamp})
                written.append(path)
        print("\nWritten: " + "; ".join(written) + ".")
    else:
        print("\nRun with --write to save the measured inputs for the plugin.")
    return {"returns": len(rows), "cards": verdicts["card"], "notes": verdicts["note"], "switches": switches,
            "went_back": back, "guardrail_off": already_off or (turn_off and write and not kept_on and not unreadable),
            "written": written}


# ---- handoff -------------------------------------------------------------------------------------------------------

def newest_main(home=None):
    """The log of the main thread written to last (rollout.written), or None. A conversation in use is never
    compressed, and a compressed log's time is only a bound, so .jsonl logs come first."""
    for paths in (rollout.rollout_files(home, include_zst=False), rollout.rollout_files(home)):
        for path in reversed(paths):
            records = rollout.iter_records(path)
            first = next(records, None)
            records.close()
            payload = first.get("payload") if first and first.get("type") == "session_meta" else None
            if isinstance(payload, dict):
                source = payload.get("source")
                if not (isinstance(source, dict) and "subagent" in source):
                    return path
    return None


def show_handoff(session=None, home=None, now=None):
    """`handoff`: prints the handoff text the plugin would build now; saves nothing."""
    home = home or rollout.codex_home()
    path = rollout.find_rollout(session, home) if session else newest_main(home)
    if not path:
        print(f"No Codex log for thread {session}." if session else "No main Codex conversation in the logs.")
        return 1
    ho = _handoff()
    if ho is None:
        print("The plugin's handoff module (codex/switch-advisor/advisor/handoff.py) is missing.")
        return 1
    f = rollout.facts(path, home=home, now=now)
    files = ho.git_status(f.cwd) if f.cwd else []
    print(ho.build(f, files, now=now))
    return 0


# ---- command line --------------------------------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["now"] + argv                       # `now` is the default command
    first = " ".join(__doc__.split("\n\n")[0].split()).split(". ")[0] + "."     # the docstring's first sentence
    ap = argparse.ArgumentParser(prog="python -m monitor.codex", description=first)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("now", help="your weekly limit and what the next message in each conversation costs")
    p.add_argument("--hours", type=float, default=24, help="conversations with a request in the last hours (24)")
    p.add_argument("--hide-titles", action="store_true", help="leave conversation titles out")
    p = sub.add_parser("spend", help="API-equivalent $ per day")
    p.add_argument("--days", type=int, default=7, help="how many days back (7)")
    p = sub.add_parser("replay", help="the rule replayed on your returns after a break, and its measured inputs")
    p.add_argument("--write", action="store_true", help="save the measured inputs (and the guardrail) for the plugin")
    p = sub.add_parser("handoff", help="print the handoff for the newest main conversation (saves nothing)")
    p.add_argument("--session", help="a thread id instead of the newest main conversation")
    a = ap.parse_args(argv)
    if a.cmd == "spend":
        show_spend(a.days)
    elif a.cmd == "replay":
        replay(write=a.write)
    elif a.cmd == "handoff":
        return show_handoff(a.session)
    else:
        show_now(a.hours, a.hide_titles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
