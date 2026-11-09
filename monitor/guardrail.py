"""Guardrail for the after-break switch: did conversations started from a handoff go worse? Local, no model calls.

Usage: python -m monitor.guardrail

Reads the plugin's decisions (data/switch_decisions/*.json) and your transcripts:
- For each accepted switch it finds the new conversation: its first prompt starts with the saved handoff.
- Over its first 3 prompts and 40 main-thread calls, it checks for trouble:
  - a correction or rework round (a PROMPT_*.md with a fix / rN / amend / followup suffix); or
  - you going back to the old conversation within 24 h.
- The baseline is your returns after an expiry that continued in place; every "Keep going here anyway" is one of them.

The stop rule (DESIGN.md §10.6) needs at least 5 switches. After that, the after-break card is turned off when:
  P(trouble rate after a switch > baseline rate + delta) >= 0.80, with delta = 0.15; or
  the saving counted so far is below $0.
Turning off means data/guardrail.json gets after_break_enabled: false; the plugin reads it when a session starts.
It is re-enabled only by hand: set the value to true, and the guardrail leaves it on from then on (it keeps reporting
what its rule says). Deleting the file instead starts the guardrail over, so it turns the card off again while the rule
still says so.
"""
import glob
import json
import os
import random
import time
from datetime import datetime, timezone

from .ingest import CODE_ROOT, _human_text, load, root_of
from .prices import call_cost

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DELTA, P_STOP, MIN_SWITCHES = 0.15, 0.80, 5


def _first_prompt_text(session_id):
    """The first prompt of a Code conversation, read in memory only."""
    for path in glob.glob(os.path.join(CODE_ROOT, "*", session_id + ".jsonl")):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"user"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                text = _human_text(d)
                if text is not None:
                    return text
    return None


EARLIER = "Turned off earlier: "
KEPT_ON = "You turned it back on, so the guardrail leaves it on. Its rule now says: "


def _was_off(prev):
    """True when an earlier run turned the card off: it marks the file "turned_off" (files written before that mark
    existed are known by their reason). A file without "updated", which every run writes, was written by hand."""
    return prev.get("turned_off") is True or "updated" not in prev or str(prev.get("reason", "")).startswith(
        ("After a switch, trouble came", "The switches so far cost more", EARLIER, KEPT_ON))


def read_prev(path):
    """What guardrail.json holds: ({...}, True), or ({}, True) when there is no file, or ({}, False) when there is one
    this can't read (UTF-16 from Windows PowerShell's Out-File, say), which may be your hand edit and is left alone.
    A UTF-8 file with a byte order mark (PowerShell's Set-Content -Encoding UTF8) reads as usual."""
    if not os.path.exists(path):
        return {}, True
    try:
        with open(path, encoding="utf-8-sig") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        return {}, False
    return (prev, True) if isinstance(prev, dict) else ({}, False)


def settle(out, prev):
    """This run's verdict `out`, given what guardrail.json held before (`prev`): once off, the card stays off until you
    set after_break_enabled back to true by hand, and from then on it stays on (the reason still gives what the rule
    says). A file you deleted starts it over. Changes and returns `out`."""
    if prev.get("after_break_enabled") is False:
        before = str(prev.get("reason", ""))
        out["after_break_enabled"], out["reason"] = False, before if before.startswith(EARLIER) else EARLIER + before
    elif prev.get("after_break_enabled") is True and _was_off(prev):
        out["after_break_enabled"], out["kept_on_by_hand"] = True, True
        out["reason"] = KEPT_ON + out["reason"]
    if out["after_break_enabled"] is False or out.get("kept_on_by_hand"):
        out["turned_off"] = True
    return out


def _trouble(prompts_after, old_session_prompts, ts):
    """True if the next prompts are a correction round or you went back to the old conversation."""
    for p in prompts_after[:3]:
        if p.stem and root_of(p.stem) != p.stem.lower():
            return True
    return any(ts < q.ts <= ts + 86400 for q in old_session_prompts)


def _p_worse(k, n, k0, n0, draws=20000, seed=7):
    rnd = random.Random(seed)
    hits = 0
    for _ in range(draws):
        ps = rnd.betavariate(0.5 + k, 0.5 + n - k)
        pb = rnd.betavariate(0.5 + k0, 0.5 + n0 - k0)
        hits += ps > pb + DELTA
    return hits / draws


def main():
    decisions = []
    for f in sorted(glob.glob(os.path.join(HERE, "data", "switch_decisions", "*.json"))):
        try:
            decisions.append(json.load(open(f, encoding="utf-8")))
        except (OSError, ValueError):
            pass
    calls, prompts, _, _ = load(surfaces=("code",))
    by_session = {}
    for p in prompts:
        by_session.setdefault(p.session, []).append(p)
    first_ts = {s: ps[0].ts for s, ps in by_session.items()}

    switches, trouble, saved = 0, 0, 0.0
    for d in decisions:
        if d.get("kind") != "after-break" or d.get("choice") != "switch":
            continue
        ts = d["ts"] / 1000
        head = d.get("handoffFirstLine", "")
        new = [s for s, t0 in first_ts.items() if ts <= t0 <= ts + 7200 and head
               and (_first_prompt_text(s) or "").startswith(head[:80])]
        if not new:
            continue
        switches += 1
        trouble += _trouble(by_session[new[0]][1:], by_session.get(d["session"], []), ts)
        first = next((c for c in calls if c.session == new[0] and c.kind == "main"), None)
        # the re-cache avoided minus what the new conversation's first request really cost, at its model's prices
        first_cost = call_cost(first.model, first.inp, 0, first.cr, first.cw5, first.cw1h) if first else None
        resend = (d.get("numbers") or {}).get("resendHere")
        if first_cost is not None and resend is not None:   # a decision logged without its numbers can't be valued
            saved += resend - first_cost

    # baseline: your returns after an expiry to a large conversation that continued in place
    n0 = k0 = 0
    main_calls = {}
    for c in calls:
        if c.kind == "main":
            main_calls.setdefault(c.session, []).append(c)
    for s, ps in by_session.items():
        cs = main_calls.get(s, [])
        for i, p in enumerate(ps[1:], 1):
            prev = [c for c in cs if c.end <= p.ts]
            if not prev or p.ts - prev[-1].end <= 3600 or prev[-1].prompt_tokens < 230_000:
                continue
            n0 += 1
            k0 += _trouble(ps[i + 1:], [], p.ts)
    # every "Keep going here anyway" is one of these returns (a break over an hour in a conversation of 230K or more),
    # so the history above already counts it, with its outcome

    p_worse = _p_worse(trouble, switches, k0, n0) if switches else 0.0
    enabled, reason = True, f"{switches} switches so far; at least {MIN_SWITCHES} are needed before the rule can stop."
    if switches >= MIN_SWITCHES:
        if p_worse >= P_STOP:
            enabled, reason = False, (f"After a switch, trouble came {trouble}/{switches} times, against {k0}/{n0} when you "
                                      f"continued in place: P(worse by more than {DELTA:.2f}) = {p_worse:.2f}.")
        elif saved < 0:
            enabled, reason = False, f"The switches so far cost more than they saved (${saved:.2f})."
        else:
            reason = f"OK: trouble {trouble}/{switches} after switches vs {k0}/{n0} in place; saved ${saved:.2f}."
    out = {"after_break_enabled": enabled, "reason": reason, "switches": switches, "trouble": trouble,
           "baseline": [k0, n0], "p_worse": round(p_worse, 3), "saved_usd": round(saved, 2),
           "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = os.path.join(HERE, "data", "guardrail.json")
    prev, readable = read_prev(path)
    if not readable:
        print(json.dumps(out, indent=2))
        print(f"{path} can't be read as JSON, so the guardrail leaves it as it is (the plugin then keeps the card on).")
        return
    settle(out, prev)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
