"""API-equivalent dollars your Claude Code and Cowork sessions used, per day (local, read-only, no model calls).

Usage: python -m monitor.spend [--days 7]

To set your weekly-limit rate in data/plan.json, note how many points your weekly usage meter rose over a few days
(no weekly reset in between), and divide the dollars printed here for the same days by those points. Do it for two
periods and put both results in, higher first: {"usd_per_pt": [high, low]}. Usage on claude.ai, your phone or another
computer moves the meter but isn't in these logs, so keep those days out.
"""
import argparse
import time
from collections import defaultdict
from datetime import datetime

from .ingest import load
from .prices import call_cost


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7, help="how many days back (default 7)")
    days = ap.parse_args().days
    since = time.time() - days * 86400
    calls, _, _, _ = load(since=since)
    by_day, unknown = defaultdict(float), 0
    for c in calls:
        if c.start < since:
            continue
        usd = call_cost(c.model, c.inp, c.out, c.cr, c.cw5, c.cw1h)
        if usd is None:
            unknown += 1
            continue
        by_day[datetime.fromtimestamp(c.start).strftime("%Y-%m-%d %a")] += usd
    for day in sorted(by_day):
        print(f"{day}  ${by_day[day]:,.2f}")
    print(f"total           ${sum(by_day.values()):,.2f}")
    if unknown:
        print(f"({unknown} calls on models without a known price are left out)")


if __name__ == "__main__":
    main()
