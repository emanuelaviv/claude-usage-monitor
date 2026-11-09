"""The nightly job: local only, no model calls. It writes its log to data/nightly.log.

Usage: python -m monitor.nightly        (Task Scheduler runs it with pythonw, so no window opens)

1. Keeps the app's plan-meter history. The desktop app's file holds only ~30 days, so it is copied to
   data/raw/plan-usage-history-YYYYMMDD.json.
2. Refreshes the switch rule's inputs from your history (python -m monitor.backtest_switch).
3. Runs the guardrail (python -m monitor.guardrail). That may turn the after-break card off.
4. Checks the card's estimates against what happened (python -m monitor.verify_estimates).
5. When Codex is installed (CODEX_HOME/sessions exists), refreshes the Codex plugin's inputs and its guardrail from
   your Codex logs, and deletes the plugin's handoffs that nobody used in time (python -m monitor.codex replay
   --write).
"""
import contextlib
import io
import os
import shutil
import traceback
from datetime import datetime

from .ingest import claude_data_dirs

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def codex_replay():
    """Step 5, the Codex plugin's inputs and guardrail; skipped when there are no Codex logs. Returns the log lines:
    a failure is logged like the other steps', never raised."""
    try:
        from . import codex
        if not os.path.isdir(codex.rollout.sessions_dir()):
            return []
        with contextlib.redirect_stdout(io.StringIO()):
            s = codex.replay(write=True)
        return [f"codex replay: ok ({codex.plural(s['returns'], 'return')}, {codex.plural(s['cards'], 'card')}, "
                f"{codex.plural(s['switches'], 'switch', 'switches')}; cards "
                f"{'turned off by the guardrail' if s['guardrail_off'] else 'on'})"]
    except Exception:
        return [f"codex replay: FAILED\n{traceback.format_exc()}"]


def main():
    os.makedirs(os.path.join(HERE, "data", "raw"), exist_ok=True)
    log_path = os.path.join(HERE, "data", "nightly.log")
    lines = [f"=== {datetime.now().isoformat(timespec='seconds')}"]

    found = [os.path.join(d, "plan-usage-history.json") for d in claude_data_dirs()]
    src = next((p for p in found if os.path.exists(p)), "")
    try:
        if src:
            dst = os.path.join(HERE, "data", "raw", f"plan-usage-history-{datetime.now():%Y%m%d}.json")
            shutil.copyfile(src, dst)
            lines.append(f"meter history copied to {dst}")
    except OSError as err:
        lines.append(f"meter history: {err}")

    for name in ("backtest_switch", "guardrail", "verify_estimates"):
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                mod = __import__(f"monitor.{name}", fromlist=["run", "main"])
                (getattr(mod, "run", None) or mod.main)()
            lines.append(f"{name}: ok")
            if name == "guardrail":
                lines.append(out.getvalue().strip())
        except Exception:   # one failing step must not stop the others
            lines.append(f"{name}: FAILED\n{traceback.format_exc()}")

    lines += codex_replay()

    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
