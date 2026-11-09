"""OpenAI API list prices for the models Codex runs, in $ per million tokens (developers.openai.com/api/docs/pricing,
read on 2026-11-07).

Cached input is part of the input count in Codex's logs, so a request costs
    (input - cached) * input price + cached * cached price + output * output price.
A request with more than 272K input tokens is billed at the long-context rates as a whole. Fast mode costs twice the
standard rates (2.5 times on GPT-5.5) and Ultrafast six times; Codex logs Fast mode's service tier as "priority"
("fast" is read too). On a ChatGPT plan these dollars measure how much of your limits a request uses (Codex credits are
25 per dollar on the GPT-6 and GPT-5.6 models), not what you pay. Codex records no cache writes, so none are charged
here.
"""

LONG_AT = 272_000

# model: ((input, cached input, output) up to 272K input tokens, (the same) above it, or None)
PRICES = {
    "gpt-6.1-sol": ((2.00, 0.10, 10.00), (4.00, 0.20, 15.00)),
    "gpt-6.1-terra": ((1.00, 0.05, 5.00), (2.00, 0.10, 7.50)),
    "gpt-6-astra": ((10.00, 1.00, 50.00), (20.00, 2.00, 75.00)),
    "gpt-6-sol": ((2.00, 0.20, 10.00), (4.00, 0.40, 15.00)),
    "gpt-6-luna": ((0.10, 0.01, 0.50), (0.20, 0.02, 0.75)),
    "gpt-5.6-sol": ((4.00, 0.40, 20.00), (8.00, 0.80, 30.00)),
    "gpt-5.6-terra": ((2.00, 0.20, 12.00), (4.00, 0.40, 18.00)),
    "gpt-5.6-luna": ((0.20, 0.02, 1.20), (0.40, 0.04, 1.80)),
    "gpt-5.5": ((5.00, 0.50, 30.00), (10.00, 1.00, 45.00)),     # left Codex with ChatGPT sign-in on 2026-10-14
    "gpt-5.3-codex": ((1.75, 0.175, 14.00), None),
}
TIER = {"fast": 2.0, "priority": 2.0, "ultrafast": 6.0}
FAST = {"gpt-5.5": 2.5}           # models whose Fast mode costs other than twice Standard


def rates(model, input_tokens=0, tier=None):
    """($ per token) input, cached input, output for one request, or None for a model without a known price. A name
    with a date or a suffix gets the longest known name it starts with, but a Pro model is never priced as its base
    model."""
    if not model:
        return None
    m = model.lower().strip()
    key = m if m in PRICES else next((k for k in sorted(PRICES, key=len, reverse=True) if m.startswith(k)), None)
    if key is None or ("-pro" in m and "-pro" not in key):
        return None
    short, long = PRICES[key]
    pin, pc, pout = long if (long and input_tokens > LONG_AT) else short
    speed = (tier or "").lower()
    k = (FAST.get(key, TIER[speed]) if speed in ("fast", "priority") else TIER.get(speed, 1.0)) / 1e6
    return pin * k, pc * k, pout * k


def request_cost(model, inp, cached, out, tier=None):
    """API-equivalent $ of one logged request, or None for an unknown model."""
    r = rates(model, inp, tier)
    if r is None:
        return None
    pin, pc, pout = r
    cached = min(cached, inp)
    return (inp - cached) * pin + cached * pc + out * pout
