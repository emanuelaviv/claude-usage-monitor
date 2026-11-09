# switch-advisor for Codex

A Codex plugin for the Codex desktop app and the Codex CLI. It helps when you come back to a large Codex conversation
after a break. By then Codex's prompt cache has often expired, so your next message re-reads the whole conversation at
the full input price, ten times the cached one (twenty times on GPT-6.1 Sol and Terra). The plugin compares that with
starting a new conversation from a handoff built on your computer from this one, and holds your prompt back only when
switching clearly pays.

It is the Codex version of the Claude Code plugin in [../../mod/switch-advisor](../../mod/switch-advisor). Codex has no
way for a plugin to draw a band above the prompt, so this version works through hooks.

It has not yet been tried in Codex itself: it is tested against Codex's source and with replays of real logs.

## Install

In the desktop app, in the Plugins tab, choose **Add**, then **Add a marketplace**, enter
`emanuelaviv/claude-usage-monitor` as the **Source** and select **Add marketplace**. Then select **Install** on Switch
advisor.

With the Codex CLI:

```
codex plugin marketplace add emanuelaviv/claude-usage-monitor
codex plugin add switch-advisor@claude-usage-monitor
```

From a clone of this repository, give its folder instead: `codex plugin marketplace add <path to the clone>`, as a
full path or one starting with `./`, `../`, `.\`, `..\` or `~/` (`.` from inside the clone). Codex reads any other
path as a GitHub repository, or refuses it.

Then trust its hooks. In the desktop app, select **Review hooks** in the composer, or open Settings > Hooks; in
the CLI, run `/hooks`. Codex skips a plugin's hooks until you trust them, and again after every update of the plugin,
since the trust covers all of its files.

It needs Python 3.10 or later: `python` on Windows, `python3` on macOS and Linux.

On Windows:
- Codex starts each hook through PowerShell, which adds about 0.2 to 0.4 seconds to every prompt.
- PowerShell looks for a program in the current folder only when your PATH holds `.` or another relative folder, so
  with a usual PATH a `python.bat` in a project can't run in Python's place.
- Codex uses `cmd.exe` instead when a conversation has no single local environment, or when it finds no PowerShell.
  `cmd.exe` does look in the project first. And if Python can't open the plugin's script there (for example, because
  of a `%NAME%` in the folder's path), every prompt is held back with Python's error.
- The plugin's folder, under `CODEX_HOME` (by default in your user folder), must not contain a `$` followed by a
  letter, digit, `_`, `$`, `^`, `{` or `(`, or a backtick: PowerShell reads those itself, and the hooks then fail and
  let every prompt through.

On macOS and Linux, the plugin's folder must not contain a `$`, a backtick, `"` or `\`: the shell can read those
itself, and then every prompt is held back with an error.

## What it does

When you send a prompt to a conversation that has been idle for an hour or more:

- **When a switch clearly pays**, the prompt is held back. In the desktop app it stays in the conversation, marked
  "Hook blocked this message", with the numbers below it; in the CLI they show under "Blocked by hook":
  - **To switch:** start a new conversation in the same project folder (in the CLI, `/new` or `/clear`) and send
    your prompt there within 30 minutes. It starts from the handoff. A new Git worktree is another folder and doesn't
    get it: with `/new`, choose Use current Git worktree when asked; in the desktop app, keep the composer on Local.
  - **To stay:** send the prompt here again within 15 minutes. That conversation isn't checked again for 12 hours.

  Only a prompt sent from the desktop app's composer or typed in the CLI can get the card: Codex logs what started
  those turns as `composer` and `user`, and older Codex logs nothing, which counts too. Every other turn gets the
  note instead: one sent from the ChatGPT phone app (`remote_android` from the Android app; what the iPhone app logs
  is not checked yet), an automation, a retry after a capacity error, an answer sent from the question panel, an
  edited or queued message, plan feedback or Implement plan, and any other kind Codex logs. For most of these,
  nobody may be there to see the card. You also get the note when Codex gives the plugin no
  data folder, when the monitor's guardrail has turned the card off, when the conversation's log holds no request
  you typed (Codex marks typed text only since late October 2026), or while another conversation's handoff waits in
  the same project.
- **Otherwise**, when sending here is expected to cost at least $0.50, a one-line note shows what the message costs if
  the cache expired, and how often it had after breaks that long. Below that, nothing shows. The prompt is sent either
  way.

The card shows:
- what sending here costs if the cache expired and how often it had after breaks that long, what it costs with a warm
  cache, and the average, weighted by how often the cache had expired;
- what a new conversation costs, once it has re-read its files;
- the saving now, the typical saving by your next break, and the worst case;
- the share of your weekly limit, and how much of it you have used;
- the quality risk, with what the handoff might not carry.

**When a switch pays:** the cache has probably expired (the break is about 2 hours or longer), and all of these hold:
- even in the worst case, where the new conversation re-reads 150K tokens of files, it saves at least $0.50, both now
  and by your next break;
- the typical saving by your next break is at least $2, or $5 when the quality risk is medium.

With a 272K context window this is rare, since Codex compacts a conversation by itself near the end of its window. It
happens mostly in conversations above 272K tokens, where every request costs the long-context price: a raised context
window, or a model whose default window is 500K.

**The quality risk** weighs what the handoff can't carry:
- **High:** agents started in the conversation are still running, its last reply asks you something and is longer than
  the 1,500 characters the handoff keeps, or it asked you something in Codex's question panel that the plugin can't
  read. It never holds your prompt back then; when a switch would otherwise pay, the note says what it would save.
- **Medium:** your message answers its last reply or a question in the question panel, the conversation coordinated
  agents whose reports stay in it, or more than 12 files have uncommitted changes.
- **Information only:** files with uncommitted changes. The new conversation sees them with `git status`.

**The handoff** is built on your computer from the conversation's log, with no model call. It holds:
- the project and branch;
- your first request and up to three of your latest;
- the last reply;
- the questions still waiting for your answer in the question panel, with their options: the newest 3, how many older
  ones it leaves out, and a line for any it can't read;
- the uncommitted files;
- how many agents the conversation started.

It goes only into a new conversation in the same project folder whose first prompt you send within 30 minutes. It
never includes the prompt that was held back: you send that yourself. The plugin keeps one handoff per project.
While one waits, a card in another conversation in that project becomes a note, so the waiting handoff is never
replaced.

**Trying it:**
- `switch-advisor test`, sent as a prompt, shows the card with this conversation's numbers as if you came back after
  3 hours, even when the rule wouldn't show one; when a real break would give the note instead, the note shows with
  it. A new conversation in this project folder whose first prompt you send within 30 minutes gets the handoff. A
  test never replaces the handoff of a real card, and a real card replaces a test's. It is held back on the phone
  too, so you can see whether your phone shows the card.
- `switch-advisor demo` shows the card with example numbers only, for screenshots. It keeps no handoff, so its To
  switch and To stay steps do nothing.
- Neither reaches the model once the hooks run at all. On an older Python 3, where the hooks do nothing else, both
  are held back with a message that the plugin needs Python 3.10 or later, naming the Python the hooks run; if one
  fails partway, it is held back with what failed. When the hooks don't run (not trusted yet, or no Python found),
  both go to the model like any other prompt, so try `switch-advisor demo` in a new conversation first.

## The numbers

- **Dollars are API list-price equivalents** at the conversation's model and service tier: Fast costs 2 times
  Standard (2.5 times on GPT-5.5), and Ultrafast 6 times. On a ChatGPT plan they measure how much of your limits a
  request uses, not what you pay.
- **Cache expiry has no fixed time in Codex.** The plugin uses how often the cache had expired after breaks of each
  length. Its defaults come from one heavy user's logs: about 1 in 5 breaks of 30 to 60 minutes, 4 in 10 of 1.5 to
  2 hours, 6 in 10 of 2 to 6 hours, and nearly all breaks over 12 hours. A miss keeps only the shared prefix (about
  20K tokens) cached. From a clone of this repository, `python -m monitor.codex replay --write` measures yours.
- **The weekly-limit share** comes from Codex's own logs. They record the plan's weekly meter, so the plugin divides
  the API-equivalent dollars of recent weekly windows by how far the meter rose, at most twice a day and in the
  background. A window where more than 2% of the tokens went to a model without a known price is left out. Usage the
  logs don't see, such as Codex cloud tasks or another computer, moves the meter too, so the share is an estimate.
- **Fixed:** the re-read allowance (80K tokens, 150K in the worst case) and the handoff's size (2K tokens).

## What it never does

- call a model;
- send a prompt;
- add text to the conversation you are in;
- check prompts in agent or delegate threads (such as a `/review` run), or prompts sent while a turn is still running.

If anything fails, your prompt is sent as usual, except in the two cases under Install where every prompt is held
back.

## Files it reads and writes

- **Reads:**
  - the conversation's log, which Codex passes to the hook;
  - the end of every uncompressed session log, to date it by its last record; then the first and last lines of those
    written in the last 15 minutes, to see whether this conversation's agents are still running;
  - the ends of the 8 session logs written to last, for the newest reading of your weekly meter;
  - recent session logs, for the weekly-limit rate;
  - `git status` in your project, run with the `git` found in an absolute folder on your PATH.
- **Writes**, only in the plugin's data folder under `~/.codex/plugins/data/`:
  - each conversation's state;
  - the pending handoff, deleted when used. An unused one is deleted by the first hook to run after 30 minutes, or by
    the monitor's nightly job;
  - a decision log, with times, event names, numbers, ids, model names, flag names, the risk level, the plugin's
    version and what started the turn (such as `composer` or `remote_android`) only, no text;
  - an error log, with the time, the hook and step that failed, and the exception's type only;
  - the weekly-limit rate;
  - `params.json` and `guardrail.json`, when the monitor writes them. It writes `guardrail.json` to turn the card off
    once 4 or more switches were made and half or more of them went back to the old conversation within 24 hours.
    To turn the card back on, set `after_break_enabled` to `true` in it, and the monitor leaves it on; deleting the
    file instead lets the monitor turn the card off again.

  Only the pending handoff holds conversation text.

## Turning it off

- **Remove it:** `codex plugin remove switch-advisor@claude-usage-monitor`, or use the Plugins tab in the desktop app.
  That leaves its data folder: to remove its state, logs and any waiting handoff, also delete
  `~/.codex/plugins/data/switch-advisor-claude-usage-monitor` (or the same folder under your `CODEX_HOME`, if you set
  one).
- **Drop the marketplace too:** `codex plugin marketplace remove claude-usage-monitor`.
- **Pause it:** turn its hooks off in Settings > Hooks.
