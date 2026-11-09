"""Your $ per 1% of the weekly limit, measured from Codex's own logs.

Codex records the plan's weekly meter with its responses (token_count events: rate_limits, the window of 10,080
minutes). Over one weekly window, the API-equivalent dollars of the requests in the logs, divided by how far the meter
rose meanwhile, give what 1% of the weekly limit buys. The meter also counts usage these logs don't see (Codex cloud,
other computers), so the rate is an estimate and callers say "about".
"""
import statistics
import time
from bisect import bisect_right
from collections import Counter

from . import prices, rollout

AUTO_REVIEW = "codex-auto-review"   # Codex's approval reviewer: no published price
MIN_SPAN_S = 6 * 3600               # a window's readings must span this long
MIN_RISE = 10.0                     # ...and the meter must rise this many points
RECENT = 3                          # the median is over this many of the most recent windows
MAX_UNPRICED = 0.02                 # a window counts only if at most this share of its tokens has no known price
ZST_LAG_S = rollout.ZST_LAG_S       # a compressed log this Python can't open is taken to end within this of its
                                    # modification time (the longest seen is 35 h)


def _read(home, now, days, budget_s):
    """(calls, meters, complete_after) from the logs written in the last `days` (rollout.log_times), newest first,
    until the time budget runs out. Every record later than complete_after was read: the logs left unread (out of
    time, or older than `days`) hold nothing later than when they were last written. A compressed log this Python
    can't open has a bound only in its creation time, which can be days after its last record: it is taken to end
    within ZST_LAG_S of its modification time instead (rollout.zst_bound), or the rate would stay unknown on Pythons
    before 3.14."""
    began = time.monotonic()
    complete_after = now - days * 86400
    calls, meters, seen = [], [], set()
    for t, path in reversed(rollout.log_times(home, since=complete_after)):
        if time.monotonic() - began >= budget_s:
            complete_after = max(complete_after, t)               # the newest of the logs not read
            break
        if path.endswith(".zst") and not rollout.ZSTD_AVAILABLE:
            complete_after = max(complete_after, rollout.zst_bound(path, t))
            continue
        th = rollout.read_thread(path)
        if th is None:
            continue
        for c in th.calls:
            if c.response_id is not None:
                if c.response_id in seen:
                    continue
                seen.add(c.response_id)
            calls.append(c)
        meters.extend(th.meters)
    return calls, meters, complete_after


def _costs(calls):
    """(timestamps, $ or None, tokens) of every call, by time. The approval reviewer is priced like the period's
    most-used priced model (by number of calls); a model without a known price gives None."""
    used = Counter(c.model for c in calls if c.model and c.model != AUTO_REVIEW and prices.rates(c.model) is not None)
    stand_in = used.most_common(1)[0][0] if used else None
    calls = sorted(calls, key=lambda c: c.end)
    costs = [prices.request_cost(stand_in if c.model == AUTO_REVIEW else c.model, c.inp, c.cached, c.out, c.tier)
             for c in calls]
    return [c.end for c in calls], costs, [c.inp + c.out for c in calls]


def compute_rate(home=None, now=None, days=21, budget_s=60.0):
    """$ per 1% of the weekly limit, or None when no weekly window qualifies.

    Meter readings (the "codex" limit, weekly window) are grouped into weekly windows by their reset time, rounded
    to the hour, and plan. A window counts when its readings span at least 6 h, the meter rose at least 10 points
    from its first to its last reading, and the logs hold requests in between: its rate is the API-equivalent $ of
    the requests after the first reading up to the last, divided by that rise. Only windows of the plan of the
    newest reading count (a plan change changes the rate), only those whose requests were all read, and only those
    whose requests have known prices: the meter counts the others too, so more than 2% of the tokens on a model
    without a price would make 1% look cheaper than it is. The result is the median rate of the 3 most recent
    windows that count:
        {"usd_per_pt", "windows" (how many), "computed_at", "detail": [{"resets", "plan", "rise", "usd", "calls",
         "unpriced"}, ...] (oldest first)}"""
    now = time.time() if now is None else float(now)
    calls, meters, complete_after = _read(home, now, days, budget_s)
    weekly = sorted((m for m in meters if m.weekly_pct is not None and m.weekly_resets is not None), key=lambda m: m.t)
    if not weekly:
        return None
    plan = weekly[-1].plan
    groups = {}
    for m in weekly:
        if m.plan == plan:
            groups.setdefault(int(round(m.weekly_resets / 3600.0)), []).append(m)
    times, costs, sizes = _costs(calls)
    kept = []
    for hour, readings in groups.items():
        first, last = readings[0], readings[-1]
        rise = last.weekly_pct - first.weekly_pct
        if last.t - first.t < MIN_SPAN_S or rise < MIN_RISE or first.t < complete_after:
            continue
        lo, hi = bisect_right(times, first.t), bisect_right(times, last.t)
        between = costs[lo:hi]
        usd = sum(u for u in between if u is not None)
        if usd <= 0:
            continue                # the meter rose on usage these logs don't hold
        unpriced = sum(n for n, u in zip(sizes[lo:hi], between) if u is None)
        if unpriced > MAX_UNPRICED * sum(sizes[lo:hi]):
            continue                # the meter rose on usage these logs can't price
        kept.append((hour * 3600, rise, usd, len(between), sum(1 for u in between if u is None)))
    if not kept:
        return None
    used = sorted(kept)[-RECENT:]
    detail = [{"resets": resets, "plan": plan, "rise": round(rise, 2), "usd": round(usd, 4), "calls": n,
               "unpriced": unpriced} for resets, rise, usd, n, unpriced in used]
    return {"usd_per_pt": round(statistics.median(usd / rise for _r, rise, usd, _n, _u in used), 4),
            "windows": len(used), "computed_at": now, "detail": detail}
