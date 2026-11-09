"""switch-advisor for Codex: when you come back to a large Codex conversation after a break, it says whether a new
conversation started from a handoff is cheaper than re-sending this one. Local and read-only on your logs; no model
calls. The hooks in ../hooks call into this package; the monitor (monitor/codex.py in the repository) uses it too."""
VERSION = "0.1.0"
