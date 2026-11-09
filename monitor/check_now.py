"""Live switch advice for your recently active conversations (read-only).

Usage: python -m monitor.check_now [--hours 24] [--hide-titles]

For each Code or Cowork conversation with a reply in the last few hours, it shows:
- how big the conversation is;
- whether its prompt cache is still warm;
- what the next message will cost to resend;
- whether starting a new conversation would pay off, if your next prompt is a packet that stands on its own.

Whether a particular packet stands on its own is decided when it is named (see packets.py). This command can't
know your next prompt, so the packet line is conditional.
"""
import argparse
import json
import os
import time
from datetime import datetime

from .backtest_switch import HERE, session_ttl
from .ingest import load
from .switch_advisor import Params, State, advise, pts


def params(surface):
    P = Params()
    try:
        with open(os.path.join(HERE, "data", "advisor_params.json"), encoding="utf-8") as fh:
            m = json.load(fh)[surface]
        P = Params(base_tokens=m["base_tokens"], base_shared=m["base_shared"],
                   calls_new_task=tuple(m["calls_new_task"]), calls_follow_up=tuple(m["calls_follow_up"]),
                   growth_per_call=m["growth_per_call"], compact_at=m["compact_at"], cold_returns=m["cold_returns"])
    except (OSError, KeyError, ValueError):
        pass
    return P


def main(hours=24, hide_titles=False):
    now = time.time()
    calls, _, titles, _ = load(since=now - hours * 3600)
    sessions = {}
    for c in calls:
        if c.kind == "main":
            sessions.setdefault((c.surface, c.session), []).append(c)
    rows = [(max(cs, key=lambda c: c.end), surf, sess, cs) for (surf, sess), cs in sessions.items()]
    rows = [r for r in rows if now - r[0].end <= hours * 3600]
    if not rows:
        print(f"No Code or Cowork replies in the last {hours} h.")
        return
    for last, surf, sess, cs in sorted(rows, key=lambda r: -r[0].end):
        P = params(surf)
        ttl = session_ttl(cs)
        C = last.prompt_tokens + last.out
        idle = now - last.end
        base = State(last.model, C, idle, ttl)
        title = "" if hide_titles else titles.get(sess, "")
        when = datetime.fromtimestamp(last.end).strftime("%a %H:%M")
        warm_until = datetime.fromtimestamp(last.end + ttl).strftime("%H:%M")
        follow = advise(base, P)
        print(f"\n{surf.title()} {sess[:8]} {('“' + title + '”') if title else ''}")
        print(f"  {C / 1000:.0f}K tokens of context · last reply {when} ({idle / 60:.0f} min ago) · {last.model}")
        notes = [n for n in follow.notes if not n.startswith("The cache stays warm")]
        print(f"  " + (f"Cache warm until {warm_until}. " if not base.cold else "") + " ".join(notes))
        week = pts(follow.resend_usd, P)
        print(f"  Your next message here resends the context for ${follow.resend_usd:.2f}"
              + (f" ({week} of your weekly limit)." if week else "."))
        if surf == "cowork":
            print("  New Cowork tasks run in the cloud: they start without this session's memory, and this tool "
                  "can't see them, so no switch is suggested here.")
            continue
        low = advise(State(last.model, C, idle, ttl, new_task=True, risk="low", handoff_exists=True), P)
        med = advise(State(last.model, C, idle, ttl, new_task=True, risk="medium", handoff_exists=True), P)
        if low.action == "new conversation":
            week = pts(low.saving_usd[1], P)
            line = (f"  If your next prompt is a packet that stands on its own: start a new conversation. {low.reason}"
                    + (f" That is about {week} of your weekly limit." if week else ""))
            if med.action != "new conversation":
                line += " For a packet that only allows a new session, the saving is below the $5 bar."
            print(line)
        else:
            print(f"  Even for a packet that stands on its own: {low.reason[0].lower() + low.reason[1:]}")
        print("  For a follow-up or a round written for this conversation: continue here.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--hide-titles", action="store_true")
    a = ap.parse_args()
    main(a.hours, a.hide_titles)
