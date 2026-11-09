# Claude usage monitor and optimizer: design

Version 0.4 · 2026-11-07 · Status: proposal. The switch-conversation advisor is built and backtested, for Claude Code and Cowork (§10) and for Codex (§11); nothing else is built yet. v0.2 folded in an independent review covering facts, statistics and scope.

Terms used throughout:

- **pt**: one percentage point of the weekly plan limit.
- **$**: API-equivalent dollars at list price. Except for optional paid overage, subscription plans are flat-rate, so $ is only a common unit for weighing usage.
- Times are UTC, except in examples of on-screen text, which show local time.

## 1. Summary

Build it as a small local tool:

- a Python ingester that reads the logs Claude Code and Cowork already write;
- a SQLite store;
- an analysis layer;
- a report, then a local web page, and later an optional pane inside the Claude desktop app.

Most of it can be built from data Claude Code and Cowork already keep on the machine. The quality-aware recommender also needs two things the logs can't give:

- a dense record of the plan meter, which a 20-line mod should be able to collect going forward (it still needs a one-session test);
- small randomized trials, because one user's history has little variation in model or effort (the author's had almost none).

What analyzing the author's own logs changed (§2.2):

1. **The weekly % follows API-equivalent dollars, not tokens.** On the author's account, one $-per-pt rate fitted two weeks of data to about ±10%, until a break. Some usage also happens where the local logs can't see it.
2. **The biggest lever is context size and cache rebuilds, not effort.** In the author's data, cache reads plus writes were about 81% of cost across Code and Cowork.
3. **In a handoff-file workflow like the author's, corrections are review rounds in handoff files**, not chat messages.
4. **Effort and model savings can't be read from a user's history.** They start as untested estimates and become recommendations through randomized trials. A confidence % appears only once the user's data outweighs the assumptions.
5. **Near the limit, runway matters more than dollars**, so the tool maximizes runway (work done before the limit) rather than minimizing dollars.
6. **Cowork is only partly visible.** New Cowork tasks run in the cloud. Only older local Cowork sessions leave logs on the machine.

## 2. What is already on the machine (checked 2026-11-06/07)

### 2.1 Sources

`CD` below means `%LOCALAPPDATA%/Claude-Data/claude_<account id>`.

| Source | Path | What it gives | Caveats |
|---|---|---|---|
| Claude Code transcripts | `~/.claude/projects/<enc-cwd>/<session>.jsonl`, plus `<session>/subagents/agent-*.jsonl` and `<session>/subagents/workflows/wf_*/agent-*.jsonl` | Every API call: model, effort, tokens by type (incl. 5-minute and 1-hour cache writes, thinking), timestamps, cwd, branch, prompt id, agent/skill/plugin/MCP attribution, permission decisions, compactions, limit events | CLI transcripts are deleted after 30 days by default (`cleanupPeriodDays`). Desktop-app transcripts are kept, per the docs. |
| Desktop Code-tab metadata | `CD/claude-code-sessions/<acct>/<org>/local_<id>.json` | Title; current model and effort; `cliSessionId` (maps to the transcript); `postTurnSummary.status_category`; ultracode flag | Current values only. Also holds free text, so read allow-listed fields only (§8). |
| Cowork, local sessions | `CD/local-agent-mode-sessions/<acct>/<org>/local_<id>/.claude/projects/**.jsonl`, plus `local_<id>.json` and `audit.jsonl` | Same transcript schema (`entrypoint` local-agent). The audit file holds run totals (per-run up to build 2.1.271, cumulative from 2.1.275) and meter readings. | The files sit under hidden `.claude` folders, so walk them with `os.walk`. More than one account can be present; partition by org. **Cowork tasks that run in the cloud leave nothing locally**, so only older local sessions are visible. |
| Plan meter history | `CD/plan-usage-history.json` | `{t, org, u:{fh: 5-hour %, sd: 7-day %}}`, whole % only, no reset time | Irregular: a few samples a day at best, with many gaps over 6 h and whole days missing, even on heavy-use days. Rolling ~30 days, so copy it daily. |
| Meter readings in Cowork | `audit.jsonl` → `rate_limit_event.rate_limit_info` | Utilization (0–1), resetsAt, status, warning thresholds, overage flags | Written only while a local Cowork session runs. |
| Live meter, once a mod is installed | A Claude Code mod: `$.session.usage()` on load plus the `session.measure` event after each main-thread turn | 5-hour and 7-day % at 0.1-pt resolution, with reset times, at no token cost | Documented for the desktop Code tab (2.1.286+); not yet tested end to end. The status line likely doesn't run in the desktop app (issue #41456), only in the CLI. |

### 2.2 What the author's data showed

The design was shaped by an analysis of the author's own Claude Code and Cowork logs. Only shares, ratios and illustrative figures are given here; another user's numbers will differ.

**Claude Code:** thousands of API calls came from relatively few typed prompts.

- **Cost split (Code):** cache reads about half, cache writes and output about a quarter each (output mostly thinking); uncached input about 0.
- **By thread:** main threads a little over half of cost, workflow agents most of the rest, subagents about 2%.
- **Effort and model:** one model only (Opus 5.5); most spend at xhigh, some at max, little lower. History therefore can't show what another effort or model would do.
- **Context:** main-thread calls re-read several hundred thousand tokens each.
- **Per task:** heavy-tailed; the p90 is about 8× the median.
- **Project:** 99% of spend was in one project.

**Cowork:** in one long local session at effort max, calls that rebuild the cache after more than an hour idle (about 600K tokens rewritten, about $5 each at list price) were roughly half that session's cost (47–52%, depending on the counting). Cache writes were well over half of Cowork's cost on Opus 5.5.

**Plan meter:**

- **Reset:** weekly, at a time fixed per account; the meter reports it as `resetsAt`.
- **Past 100%,** work continues only as paid overage, and only when extra usage is on.
- **Rate:** two weeks of one account's data fitted a single $-per-pt rate (21 independent increments, about ±10%). The share of the weekly meter that a full 5-hour window uses depends on the plan, so it is fitted per account (§5.1).
- **Breaks:** the meter can drop to 0 with the reset time unchanged, and a window (in the author's data, the 5-hour one) can stop being reported. Either one starts a new regime (§5.1).
- **After a break the rate is uncalibrated.** In the author's data, logged $ per pt varied several-fold between segments after a break, and in one segment the meter rose with no local calls. Either the limit changed, or usage came from surfaces the local logs don't cover (claude.ai, mobile, cloud Cowork, another machine). The tool must show both scenarios until the rate is recalibrated.

**Quality signals (in the author's workflow):**

- **Corrections came as handoff files,** not chat messages: a follow-up prompt names a review-round file such as `PROMPT_<stem>_fixes.md`. On 40 hand-labeled follow-ups:
  - a word-list detector scored precision 0.20 and recall 0.06;
  - reading the referenced file's header scored 0.88 and 0.88 (measured on the same 40 labels used to design the rule).
- **Rework was a large share of Code spend:** 44–59%, depending on the rule. The reworked work had already run at xhigh.
- **Interrupts and user rejections were rare;** the interrupts all came from one workflow stop.
- **Commits can't mark success:** Claude never ran a git commit in these transcripts.

## 3. Corrections to the original idea

| # | Original assumption | What the data says | Design change |
|---|---|---|---|
| C1 | % weekly per conversation can be computed from tokens | The meter's units are hidden. On the author's account it followed API-equivalent $ at one fitted rate until a break; after it the rate was uncalibrated, and some usage never reaches the local logs. | Log the meter densely, fit $ per pt per regime, and show unattributed usage as its own stream. Per-conversation % is an estimate with a range. |
| C2 | Tokens measure usage | In the author's data, cache reads were 97% of tokens but 38% of combined cost (47% in Code) | Aggregate in component-priced $ and convert to pts. Show tokens by type alongside, not summed raw. |
| C3 | Effort and model are the main levers | In the author's data, cache reads plus writes were about 81% of combined cost (75% in Code, 89% in Cowork on Opus 5.5), and halving thinking would have saved 4–11%. Sonnet 5.5 would cost 0.65–0.77× Opus 5.5 on that token mix, not 0.5×, because both charge the same for cache reads. | Rank session-hygiene levers next to effort and model. Their cost effect is exact. |
| C4 | Corrections are steering messages in chat | In the author's workflow they are review rounds in handoff files | For Code, quality = defect rework rounds per packet, from the handoff-file rule, validated on 40–60 labels from the user |
| C5 | Average per conversation | A conversation holds many tasks, and costs are heavy-tailed (in the author's data the p90 is 8× the median) | Unit = task (a handoff packet or prompt chain). Show median and p90. |
| C6 | History shows what another effort or model would do | The author's history: Opus 5.5 only, almost all xhigh, and harder tasks got higher effort. At the cost spread seen there, a raw A/B test needs about 1,200–1,700 tasks per option to detect a 15% difference in total cost. | Priors (price math plus Anthropic's published curves), then randomized trials where switching is free: workflow runs, and effort escalation inside a session |
| C7 | More data means more confidence | Before any data on a topic, a % would only echo the analyst's priors. Observational evidence levels off because selection bias doesn't shrink with n. | An evidence bar that fills as data arrives. A % appears once data outweighs the prior, and it means P(the card's saving is real). |
| C8 | Report savings in tokens or $ | Flat-rate plan (overage only when enabled); near the limit, runway is what counts | Show savings as pts per week and extra tasks before the limit. Show $ of overage avoided when extra usage is on. |
| C9 | Forecast from history | Hours spent near the cap hide true demand; regime breaks split the history | Censor from the first reading at 95% until the reset; update the week's level from week-to-date spend; split history at breaks |
| C10 | A recommendation applies anytime | A model switch mid-session rebuilds the cache (for example, about $3.8 for a 470K-token context). Effort switches kept the cache in 3 of 4 observed cases on current builds. | Model changes go to a new session. Effort changes apply at the next prompt at about $0. The tool checks this after every Claude Code update. |
| C11 | The logs will be there | CLI transcripts age out after 30 days; the meter file keeps 30 days and is sparse | Ingest into the tool's own store, copy the meter file daily, and add a logging mod |
| C12 | The monitor is free | Classifying with Claude would spend the user's limit | Code quality uses rules only. A local model is added only if needed. Zero Claude calls. |
| C13 | Cowork is visible like Code | New Cowork tasks run in the cloud and leave no local log | Per-conversation Cowork numbers exist only for older local sessions. Cloud tasks show up as unattributed meter movement, which the user can tag. |

What already exists: Anthropic's `/usage` shows attribution and effort-switch rows (press `f` to apply them), and `/insights` reports friction points. Neither one estimates savings, weighs quality, gives a confidence, forecasts next week, or covers Cowork. That gap is this tool's job.

## 4. Architecture

```
Claude Code transcripts ───┐
Cowork local sessions ─────┤
Desktop session metadata ──┼─► ingest (Python, incremental) ─► SQLite ─► analysis ─► static report (Phase 1)
meter: mod log, history ───┤                                                     ├─► local page (Phase 2)
  file, Cowork audit       │                                                     └─► desktop pane via mod (Phase 4)
manual tags ───────────────┘
```

- **Runtime:**
  - Python 3.10 or later (tested on 3.14), standard library only (Pillow and numpy, plus fontTools for kerning, are needed only by `tools/make_band_images.py`).
  - Local time = `datetime.fromtimestamp(ts, timezone.utc).astimezone()`. This uses the Windows time zone and handles daylight saving. On Windows, `zoneinfo` needs the `tzdata` package; don't hard-code daylight-saving dates.
- **Schedule (Windows Task Scheduler):**
  - ingest every 15 minutes, plus a nightly refit;
  - absolute `pythonw.exe` path, so there is no console window and no WindowsApps alias;
  - MultipleInstances IgnoreNew, StartWhenAvailable, a 10-minute execution limit, and a lock file.
- **UI:**
  - Phase 1: a static HTML report that each ingest regenerates. It opens no network port.
  - Phase 2: a local server for labels, trials and tags. It binds to 127.0.0.1 only, requires a random per-install token, rejects any Host header other than localhost, and sends no CORS headers.
  - The chart library is vendored as a local file (a one-time download, with the user's consent).
- **Live meter:** a mod of about 20 lines. It calls `$.session.usage()` on load and on every `session.measure`, and appends `{ts, kind, percentUsed, resetsAt}` to `data/meter.jsonl`. It has no UI and no `turn.step`. Mods are unsandboxed, so it stays small enough to read in full.
- **Cross-check:** `ccusage --json` (optional install) gives independent token totals.
- **Not used:**
  - The undocumented OAuth usage endpoint, browser cookies, proxies and probe requests. They raise terms-of-service and stability problems.
  - OpenTelemetry. It would also capture the few background calls the transcripts miss, but it records only going forward, needs a collector, and may not be emitted by the desktop app.

### 4.1 Ingestion rules

1. **One API call = one `message.id`** (1:1 with `requestId`). Each content block is written as a separate record with identical usage, so keep one per id.
   - Dedupe globally across files: Cowork forks and resumes copy ids into later transcripts.
   - Keep the copy with the largest `output_tokens`.
   - In the author's logs, naive sums overstated cost 2.35× in Code and 2.9× in Cowork.
2. **Sum main, subagent and workflow files.** No id appears in more than one Code file.
3. **Cost per call** (Opus 5.5, $ per million tokens): `input×4 + output×20 + cache_read×0.20 + ephemeral_5m×5 + ephemeral_1h×8`. Output already includes thinking.
   - This matches Claude Code's `cost-state` to the cent in 5 of 11 runs.
   - In the other 6 it is 0.3–4.3% low, likely because title, summary and compaction calls aren't logged.
   - Keep a versioned price table per model:
     - Sonnet 5.5: $2 input / $10 output / $0.20 cache read;
     - Haiku 5.5: $0.10 / $0.50 for prompts up to 100K tokens, $0.50 / $2.50 beyond;
     - Fable 5.1: $10 / $50 / $0.25;
     - cache writes 1.25× (5-minute) or 2× (1-hour) the input price;
     - for retired models, use the client's own `costUSD`.
4. **Never sum:**
   - Cowork `audit.jsonl` assistant usage: it is a message-start snapshot.
   - `cost-state` as a session total: it covers one process run.
   - `queued_command.usage` and `wf_*.json totalTokens`: they hold context sizes, not tokens used.
5. **Cowork sessions without a transcript** come from audit `result` records.
   - Up to build 2.1.271 these are per-run totals; from 2.1.275 they are cumulative per `session_id`, so take differences. The switch happened between those two builds.
   - The newest builds include `model_usage_since_last_result`.
6. **Links:**
   - call → prompt: walk `parentUuid` up to the nearest user record and take its `promptId`;
   - subagent → parent prompt: via `toolUseResult.agentId`;
   - workflow agent → parent prompt: via `toolUseResult.runId`, which equals the `wf_<run>` folder name.
   - In the author's logs, every Code call could be attributed this way.
7. **Human prompts:** user records that:
   - have no tool_result;
   - are not `isMeta`, `isCompactSummary`, a task notification, scheduled, peer or a command;
   - have `origin.kind=='human'` or `promptSource` typed or queued.
   - Mid-turn human messages are `attachment.type=='queued_command'` with `origin.kind=='human'`. They are rare; most queued commands are task notifications.
   - **Cowork:** every prompt has `promptSource` sdk. Treat sessions whose metadata `sessionType=='scheduled'`, or prompts that start with a `<scheduled-task` tag, as scheduled.
8. **Project** = the git root of the per-call `cwd`, with worktrees resolved through their `.git` file. Don't use the projects folder name.
9. **Effort:**
   - **Code:** the per-call `effort` field, which equals `api-request.params.output_config.effort`.
   - **Cowork:** per call only from build 2.1.215. Mark earlier calls "inferred" (from `effortOverride`) or "unrecorded".
10. **Partition by `<acct>/<org>`**, and calibrate only against the meter's org.
11. **Incremental reads:**
    - Store each file's byte offset, size and mtime. Advance the offset only to the last complete newline, and re-read a file if it shrinks.
    - `plan-usage-history.json` is rewritten in place, so retry if it fails to parse.
    - Assert that each run finds more than 0 Cowork transcripts. A plain recursive glob misses the hidden folders.
    - A full re-ingest takes about 20 seconds.

### 4.2 Store (SQLite)

| Table | Columns |
|---|---|
| `call` | id PK, ts, surface, session, prompt_id, task_id, kind (main / subagent / workflow), agent_id, workflow, model, effort, effort_src (recorded / inferred / unrecorded), project, input, cw5, cw1h, cr, out, think, cost_usd, attribution |
| `prompt` | id (message uuid) PK, session, ts, kind, length, handoff_stem, label, label_src (rule / user). No prompt text. |
| `task` | id PK, topic, surface, project, opened, closed, rounds, defect_rounds, requirement_rounds, cost_usd, outcome |
| `meter` | ts, org, window (five_hour / seven_day / spend_limit), pct, resets_at, overage flags, source (mod / plan_history / audit / manual) |
| `regime` | id, org, start, end, beta_mean, beta_sd, n_increments, status (calibrated / uncalibrated) |
| `tag` | start, end, label (e.g. "cloud Cowork", "phone", "other computer") for unattributed bursts |
| `topic`, `trial`, `assignment` | topics with their rule and edit flag; trials with arms, block seed, looks, guardrail state; per-unit arm, p = 0.5, assigned_at, complied |
| `price`, `source_file` | price per model with valid_from; per-file offset, size and mtime |

## 5. Analytics

### 5.1 Calibrating % of the weekly limit

- **Model:** fit only non-overlapping increments between meter readings in the same regime.
  - Use weighted least squares with Student-t errors, Δmeter = β · ($ logged), and variance 2/12 + (κ·β·$)².
  - κ is estimated per account: 0.46 on the author's pre-break data.
  - Default to the ratio estimator ΣΔmeter / Σ$ with a bootstrap interval, and report "$ per pt ± x%". Don't report R².
- **Calibrated badge:** the posterior CV of β is ≤ 15% (about 10 increments at κ 0.46), and the regime has at least 48 hours of data.
- **Unattributed usage** is its own intermittent stream, not a steady drift.
  - Fit a daily burst probability and burst size to the residuals Δmeter − β̂·$, and simulate them in the forecast.
  - In the author's data the residuals were near 0 before a break, with a burst after it.
  - Tagging a burst (cloud Cowork, phone, other computer) labels it in the charts.
- **Regime breaks:** declare a break when:
  - the meter drops more than 2 pts other than at the account's weekly reset (`resetsAt`);
  - the set of windows changes; or
  - the plan label changes.

  A CUSUM on standardized residuals flags "possible limit change or unlogged usage".
- **Prior after a break:** log β ~ N(log(β_old × plan ratio), 0.7²) when the plan label changed, and N(log β_old, 0.5²) otherwise. Until calibrated, every forecast and pts figure is shown under each plausible β (on the author's account, the pre-break fit and a rough post-break rate).
- **Weights:** per-model and per-token-type weights can't be identified yet (one model, collinear components). Use one slope proportional to API price, and test it once a second model is in use.
- **5-hour window:** β5 ≈ β7 / r, with r (the share of the weekly meter that a full 5-hour window uses) fitted per account. It depends on the plan.

### 5.2 Tasks and topics

**Assumption:** work is handed off as `PROMPT_<stem>.md` files that the prompt names, the workflow this tool was built around (in the author's sessions this keyed 88% of Code $). Without such files, tasks fall back to the session and gap rule below, and the packet risk classes (§10.1) don't apply.

- **Code:** a task is a handoff packet.
  - Take the `PROMPT_<stem>.md` the prompt references, and find its root by removing a rework suffix (`_fixes`, `_rN`, `_amendN`, `_errata`, `_repair`, `_followup`, ...).
  - Free-text turns attach to the open task.
  - Rework rounds are split into defect rounds (`_fixes`, `_rN`, `_repair`) and requirement rounds (`_amend`, `_revision`, `_errata`). The user confirms the mapping (§9, question 3).
- **Cowork (local sessions) and Code turns without a stem:** a new task starts at:
  - the first prompt of a session; or
  - a gap of more than 12 hours, **unless** the follow-up is a correction, a re-ask or a continuation.
- **Topics:** a packet family, session title or project. Keep at most 10 active topics, and let the user edit them.
- **"Active"** = used in the current weekly window.

### 5.3 Quality

- **Code: rules only.**
  - The handoff-header rule plus the rework suffix gives defect rounds per packet.
  - Validate it on 40–60 Code follow-ups that the user labels (about 20–30 minutes).
- **Cowork:** quality data exists only for older local sessions.
  - A local-model classifier (for example Qwen via Ollama, temperature 0) is deferred until a local stream of free-text follow-ups exists.
  - A 200-item labeling gate takes about 2 hours.
- **Signals counted against a model or effort:**
  - defect rounds;
  - major corrections and re-asks;
  - main-thread interrupts;
  - user tool rejections;
  - abandonment.

  Not counted: requirement rounds, policy denials and workflow-agent stops.
- **Positive close:** the next packet opens; a closure prompt; tests pass after the last edit (exit code 0).
- **Effective cost per completed task** = the metered cost of all rounds, including rework tokens, divided by the number of completed tasks.
  - This is the only cost that feeds pts, runway and confidence. Corrections already count through their tokens.
  - They count a second time only through the quality guardrail (§5.5).
  - Review time is shown on its own line, in minutes, if the user enters minutes per correction. It is never converted into pts.

### 5.4 Forecast and time to limit

- **Model**, one per surface: Code, plus local Cowork sessions.
  - whether an hour is available and active;
  - $ per active hour (EWMA, half-life 14 days);
  - a week-level effect, updated from the current week's complete, uncensored days: log W | data ~ N((−½ + Σ(log x_d − μ)/s²)/prec, 1/prec), with prec = 1/s_W² + n_d/s²;
  - an hour-of-day profile from the last 28 days;
  - the unattributed burst stream (§5.1).

  There is no weekday/weekend split (the backtest found no gain) and no scheduled-task term (none ran in the analyzed period).
- **Censoring:** from the first reading at 95% or above (or any rejection or overage) until the reset, the hours are censored. They are dropped from the fits, because users ration near the cap, so recorded use understates demand.
- **Monte Carlo:** 5,000 paths in $, converted with drawn β (or with each plausible β while uncalibrated). Outputs:
  - P10, P50 and P90 for the rest of this week and for next week, in pts, $ and tokens by type;
  - uncapped demand against the 100% line;
  - the probability of reaching 100% before the reset, and when, in local time (e.g. "likely <weekday> <hh:mm>–<hh:mm>");
  - a pace line: (100 − meter) / days to reset.
- **Validation:** backtest the model that ships, in pts, with censoring and regime conversion. Compare it against persistence and a pace baseline (meter + recent burn × hours left) with a paired sign test across origins. Add a Brier score for P(hit) once 10 weeks have resolved.
  - A prototype (Cowork $ only, no censoring) scored about 20% lower CRPS than persistence over 20 origins. With 20 origins that is directional only.
- **Display:** show the 7-day forecast after 14 days of data, labeled "low confidence" until 6 weeks.

### 5.5 Recommendations

**Levers**, in the order the tool checks them:

| Lever | Cost side | Quality side |
|---|---|---|
| Session hygiene: a fresh session plus a handoff summary instead of resuming a huge context after more than an hour; compacting earlier (`modelSettings.<model>.autoCompactWindow`); splitting long sessions | Exact. For example, rebuilding 584K tokens costs about $4.7, versus about $0.5 for a 60K summary. | Unknown (re-asks after losing context), so a guardrail trial runs before "Recommended". In Cowork, a new task runs in the cloud and leaves the tool's view, so the card warns about that. |
| Workflow shape: agents per run, and effort for builder and verifier roles | Exact per run, after the fact | A trial |
| Effort per topic, including escalation: start at high and move to xhigh at the next prompt after the first review defect, using max only for design and closure | Priors, then a trial | A trial |
| Model per topic or role | Price math on the user's token mix, times a prior on the token multiplier | A wide prior, then a trial |

**Priors for untested options** (each counts as 10 tasks; log-scale sd ≥ 0.30):

- **One effort step down:** output cost × the published step ratio for Opus 5.5:
  - medium → low 0.52
  - high → medium 0.72
  - xhigh → high 0.39
  - max → xhigh: no published row, so 0.7 with sd 0.5

  Requests per task × 1.0 with sd 0.25: no saving is assumed until a trial measures one. Corrections × 1.25 (coding) or 1.05 (knowledge work), sd 0.30.
- **One step up:** cost from the published top step (about ×2.5 on output). Corrections × 1.0 with sd 0.30, centered on no change, because in the author's history the reworked work had already run at xhigh.
- **Model:** price ratio on the user's token mix, times a token-multiplier prior LN(log 1.15, 0.25²) for coding. On the author's mix:
  - Sonnet 5.5: 0.65–0.77
  - Fable 5.1: about 1.9 (Code) or 2.1 (Cowork). Fable also has a separate weekly meter, which the mod can't read.
- **Anthropic's published Opus 5.5 numbers** (SWE-bench Pro subset; directional only):

  | Effort | Solved | Cost per task |
  |---|---|---|
  | low | 87.4% | $0.11 |
  | medium | 92.8% | $0.21 |
  | high | 95.3% | $0.29 |
  | xhigh | 96.7% | $0.74 |
  | low, failures re-run at high | about 97% | $0.17 |

**Engine.** At first, use one conjugate two-arm comparison per lever and topic:

- a normal model on log cost, with the 10-task prior;
- the estimand is the arithmetic-mean cost (exp(μ + σ²/2) per arm), because the meter adds dollars;
- Gamma-Poisson for corrections.

Move to partial pooling across topics only once several topics have trials. Adjust only with pre-treatment covariates (context size at the start, cold-start flag, prompt length, files referenced). Plan trials with σ = 1.4–1.7 until a smaller residual σ is shown. Splitting cost into its components is not variance reduction.

**What each card shows:**

1. **Projected saving:** pts per week (80% interval) and extra tasks before the limit, from metered cost only:

   S = β · (E[C current] − E[C option]) × the forecast tasks for the topic − switching cost

   While uncalibrated, it is shown under each plausible β.
2. **The saving split by channel**, each marked measured or assumed: output per request (a 20–46-task-per-arm trial can measure it), requests per task, corrections.
3. **An evidence bar:** n per arm and the data share (data precision ÷ posterior precision). This is what rises with data.
4. **A confidence %**, only once the data share is ≥ 0.5: Conf = P(saving ≥ 1 pt/week | data, prior). Before that, the card reads "untested estimate: ~X pts/week (range)", with no %.
5. **The quality line:** the expected change in defect rounds, plus the trial's detection power (for example "by 40 runs this trial catches a doubling of defect rounds about half the time, and a tripling 9 times in 10").
6. **Buttons:** Copy instruction, Start trial, Dismiss. The next ingest marks the card "applied" or "not followed" from the per-call effort and model fields.

**Tiers:**

| Tier | Condition |
|---|---|
| **Worth a trial** | The prior's median saving is ≥ 2 pts/week, and a trial is feasible: the unit leaves a local log, switching costs about 0, and there are ≥ 10 eligible units a week |
| **Recommended** | Concurrent randomized data; data share ≥ 0.5 (about 21+ tasks per arm); ≥ 2 distinct weeks per arm; Conf ≥ 0.90 at a pre-registered look (20, 40 or 80 per arm); and the guardrail has not fired. Simulated on the author's numbers, an option with no real effect reaches this 4–8% of the time. |
| **Keep current** | The trial reached 80 per arm or 6 weeks without Recommended; or the guardrail fired; or the prior saving is < 2 pts/week |
| **Collecting data** | A trial is running: "n of ~N needed" |

Accounting levers show their cost as exact, but they also pass a guardrail trial before Recommended.

**How a change is applied** (the tool never edits settings without the user's consent):

| What changes | How |
|---|---|
| Effort in a Code-tab session | The effort picker or `/effort` before the next prompt. Same session is fine. |
| Default effort for new Code sessions | `modelSettings.claude-opus-5-5.effortLevel` in `~/.claude/settings.json` (low to xhigh; max is per session) |
| Model | Start a new session |
| Workflow roles | A line in the `PROMPT_*.md` header, if you use handoff files, e.g. "Builders: Opus 5.5 high; final reviewers: xhigh" |
| Cowork | The task's effort picker. The tool can't see cloud tasks' results. |

**Trials** (opt-in; deadline-critical topics excluded; final reviewers and local Cowork sessions are never downgraded unless the user opts in):

- **Assignment:**
  - Each unit (a workflow run, or a packet family) gets a seeded random draw in permuted blocks of 2–4 when it is created.
  - The arm is revealed only when the unit starts.
  - p = 0.5 is logged, controls are concurrent, and each assignment is written to `data/assignments.jsonl` right away.
- **Trial 1, workflow runs:** randomize the whole run, so all eligible non-reviewer roles get the same arm (current vs one effort step down). Analyze cost per agent and quality per run. At a heavy user's volume, the output channel is measurable within weeks; total cost takes longer.
- **Trial 2, Code packets:** the escalation policy against fixed xhigh. A total-cost effect of 30% needs about 205 packets per arm, which takes months at a heavy user's pace. Until then, report the measured output channel and the guardrail.
- **No Cowork effort trial:** new tasks run in the cloud.
- **No Thompson sampling:** too little volume.
- **Guardrail:** the trial arm gets its own weak prior, λ_a ~ Gamma(0.5, 0.5/λ̄), independent of the control. After every closed unit from the 5th, stop and revert if P(λ_a > λ_c + δ) ≥ 0.80. δ = 0.5 defect rounds per packet in Code. No non-inferiority claims: at about 0.15 corrections per task (the author's rate), that needs hundreds of tasks.

### 5.6 What won't work at single-user volume

- per-cell estimates without pooling;
- observational effort or model effects;
- raw total-cost A/B tests on Code packets;
- treating the cost components as variance reduction;
- keyword-based quality detection;
- day-of-week seasonality;
- per-model meter weights;
- switching models inside a session;
- embedding-based topic clusters;
- using meter history across a regime break without a new calibration.

## 6. UI

The main page fits on one screen:

1. **Week strip:**
   - the meter now (official);
   - the projected value at reset (P50 and P10–P90), under each plausible rate until calibrated;
   - the chance of hitting the limit before reset, and the likely time;
   - a pace line, "(100 − meter) / days to reset" pts per day, flagged when today's burn is higher;
   - the 5-hour window, when present;
   - unattributed pts this week, with tags;
   - "past 100%: X pts ≈ $Y of usage credits", when extra usage is on;
   - a calibration badge.
2. **Daily chart:**
   - one bar per day, stacked by Code main thread, workflow agents, subagents, local Cowork and unattributed, in pts (toggle to $ or tokens);
   - the official meter line, with a projection fan to the reset;
   - a 100% line and reset markers;
   - next week's band and uncapped demand.
3. **Breakdown table**, with tabs for conversations, projects, topics, effort and model. Columns:
   - tokens by type (input, cache write, cache read, output incl. thinking);
   - average tokens per conversation;
   - pts (80% range) and $;
   - tasks, median and p90 per task;
   - rework share and effort mix;
   - change vs last week.

   The default filter is "active".
4. **Recommendations**, as cards (§5.5). Illustrative example, while the account's rate is uncalibrated: "Main project, workflow runs: builder agents xhigh → high · untested estimate: saves ~X pts/week (80%: a–b) at the rate fitted before the break, or ~Y at the rough rate since · all from output per request (a trial can measure this); requests per task assumed unchanged · quality: unknown (the coding prior expects +25% defect rounds) · trial: 20 runs per arm; it catches a doubling of defect rounds about half the time by 40 · [Start trial] [Copy instruction]"
5. **What-if grid** per topic: effort (low to max) × model (Sonnet 5.5, Opus 5.5, Fable 5.1). Each cell shows pts per week, $ and correction risk. Untested cells are marked as estimates.
6. **Trials:** arms, n, data share, the guardrail state and compliance.
7. **Data health:**
   - meter readings per day, by source;
   - time of the last ingest;
   - the price check against cost-state;
   - rule validation scores;
   - the cache behavior after effort switches on the current build;
   - coverage gaps (cloud Cowork, other devices).

A "hide titles" switch masks session titles everywhere. Later: a compact band in the desktop app, such as "40% · ~70% at reset · 2 cards" (illustrative).

## 7. Build plan

| Phase | Scope | Exit check |
|---|---|---|
| 0, first (about 1 hour) | Copy `plan-usage-history.json` into `data/raw/` right away, then daily (the nightly job in §10.5 does this); it loses a day of history every day. Write the 20-line logging mod and test it in one Code-tab session. Start the ingester into SQLite. Optionally raise `cleanupPeriodDays` (CLI only), with the user's consent. | Meter readings are arriving from the mod; history is preserved |
| 1, MVP: static report | Ingest (§4.1), per-regime calibration with an uncalibrated mode, daily chart and breakdown (tokens and pts), forecast with censoring, time to limit and pace line, session-hygiene cards (cost exact), unattributed stream | $ within 5% of cost-state where comparable; the forecast beats persistence and the pace baseline in a paired backtest |
| 2: local page | 127.0.0.1 server with a token; tasks and topics; Code quality rules validated on 40–60 labels; effective cost per task; untested-estimate cards and the what-if grid; Copy instruction with a compliance check; unattributed tags | Rules pass a precision and recall of at least 0.70 on the user's labels |
| 3: trials | Randomized workflow-run trials and the escalation policy; the conjugate engine; the guardrail with detection power; the Recommended tier; validation pages (calibration of confidence, backtests) | Pre-registered looks work end to end; 80% intervals cover about 80% |
| 4, optional | A desktop band or pane via the mod; trial arms applied through the mod's `turn.step`, only with the user's consent; a local-model labeler if a free-text stream appears | — |

The research prototypes behind Phase 1 (ingesters for Code and Cowork, meter extraction, the calibration fit, the backtest and nowcast, the confidence simulations) are not part of this repository. Two lessons carried over: generic globs miss Cowork's hidden folders, and daylight-saving dates must not be hard-coded.

## 8. Risks and guardrails

- **The log formats are internal and change** (for example, audit result totals switched from per-run to cumulative between builds 2.1.271 and 2.1.275). Use versioned parsers, check the schema on every ingest, and fail loudly.
- **Calibration:** regime breaks and unlogged usage. Calibrate per regime, show uncalibrated scenarios, keep the unattributed stream explicit, and run a CUSUM alert.
- **Small n:** untested estimates carry no %, Recommended requires randomized data, and the guardrail shows its detection power.
- **Privacy:**
  - **Allow-list only.** From session metadata, read only sessionId, cliSessionId, title, model, effort / effortOverride, sessionType, createdAt, lastActivityAt, isArchived, error, postTurnSummary.status_category and sessionSettings.ultracode. From `audit.jsonl`, read only rate_limit_event and result records.
  - Prompt text is read from transcripts in memory, only for the labeling screen. Only the message uuid and the label are stored. Nothing is written to logs or exports, and nothing leaves the machine.
  - No credentials are read. The UI binds to localhost only.
- **The monitor must not spend the user's limit:** no Claude calls.
- **Mods are unsandboxed:** run only code you have read, and keep it minimal.

## 9. Open questions

1. When the meter drops to 0 without a new reset time and the plan label is unchanged, should the post-break prior (§5.1) assume a limit change, or keep the old rate?
2. Can usage from surfaces the local logs don't cover (claude.ai, mobile, cloud Cowork, other machines) be attributed automatically, or only tagged by hand?
3. Which rework suffixes mean a model defect, and which mean a change of requirements?
4. Should other accounts on the same machine be included, or only the one the meter tracks?
5. When extra usage is on, should its monthly spend limit get its own forecast and time to limit, like the weekly meter?
6. Is the order right: static report, then a local page, then a desktop pane?

## 10. Switch-conversation advisor (built 2026-11-06)

Code: `monitor/` (see README.md). It was verified by three independent checks: a code and accounting review, a from-scratch replication, and a realism and quality-risk review. Their fixes are in.

### 10.1 The rule

The rule's inputs (fresh-session size, calls per packet, returns after an expiry, the compaction point) are measured from the user's own history: `python -m monitor.backtest_switch` writes them to `data/advisor_params.json`. The figures below are the author's. The plugin uses them as defaults; when it runs from a checkout, that file replaces them.

**Cost side.** Compare continuing with starting a new conversation over the next packet's life (median 80 main-thread calls in Code). Only the resent context counts; output and new content cost the same either way.

- **Continue:**
  - Every call resends the context C: a warm read at $0.20 per M.
  - After an hour idle, the next message re-caches C at $8 per M. The ~45K shared prefix stays cached.
  - A packet sees 0.6 such returns on average in Code, and each re-caches C again.
  - The horizon stops at the auto-compaction point (about 961K, compacted to about 102K).
- **New conversation:**
  - It starts at the fresh-session size (79K, of which 45K is read from the shared cache), plus an 80K re-read allowance. Those tokens are written once, then read on every call.
  - Later expiries re-cache this smaller conversation instead.

**Quality side** (`monitor/packets.py`). The first lines of the `PROMPT_*.md` file the prompt names set a risk class:

- **low:** a separate piece of work, not addressed to this conversation;
- **medium:** the header allows a new session, or the packet builds from files on disk in the same family;
- **high:** written for this conversation, a correction or follow-up round, a stale file (unchanged for 6 h or more), or a re-reference.

A switch is suggested only for low risk (expected saving ≥ $2) or medium risk (≥ $5), and only when the conversation has at least 250K tokens. Follow-ups and high-risk packets never get one.

### 10.2 Backtest

The replay ran on part of the author's Code and Cowork history; read it as one account's example. "Lost" counts switches whose own value was negative.

| Policy | Code switches | Lost | Cowork |
|---|---|---|---|
| Suggested (low or medium packets) | 3 (1 low, 2 medium) | 0 | no suggestions |
| Every new packet, cost only, 80K re-read | 12 (6 high) | 2 | — |
| Every new packet, cost only, no re-read | 13 (6 high) | 1 | — |
| Suggested + handoff before an expiry when the user was at the computer (upper bound) | 4 | 0 | 2 mid-task switches |

The first version reported nearly 7 times the saving of the suggested policy, from 15 "low-risk" switches. It had two errors:

- After a real compaction it shrank the replayed new conversation to the fresh-session size.
- It treated fix and follow-up rounds of the same packet as new packets.

### 10.3 What this means

- **Quality-aware switching saved little in the author's history.** Most packets there were written for the conversation that was running. Switching there risks extra review rounds, and a single extra round would cancel a switch's cost-only saving.
- **Little was left to switch.** In the author's recent history, each Code session carried a single packet family.
- **The bigger cost is coming back to a large conversation after a break.** Each return re-caches the whole context. In the author's long Cowork session, those returns were about half of its cost. Switching after the fact doesn't avoid that cost. Two things do:
  - knowing before you send: the advisor shows the re-cache cost;
  - wrapping up before you step away: a handoff while the cache is warm.

### 10.4 Next: the live notice

Decide when a prompt is submitted, the one moment you are reliably at the computer. The engine is a Claude Code mod in the desktop Code tab: a `prompt.submit` hook, `$.ui.ask` / `$.ui.toast`, and `$.ui.status` for a passive line.

| Element | When it shows | Behaviour |
|---|---|---|
| Card | Only for low- or medium-risk packets above their bar | `[New conversation] [Keep going here anyway]` |
| Status line | When the context passes 2× the fresh size | e.g. "Warm until 15:40 · 640K tokens · after that the next message re-caches it (≈$4.77)" |
| Minute-50 notice | Only if there was input in the last 15 min | Once per idle gap |
| Cowork | Always | Status line only |
| Dismissal | After you dismiss a card | Quiet for that conversation for the rest of the day |

§10.6 describes what was built.

### 10.5 Zero-cost automation and the switching procedure (designed and prototyped 2026-11-06)

**Nothing in the monitor spends usage.**

| Piece | Where it runs | Claude usage |
|---|---|---|
| Live check at prompt submit, and the status line | Inside the Claude app, as a mod: `$.session.usage()` ("the plain call costs nothing"), turn timings, and the packet header read with `$.fs` | None |
| Refreshing the rule's inputs | Windows Task Scheduler runs `pythonw -m monitor.nightly` once a night (README.md has the command). It copies the meter history and runs `monitor.backtest_switch` (about 20 s), the guardrail and `monitor.verify_estimates`, all locally. | None |
| The prompt for the new conversation | Built from the transcript: `$.session.messages()` in the mod, or `python -m monitor.handoff` | None |
| Switching | Drop the prompt (`{ drop }`), run `/clear` (`$.command.run`, queued after the hook returns), then put the new prompt in the box (`$.prompt.fill`). You review it and send. Since 0.4.0 the plugin no longer clears: the old conversation stays, and the handoff waits for a new one (§10.6). | Only the message you were sending anyway, now in a small conversation |

The mod never does any of the following on its own: send a prompt, add context to one, compact, request a token-count breakdown, run `/insights`, or schedule a Claude task.

**The new conversation's first prompt** comes from `monitor/handoff.py`.

| Case | What the prompt holds | Size |
|---|---|---|
| New packet | The prompt being sent, plus a preamble: the project and branch, the packet path, the files its header says to read first, and the reports the old conversation wrote | About 175 tokens |
| Resume | The original request, the old conversation's last message (trimmed), its task list, the files it changed, the last build or test result, and the questions it left open | 400–750 tokens in four of the author's sessions |

Asking Claude for a richer handoff while the cache is warm is an option. It costs about $0.20–0.40 with thinking. After an expiry it also pays the re-cache.

**This makes switching after a break worth considering.** The free handoff means coming back to a large conversation no longer forces the re-cache. Replayed on the author's history (same windows as §10.2):

| Surface | Switches | Switches that lost |
|---|---|---|
| Code | 21 (15 mid-task) | 6 |
| Cowork | 32 (all mid-task) | 2 |

These are upper bounds. Most of these switches are mid-task, and a handoff built from the transcript may miss details. Offer this only as an opt-in, with the guardrail trial from §5.5.

**To verify when building the mod:**

- what `/clear` does in the desktop Code tab;
- that `$.prompt.fill` lands in the new conversation;
- the clipboard fallback (`clip.exe` through `$.process.run`) for when it doesn't;
- whether mods run in Cowork at all.

### 10.6 After-break switch with money and quality safeguards (built 2026-11-07)

The plugin is in `mod/switch-advisor/`; README.md says how to install it. Two analyses of the author's history set the rule.

**Money.**
- **When it can appear:** only after a break, once the cache has expired. It never appears while the cache is warm.
- **Size test:** a card appears only when the conversation is larger than a new one would be after a pessimistic 150K re-read (C ≥ B + 150K + 1K, about 230K in Code), and when even that worst case, (C − 230K) × $8/M, saves at least $0.50 (about 293K at Opus 5.5 prices). The card shows the worst case.
- **Where it shows:** a band above the prompt as soon as the cache has expired and a switch pays (New conversation, Keep going here anyway, Details; never drawn on a phone), and the same card at prompt submit if the band went unanswered. Keep going here anyway on the card keeps the plugin quiet in that conversation for 12 hours.
- **Claude Code's own summary:** sometimes Claude Code computes a summary about 55 minutes into a break and applies it when you send. Then continuing is cheap, and a switch would have lost money. The plugin sees this ahead of time through the `session.compact` hook with `trigger: precompute`, and stays quiet.
- **Packets:** a prompt that names a `PROMPT_*.md` goes by the packet's risk class. A high-risk packet gets only a note.
- **Replayed on the author's history** (same windows as §10.2):

  | Surface | Switches | Switches that lost money |
  |---|---|---|
  | Code | 6 | 0 |
  | Cowork | 29 | 0 |

  The earlier rule made 21 Code switches, 6 of which lost money. The money analysis also held up under a time split and leave-one-session-out.
- **Cowork** (since 0.3.0; revised in 0.4.0 after a closer look on 2026-11-08):
  - **Installing.** Cowork loads only the plugins in your Claude account, never `CLAUDE_CODE_PLUGIN_DIRS`: each Cowork task has its own Claude Code configuration folder, with empty settings. So Cowork needs the plugin installed in your account: Customize › Plugins › Add, either a marketplace or an uploaded `.plugin` file.
  - **What shows there.** New Cowork tasks run in Anthropic's cloud, where a mod's hooks run but nothing it draws appears. A local task's view doesn't show a mod's band, status line or toasts either (desktop app 4.23118), and it doesn't follow a `/clear` that the engine runs. The card at submit can still show, as Cowork's own question.
  - **No band during a break.** In the author's logs since September 2026, Cowork started Claude Code anew for every message and stopped it when the reply ended. So nothing runs during a break, and the band can't be waiting when you come back.
  - **Switching.** New conversation therefore never clears (see below). In Cowork the handoff is fetched with `/switch-test restore`, to paste into a new task: each task keeps its own plugin store, so a new task can't pick it up by itself.
  - **Fail-safe.** Each step fails safe: a card that can't be asked, or isn't answered within 2 minutes, sends the prompt as usual.
  - **One copy acts.** The account's copy also loads in the Code tab, next to the folder that `CLAUDE_CODE_PLUGIN_DIRS` names. It reads that setting at session start and steps back there, so only one copy acts. A guard on `plugin.register` was tried, but the test kit can't raise that event, so it was left out.
- **Switching without clearing** (0.4.0). New conversation used to run `/clear` and put the handoff in the new conversation's box. It no longer touches the conversation you switch away from.
  - **Where the handoff waits.** It is kept in the plugin's store, with your unsent prompt at its end. There is one per conversation, under its own key, filed under the conversation's project folder (`$.session.root()`: where it started, or where `/cd` or a worktree move took it; the shell's own folder moves with every `cd`). Folders are compared whole, without case. So two conversations in one folder never replace each other's.
  - **Switching again.** A second switch in the same break adds its request to the first handoff, once. The requests are kept as a list and compared exactly, so one whose words already appear in the handoff ("continue", say) is still added, and the first replaces the band's placeholder. A switch after a later break makes a new handoff. If the card held a message back in an earlier break, no new conversation took it, and you haven't sent it here since, the new handoff carries it, set apart before your new request (in a packet's handoff too, which ends with your prompt), and the note says so. A held message that goes out here (and isn't refused by a hook beneath) is dropped from the messages a later switch carries as it is sent, so a later compaction can't make it look unsent; the handoff's own text stays as it was. In a packet's handoff, which has no "My next request:" line, that line is added after the block, so your prompt never reads as part of it.
  - **Who takes it.** In the terminal, the next new conversation in that folder within 30 minutes finds it in its box, once: one with nothing sent yet, an empty box, and not the one it came from. "Nothing sent" means no turn, or only rows that are a slash command's own; a reply, a compaction summary or a skill's run counts as something sent, so a resumed conversation never takes another's handoff. A new session in the desktop app's Code tab has no box the plugin can fill before its first message: the app keeps the new-session box until then. So there the note says to paste the handoff (it's on the clipboard) or to send `/switch-test restore` first. A taken handoff is marked, not deleted, so restore still shows it.
  - **After a `/clear`.** A `/clear` puts the handoff that the cleared conversation made in its last break into the new box, however old it is (up to 24 hours). If Claude replied there after the switch (to you, or to a background task's report, which the handoff then lacks), or another box took it, a toast says it isn't in the box and why, and `/switch-test restore` puts it there anyway. Only a window whose conversation made no handoff takes the folder's newest within 30 minutes, and the toast says it was kept for the folder, not made there. So two windows never swap handoffs. The engine names the cleared conversation in `session.end` (reason `clear`) and fires no `session.start` for the new one. Its turn count already includes the `/clear`'s own row, which 0.4.0's first build tripped over (the box stayed empty), so this path goes by the plugin's own note of whether anything was sent, reset in `session.end`, before the SessionStart hooks beneath run.
  - **A pasted handoff** sent as a new conversation's first message is marked taken when everything before its request matches (for a packet's, everything before your prompt), with spaces, line ends and zero-width characters ignored. So writing over the band's placeholder, rewording your prompt or leaving out an earlier break's block still counts.
  - **Other ways to get it.** `/switch-test restore` shows it until it is 24 hours old; it is deleted the first time the plugin starts after that. In a window, restore gives this conversation's own handoff first, then the one its box took, then the one made by the conversation it cleared, then the newest no box took. Putting a handoff another box took into this box leaves it marked as that box's, and one this box held before, while nothing was sent here, is freed for another. What a command returns is part of the conversation: Claude reads it with the next message sent there. So `/switch-test restore all` only lists the handoffs kept for the folder, one line each (age, whose, and the first line of its request), filling and marking nothing, and `/switch-test restore <number>` gives one of them as restore does. A `/switch-test` handoff never replaces a real one from the same conversation, taken or not. If none of the store, the clipboard or, from a checkout, `data/handoffs/` takes a handoff, the prompt is sent as usual, never dropped. Otherwise the line a held-back prompt shows names the way that did work.
  - **Store upkeep.** The store is one JSON file with a 4 MiB limit. At each start the plugin deletes handoffs 24 hours old and, up to 50 at a time, the notes of other conversations idle for 30 days (when Claude last replied, a summary prepared, the band dismissed) and quiet periods that are over. A resume notes the conversation's break and the resume itself, so no other session's upkeep deletes the break, or the band's dismissal of it, of a conversation resumed in the last 30 days, however long it had been idle before.
  - **The card's answers.** At prompt submit, a card you close, one that can't be shown, or one with no answer within 2 minutes is no choice. The prompt is sent, with no quiet period and nothing logged. Until 0.4.0 these counted as Keep going here anyway, which muted the card for 12 hours after one you may not have seen.
  - **After the limit.** The card can't be taken off the screen, so an answer picked on it later gets a note that the message has already gone out and the answer changed nothing. Details and `/switch-test` have no time limit, since nothing is held while you read them. Details answered after its break is over (a notification's or a schedule's turn ran meanwhile) changes nothing, and says so.
  - **Handoffs after a compaction.** A handoff built after a compaction now quotes what the compaction summary says was asked, what is pending, the current work and the next step, without the engine's preamble and closing instructions. Before, it quoted the summary's preamble as "the original request". Section headings count only at the start of a line, plain ("8. Current Work:") or in bold on a line of their own ("8. **Current Work:**", "**8. Current Work:**"): 6 of the 24 summaries in the author's transcripts use bold, and the digest now carries all four sections in all 24 (18 before). An indented numbered item is not taken for a heading, nor a numbered line that only opens in bold ("1. **Update the docs** in README.md"). "**1. Primary Request and Intent:** Build…", text on the heading's line, is read too. Not read: such a line whose number sits outside the bold ("1. **Primary Request and Intent:** Build…") or whose colon follows it ("**1. Primary Request and Intent**: Build…"), which none of the 24 has; the handoff then quotes the summary's first 700 characters. The packets your own messages named before the compaction count as already worked on.
  - **Where nothing draws.** `/switch-test demo` and `/switch-test band` say when the window doesn't draw what plugins show (Cowork, a cloud session), instead of saying the band is up.

**Quality.**
- **What the author's history shows:** natural experiments in it (38 compactions and 10 fresh sessions on known packets) found no measurable rise in corrections after continuing from a summary. But the data can't rule out a +0.15 rise in correction rounds per switch, which is enough to cancel the saving. The one clear effect is re-reading: about 20K extra tokens per 100 calls, inside the 80K allowance.
- **The card names what the handoff may not carry:**

  | Level | Flags |
  |---|---|
  | High | a workflow still running; messages that exist only in the chat; the last build or test failed; a long reply that asks you something |
  | Medium | your message answers its last reply; open task-list items; more than 12 files changed in this task; a workflow that reported just before the break; a background command |
  | Info | uncommitted changes |

- **What each risk level shows:**

  | Risk | When the card shows | Default button |
  |---|---|---|
  | Low | typical saving ≥ $2 | New conversation |
  | Medium | typical saving ≥ $5 | New conversation |
  | High | typical saving ≥ $5 | Keep going here anyway (the other button is "New conversation") |

  You decide every time; nothing switches on its own.
- **Handoff fixes:** wrapping up a turn now closes the task list and carries its question. Workflow results that reported just before the break are listed.

**Guardrail.** Run `python -m monitor.guardrail` locally, with no model calls.
- It matches each accepted switch to the new conversation, and counts trouble in the first 3 prompts: a correction or rework round, or you going back to the old conversation within 24 h.
- It compares that with returns that continued in place (18 of 25 in the author's history).
- After 5 switches it turns the band and the after-break card off if P(worse by more than 0.15) ≥ 0.80, or if the switches saved less than $0. It writes `data/guardrail.json`, and stays off until you set `after_break_enabled` back to `true` by hand; from then on it stays on (deleting the file instead starts the guardrail over).
- At this baseline it only catches large harm quickly.

**Which prompts the card asks about.** Only one you sent in the app on this computer, as far as the engine can tell. The engine (Claude Code 2.1.317, the version the desktop app bundles) gives a prompt with a human origin the origin `composer`: Enter in the terminal, and any message the desktop app or Cowork relays without declaring an origin, which it presumes human from the message's client platform. It gives `bridge` to a message over its own Remote Control from a phone or the web, and `sdk` to one the app declared automatic (a CI event, a continue after a quit or a usage limit), as it drops that label locally. The app sends some continues with no label (after a sleep, or once restored from its queue after a restart), so the plugin also knows the app's continue messages by their wording, those after a restart on a remote host included ("… while you were working … Please continue from where you left off."). A prompt sent while a turn is running gets no card either, since that turn re-writes the cache. Nor does a slash command, even a skill or a built-in command that asks the model (`/init` or `/code-review`, say), since New conversation would put it in the box as plain text, where it no longer runs as a command; nor a prompt with a pasted image or file, which the new conversation's box can't take; nor a prompt the engine marks as one you did not see typed (`isMeta`). An answer typed under the card's Other counts as neither choice: the prompt is sent, with no quiet period. The author's transcripts agree: typed Code-tab and recent Cowork prompts carry a human origin, and the two Code-tab prompts without one are the app's usage-limit and quit continues. What the engine can't tell apart still looks typed: a message relayed from a phone through Cowork's Dispatch (the desktop relays it as its own), and phone or web messages in a cloud session. `/switch-test` refuses to run over Remote Control: the card and the handoff it makes are for a new conversation at the computer. Up to 0.3.0 the card also asked about `sdk` prompts while the desktop window was attached; 0.3.1 stopped that, 0.3.2 added the continue wording, the running-turn skip and the `/switch-test` refusal, and 0.3.3 the slash-command, attachment and `isMeta` skips.

**Not verified yet, in the app itself:**
- that the desktop Code tab's clipboard copy lands, and that the box fills there after a `/clear` or from `/switch-test restore` sent as a new session's first message (either way, `/switch-test restore` prints the handoff; from a checkout it is also saved in `data/handoffs/`). In the engine alone (2.1.317, run in a terminal and in stream mode with no account and no model calls), a reviewer saw the fixed `/clear` path fill the box and mark the handoff taken, and restore as a new session's first message take the box path;
- that the precompute event reaches mods;
- in Cowork: that the module loads from the account, and that the card shows and its answer comes back (`/switch-test` in a large task shows both; `/switch-test demo` says whether the band can be drawn there).

## 11. The Codex version (built 2026-11-07)

Codex, the desktop app and the CLI, keeps its own session logs, and a Codex plugin can ship hooks. `codex/switch-advisor/` is the same advisor for Codex. `python -m monitor.codex` brings Codex into the monitor, using the plugin's own `advisor/` package, so both decide the same way. Like the rest, it reads only the logs on this computer and makes no model calls.

The plugin has not yet been tried in Codex itself: it is tested against Codex's source and with replays of real logs.

### 11.1 What Codex logs, and how its economics differ

- **Logs.**
  - Each conversation is one file, `~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`. Codex compresses older ones (`.jsonl.zst`, which Python 3.14 reads).
  - On Windows a log's modification time can stay near the conversation's start while Codex keeps writing to it, so the plugin dates a log by its last record, and a compressed one by when it was compressed.
  - Every model response is followed by a `token_usage_record`: its input (cached part included), cached input and output. Older logs have only `token_count` events, whose `last_token_usage` the plugin reads instead.
  - `token_count` events also carry the plan's own meter: the weekly window's used percent and reset time.
  - Text the person typed is marked with provenance `user`. AGENTS.md, environment context and app pages are marked otherwise, and never count as a request.
- **No cache-write premium.** Cached input costs a tenth of input (a twentieth on GPT-6.1 Sol and Terra). A request whose cache missed pays full input on everything except the shared prefix of instructions and tools (about 20K tokens), which stays cached.
- **The cache has no fixed lifetime.** The chance that it missed grows with the idle time before a request. Measured on one heavy user's main conversations (requests of 20K tokens or more):

  | Idle | 5–15 min | 15–30 min | 30–60 min | 1–1.5 h | 1.5–2 h | 2–4 h | 4–6 h | 6–12 h | 12–24 h | over 24 h |
  |---|---|---|---|---|---|---|---|---|---|---|
  | P(missed) | 0.07 | 0.11 | 0.19 | 0.25 | 0.39 | 0.58 | 0.64 | 0.76 | 0.92 | 1.00 |

  These are the plugin's defaults. `python -m monitor.codex replay --write` measures your own curve.
- **Compaction.** Codex compacts a conversation by itself near the end of its window. Over all of that user's logs it did so at about 86% of the window (about 221K tokens of the 258K effective in a 272K window), down to about 39K; in the last two weeks, at about 94%, down to about 29K. The plugin's defaults are the first. `replay --write` measures these, the size of a new conversation's first request and the shared prefix from the last 14 days, or from the newest 5 samples when those days hold fewer, since they follow Codex's own behaviour. With a 272K window a conversation never gets very large.
- **Long context.** A request above 272K input tokens costs the long-context price as a whole: twice the input and cached rates, 1.5 times output. That happens with a raised window, or with a model whose default window is 500K.
- **Prices** for each model and speed tier are in `advisor/prices.py`: OpenAI's list prices as of 2026-11-07. Fast mode (logged as `priority`; `fast` is read too) costs twice Standard (2.5 times on GPT-5.5), and Ultrafast six times.
- **The weekly meter is in the logs**, so "% of your weekly limit" needs no setup. Over each weekly window, the API-equivalent dollars of the requests in the logs, divided by how far the meter rose, give $ per 1%. The plugin uses the median of the 3 most recent windows, refreshed in the background at most twice a day. Usage the logs don't see (Codex cloud, another computer) moves the meter too, so the rate is an estimate.

### 11.2 The rule

For a conversation whose next request resends C tokens, after an idle time t with P(missed) = q:

- **Sending here** is expected to cost q × (cold) + (1 − q) × (warm). Warm is C × cached; cold is the shared prefix × cached plus the rest × input.
- **A new conversation** costs a cold request of N = B + R + H tokens: its first request (27K), the files it reads again (80K, as measured for Claude Code), and the handoff (2K). The worst case uses R = 150K.
- **Save now** = sending here − the new conversation; **worst case** = sending here − the worst-case new conversation.
- **Typical by your next break** = save now, plus each of the next 22 (p25) or 50 (p50) calls. Until Codex compacts the old conversation, a call saves C × cached − N × cached. After that, the old conversation holds about 39K, less than the new one, so those calls count against the switch. The compaction's own request is left out, which keeps the estimate on the cautious side.
- **A card needs:**
  - a break of at least an hour, and q ≥ 0.5 (about 2 hours or more);
  - a worst case that still saves at least $0.50, both now and by your next break. If the new conversation re-reads 150K tokens, each of its later calls resends that much more, and after Codex compacts the old conversation those calls count against the switch;
  - both typical figures at or above the bar: $2 at low quality risk, $5 at medium.

  High risk never holds a prompt back. The note then says what a switch would save and the main reason.
- **A note** (the prompt goes through) appears after an hour or more when q ≥ 0.25 and sending here is expected to cost at least $0.50. It gives what the message costs if the cache missed, next to how often it had after breaks that long, since the expected figure would read as the cost of a miss.
- **The card** opens with the same cost of a miss and its share of the weekly limit, then the average, from which the saving is counted, so a card and a note for the same moment give the same figures. The times it gives for switching and staying are never rounded up: whole minutes, rounded down, and hours only when exact. The time away is rounded to a tenth of an hour.

### 11.3 What the hooks do

- **UserPromptSubmit**, run on every prompt:
  - Most prompts come without a break; the hook reads the end of the conversation's log and stops there, in about 0.1 s including Python's start-up (on Windows, PowerShell's own start-up adds 0.2 to 0.4 s; see below). Idle time counts from the last model response (the last `token_usage_record`, or in older logs the last new `token_count`), because Codex logs the new turn's start before the hook runs.
  - It leaves alone a prompt typed in an agent's thread (Codex then sends the main conversation's `session_id` with `agent_id` or `agent_type`) or in a delegate's, such as a `/review` run (Codex sends the main conversation's `session_id` with no agent fields, but the delegate's own log, which marks it as a subagent), so a `/review` soon after a card doesn't count as staying. It also leaves alone one sent while a turn of the conversation is still running, judged by that turn's own last record and for 24 hours at most.
  - A prompt sent from the ChatGPT phone app to a conversation on this computer runs the same hooks, as its turn runs here; the hook's input doesn't say where it came from. Codex writes the turn's `task_started` to the log before the prompt hook runs (in the source of the installed release: the regular task emits it before the turn runs, the event is written to the log before it is delivered, and the prompt hook runs inside the turn), with the hook's `turn_id` and `turn_attribution.turn_trigger`: `composer` from the desktop app, `user` from the CLI, `remote_android` from the Android app, and others for an automation (`automation_cron_…`, `automation_heartbeat_…`), a retry after a capacity error, an answer from the question panel, an edited (`edit_user_message`) or queued (`composer_queue`) message, plan feedback (`plan_feedback`, `plan_implementation`) and the app's other kinds. Only a prompt with `composer` or `user`, or with no trigger to read (older Codex records none, and a turn whose start can't be matched is treated as one from the computer), can get the card; a trigger that isn't a short identifier is logged as `other`. Any other gets the note where the card would show: for most of these nobody may be there to see the card, and an automation's held-back prompt would just be lost. Whether the phone shows a hook's card or note isn't documented, and a held-back prompt that the phone doesn't explain would look like a message that went nowhere; sending it again would count as staying. `switch-advisor test` is still held back there, to see what the phone shows. The decision log records the trigger.
  - After a break, a card holds the prompt back with `{"decision": "block", "reason": <card>}`. In the Codex desktop app, the prompt then stays in the conversation as a not-sent message marked "Hook blocked this message", with the card below it. In the CLI, the prompt stays in the scrollback, with the card under "Blocked by hook".
  - The plugin keeps one handoff per project folder, matched after resolving links and short names. While one waits, a card in another conversation in that folder becomes a note, so the waiting handoff is never replaced by another conversation's. A test's handoff doesn't count: a real card replaces it, and a test never replaces a card's. A handoff dated more than a minute ahead of the clock is stale.
  - Sending a prompt to the same conversation within 15 minutes means stay: that conversation isn't checked again for 12 hours. After a switch, such a prompt only keeps the conversation quiet; the decision log counts a stay only when the prompt dropped a waiting handoff.
  - Without a card, a `systemMessage` note lets the prompt through.
- **SessionStart** (startup, or clear: the CLI's `/clear` starts a new conversation with that source): a new conversation in the same project folder gets the handoff as developer context (`additionalContext`) when its first prompt is sent within 30 minutes of the card, since Codex runs SessionStart as that first turn starts, before the prompt hook. The handoff is built on this computer from the old log:
  - the project and branch;
  - the first request and up to three of the latest;
  - the last reply;
  - the questions still waiting in Codex's question panel, with their options: the newest 3, each title up to 500 characters with up to 8 options of 120, a cut marked; how many older ones are left out; and a line for any the plugin can't read. They come right after the last reply and are never cut. Past the handoff's 8,000 characters the reply shrinks first, or is left out; if that is not enough, the other parts are cut from the end, the agent count and the files first, and the questions then come last. A question call whose arguments aren't JSON asks nothing, as Codex answers it with an error;
  - the uncommitted files;
  - how many agents the old conversation started.

  It never includes the prompt that was held back.
- **SessionStart** (async): refreshes the weekly-limit rate.
- **On Windows** Codex runs a hook's command in the conversation's shell, which on Windows is PowerShell (`pwsh`, else Windows PowerShell), as `-NoProfile -Command <command>`. It uses `cmd.exe /C` instead when a conversation has no single local environment (then only the hooks go through cmd; the conversation's own commands keep their environment's shell), or when it finds no PowerShell. That is the source of the installed release (rust-v0.167.0-alpha.3.2) and of the main branch: `build_hooks_config` in core/src/session/mod.rs and `build_command` in hooks/src/engine/command_runner.rs. The hook runs in the project folder, outside Codex's sandbox. PowerShell looks for a bare program name in the current folder only through a relative PATH entry such as `.`, which exposes every command a conversation runs in the same way; cmd does by default, before PATH, so through cmd a `python.bat` placed in the project would run in Python's place. The Windows commands are therefore one plain command that parses in every shell: `python "${PLUGIN_ROOT}\hooks\<script>.py"`. Tried with Codex's own quoting and a PATH of absolute folders, in Windows PowerShell and in the PowerShell 7 of Codex's runtime folder, no planted `python.bat` or `.cmd` runs. Through cmd one would: that is the case the plugin can't guard, as no command string is safe in both shells. Two more limits come from the shells:
  - PowerShell expands a `$` followed by a letter, a digit, `_`, `$`, `^`, `{` or `(`, and a backtick, inside the command's quotes, so a plugin folder whose path holds one breaks every hook (they fail, and prompts go through);
  - through cmd, Python exits 2 with its error on stderr when it can't open the script (a `%NAME%` in the folder's path, a missing file), which Codex reads as a block, so every prompt is held back with that error.

  Earlier forms failed:
  - `cd /d "%SystemRoot%" && python …`, written for cmd, is a parse error in Windows PowerShell and a failed `cd` in PowerShell 7, so no hook ran;
  - `set "NoDefaultCurrentDirectoryInExePath=1" & python …` fails in PowerShell too;
  - a forward slash after a `\\?\` plugin path is read as part of a name, so the script's path takes backslashes.

  PowerShell's start-up adds 0.2 to 0.4 s to every prompt, which any Codex hook pays on Windows. The hooks print ASCII-only JSON, with any lone surrogate replaced, as Codex's JSON reader refuses one. The plugin runs `git` by its full path, found in an absolute PATH folder. On macOS and Linux the shell searches the project only when PATH holds `.` or an empty entry, and then so does every command Codex runs there, so the POSIX commands stay plain. They put the path in double quotes too, so a plugin folder whose path holds a `$`, a backtick, a `\` or a `"` can break every hook there; then, as through cmd, Python exits 2 with its error, and Codex holds every prompt back with it.
- **Quality flags:**
  - high: agents of this conversation still running; a reply that asks something itself and is longer than the 1,500 characters the handoff keeps; a question in Codex's question panel that the plugin can't read, whatever else asks;
  - medium: your message answers the last reply or a question in the question panel (the handoff carries the newest 3, and says how many it leaves out); the conversation coordinated agents whose reports stay in it; more than 12 uncommitted files;
  - info: uncommitted files.
- **Trying it:** `switch-advisor test` shows the card with the conversation's own numbers as if after 3 hours, even when the rule wouldn't show one, with the note as a `systemMessage` when a real break would give that instead. It keeps a real handoff to try, unless a card's handoff is waiting in that folder. `switch-advisor demo` shows example numbers and keeps no handoff. Both are held back whenever the hooks run, so neither reaches the model: on an older Python 3, where the plugin does nothing else, they get a message that it needs 3.10 or later, naming the Python the hooks run (the entry script reads its input as bytes and is plain ASCII, so neither the console's code page nor an old interpreter stops it), and when one fails partway it is held back with what failed. When the hooks don't run (not trusted yet, or no Python found), both go to the model like any other prompt.
- **Files:** the plugin keeps everything in its data folder under `~/.codex/plugins/data/`:
  - per-conversation state;
  - the pending handoff, deleted when used. An unused one is deleted by the first hook to run after 30 minutes, or by the nightly monitor job;
  - a decision log with times, event names, numbers, ids, model names, flag names, the risk level, the plugin's version and the turn's trigger only;
  - the rate;
  - an error log with the time, the hook and step that failed, and the exception's type only.

  Only the pending handoff ever holds conversation text.

### 11.4 What the replay found

On that user's logs: 163 returns in 22 of their 46 main conversations, all but one with a 272K window (the other had 500K). A return is a prompt sent after a break of an hour or more; a long wait on a tool inside a turn doesn't count, since the prompt hook doesn't run then, and nor does text Codex adds to a turn itself.
- **The rule's verdicts:** no card, and a note on 126 returns with the inputs measured from the logs (114 with the defaults).
- **Switching anyway would have saved almost nothing:** a switch at every return saves about a cent at the median, and about half of those switches (80) would have cost more. With compaction and a cheaper cold read, a new conversation's re-reading costs about as much as coming back.
- **The estimates held:** the expected cost of coming back was within 5% of the realized cost in total (24% at the median per return), and the miss curve predicted 116 misses where 117 happened.

So at the common window the plugin mostly informs; the card is for conversations above the long-context threshold. `python -m monitor.codex replay` runs the same replay on your logs.

### 11.5 Not verified yet

- that the installed Codex runs the hooks as the source of its release does: `${PLUGIN_ROOT}` filled in, `commandWindows` used, and the conversation's PowerShell as the shell;
- how the desktop app and the CLI show a held-back prompt, which §11.3 has from their code only;
- how the desktop app shows a `systemMessage` note, and what the ChatGPT phone app shows for a held-back prompt and for a note (until that is known, phone prompts get only the note), and which trigger an iPhone sends;
- that SessionStart fires when a new desktop conversation's first turn starts, as in the source (core/src/session/turn.rs), and whether it fires for an agent's thread;
- whether the prompt hook runs for a message typed while a turn is running. The running-turn check holds either way, but after a crash it could keep one prompt from being checked;
- whether prompts that Codex's automations send reach the hook, and how Codex handles a handoff longer than the hook's `additionalContextLimit`;
- which Python the hooks get: Codex's runtime folder holds a Python 3.12, and if Codex puts it first on the hooks' PATH, the plugin can't read compressed logs there;
- the POSIX commands, which were not run here;
- the guardrail on real switches, of which there are none yet. `python -m monitor.codex replay --write` turns the card off after at least 4 switches if half of them went back to the old conversation within a day.
