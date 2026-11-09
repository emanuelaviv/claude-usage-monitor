// The switch rule. Pure: no `$` calls, no model. Mirrors monitor/switch_advisor.py, monitor/packets.py and the
// after-break analysis in DESIGN.md §10.6.
//
// Costs cover the resent context only (output and new content cost the same either way). Opus 5.5 prices: a warm
// cache read is $0.20 per M tokens; after the 1-hour cache expires the next message re-writes the context at
// $8 per M, except a ~45K shared prefix that stays cached.
//
// A switch card is shown only after a break (the cache expired), never while it is warm, and only when:
//   - the conversation is bigger than a new one would be even if it re-reads 150K tokens of files
//     (so the switch can't lose money on the re-cache it avoids), and
//   - Claude Code hasn't already prepared its own summary of it during the break (then continuing is cheap).
import type { Facts } from './handoff'

export type Params = {
  baseTokens: number          // a fresh conversation's first request (system prompt, tools, memory)
  baseShared: number          // ...of which read from the shared cache
  rereadTokens: number        // files a new conversation is expected to read again
  rereadWorst: number         // ...in the worst case
  handoffTokens: number       // the handoff prompt itself
  callsBetweenReturns: [number, number]   // main-thread calls from one return to the next (p25, p50)
  compactAt: number           // auto-compaction point
  barLow: number              // minimum typical saving: low quality risk
  barMedium: number           // ...medium or high quality risk
  floorMin: number            // the worst-case saving must be at least this
  ttlMs: number               // cache lifetime of main threads
  quietMs: number             // after "Keep going here anyway" on the card, stay quiet this long in that conversation
  usdPerPt: [number, number] | null  // $ per 1% of your weekly limit (a range), once calibrated for your account; else $ only
}

/** Typical values from one heavy user's sessions; a checkout's data/advisor_params.json replaces them with yours. */
export const DEFAULTS: Params = {
  baseTokens: 79_333, baseShared: 45_240, rereadTokens: 80_000, rereadWorst: 150_000, handoffTokens: 1_000,
  callsBetweenReturns: [45, 115], compactAt: 961_476,
  barLow: 2, barMedium: 5, floorMin: 0.5, ttlMs: 3_600_000, quietMs: 12 * 3_600_000, usdPerPt: null,
}

/** The rate /switch-test demo shows its "% of your weekly limit" with: example numbers, like the rest of the demo. */
export const EXAMPLE_RATE: [number, number] = [12, 4]

/** Your $ per 1% of the weekly limit, from a checkout's data/plan.json: { "usd_per_pt": [high, low] }. */
export function rateFrom(json: any): [number, number] | null {
  const r = json?.usd_per_pt
  return Array.isArray(r) && r.length >= 2 && r.every((x: unknown) => typeof x === 'number' && x > 0) ? [r[0], r[1]] : null
}

/** Measured inputs from data/advisor_params.json (written by `python -m monitor.backtest_switch`). */
export function paramsFrom(json: any): Params {
  const c = json?.code ?? {}
  const num = (v: unknown, d: number) => (typeof v === 'number' && isFinite(v) ? v : d)
  const pair = (v: unknown, d: [number, number]): [number, number] =>
    Array.isArray(v) && v.length >= 2 && v.every(x => typeof x === 'number') ? [v[0], v[1]] : d
  return {
    ...DEFAULTS,
    baseTokens: num(c.base_tokens, DEFAULTS.baseTokens),
    baseShared: num(c.base_shared, DEFAULTS.baseShared),
    callsBetweenReturns: pair(c.calls_between_returns, DEFAULTS.callsBetweenReturns),
    compactAt: num(c.compact_at, DEFAULTS.compactAt),
  }
}

const PRICES: Record<string, [number, number, number]> = {      // $ per M: input, output, cache read
  'opus-5-5': [4, 20, 0.2], 'sonnet-5-5': [2, 10, 0.2], 'fable-5-1': [10, 50, 0.25],
  'haiku-5-5': [0.5, 2.5, 0.05],   // its rate above a 100K-token prompt, as every conversation here is; cache read ~10%
}
const FAMILY: Record<string, string> = { opus: 'opus-5-5', sonnet: 'sonnet-5-5', fable: 'fable-5-1', haiku: 'haiku-5-5' }

/** The session's model as the app names it ("Opus 5.5", "claude-sonnet-5-5[1m]"); another version of a family is
 *  priced as its current model, and anything unknown as Opus 5.5. */
function price(model?: string): { pcr: number; pw: number } {
  const m = (model ?? '').toLowerCase().replace(/[\s._]+/g, '-')
  const key = Object.keys(PRICES).find(k => m.includes(k)) ?? FAMILY[Object.keys(FAMILY).find(f => m.includes(f)) ?? 'opus']
  const [pin, , pcr] = PRICES[key]
  return { pcr: pcr / 1e6, pw: (2 * pin) / 1e6 }     // 1-hour cache writes cost 2x input
}

/** Writing a context into the cache from scratch: the shared prefix is still read. */
export function writeCost(tokens: number, P: Params, model?: string): number {
  const { pcr, pw } = price(model)
  const shared = Math.min(P.baseShared, tokens)
  return shared * pcr + (tokens - shared) * pw
}

export const usd = (x: number) => `$${x.toFixed(2)}`

/** "0.4–1.2%" of the weekly limit, or undefined until the rate is calibrated for this account. */
export function share(x: number, P: Params): string | undefined {
  if (!P.usdPerPt) return undefined
  const [a, b] = P.usdPerPt.map(r => x / r).sort((p, q) => p - q)
  return b < 0.05 ? '<0.1%' : `${a.toFixed(1)}–${b.toFixed(1)}%`
}

// ---- packets --------------------------------------------------------------------------------------------

const SELF = /\(the Claude Code (session|chat|conversation) that (built|wrote|ran|did)/i
const FALLBACK = /(if you can.t reach it|otherwise|or)\W+a new Claude Code (session|chat|conversation)/i
const YOUR = /\b(you built|you ran|you wrote|your (own )?(report|design|spec|rehearsal|driver|clone|fixes|feasibility note|note|reading|plan|revised design|open items|closure build report|sharpenings|rejection))\b/gi
const REWORK = /(_fix(es)?|_r\d+|_amend\d*|_revision\d*|_errata\d*|_repair|_followup|_carry_fixes)$/i

export function family(stem: string): string {
  const s = stem.toLowerCase().replace(/^claude_/, '')
  const m = s.match(/^([a-z]+\d+)(_(android|backend|ios|web))?/)   // a numbered stream, with its component
  return m ? m[1] + (m[2] ?? '') : s.split('_')[0]
}

export type Level = 'low' | 'medium' | 'high'
export type Risk = { level: Level; why: string }

/** header undefined: the packet file was not found. ageS: seconds since the file last changed. */
export function classifyPacket(stem: string, header: string | undefined, earlier: string[], ageS: number): Risk {
  if (earlier.includes(stem)) return { level: 'high', why: 'this conversation already worked on this packet' }
  if (header === undefined) return { level: 'high', why: "the packet file wasn't found, so it can't be checked" }
  if (ageS > 6 * 3600) return { level: 'high', why: `the packet file hasn't changed for ${Math.round(ageS / 3600)} h, so it reads as a reference` }
  const first = header.split('\n')[0] ?? ''
  const sameFamily = earlier.some(s => family(s) === family(stem))
  if (FALLBACK.test(header)) return { level: 'medium', why: 'the packet allows a new session, but that session has to re-read the spec first' }
  if (SELF.test(first) || (header.match(YOUR)?.length ?? 0) >= 2) return { level: 'high', why: 'the packet is written for this conversation and builds on its work' }
  if (sameFamily && REWORK.test(stem)) return { level: 'high', why: "it's a correction or follow-up round of this conversation's work" }
  if (sameFamily) return { level: 'medium', why: 'same work stream: a new session has to re-read its files' }
  return { level: 'low', why: "a separate piece of work that isn't addressed to this conversation" }
}

// ---- what a handoff can't carry -------------------------------------------------------------------------

export type Flag = { key: string; level: Level | 'info'; text: string }

export const openPlanFlag = (open: number): Flag => ({ key: 'open-plan', level: 'medium',
  text: `${open} task-list item${open > 1 ? 's are' : ' is'} still open; the list travels, the details of a half-done step don't` })

const SHORT_REPLY = /^\s*(yes|ok|okay|sure|continue|go on|go ahead|proceed|do it|next)\b/i
const NAMES_FILE = /[\w./\\-]+\.(md|txt|json|ya?ml|py|kt|kts|ts|tsx|js|sql|gradle|csv|html|docx|xlsx|pptx|pdf)\b/i

/** Flags from the transcript (Facts) and the prompt being sent; `uncommitted` comes from git status. */
export function qualityFlags(f: Facts, promptText: string, uncommitted?: number): Flag[] {
  const flags: Flag[] = []
  const reply = f.lastText.trim()
  if (f.backgroundRunning > 0) {
    flags.push({ key: 'background-running', level: 'high', text: 'a workflow started here is still running, and its result will come back to this conversation, not the new one' })
  }
  if (f.chatOnlyMessages >= 2) {
    flags.push({ key: 'chat-only', level: 'high', text: `you've sent ${f.chatOnlyMessages} messages since anything was written to a file, and the handoff keeps only the conversation's start and its last reply` })
  }
  if (f.lastCheck && !f.lastCheck.ok && f.lastCheck.recent) {
    flags.push({ key: 'failed-check', level: 'high', text: `the last build or test (${f.lastCheck.cmd}) failed; the new conversation is told it failed, not the error or what was tried` })
  }
  if (f.asksYou && reply.length > 1500) {
    flags.push({ key: 'long-question', level: 'high', text: `its last reply asks you something and runs ${reply.length} characters, and the handoff keeps the first 1,500` })
  } else if (f.asksYou || (!NAMES_FILE.test(promptText) && (promptText.trim().length < 100 || SHORT_REPLY.test(promptText)))) {
    flags.push({ key: 'awaiting-answer', level: 'medium', text: 'your message answers its last reply; the handoff carries that reply, not what was said before it' })
  }
  const open = f.plan.filter(i => i.status !== 'completed').length
  if (open) flags.push(openPlanFlag(open))
  if (f.editsSincePacket > 12) flags.push({ key: 'many-files', level: 'medium', text: `it changed ${f.editsSincePacket} files in this task, and the handoff names the last 12` })
  if (f.backgroundFinished > 0) flags.push({ key: 'background-finished', level: 'medium', text: "a workflow finished just before your break, and its findings are only in this conversation's last turn" })
  if (f.backgroundShell > 0) flags.push({ key: 'background-shell', level: 'medium', text: "a command started in the background here may still be running; the new conversation can't read its output" })
  if (uncommitted) flags.push({ key: 'uncommitted', level: 'info', text: `${uncommitted} files have uncommitted changes; the new conversation sees them with git status` })
  return flags
}

export const levelOf = (flags: Flag[]): Level =>
  flags.some(f => f.level === 'high') ? 'high' : flags.some(f => f.level === 'medium') ? 'medium' : 'low'

// ---- the card after a break -----------------------------------------------------------------------------

export type Card = {
  show: boolean
  options: [string, string]            // first is the default choice
  question: string
  note: string                         // a one-line notice when no card is shown
  numbers: { resendHere: number; newConv: number; saveNow: number; typical: [number, number]; floor: number }
  risk: Level
  flags: string[]
}

/**
 * Back after a break: continue here (re-cache everything) or start a new conversation from a handoff?
 * packet: the prompt names a packet (its risk replaces the mid-task flags); precomputed: Claude Code already
 * prepared its own summary of this conversation during the break.
 */
export function afterBreak(ctx: number, idleMs: number, flags: Flag[], P: Params,
  opts: { packet?: Risk; precomputed?: boolean; model?: string } = {}): Card {
  const { pcr, pw } = price(opts.model)
  const N = P.baseTokens + P.rereadTokens + P.handoffTokens
  const NW = P.baseTokens + P.rereadWorst + P.handoffTokens
  const resendHere = writeCost(ctx, P, opts.model)
  const newConv = writeCost(N, P, opts.model)
  const saveNow = resendHere - newConv
  const typical: [number, number] = [(ctx - N) * (pw + P.callsBetweenReturns[0] * pcr), (ctx - N) * (pw + P.callsBetweenReturns[1] * pcr)]
  const floor = (ctx - NW) * pw
  const numbers = { resendHere, newConv, saveNow, typical, floor }
  const risk: Level = opts.packet ? opts.packet.level : levelOf(flags)
  const keys = opts.packet ? ['packet:' + opts.packet.level] : flags.map(f => f.key)
  const k = Math.round(ctx / 1000)
  const hours = Math.round(idleMs / 360_000) / 10
  const none: Card = { show: false, options: ['Keep going here anyway', 'New conversation'], question: '', note: '', numbers, risk, flags: keys }
  if (idleMs <= P.ttlMs || ctx < NW) return none
  if (opts.precomputed) {
    return { ...none, note: `Claude Code prepared a summary of this conversation during your break, so sending here is cheap. No switch needed.` }
  }
  const bar = risk === 'low' ? P.barLow : P.barMedium
  const pays = typical[0] >= bar && floor >= P.floorMin
  const week = share(resendHere, P)
  const head = `You were away ${hours} h, so this conversation's cache expired. Sending here re-caches ${k}K tokens: about ${usd(resendHere)}` +
    `${week ? ` (${week} of your weekly limit)` : ''}. A new conversation starts at about ${Math.round(N / 1000)}K with a handoff built from this one: about ${usd(newConv)}. ` +
    `You save about ${usd(saveNow)} now and typically ${usd(typical[0])}–${usd(typical[1])} by your next break. ` +
    `Worst case: still about ${usd(floor)}, even if the new conversation has to re-read ${Math.round(P.rereadWorst / 1000)}K tokens of files.`
  const steps = Math.max(1, Math.round((P.compactAt - ctx) / 1800))     // past the compaction point: any step now
  const near = ctx >= 750_000 ? ` Claude Code would compact this conversation by itself within about ${steps} step${steps === 1 ? '' : 's'}, so most of the saving is the re-cache you skip now.` : ''
  if (opts.packet) {
    if (opts.packet.level === 'high') {
      return { ...none, note: `Re-caching ${k}K tokens (${usd(resendHere)}). No switch suggested: ${opts.packet.why}.` }
    }
    if (!pays) return { ...none, note: `Re-caching ${k}K tokens (${usd(resendHere)}).` }
    const q = `${head}${near} Quality risk: ${risk}. This packet ${opts.packet.why}.` +
      (risk === 'medium' ? ' Check its first reply against the spec.' : '') + ' Start this packet in a new conversation?'
    return { ...none, show: true, options: ['New conversation', 'Keep going here anyway'], question: q }
  }
  if (!pays) return { ...none, note: `Re-caching ${k}K tokens (${usd(resendHere)}).` }
  const shown = flags.filter(f => f.level !== 'info')
  const info = flags.filter(f => f.level === 'info').map(f => ` (${f.text}.)`).join('')
  if (risk === 'low') {
    const q = `${head}${near} Quality risk: low. Its last reply doesn't ask you anything, the task list is done, no build or test failed, ` +
      `nothing is running in the background, and everything you asked since its last file fits in the handoff.${info} ` +
      `You'll see the handoff before it's sent. Start a new conversation?`
    return { ...none, show: true, options: ['New conversation', 'Keep going here anyway'], question: q }
  }
  if (risk === 'medium') {
    const q = `${head}${near} Quality risk: medium. The handoff carries the conversation's start (your first message, or after a compaction its summary), its last reply (first 1,500 characters), ` +
      `the task list, the last 12 changed files and the last test result. It may miss: ${shown.map(f => f.text).join('; ')}.${info} ` +
      `If you switch, check its first reply against these. Start a new conversation?`
    return { ...none, show: true, options: ['New conversation', 'Keep going here anyway'], question: q }
  }
  const q = `${head}${near} But the quality risk is high: ${shown.filter(f => f.level === 'high').map(f => f.text).join('; ')}.${info} ` +
    `Continuing here is the safer choice. Keep going here?`
  return { ...none, show: true, options: ['Keep going here anyway', 'New conversation'], question: q }
}

/** The band's two lines above the prompt, the same for a real offer and the demo. prefix: 'TEST · ' for /switch-test band. */
export function bandLines(card: Card, ctx: number, idleMs: number, flags: Flag[], P: Params, prefix = ''): { line1: string; line2: string } {
  const n = card.numbers
  const hours = Math.round(idleMs / 360_000) / 10
  const issues = flags.filter(f => f.level !== 'info').map(f => f.text)
  const week = share(n.resendHere, P)
  const line1 = `${prefix}Back after ${hours} h: sending here re-caches ${Math.round(ctx / 1000)}K tokens (${usd(n.resendHere)}${week ? `, ${week} of your weekly limit` : ''}).`
  const line2 = card.risk === 'high'
    ? `A new conversation would save about ${usd(n.saveNow)}, but the quality risk is high: ${flags.find(f => f.level === 'high')?.text ?? issues[0] ?? ''}.`
    : `A new conversation from a handoff saves about ${usd(n.saveNow)} now, typically ${usd(n.typical[0])}–${usd(n.typical[1])} ` +
      `by your next break (worst case ${usd(n.floor)}). Quality risk is ${card.risk}${issues.length ? `: ${issues.join('; ')}` : ''}.`
  return { line1, line2 }
}

// ---- status line ----------------------------------------------------------------------------------------

export function statusLine(ctx: number, idleMs: number, P: Params, precomputed = false, model?: string): string | undefined {
  if (ctx < 2 * P.baseTokens) return undefined
  const k = `${Math.round(ctx / 1000)}K tokens`
  const recache = usd(writeCost(ctx, P, model))
  if (idleMs <= P.ttlMs) {
    const min = Math.max(1, Math.round((P.ttlMs - idleMs) / 60_000))
    return `${k} · cache warm ${min} more min · after that the next message re-caches it (${recache})`
  }
  if (precomputed) return `${k} · cache expired · Claude Code has a summary ready, so the next message is cheap`
  return `${k} · cache expired · the next message re-caches it (${recache})`
}
