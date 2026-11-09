"""Codex runs this when you send a prompt (the UserPromptSubmit hook). It reads the hook's JSON from stdin and prints
at most one JSON object: a block with the card, a systemMessage with a note, or nothing, and the prompt is sent.
Plain text on stdout would be added to the model's context, so nothing else is ever printed, and any failure means
no output and exit code 0."""
import json
import os
import sys

# No advisor/__pycache__ is written beside the installed code. Codex fingerprints a plugin's files to check that
# its hooks are still the ones you approved, and on Codex's main branch that fingerprint skips __pycache__ and
# .pyc files, so this is a precaution: it keeps the installed copy exactly as approved even if a build does hash
# them. Compiling the package at each run costs about 15 ms.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


TRIGGERS = ("switch-advisor demo", "switch-advisor test")


def too_old():
    """The plugin needs Python 3.10 or later. An older one leaves every prompt alone, except the two trigger prompts:
    they are what you send to check that the plugin works, so they are held back with the reason, instead of going to
    the model as if nothing were installed. This part runs on any Python 3."""
    try:
        raw = getattr(sys.stdin, "buffer", sys.stdin).read()    # bytes: the console's code page doesn't matter
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        payload = json.loads(text.lstrip(u"\ufeff"))
        said = " ".join(payload.get("prompt", "").split()).lower()
    except Exception:
        return
    if said in TRIGGERS:
        reason = ("Switch advisor needs Python 3.10 or later, and its hooks run Python %d.%d (%s). Until they run a "
                  "newer one, the plugin does nothing and your prompts are sent as usual."
                  % (sys.version_info[0], sys.version_info[1], sys.executable or "python"))
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=True) + "\n")
        sys.stdout.flush()


def main():
    if sys.version_info < (3, 10):
        too_old()
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
    out = hook.on_prompt_submit(payload)
    if isinstance(out, dict):
        sys.stdout.write(hook.to_json(out) + "\n")          # ASCII only, as Codex's JSON reader takes it
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
    os._exit(0)    # exit code 0 whatever happened, even when Codex has closed our stdout (output is flushed above)
