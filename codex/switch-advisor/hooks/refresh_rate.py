"""Codex runs this in the background when a conversation starts (also with the CLI's /clear) or resumes (an async
SessionStart hook). It measures $ per 1% of the weekly limit from Codex's logs, at most twice a day, and keeps it in
rate.json in the plugin's data folder. It never prints anything and always exits with code 0."""
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
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            sys.stdin.buffer.read()     # the hook's JSON isn't needed, but it is read so Codex's write never fails
    except Exception:
        pass
    from advisor import hook
    hook.refresh_rate()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    os._exit(0)    # exit code 0 whatever happened
