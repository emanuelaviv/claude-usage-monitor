"""Claude usage monitor (local, read-only)."""
import sys

# The reports print characters such as "≈" and "−". On Windows, output that is piped or redirected (for example when
# Claude Code runs these commands) defaults to a code page that can't encode them, so write UTF-8 instead.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
