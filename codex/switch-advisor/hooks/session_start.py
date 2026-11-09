"""Codex runs this when a new conversation starts (the SessionStart hook, source "startup", or "clear" after the CLI's
/clear). It reads the hook's JSON from stdin and prints at most one JSON object: the handoff from a conversation you
just switched away from, as developer context for this one, or nothing. Any failure means no output and exit code 0."""
import json
import os
import sys

# No advisor/__pycache__ is written beside the installed code. Codex fingerprints a plugin's files to check that
# its hooks are still the ones you approved, and on Codex's main branch that fingerprint skips __pycache__ and
# .pyc files, so this is a precaution: it keeps the installed copy exactly as approved even if a build does hash
# them. Compiling the package at each run costs about 15 ms.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    if sys.version_info < (3, 10):      # the plugin needs Python 3.10 or later; an older one does nothing
        return
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        payload = json.loads(sys.stdin.read().lstrip("\ufeff"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    from advisor import hook
    out = hook.on_session_start(payload)
    if isinstance(out, dict):
        sys.stdout.write(hook.to_json(out) + "\n")          # ASCII only, as Codex's JSON reader takes it
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    os._exit(0)    # exit code 0 whatever happened, even when Codex has closed our stdout (output is flushed above)
