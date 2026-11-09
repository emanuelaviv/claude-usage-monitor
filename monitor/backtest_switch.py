"""Backtest the switch-conversation rule on this machine's own history.

Usage: python -m monitor.backtest_switch

For every prompt you typed in a main conversation, it asks the rule what to do and replays what actually happened
after it. When the rule says "new conversation", the replay follows a second, smaller conversation through the
rest of that session:
- it starts at the fresh-session size plus a re-read allowance;
- it grows by the same tokens the real one added, and compacts at its own trigger;
- it re-caches whenever the real conversation came back after the cache expired.

The second conversation can end up larger than the real one after the real one compacts. Then the saving goes
negative, and the replay counts that. Each later call is priced both ways with its real token counts.

Writes data/advisor_params.json, data/switch_points.json and reports/switch_backtest.md.
Prompt text is never stored; only PROMPT_*.md names and their first lines are read, in memory.
"""
import bisect
import collections
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timezone

from .ingest import load, root_of
from .packets import classify
from .prices import WRITE_MULT, lookup
from .switch_advisor import Params, State, advise, cost_continue, cost_switch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def q(xs, ps=(0.25, 0.5, 0.75)):
    xs = sorted(xs)
    return tuple(xs[min(len(xs) - 1, int(p * len(xs)))] for p in ps) if xs else (0, 0, 0)


def session_ttl(calls):
    return 3600 if sum(c.cw1h > 0 for c in calls) >= sum(c.cw5 > 0 for c in calls) else 300


def is_drop(prev_ctx, ctx):
    """A compaction: the context fell below half of a large previous one."""
    return prev_ctx is not None and prev_ctx >= 200_000 and ctx < 0.5 * prev_ctx


def build(calls, prompts, classify_risk=True):
    """Group main-thread calls and prompts by (surface, session); mark new packets and their risk."""
    main = collections.defaultdict(list)
    for c in calls:
        if c.kind == "main":
            main[(c.surface, c.session)].append(c)
    pr = collections.defaultdict(list)
    for p in prompts:
        pr[(p.surface, p.session)].append(p)
    sessions = {}
    for key, cs in main.items():
        ps = pr.get(key, [])
        marked, cur, earlier = [], None, []
        ttl = session_ttl(cs)
        for i, p in enumerate(ps):
            r = root_of(p.stem)
            rework = r is not None and p.stem.lower() != r
            same = r is not None and cur is not None and (r == cur or (rework and cur.startswith(r + "_")))
            new = i == 0 or (r is not None and not same)
            risk, why = "high", "follow-up"
            if new and i > 0 and classify_risk:
                risk, why = classify(p.stem, p.cwd, p.ts, earlier)
            if r is not None and (new or cur is None):
                cur = r
            if p.stem:
                earlier.append(p.stem)
            nxt = ps[i + 1].ts if i + 1 < len(ps) else float("inf")
            n_actual = sum(1 for c in cs if p.ts <= c.start < nxt)
            marked.append([p, new, n_actual, risk, why])
        # a packet's life: from its first prompt to the next new packet; count its calls and later cold returns
        for i, m in enumerate(marked):
            if not m[1]:
                continue
            j = next((k for k in range(i + 1, len(marked)) if marked[k][1]), len(marked))
            end = marked[j][0].ts if j < len(marked) else float("inf")
            life = sum(1 for c in cs if m[0].ts <= c.start < end)
            colds = 0
            for k in range(i + 1, j):
                prev = [c for c in cs if c.end <= marked[k][0].ts]
                colds += bool(prev) and marked[k][0].ts - prev[-1].end > ttl
            m.extend([life, colds])
        sessions[key] = (cs, [tuple(m[:5]) + (tuple(m[5:]) if len(m) > 5 else (None, None)) for m in marked])
    return sessions


def measure(sessions):
    """Fresh-session size, calls per prompt, growth per call and the compaction point, per surface."""
    out = {}
    for surf in ("code", "cowork"):
        firsts, grow, trig, post = [], [], [], []
        new_n, fol_n, cold_n, between = [], [], [], []
        for (s, _), (cs, marked) in sessions.items():
            if s != surf or not cs:
                continue
            firsts.append(cs[0])
            for a, b in zip(cs, cs[1:]):
                if is_drop(a.prompt_tokens, b.prompt_tokens):
                    trig.append(a.prompt_tokens)
                    post.append(b.prompt_tokens)
                elif b.prompt_tokens > a.prompt_tokens:
                    grow.append(b.prompt_tokens - a.prompt_tokens)
            ttl = session_ttl(cs)
            returns = [m[0].ts for m in marked
                       if (prev := [c for c in cs if c.end <= m[0].ts]) and m[0].ts - prev[-1].end > ttl]
            for a, b in zip(returns, returns[1:] + [float("inf")]):
                between.append(sum(1 for c in cs if a <= c.start < b))
            for _, new, n, _, _, life, colds in marked:
                if new and life:
                    new_n.append(life)
                    cold_n.append(colds)
                elif not new and n:
                    fol_n.append(n)
        out[surf] = {
            "base_tokens": q([f.prompt_tokens for f in firsts])[1] if firsts else 79_000,
            "base_shared": q([min(f.cr, f.prompt_tokens) for f in firsts])[1] if firsts else 45_000,
            "calls_new_task": q(new_n) if new_n else (39, 68, 86),
            "calls_follow_up": q(fol_n) if fol_n else (7, 17, 79),
            "growth_per_call": q(grow)[1] if grow else 3_000,
            "cold_returns": round(sum(cold_n) / len(cold_n), 2) if cold_n else 1.0,
            "calls_between_returns": list(q(between, (0.25, 0.5))) if between else [45, 115],
            "compact_at": q(trig)[1] if trig else 969_000,
            "compact_to": q(post)[1] if post else 101_000,
            "sessions": len(firsts), "new_prompts": len(new_n), "follow_prompts": len(fol_n),
        }
    return out


def simulate(cs, marked, P, decide, compact_to=101_000, skip=frozenset()):
    """Replay one session under a decision rule. Returns (net saving $, switch records)."""
    ttl = session_ttl(cs)
    events = [(m[0].ts, 0, i, m) for i, m in enumerate(marked)] + [(c.start, 1, i, c) for i, c in enumerate(cs)]
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    cf_ctx, first_pending, last, prev_ctx = None, False, None, None
    c_actual_sw = size_sw = 0
    total, switches, seen_prompt = 0.0, [], False
    for _, typ, _, obj in events:
        if typ == 0:
            p, new, n_actual, risk, why = obj[:5]
            if new and obj[5]:
                n_actual = obj[5]                # the packet's whole life
            first_of_session, seen_prompt = not seen_prompt, True
            if last is None or first_of_session or not (lookup(last.model) or (0, 0, 0, False))[3]:
                continue
            c_actual = last.prompt_tokens + last.out
            c_cf = max(P.base_tokens, cf_ctx + last.out) if cf_ctx is not None else c_actual
            s = State(last.model, c_cf, p.ts - last.end, ttl, new, risk, handoff_exists=new and p.stem is not None)
            nxt = next((c2 for c2 in cs if c2.start >= p.ts), None)
            try:
                mode = None if p.ts in skip else decide(s, n_actual, P, p, nxt)
            except TypeError:
                mode = None if p.ts in skip else decide(s, n_actual, P, p)
            if not mode:
                continue
            pin, pout, pcr, _ = lookup(last.model)
            pin, pout, pcr = pin / 1e6, pout / 1e6, pcr / 1e6
            pw = pin * WRITE_MULT.get(ttl, 2.0)
            shared = min(P.base_shared, c_cf)
            if mode == "exists":
                ho, size = 0.0, P.base_tokens + P.reread_tokens
            elif mode == "free":                 # handoff built from the transcript locally: no model call
                ho, size = 0.0, P.base_tokens + P.reread_tokens + 1_000
            elif mode == "warm":                 # handoff written before the cache expired
                ho = c_cf * pcr + P.handoff_tokens * pout
                size = P.base_tokens + P.reread_tokens + P.handoff_tokens
            else:                                # handoff written now, at this prompt
                ho = (shared * pcr + (c_cf - shared) * pw if s.cold else c_cf * pcr) + P.handoff_tokens * pout
                size = P.base_tokens + P.reread_tokens + P.handoff_tokens
            total -= ho
            c_actual_sw, size_sw, first_pending = c_actual, size, True
            switches.append({"ts": p.ts, "stem": p.stem, "new_task": new, "risk": risk, "why": why,
                             "cold": s.cold, "mode": mode, "context": c_cf, "overhead": ho, "model": last.model})
            continue
        c = obj
        ctx = c.prompt_tokens
        drop = is_drop(prev_ctx, ctx)
        idle_before = c.start - last.end if last is not None else 0.0
        growth = (last.out if last is not None else 0) if drop else ctx - (prev_ctx if prev_ctx is not None else ctx)
        pr = lookup(c.model)
        if cf_ctx is None and not first_pending:
            prev_ctx, last = ctx, c
            continue
        if first_pending:                        # the new conversation's first request
            new_ctx = max(size_sw + (max(ctx - c_actual_sw, 0) if not drop else growth), P.base_tokens)
        else:
            new_ctx = max(cf_ctx + growth, P.base_tokens)
        if not pr or not pr[3]:                  # unpriced model: keep tracking the size only
            cf_ctx, first_pending = new_ctx, False
            prev_ctx, last = ctx, c
            continue
        pin, pcr = pr[0] / 1e6, pr[2] / 1e6
        pw = pin * WRITE_MULT.get(ttl, 2.0)
        actual = c.inp * pin + c.cr * pcr + c.cw5 * pin * 1.25 + c.cw1h * pin * 2.0
        if first_pending:
            cf = min(P.base_shared, new_ctx) * pcr + max(new_ctx - P.base_shared, 0) * pw
            first_pending = False
        elif drop and idle_before > ttl and new_ctx >= 288_000:   # both compact on a cold return
            new_ctx, cf = ctx, actual
        elif drop and idle_before > ttl:         # the smaller one re-caches instead
            cf = min(c.cr, new_ctx) * pcr + max(new_ctx - c.cr, 0) * pw
        elif drop:                               # the real one compacted mid-turn; ours stays warm
            g = max(growth, 0)
            cf = max(new_ctx - g, 0) * pcr + g * pw
        elif new_ctx >= P.compact_at:            # ours reaches its own compaction point
            new_ctx = compact_to
            cf = min(P.base_shared, new_ctx) * pcr + max(new_ctx - P.base_shared, 0) * pw
        elif c.cw >= 0.5 * ctx:                  # the real call re-cached after an expiry
            cf = min(c.cr, new_ctx) * pcr + max(new_ctx - c.cr, 0) * pw
        else:
            new_tok = c.inp + c.cw
            cf = max(new_ctx - new_tok, 0) * pcr + c.inp * pin + c.cw5 * pin * 1.25 + c.cw1h * pin * 2.0
        total += actual - cf
        cf_ctx = new_ctx
        prev_ctx, last = ctx, c
    return total, switches


# ---- decision policies -------------------------------------------------------------------------------------

def suggested(s, n_actual, P, p):
    """What the advisor would show: low- and medium-risk packets above their bars."""
    return ("exists" if s.handoff_exists else "now") if advise(s, P).action == "new conversation" else None


def every_packet(s, n_actual, P, p):
    """Cost only: every new packet in a large conversation, whatever its risk."""
    if not (s.new_task and s.handoff_exists):
        return None
    loose = replace(P, bar_medium=P.bar_low)
    return "exists" if advise(replace(s, risk="low"), loose).action == "new conversation" else None


def hindsight(s, n_actual, P, p):
    """Every new packet, knowing the real call count (upper bound)."""
    if not (s.new_task and s.handoff_exists and n_actual) or s.context_tokens < P.min_context:
        return None
    return "exists" if cost_continue(s, n_actual, P) - cost_switch(s, n_actual, P) >= P.bar_low else None


def handoff_when_around(presence):
    """Suggested, plus a handoff written before the cache expired, but only when you were evidently at the
    computer around minutes 40-60 of the idle gap (a prompt anywhere then). Upper bound; mid-task risk."""
    def decide(s, n_actual, P, p):
        mode = suggested(s, n_actual, P, p)
        if mode or not s.cold or s.handoff_exists:
            return mode
        start = p.ts - s.idle_s
        if not presence(start + 2400, start + 3600):
            return None
        later = replace(s, idle_s=s.ttl_s + 1)
        n50 = (P.calls_new_task if s.new_task else P.calls_follow_up)[1]
        if cost_continue(later, n50, P) - cost_switch(later, n50, P, handoff_now_warm=True) >= P.bar_low:
            return "warm"
        return None
    return decide


def free_handoff_on_return(s, n_actual, P, p):
    """Suggested, plus: when you come back after the cache expired to a large conversation, start fresh with a
    handoff built locally from the transcript (no model call). Mid-task, so quality risk is medium or higher."""
    mode = suggested(s, n_actual, P, p)
    if mode or not s.cold or s.new_task or s.context_tokens < P.min_context:
        return mode
    as_packet = replace(s, new_task=True, handoff_exists=True)      # the packet's life: later returns count too
    if cost_continue(as_packet, P.calls_new_task[1], P) - cost_switch(as_packet, P.calls_new_task[1], P) >= P.bar_medium:
        return "free"
    return None


def after_break_rule(s, n_actual, P, p, nxt=None):
    """The plugin's rule (DESIGN 10.6). Cards only after a break; the conversation must be bigger than a new one
    would be even after a 150K re-read; no card when Claude Code compacted the conversation itself on that return
    (the plugin sees that coming); packets go by their risk class; no warm cards at all."""
    if not s.cold:
        return None
    n_pess = P.base_tokens + 150_000 + 1_000
    if s.context_tokens < n_pess:
        return None
    if nxt is not None and nxt.prompt_tokens < 0.5 * s.context_tokens and s.context_tokens >= 200_000:
        return None                              # the real conversation was compacted on return (precomputed)
    pr = lookup(s.model)
    pin, pcr = pr[0] / 1e6, pr[2] / 1e6
    pw = pin * WRITE_MULT.get(s.ttl_s, 2.0)
    N = P.base_tokens + P.reread_tokens + 1_000
    floor = (s.context_tokens - n_pess) * pw
    typical = (s.context_tokens - N) * (pw + P.calls_between_returns[0] * pcr)   # as the plugin: p25, measured
    if s.new_task and p.stem is not None:
        if s.risk == "high":
            return None
        bar = P.bar_low if s.risk == "low" else P.bar_medium
        return "exists" if typical >= bar and floor >= 0.5 else None
    if p.stem is not None:                       # a follow-up round named by its prompt file
        return None
    return "free" if typical >= P.bar_low and floor >= 0.5 else None


def run():
    t0 = time.time()
    calls, prompts, titles, stats = load()
    sessions = build(calls, prompts)
    meas = measure(sessions)
    spans = {}
    for surf in ("code", "cowork"):
        ts = [c.start for c in calls if c.surface == surf and c.kind == "main" and (lookup(c.model) or (0, 0, 0, False))[3]]
        spans[surf] = max((max(ts) - min(ts)) / 86400, 1.0) if ts else 1.0
    prompt_times = sorted(p.ts for p in prompts)

    def presence(a, b):
        i = bisect.bisect_left(prompt_times, a)
        return i < len(prompt_times) and prompt_times[i] <= b

    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "reports"), exist_ok=True)
    with open(os.path.join(HERE, "data", "advisor_params.json"), "w", encoding="utf-8") as fh:
        json.dump({"measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **meas}, fh, indent=2)

    def params_for(surf, reread=None):
        m = meas[surf]
        P = Params(base_tokens=m["base_tokens"], base_shared=m["base_shared"],
                   calls_new_task=tuple(m["calls_new_task"]), calls_follow_up=tuple(m["calls_follow_up"]),
                   calls_between_returns=tuple(m["calls_between_returns"]),
                   growth_per_call=m["growth_per_call"], compact_at=m["compact_at"], cold_returns=m["cold_returns"])
        return replace(P, reread_tokens=reread) if reread is not None else P

    policies = [
        ("Suggested: packets that stand alone or allow a new chat", suggested, None),
        ("Every new packet, cost only (80K re-read)", every_packet, None),
        ("Every new packet, cost only (no re-read)", every_packet, 0),
        ("Every new packet, hindsight call counts (80K re-read)", hindsight, None),
        ("Suggested + handoff before expiry when you were around (upper bound)", handoff_when_around(presence), None),
        ("Suggested + free transcript handoff when you return after an expiry", free_handoff_on_return, None),
        ("The plugin's after-break rule (only after a break, worst case must save)", after_break_rule, None),
    ]
    results, dump = [], {}
    for name, decide, reread in policies:
        agg = collections.defaultdict(lambda: {"saving": 0.0, "switches": 0, "risk": collections.Counter(),
                                               "risk_usd": collections.Counter(), "losing": 0})
        for (surf, sess), (cs, marked) in sessions.items():
            P = params_for(surf, reread)
            total, sw = simulate(cs, marked, P, decide, meas[surf]["compact_to"])
            a = agg[surf]
            a["saving"] += total
            a["switches"] += len(sw)
            for x in sw:     # marginal value: the same replay without this one switch
                without, _ = simulate(cs, marked, P, decide, meas[surf]["compact_to"], skip=frozenset({x["ts"]}))
                x["marginal"] = total - without
                kind = x["risk"] if x["mode"] == "exists" else "mid-task"
                a["losing"] += x["marginal"] < 0
                a["risk"][kind] += 1
                a["risk_usd"][kind] += x["marginal"]
                dump.setdefault(name, []).append({**x, "surface": surf, "session": sess})
        results.append((name, dict(agg)))
    with open(os.path.join(HERE, "data", "switch_points.json"), "w", encoding="utf-8") as fh:
        json.dump(dump, fh, indent=1)

    multi, last_multi = collections.Counter(), {}
    for (surf, sess), (cs, marked) in sessions.items():
        roots = {root_of(m[0].stem) for m in marked if m[0].stem}
        if len(roots) > 1:
            multi[surf] += 1
            last_multi[surf] = max(last_multi.get(surf, 0), max(m[0].ts for m in marked))
    write_report(results, meas, spans, stats, multi, last_multi, time.time() - t0)
    return results


def write_report(results, meas, spans, stats, multi, last_multi, secs):
    P = Params()
    loc = lambda t: datetime.fromtimestamp(t).strftime("%b %d") if t else "none"
    L = ["# Switch-conversation rule: backtest on your history", "",
         f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from {stats['files']} transcript files "
         f"({stats['cowork_files']} Cowork) in {secs:.0f} s. Priced history: {spans['code']:.0f} days of Code, "
         f"{spans['cowork']:.0f} days of Cowork (calls on models with exact prices).",
         "",
         "$ = API-equivalent dollars. "
         + (f"% = share of your weekly limit at ${P.usd_per_pt[0]} to ${P.usd_per_pt[1]} per 1% (from data/plan.json). "
            if P.usd_per_pt else "% of the weekly limit appears once you set your rate in data/plan.json. ")
         + "Savings are net of every switch's overhead. A switch's value is measured by replaying the session "
         "without it.",
         "",
         "## Results", "",
         "| Policy | Surface | Switches | By risk (count) | By risk ($) | Net saving | Per week | % of weekly limit, per week | Switches that lost |",
         "|---|---|---|---|---|---|---|---|---|"]
    for name, agg in results:
        for surf in ("code", "cowork"):
            a = agg.get(surf)
            if not a:
                continue
            wk = a["saving"] / (spans[surf] / 7)
            rc = ", ".join(f"{k} {v}" for k, v in a["risk"].most_common()) or "none"
            ru = ", ".join(f"{k} ${v:.0f}" for k, v in sorted(a["risk_usd"].items(), key=lambda kv: -kv[1])) or "none"
            share = f"{wk / P.usd_per_pt[0]:.1f}–{wk / P.usd_per_pt[1]:.1f}%" if P.usd_per_pt else "—"
            L.append(f"| {name} | {surf} | {a['switches']} | {rc} | {ru} | ${a['saving']:.0f} | ${wk:.0f} | "
                     f"{share} | {a['losing']} |")
    L += ["", "## Inputs measured from your sessions (typical defaults where a surface has no history)", "",
          "| | Code | Cowork |", "|---|---|---|"]
    for k, label in [("base_tokens", "Fresh-session size, median tokens"), ("base_shared", "…read from the shared cache"),
                     ("calls_new_task", "Main-thread calls over a packet's life (p25, p50, p75)"),
                     ("cold_returns", "Returns after an expiry during a packet's life (mean)"),
                     ("calls_follow_up", "Main-thread calls per follow-up (p25, p50, p75)"),
                     ("growth_per_call", "Context growth per call, median tokens"),
                     ("compact_at", "Auto-compaction point, median tokens"), ("compact_to", "…compacted to"),
                     ("sessions", "Sessions"), ("new_prompts", "New-packet prompts"), ("follow_prompts", "Follow-up prompts")]:
        L.append(f"| {label} | {meas['code'][k]} | {meas['cowork'][k]} |")
    L += ["", f"Sessions that named more than one packet family: Code {multi.get('code', 0)} "
          f"(latest {loc(last_multi.get('code'))}), Cowork {multi.get('cowork', 0)}.",
          "", "## How to read this", "",
          "- **What changes:** only the resent context of main-thread calls. Subagents and workflow agents keep "
          "their own contexts, so a new conversation doesn't change their cost.",
          "- **Risk** comes from the packet's header (packets.py):",
          "  - low: a separate piece of work;",
          "  - medium: the header allows a new session, or the packet builds from files on disk;",
          "  - high: written for this conversation, a correction or follow-up round, or a stale file.",
          "  High-risk packets never get a suggestion.",
          "- **Quality is not measured.** One avoidable review round can cost more than a switch saves, so a few "
          "of them would cancel the cost-only savings.",
          "- **Habits:** the weekly figure reflects the habits in this history. A session that carries one packet "
          "leaves nothing to switch.",
          "- **In-sample:** the call-count inputs come from the same history."]
    with open(os.path.join(HERE, "reports", "switch_backtest.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    run()
    print(open(os.path.join(HERE, "reports", "switch_backtest.md"), encoding="utf-8").read())
