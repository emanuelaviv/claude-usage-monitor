"""API list prices in $ per million tokens (Anthropic's Claude API reference).

Cache writes cost 1.25x the input price with the 5-minute TTL and 2x with the 1-hour TTL.
Only Opus 5.5, Sonnet 5.5, Haiku 5.5 and Fable 5.1 are exact; Haiku 5.5's cache-read price is assumed at 10% of
input. Older models are approximate. For those, Claude's own costUSD ran 1.3-2x higher than these figures.
"""

# model id prefix: (input, output, cache read)
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),   # approximate
    "claude-opus-5": (5.00, 25.00, 0.50),     # approximate
    "claude-opus-4-8": (5.00, 25.00, 0.50),   # approximate
    "claude-opus-4-7": (5.00, 25.00, 0.50),   # approximate
    "claude-sonnet-5": (2.00, 10.00, 0.20),   # approximate
}
# a second rate card above a prompt size: (prompt tokens, (input, output, cache read))
LONG = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}
EXACT = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"}
WRITE_MULT = {300: 1.25, 3600: 2.0}           # cache TTL in seconds -> write multiplier


def lookup(model, prompt_tokens=None):
    """Return (input, output, cache_read, exact) for a model id, or None if it is unknown.
    prompt_tokens picks the rate card for models with two; without it, the large-prompt card (this tool prices large
    conversations)."""
    if not model:
        return None
    for key in sorted(PRICES, key=len, reverse=True):   # longest prefix first: opus-5-5 before opus-5
        if model.startswith(key):
            pin, pout, pcr = PRICES[key]
            if key in LONG and (prompt_tokens is None or prompt_tokens > LONG[key][0]):
                pin, pout, pcr = LONG[key][1]
            return pin, pout, pcr, key in EXACT
    return None


def call_cost(model, inp, out, cache_read, cw5, cw1h):
    """API-equivalent $ for one call, or None for an unknown model."""
    p = lookup(model, inp + cache_read + cw5 + cw1h)
    if p is None:
        return None
    pin, pout, pcr, _ = p
    return (inp * pin + out * pout + cache_read * pcr
            + cw5 * pin * WRITE_MULT[300] + cw1h * pin * WRITE_MULT[3600]) / 1e6
