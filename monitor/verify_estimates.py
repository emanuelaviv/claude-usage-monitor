"""Check the switch card's estimates against what really happened. Local, no model calls.

Usage: python -m monitor.verify_estimates     (the nightly job runs it too)

1. Re-cache cost. For every return after a break, the card's "sending here re-caches C tokens: about $X" is
   compared with what the first call after the return really cost.
2. New-conversation cost. The fresh-session size and the 80K re-read allowance (150K worst case) are compared
   with how fresh sessions really started and grew. Growth in their first 40 calls is set against the growth of
   a continuing conversation after a return.
3. Savings. For the plugin rule's switches in the replay, "you save $X now" and "typically $A-$B by your next
   break" are compared with the replayed value of each switch.
4. Live cards. Each card the plugin showed (data/switch_decisions/) is checked against the next real call:
   - "Send here": its re-cache estimate against that call's cost;
   - a switch: its new-conversation estimate against the new conversation's first call.

Writes reports/estimate_check.md.
"""
import glob
import json
import os
import statistics as st
from datetime import datetime

from .backtest_switch import build, is_drop, session_ttl
from .ingest import load
from .prices import lookup

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _params():
    try:
        return json.load(open(os.path.join(HERE, "data", "advisor_params.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_cost(tokens, shared, model):
    """$ to cache `tokens` from scratch, the shared prefix read; None for a model without a known price."""
    p = lookup(model, tokens)
    if p is None:
        return None
    pin, _, pcr, _ = p
    s = min(shared, tokens)
    return (s * pcr + (tokens - s) * pin * 2) / 1e6


def ctx_cost(c):
    """$ of one call's input side; None for a model without a known price."""
    p = lookup(c.model, c.inp + c.cr + c.cw5 + c.cw1h)
    if p is None:
        return None
    pin, _, pcr, _ = p
    return (c.inp * pin + c.cr * pcr + c.cw5 * pin * 1.25 + c.cw1h * pin * 2) / 1e6


def summary(pairs):
    """pairs: (predicted, actual). Returns n, medians, median absolute % error and median bias (%)."""
    if not pairs:
        return None
    errs = [(p - a) / a * 100 for p, a in pairs if a > 0]
    return {"n": len(pairs), "pred": st.median(p for p, _ in pairs), "actual": st.median(a for _, a in pairs),
            "mape": st.median(abs(e) for e in errs) if errs else None, "bias": st.median(errs) if errs else None,
            "p90": sorted(abs(e) for e in errs)[int(0.9 * (len(errs) - 1))] if errs else None}


def fmt(s, money=True):
    if not s:
        return "| — | — | — | — | — | — |"
    m = (lambda x: f"${x:.2f}") if money else (lambda x: f"{x / 1000:.0f}K")
    return (f"| {s['n']} | {m(s['pred'])} | {m(s['actual'])} | {s['mape']:.0f}% | {s['bias']:+.0f}% | {s['p90']:.0f}% |"
            if s["mape"] is not None else f"| {s['n']} | {m(s['pred'])} | {m(s['actual'])} | — | — | — |")


def main():
    params = _params()
    calls, prompts, _, _ = load()
    sessions = build(calls, prompts, classify_risk=False)
    out = ["# Do the switch card's estimates hold?", "",
           f"Generated {datetime.now():%Y-%m-%d %H:%M} from your transcripts (calls on models with exact prices).", ""]

    # 1. re-cache cost at returns after a break
    groups = {"code": {"re-cached": [], "compacted by Claude Code": []}, "cowork": {"re-cached": [], "compacted by Claude Code": []}}
    for (surf, _), (cs, marked) in sessions.items():
        ttl = session_ttl(cs)
        shared = (params.get(surf) or {}).get("base_shared", 45_240)
        for m in marked:
            p = m[0]
            prev = [c for c in cs if c.end <= p.ts]
            after = [c for c in cs if c.start >= p.ts]
            if not prev or not after or p.ts - prev[-1].end <= ttl:
                continue
            last, first = prev[-1], after[0]
            if not (lookup(last.model) or (0, 0, 0, False))[3] or last.prompt_tokens + last.out < 100_000:
                continue
            pred, actual = write_cost(last.prompt_tokens + last.out, shared, last.model), ctx_cost(first)
            if pred is None or actual is None:     # the call after the return ran on a model without a known price
                continue
            kind = "compacted by Claude Code" if is_drop(last.prompt_tokens, first.prompt_tokens) else "re-cached"
            groups[surf][kind].append((pred, actual))
    out += ["## 1. \"Sending here re-caches C tokens: about $X\"", "",
            "Each return after a break, with a context of at least 100K tokens. % error = (estimate − actual) / actual; bias above 0 means the card overstates.", "",
            "| Surface | What happened | Returns | Estimate (median) | Actual (median) | Typical error | Bias | 90% of errors under |",
            "|---|---|---|---|---|---|---|---|"]
    for surf, g in groups.items():
        for kind, pairs in g.items():
            out.append(f"| {surf} | {kind} " + fmt(summary(pairs)))
    out += ["", "When Claude Code compacted the conversation itself on return, continuing was cheap. The plugin "
            "doesn't show the card then: it sees the summary being prepared during the break.", ""]

    # 2. new conversation: fresh-session size and re-read allowance
    out += ["## 2. \"A new conversation starts at about N tokens\"", ""]
    for surf in ("code", "cowork"):
        pr = params.get(surf) or {}
        B, shared = pr.get("base_tokens", 79_333), pr.get("base_shared", 45_240)
        firsts = [cs[0] for (s, _), (cs, _) in sessions.items() if s == surf and cs and (lookup(cs[0].model) or (0, 0, 0, False))[3]]
        pairs = [(write_cost(B, shared, f.model), ctx_cost(f)) for f in firsts]
        s1 = summary(pairs)
        # growth over the first 40 calls of a fresh session vs 40 calls after a return in a continuing one
        fresh, cont = [], []
        for (s, _), (cs, marked) in sessions.items():
            if s != surf or len(cs) < 41:
                continue
            fresh.append(cs[40].prompt_tokens - cs[0].prompt_tokens)
            ttl = session_ttl(cs)
            for m in marked:
                i = next((k for k, c in enumerate(cs) if c.start >= m[0].ts), None)
                if i and i + 40 < len(cs) and m[0].ts - cs[i - 1].end > ttl and not any(
                        is_drop(cs[j - 1].prompt_tokens, cs[j].prompt_tokens) for j in range(i, i + 41)):
                    cont.append(cs[i + 40].prompt_tokens - cs[i].prompt_tokens)
        excess = (st.median(fresh) - st.median(cont)) if fresh and cont else None
        out += [f"**{surf.title()}:** first request of a fresh session, estimate vs actual:", "",
                "| Sessions | Estimate (median) | Actual (median) | Typical error | Bias | 90% of errors under |", "|---|---|---|---|---|---|",
                fmt(summary(pairs)).replace("| ", "| ", 1), ""]
        if excess is not None:
            out.append(f"Growth over the first 40 calls: fresh sessions {st.median(fresh) / 1000:.0f}K (n={len(fresh)}), "
                       f"continuing after a return {st.median(cont) / 1000:.0f}K (n={len(cont)}). The extra, about "
                       f"{excess / 1000:.0f}K, is the re-reading a new conversation does. The card assumes 80K, and 150K in the worst case.")
        out.append("")

    # 3. savings: the plugin rule's switches in the replay
    try:
        pts = json.load(open(os.path.join(HERE, "data", "switch_points.json"), encoding="utf-8"))
        rule = next((v for k, v in pts.items() if k.startswith("The plugin's after-break rule")), [])
    except (OSError, ValueError):
        rule = []
    out += ["## 3. \"You save $X now, typically $A-$B by your next break\"", "",
            "Replay of the plugin's rule. 'Realized' is each switch's own value: the session replayed with and without it.", "",
            "| Surface | Switches | 'Save now' (median) | Realized (median) | Realized below 'save now' | Realized inside or above the typical range |", "|---|---|---|---|---|---|"]
    for surf in ("code", "cowork"):
        pr = params.get(surf) or {}
        B, shared = pr.get("base_tokens", 79_333), pr.get("base_shared", 45_240)
        n25, n50 = pr.get("calls_between_returns", [45, 115])
        N = B + 80_000 + 1_000
        rows = [x for x in rule if x["surface"] == surf]
        if not rows:
            out.append(f"| {surf} | 0 | — | — | — | — |")
            continue
        model = lambda x: x.get("model") or "claude-opus-5-5"     # replays from before the model was recorded: Opus
        rows = [x for x in rows if lookup(model(x), x["context"]) is not None]
        if not rows:
            out.append(f"| {surf} | 0 | — | — | — | — |")
            continue
        now = [write_cost(x["context"], shared, model(x)) - write_cost(N, shared, model(x)) for x in rows]
        rate = lambda x: lookup(model(x), x["context"])
        lo = [(x["context"] - N) * (2 * rate(x)[0] + n25 * rate(x)[2]) / 1e6 for x in rows]
        real = [x["marginal"] for x in rows]
        below = sum(r < s for r, s in zip(real, now))
        inside = sum(r >= l for r, l in zip(real, lo))
        out.append(f"| {surf} | {len(rows)} | ${st.median(now):.2f} | ${st.median(real):.2f} | {below} of {len(rows)} | {inside} of {len(rows)} |")
    out.append("")

    # 4. live cards
    live = []
    for f in sorted(glob.glob(os.path.join(HERE, "data", "switch_decisions", "*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("kind") == "test":
            continue
        ts = d["ts"] / 1000
        nxt = next((c for c in calls if c.kind == "main" and c.start >= ts and
                    (c.session == d.get("session")) == (d.get("choice") == "stay")), None)
        actual = ctx_cost(nxt) if nxt else None
        if actual is not None and d.get("numbers"):
            key = "resendHere" if d.get("choice") == "stay" else "newConv"
            live.append((d["choice"], d["numbers"].get(key, 0), actual))
    out += ["## 4. Cards shown so far", ""]
    if live:
        for choice in ("stay", "switch"):
            pairs = [(p, a) for c, p, a in live if c == choice]
            if pairs:
                s = summary(pairs)
                out.append(f"- {choice}: {s['n']} cards; estimate median ${s['pred']:.2f}, actual ${s['actual']:.2f}, "
                           f"typical error {s['mape']:.0f}%, bias {s['bias']:+.0f}%.")
    else:
        out.append("None yet (test cards are left out). The nightly job adds them as they come.")
    path = os.path.join(HERE, "reports", "estimate_check.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()
