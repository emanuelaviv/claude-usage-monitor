"""When is starting a new conversation cheaper than continuing this one?

Every request resends the whole conversation.
- While the prompt cache is warm, the resent part is a cheap cache read ($0.20 per M tokens on Opus 5.5).
- Once the cache has expired (1 hour for main threads), the next request writes it again at the cache-write
  price ($8 per M with the 1-hour lifetime): 40 times a read. Only the shared system/tools prefix stays cached.

A new conversation starts from the fresh-session size B, about 80K tokens in Code, of which about 45K is usually
read from a shared cache. It then re-reads files the old conversation already held: the re-read allowance R,
about 80K in Code. Costs here cover the resent context of the next n main-thread calls. Output and new content
cost the same either way, so they cancel. The horizon stops at the next auto-compaction (about 970K tokens):
after that, both conversations are small.

Quality is the other half. A suggestion is only made for packets that stand on their own (low risk) or whose
header allows a new session (medium risk, higher bar). It is never made for follow-ups or rounds written for
this conversation. See packets.py.
"""
import json
import os
from dataclasses import dataclass, field

from .prices import WRITE_MULT, lookup

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plan_rate():
    """Your $ per 1% of the weekly limit, as (high, low), from data/plan.json: {"usd_per_pt": [high, low]}.
    None until you set it: then figures are in dollars only."""
    try:
        with open(os.path.join(_REPO, "data", "plan.json"), encoding="utf-8") as fh:
            r = json.load(fh).get("usd_per_pt")
    except (OSError, ValueError, AttributeError):
        return None
    ok = isinstance(r, list) and len(r) >= 2 and all(isinstance(x, (int, float)) and x > 0 for x in r[:2])
    return (r[0], r[1]) if ok else None


@dataclass
class Params:
    base_tokens: int = 79_000         # B: prompt size of a fresh session's first call (median, Code)
    base_shared: int = 45_000         # part of B read from cache even in a new session or after an expiry
    reread_tokens: int = 80_000       # R: files a new conversation has to read again
    handoff_tokens: int = 6_000       # H: a handoff summary written mid-task
    calls_new_task: tuple = (39, 68, 86)     # main-thread calls over a packet's life (p25, p50, p75)
    calls_follow_up: tuple = (7, 17, 79)     # main-thread calls a follow-up prompt takes
    calls_between_returns: tuple = (45, 115)  # main-thread calls from one return after a break to the next (p25, p50)
    cold_returns: float = 1.0         # returns after the cache expired during a packet's life (mean)
    growth_per_call: int = 3_000      # median tokens a conversation grows per call
    compact_at: int = 969_000         # auto-compaction point
    bar_low: float = 2.0              # minimum expected saving to suggest a switch, low-risk packet
    bar_medium: float = 5.0           # ... medium-risk packet
    min_context: int = 250_000        # below this a switch is never suggested
    expiry_lead_s: int = 600          # warn this long before the cache expires (only with signs you are here)
    usd_per_pt: tuple | None = field(default_factory=lambda: plan_rate())   # $ per 1% of your weekly limit; None: $ only


@dataclass
class State:
    model: str
    context_tokens: int    # C: what the next request resends (last prompt + last response)
    idle_s: float          # seconds since the last response
    ttl_s: int = 3600      # cache lifetime this conversation uses (3600 main threads, 300 agents)
    new_task: bool = False         # the next prompt names a new packet
    risk: str = "high"             # packet risk from packets.classify: low | medium | high
    handoff_exists: bool = False   # a written packet file carries what is needed

    @property
    def cold(self):
        return self.idle_s > self.ttl_s


@dataclass
class Advice:
    action: str              # "new conversation" | "continue"
    risk: str
    saving_usd: tuple        # expected saving of switching at p25 / p50 / p75 of the call count
    resend_usd: float        # what the next request costs to resend the context (a re-cache when cold)
    reason: str
    notes: list = field(default_factory=list)


def _prices(model, ttl_s):
    pin, pout, pcr, _ = lookup(model) or lookup("claude-opus-5-5")
    return pin / 1e6, pout / 1e6, pcr / 1e6, pin * WRITE_MULT.get(ttl_s, 2.0) / 1e6


def _horizon(s, n, P):
    """Calls until this conversation would auto-compact, capped at n."""
    left = max(P.compact_at - s.context_tokens, 0) // max(P.growth_per_call, 1)
    return max(1, min(n, left))


def resend_cost(s, P):
    pin, pout, pcr, pw = _prices(s.model, s.ttl_s)
    c = s.context_tokens
    if not s.cold:
        return c * pcr
    shared = min(P.base_shared, c)
    return shared * pcr + (c - shared) * pw


def _recache_extra(tokens, s, P):
    """Extra cost of one later return after an expiry, over a warm read, for a conversation of this size."""
    pin, pout, pcr, pw = _prices(s.model, s.ttl_s)
    shared = min(P.base_shared, tokens)
    return (tokens - shared) * (pw - pcr)


def cost_continue(s, n, P):
    pin, pout, pcr, pw = _prices(s.model, s.ttl_s)
    n = _horizon(s, n, P)
    k = P.cold_returns if s.new_task else 0.0
    return resend_cost(s, P) + (n - 1) * s.context_tokens * pcr + k * _recache_extra(s.context_tokens, s, P)


def cost_switch(s, n, P, handoff_now_warm=False):
    """Cost of moving the next n calls to a new conversation."""
    pin, pout, pcr, pw = _prices(s.model, s.ttl_s)
    n = _horizon(s, n, P)
    handoff = 0.0
    size = P.base_tokens + P.reread_tokens
    if not s.handoff_exists:   # one more request here writes a summary; it re-caches if the cache expired
        warm = State(s.model, s.context_tokens, 0, s.ttl_s)
        handoff = resend_cost(warm if (handoff_now_warm or not s.cold) else s, P) + P.handoff_tokens * pout
        size += P.handoff_tokens
    first = P.base_shared * pcr + max(size - P.base_shared, 0) * pw
    k = P.cold_returns if s.new_task else 0.0
    return handoff + first + (n - 1) * size * pcr + k * _recache_extra(size, s, P)


def advise(s: State, P: Params) -> Advice:
    calls = P.calls_new_task if s.new_task else P.calls_follow_up
    savings = tuple(cost_continue(s, n, P) - cost_switch(s, n, P) for n in calls)
    resend = resend_cost(s, P)
    notes = []
    if s.cold:
        notes.append(f"The cache expired {int((s.idle_s - s.ttl_s) / 60)} min ago, so the next message here "
                     f"re-caches {s.context_tokens / 1000:.0f}K tokens (${resend:.2f}).")
        if s.context_tokens >= 280_000:
            notes.append("A conversation this size is sometimes compacted on return instead, which costs less.")
    else:
        left = s.ttl_s - s.idle_s
        notes.append(f"The cache stays warm for about {int(left / 60)} more min.")
    bar = {"low": P.bar_low, "medium": P.bar_medium}.get(s.risk) if s.new_task else None
    if bar is not None and s.context_tokens >= P.min_context and savings[1] >= bar:
        reason = (f"A new conversation should save about ${savings[1]:.2f} over this packet (${savings[0]:.2f} "
                  f"to ${savings[2]:.2f}), after re-reading about {P.reread_tokens // 1000}K tokens of files.")
        return Advice("new conversation", s.risk, savings, resend, reason, notes)
    if not s.new_task:
        reason = "No switch suggested: this is a follow-up, and a new conversation would lose its context."
    elif s.risk == "high":
        reason = "No switch suggested: this packet builds on this conversation's own work."
    elif s.context_tokens < P.min_context:
        reason = "No switch suggested: the conversation is still small."
    else:
        reason = f"No switch suggested: the saving (about ${savings[1]:.2f}) is below the ${bar:.0f} bar for {s.risk} risk."
    return Advice("continue", s.risk, savings, resend, reason, notes)


def pts(usd, P: Params):
    """$ as % of the weekly limit at each candidate rate, e.g. '0.2-0.6%'; None until your rate is set."""
    if not P.usd_per_pt:
        return None
    lo, hi = sorted(usd / r for r in P.usd_per_pt)
    return f"{lo:.1f}-{hi:.1f}%" if hi >= 0.05 else "<0.1%"
