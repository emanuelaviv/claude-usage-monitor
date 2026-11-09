// switch-advisor: when you come back to a large conversation after its cache expired, says whether starting a new
// conversation from a handoff is cheaper, flags what the handoff might not carry, and lets you decide.
// It never calls a model, never sends a prompt on its own, and never adds text to a prompt.
//
// - Status line: context size, how long the cache stays warm, and what the next message costs to resend.
// - Band above the prompt: as soon as the cache has expired and a switch would pay even in the worst case, a band
//   shows the offer, with no typing needed. Its buttons are New conversation, Keep going here, and Details (the full
//   card). It is never drawn on a phone.
// - At prompt submit (a prompt you sent in the app on this computer, the terminal or the desktop window, which the
//   engine stamps `composer`; never a slash command, a prompt with a pasted image or file, one over Remote Control
//   from a phone or the web, the desktop app's own continue after a sleep, a quit, a limit or a restart, one typed
//   over a running turn, or a plugin's, a notification's or a schedule's; see typedHere), after a break, the same card
//   asks before sending unless you already chose "Keep going here" for this break. A card that is closed, can't be
//   shown or gets no answer within 2 minutes sends your prompt as usual, and the plugin asks again after your next
//   break. It skips the card when Claude Code prepared its own summary during the break (then continuing is cheap).
// - "New conversation" leaves this conversation as it is and makes the first prompt of a new one: a handoff built
//   from this one, ending with your draft or prompt, kept for this conversation's project folder. In the terminal the
//   next new conversation there within 30 minutes finds it in its box, for you to check and send; in any window, a
//   /clear here puts it in this window's box within 24 hours, unless Claude replied here since (then a toast says
//   why, and restore puts it there). A new Code-tab session's box is the app's own until
//   its first message, so there you paste it: it goes on the clipboard where the surface takes it, and /switch-test
//   restore shows it for 24 hours, to paste anywhere, a new Cowork task included. Each conversation's handoff is kept
//   apart, so two never replace each other's; a second switch in the same break adds its request to the first, and
//   one in a later break keeps a message the card held back before. When the handoff can be neither kept, copied nor
//   saved, a prompt is sent here as usual, and the band's is shown in the conversation.
// - Run from a checkout of its repository (one that has its data/ folder), it writes every offer and answer to the
//   checkout's data/switch_decisions/ and keeps the handoffs you accept in data/handoffs/. The nightly guardrail
//   (python -m monitor.guardrail) reads the decisions and can turn the after-break card off through
//   data/guardrail.json. Installed any other way (from the marketplace, say), it writes no files of its own; in your
//   project it runs git status and, when a prompt names a PROMPT_*.md file, finds it by folder names and reads it.
// - Cowork loads only the plugins in your Claude account's list, so it needs a copy installed there. New Cowork tasks
//   run in the cloud, where nothing a plugin draws appears; a local task's view may not draw it either. Each Cowork
//   task keeps its own plugin store, so a new task can't pick the handoff up: there /switch-test restore shows it, to
//   paste into the new task. In the Code tab the account's copy loads next to the one CLAUDE_CODE_PLUGIN_DIRS names:
//   only one copy acts in a session (the folder's), so you never see two bands or two cards.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Offer } from '../types'
import { PLACEHOLDER, factsFrom, fixedPart, flat, humanTexts, packetPrompt, readFirst, resumePrompt, withEarlier,
  type Where } from './handoff'
import { DEFAULTS, EXAMPLE_RATE, afterBreak, bandLines, classifyPacket, openPlanFlag, paramsFrom, qualityFlags, rateFrom,
  statusLine, type Params } from './rule'

const STEM = /PROMPT_([A-Za-z0-9_.\-]+?)\.md/i
const HANDOFF = 'handoff:'                   // the store keys of the handoffs kept for a new conversation: handoff:<folder>|<id>
const PICKUP_MS = 30 * 60_000                // a new conversation in the same folder within this long finds it in its box
const KEEP_MS = 24 * 3_600_000               // /switch-test restore shows it this long; then it is deleted
const ASK_MS = 2 * 60_000                    // the card waits this long for an answer; then your prompt is sent
const STALE_MS = 30 * 24 * 3_600_000         // a conversation's own notes (last:, pre:, dismissed:) this old are deleted
const CLOSED = ''                            // ask(): the card was closed, or couldn't be shown (nobody to ask)
const LATE = '\u0000late'                    // ask(): no answer within ASK_MS (nothing typed can equal it)
const COWORK_CWD = /[\\/]local-agent-mode-sessions[\\/]/i   // a local Cowork task's working folder
const SKIP_DIRS = new Set(['.git', 'node_modules', 'build', '.gradle', 'dist', 'venv', '.venv', '__pycache__', 'target', 'out'])
const offer = atom({ plugin: 'switch-advisor', key: 'offer' } as const, null as Offer | null)

let root: string | undefined                 // the project folder, when the plugin runs from a checkout of its repository
let P: Params = DEFAULTS
let enabled = true                           // the guardrail can turn the after-break card off
let expiryTimer: { cancel: () => void } | undefined
let offeredFor = 0                           // the break (by its last reply) an offer was computed for
let yielded = false                          // another copy of this plugin acts in this session: stay out of its way

/** The repository's folder when `pluginRoot` is a checkout of it: <repo>/mod/switch-advisor, next to monitor/, and
 *  with the data/ folder that running the monitor creates. There the plugin reads your measured inputs, your
 *  weekly-limit rate and the guardrail's switch, and keeps its log. A marketplace keeps a full copy of the repository
 *  under ~/.claude/plugins/, with no data/ folder: that is never a checkout, so the plugin writes nothing there. */
async function checkoutOf($: EngineInterface, pluginRoot: string): Promise<string | undefined> {
  try {
    const self = pluginRoot.replace(/\\/g, '/')
    const repo = self.replace(/\/mod\/switch-advisor\/?$/i, '')
    if (repo === self || /\/\.claude\/plugins\//i.test(repo + '/')) return undefined
    return (await $.fs.exists(`${repo}/monitor/guardrail.py`)) && (await $.fs.exists(`${repo}/data`)) ? repo : undefined
  } catch {
    return undefined
  }
}

const findRoot = ($: EngineInterface) => checkoutOf($, $.plugin.root)

function samePath(a: string, b: string): boolean {
  const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
  return norm(a) === norm(b)
}

/** The folders CLAUDE_CODE_PLUGIN_DIRS names: ';'-separated on Windows, ':' elsewhere, '~' for your home. */
async function pluginDirs($: EngineInterface): Promise<string[]> {
  const raw = (await $.env.get('CLAUDE_CODE_PLUGIN_DIRS')) ?? ''
  const home = (await $.env.get('HOME')) ?? (await $.env.get('USERPROFILE')) ?? ''
  const parts = raw.split(';').flatMap(p => (/^[A-Za-z]:[\\/]/.test(p.trim()) ? [p] : p.split(':')))
  return parts.map(p => p.trim()).filter(Boolean).map(p => (p.startsWith('~') && home ? home + p.slice(1) : p))
}

/** True when this copy came from somewhere else (your Claude account's plugins, a marketplace) and
 *  CLAUDE_CODE_PLUGIN_DIRS names a folder of this plugin: in the Code tab that copy loads too, and it is the one that
 *  acts (it can keep a log in its checkout). Cowork never has that setting, so there this copy acts. A copy that the
 *  setting names itself always acts. */
async function folderCopyLoaded($: EngineInterface): Promise<boolean> {
  try {
    const dirs = await pluginDirs($)
    if (dirs.some(d => samePath(d, $.plugin.root))) return false
    for (const dir of dirs) {
      try {
        if (JSON.parse(await $.fs.read(`${dir.replace(/\\/g, '/').replace(/\/+$/, '')}/.claude-plugin/plugin.json`))?.name === 'switch-advisor') return true
      } catch {
        // not a plugin folder, or not readable here
      }
    }
  } catch {
    // no environment to read: act
  }
  return false
}

/** The session's model, for its prices; Opus 5.5's when the app doesn't say. */
async function modelOf($: EngineInterface): Promise<string | undefined> {
  try {
    return await $.session.model()
  } catch {
    return undefined
  }
}

/** JSON from a file you may have edited by hand: a byte order mark first (Windows PowerShell's Set-Content -Encoding
 *  UTF8 writes one) is skipped, as the monitor skips it. */
function parseJson(text: string): any {
  return JSON.parse(text.charCodeAt(0) === 0xfeff ? text.slice(1) : text)
}

async function loadSettings($: EngineInterface): Promise<void> {
  root = await findRoot($)
  P = DEFAULTS
  enabled = true
  if (!root) return
  try {
    P = paramsFrom(parseJson(await $.fs.read(`${root}/data/advisor_params.json`)))
  } catch {
    P = DEFAULTS
  }
  try {
    P = { ...P, usdPerPt: rateFrom(parseJson(await $.fs.read(`${root}/data/plan.json`))) }
  } catch {
    // no rate set: dollars only
  }
  try {
    const g = parseJson(await $.fs.read(`${root}/data/guardrail.json`))
    enabled = g?.after_break_enabled !== false
  } catch {
    enabled = true
  }
}

/** Keeps a copy of a handoff in the checkout's data/handoffs/, when there is a checkout. */
async function saveHandoff($: EngineInterface, name: string, text: string): Promise<boolean> {
  if (!root) return false
  try {
    await $.fs.write(`${root}/data/handoffs/${name}.md`, text)
    return true
  } catch {
    return false
  }
}

async function num($: EngineInterface, key: string): Promise<number> {
  return Number((await $.store.get(key)) ?? 0)
}

async function refreshStatus($: EngineInterface): Promise<void> {
  try {
    const id = await $.session.id()
    const usage = await $.session.usage()
    const ctx = usage.context.tokens ?? 0
    const last = await num($, 'last:' + id)
    const pre = (await num($, 'pre:' + id)) > last
    const now = await $.clock.now()
    $.ui.status(last ? statusLine(ctx, now - last, P, pre, await modelOf($)) : undefined)
    expiryTimer?.cancel()
    if (last && now - last < P.ttlMs) {
      expiryTimer = $.clock.after(P.ttlMs - (now - last) + 5000, () => { void onExpiry($) })
    }
  } catch {
    // the status line is a nicety: never let it break anything
  }
}

async function onExpiry($: EngineInterface): Promise<void> {
  await refreshStatus($)
  await computeOffer($, false)
}

async function findPacket($: EngineInterface, stem: string, cwd: string): Promise<string | undefined> {
  const key = `packet:${cwd.toLowerCase()}:${stem.toLowerCase()}`
  const cached = await $.store.get(key)
  if (typeof cached === 'string' && (await $.fs.exists(cached))) return cached
  const want = `prompt_${stem}.md`.toLowerCase()
  const queue: [string, number][] = [[cwd.replace(/\\/g, '/'), 0]]
  let visited = 0
  while (queue.length && visited < 4000) {
    const [dir, depth] = queue.shift()!
    visited += 1
    let entries: { name: string; kind: string }[] = []
    try {
      entries = (await $.fs.list(dir)) as unknown as { name: string; kind: string }[]
    } catch {
      continue
    }
    for (const ent of entries) {
      const name = String(ent.name)
      if (ent.kind === 'file' && name.toLowerCase() === want) {
        const path = `${dir}/${name}`
        await $.store.set(key, path)
        return path
      }
      if (ent.kind === 'dir' && depth < 7 && !SKIP_DIRS.has(name) && !name.startsWith('.')) queue.push([`${dir}/${name}`, depth + 1])
    }
  }
  return undefined
}

async function uncommitted($: EngineInterface, cwd: string): Promise<number> {
  try {
    const r = await $.process.run(['git', '--no-optional-locks', 'status', '--porcelain'], { cwd, timeoutMs: 5000 })
    return r.exitCode === 0 ? r.stdout.split('\n').filter(l => l.trim()).length : 0
  } catch {
    return 0
  }
}

async function record($: EngineInterface, decision: Record<string, unknown>): Promise<void> {
  if (!root) return
  try {
    const now = await $.clock.now()
    const id = String(decision.session ?? '').slice(0, 8)
    await $.fs.write(`${root}/data/switch_decisions/${now}-${id}.json`, JSON.stringify({ ts: now, ...decision }, null, 1))
  } catch {
    // logging must never block a prompt
  }
}

/** A handoff kept for a new conversation: its text, the conversation's project folder, when it was made, and the
 *  conversation it came from, which never takes it back into its own box. `requests`: the requests its text ends with,
 *  in order; `held`: those of them the card kept from being sent, which exist nowhere else. `taken`: the conversation
 *  whose box it went into; /switch-test restore still shows it until it is 24 hours old. Each lives under its own store
 *  key, handoff:<folder>|<conversation>, so two conversations never write over each other's. */
type Kept = { text: string; cwd: string; at: number; from: string; test?: boolean; taken?: string; takenAt?: number
  requests?: string[]; held?: string[] }
type Entry = { key: string; h: Kept }

/** A folder as handoffs are filed and compared: forward slashes, no slash at the end, and no case, as Windows and macOS
 *  ignore it. */
const folderKey = (dir: string) => dir.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
const prefixOf = (dir: string) => `${HANDOFF}${folderKey(dir)}|`
const keyOf = (dir: string, from: string) => prefixOf(dir) + from
/** Kept for folder `dir`. By the folder it records, not the key: a folder named with a "|" would share a key's start. */
const inFolder = (e: Entry, dir: string) => folderKey(e.h.cwd) === folderKey(dir)
const isKept = (h: unknown): h is Kept => !!h && typeof h === 'object' && typeof (h as Kept).text === 'string'
  && typeof (h as Kept).at === 'number' && typeof (h as Kept).from === 'string' && typeof (h as Kept).cwd === 'string'
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)
const ago = (ms: number) => {
  const min = Math.max(1, Math.round(ms / 60_000))
  return min < 90 ? `${min} min` : `${Math.round(min / 60)} h`
}

/** The folder a handoff is kept for: the conversation's project folder, where it started (or where /cd or a worktree
 *  move took it), and where a new one starts too. The shell's own folder moves with every cd that Claude runs. */
async function handoffFolder($: EngineInterface): Promise<string> {
  try {
    return await $.session.root()
  } catch {
    return await $.session.cwd()
  }
}

/** Every handoff kept, newest first. One 24 hours old or more is deleted on the way. */
async function keptList($: EngineInterface, now: number): Promise<Entry[]> {
  let keys: string[] = []
  try {
    keys = await $.store.keys()
  } catch {
    return []
  }
  const out: Entry[] = []
  for (const key of keys) {
    if (!key.startsWith(HANDOFF)) continue
    try {
      const h = await $.store.get(key)
      if (isKept(h) && now - h.at < KEEP_MS) out.push({ key, h })
      else await $.store.delete(key)
    } catch {
      // unreadable now: left for the next look
    }
  }
  return out.sort((a, b) => b.h.at - a.h.at)
}

/** At each start: deletes the handoffs 24 hours old, and the notes of conversations idle for 30 days (when Claude last
 *  replied, a summary prepared, the band dismissed, a resume) and quiet periods that are over, up to 50 a start, so the
 *  store never fills up. This conversation's own are left alone, and so is the break of one resumed in the last 30
 *  days, however long it had been idle before (resumed:<id>, written at the resume). */
async function tidy($: EngineInterface): Promise<void> {
  try {
    const now = await $.clock.now()
    const id = await $.session.id()
    await keptList($, now)
    let looked = 0
    for (const key of await $.store.keys()) {
      if (!/^(last|pre|dismissed|quiet|resumed):/.test(key) || key.endsWith(':' + id)) continue
      if (++looked > 50) break
      const v = Number(await $.store.get(key))
      const old = key.startsWith('quiet:') ? !(v > now) : !(v > now - STALE_MS)
      if (old && /^(last|dismissed):/.test(key) && (await num($, 'resumed:' + key.slice(key.indexOf(':') + 1))) > now - STALE_MS) continue
      if (old) await $.store.delete(key)
    }
  } catch {
    // tidying is a nicety
  }
}

/** The one a new conversation takes: a real handoff before a test's, the newest first. */
const choose = (entries: Entry[]): Entry | undefined => entries.find(e => !e.h.test) ?? entries[0]

/** Marks a handoff taken by conversation `by`, unless a newer one has been kept under its key meanwhile. */
async function markTaken($: EngineInterface, e: Entry, by: string, now: number): Promise<void> {
  try {
    const cur = await $.store.get(e.key)
    if (isKept(cur) && cur.at === e.h.at) await $.store.set(e.key, { ...cur, taken: by, takenAt: now })
  } catch {
    // left untaken: at worst it is offered once more
  }
}

/** Takes conversation `by`'s mark off a handoff its box no longer holds, unless a newer one is kept under its key. */
async function untake($: EngineInterface, e: Entry, by: string): Promise<void> {
  try {
    const cur = await $.store.get(e.key)
    if (isKept(cur) && cur.at === e.h.at && cur.taken === by) {
      const { taken: _taken, takenAt: _takenAt, ...rest } = cur
      await $.store.set(e.key, rest)
    }
  } catch {
    // left marked: restore all still lists it
  }
}

/** A real handoff this conversation made in this break (after its last reply), not yet taken. */
async function ownWaiting($: EngineInterface, id: string): Promise<Kept | undefined> {
  try {
    const h = await $.store.get(keyOf(await handoffFolder($), id))
    return isKept(h) && !h.test && !h.taken && h.at > (await num($, 'last:' + id)) ? h : undefined
  } catch {
    return undefined
  }
}

/** Cowork, or a session nothing draws for (a cloud session). Each Cowork task keeps its own plugin store, so a new task
 *  can't pick a handoff up, and a toast may not show: you fetch the handoff with /switch-test restore instead. */
async function apart($: EngineInterface): Promise<boolean> {
  try {
    if (COWORK_CWD.test(await $.session.cwd()) || COWORK_CWD.test(await handoffFolder($))) return true
    return (await $.session.surfaces()).length === 0
  } catch {
    return true
  }
}

async function inTerminal($: EngineInterface): Promise<boolean> {
  try {
    return (await $.session.surfaces()).includes('terminal')
  } catch {
    return false
  }
}

function list(items: string[]): string {
  return items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`
}

/** New conversation: this conversation stays as it is, and `text` (the handoff, ending with your request) is kept for
 *  a new one in this conversation's project folder. In the terminal the next new conversation there within 30 minutes
 *  finds it in its box (offerKept), and so does this window after a /clear, however long after. It also goes on the
 *  clipboard where the surface takes it, and /switch-test restore shows it. A second switch in the same break adds
 *  `request` to this conversation's handoff instead of replacing it, once; a switch in a later break replaces it, but
 *  keeps the requests the card held back then, if no new conversation took them and you didn't send them here since.
 *  Returns what to tell you: `note`, the full account; `reason`, the line a prompt held back shows; `stranded` when it
 *  could be neither kept, copied nor saved, so the caller keeps your prompt from being lost. `saved`: a copy is in the
 *  checkout's data/handoffs/; `carries`: what your request is; `held`: the card keeps `request` from being sent. */
async function keepForNext($: EngineInterface, text: string,
  opts: { saved?: boolean; test?: boolean; carries?: string; request?: string; held?: boolean } = {}): Promise<{ note: string; reason: string; stranded: boolean }> {
  const dir = await handoffFolder($)
  const now = await $.clock.now()
  const id = await $.session.id()
  const key = keyOf(dir, id)
  let prior: Kept | undefined
  try {
    const v = await $.store.get(key)
    if (isKept(v) && now - v.at < KEEP_MS) prior = v
  } catch {
    // read as none
  }
  if (opts.test && prior && !prior.test) {                   // a test never replaces a real one, taken or not
    const note = 'A real handoff from this conversation is kept, so the test keeps none. /switch-test restore shows it.'
    return { note, reason: note, stranded: false }
  }
  const req = opts.test ? '' : (opts.request ?? '').trim()
  let requests = req ? [req] : []
  let held = req && opts.held ? [req] : []
  let carried = 0
  let merged = false
  if (!opts.test && prior && !prior.test && !prior.taken) {
    const had = prior.requests ?? []
    if (prior.at > (await num($, 'last:' + id))) {           // a second switch in the same break: adds to the first
      merged = true
      text = prior.text
      requests = had
      if (req && !had.includes(req)) {
        text = text.endsWith(PLACEHOLDER) ? text.slice(0, -PLACEHOLDER.length) + req : `${text}\n\n${req}`
        requests = [...had, req]
      }
      held = [...new Set([...(prior.held ?? []), ...held])]
    } else if (prior.held?.length) {                         // an earlier break's, which no new conversation took
      // A held message you sent here since was dropped from `held` when it went out (forgetHeld); the transcript is
      // checked too, in case that write failed.
      const sent = new Set(humanTexts(await $.session.messages()).map(t => t.trim()))
      const earlier = prior.held.filter(r => r !== req && !sent.has(r))
      if (earlier.length) {
        text = withEarlier(text, earlier, req)
        held = [...held, ...earlier]
        carried = earlier.length
      }
    }
  }
  let kept = false
  try {
    await $.store.set(key, { text, cwd: dir, at: now, from: id, ...(requests.length ? { requests } : {}),
      ...(held.length ? { held } : {}), ...(opts.test ? { test: true } : {}) })
    kept = true
  } catch {
    // the store is full or can't be written
  }
  let copied = false
  try {
    copied = (await $.ui.copy({ text })).isCopied
  } catch {
    // no clipboard here
  }
  const handoff = opts.carries ? `the handoff, with ${opts.carries},` : 'the handoff'
  const handoffEnd = opts.carries ? `the handoff, with ${opts.carries}` : 'the handoff'   // before a colon or a full stop
  const saved = opts.saved ? `a copy is in ${root}/data/handoffs/` : ''
  if (!kept && !copied && !saved) {
    return { note: `${cap(handoff)} could be neither kept nor copied.`, reason: '', stranded: true }
  }
  const also = (carried ? ` It also carries ${carried === 1 ? 'the message' : `the ${carried} messages`} you held back here after an earlier break, which no new conversation took.` : '')
    + (!kept && merged ? " The version kept before, without this message, is what a new conversation's box or /switch-test restore gets, so paste this one over it." : '')
  const reason = (where: string) => `switch-advisor: not sent here; it's in the handoff for a new conversation (${where}).`
  if (await apart($)) {
    const how = kept ? `send /switch-test restore here to see ${handoff} and paste it into a new task`
      : copied ? `${handoff} is on your clipboard: paste it into a new task`
      : `${handoff} couldn't be kept here, but ${saved}`
    const note = `This conversation stays as it is: ${how}.${kept && copied ? " It's also on your clipboard." : ''}${also}`
    return { note, reason: `switch-advisor: not sent here. ${note}`, stranded: false }
  }
  if (await inTerminal($)) {
    const ways = [copied ? "it's on your clipboard" : '', kept ? '/switch-test restore shows it' : '', saved].filter(Boolean)
    const note = kept
      ? `This conversation stays as it is. Start a new one in ${dir} within 30 minutes, or type /clear here, and ${handoff} is in its box. Also, ${list(ways)}.${also}`
      : `This conversation stays as it is. ${cap(handoff)} couldn't be kept for a new conversation, but ${list(ways)}: paste it into one.${also}`
    return { note, reason: reason(kept ? '/switch-test restore shows it' : copied ? "it's on your clipboard" : saved), stranded: false }
  }
  // The desktop app or VS Code: a new session's box is the app's own until you send its first message, so the plugin
  // can't fill it. Paste the handoff there, or /clear here, which keeps this window and fills its box.
  const how = copied ? `start a new session in ${dir} and paste ${handoffEnd}: it's on your clipboard`
    : kept ? `start a new session in ${dir} and send /switch-test restore there first: it puts ${handoff} in the box`
    : `start a new session and paste ${handoffEnd}: ${saved}`
  const orClear = kept ? ` Or type /clear here, and it's in this window's box.${copied ? ' /switch-test restore shows it for 24 hours.' : ''}` : ''
  const note = `This conversation stays as it is: ${how}.${orClear}${also}`
  return { note, reason: reason(copied ? "it's on your clipboard" : kept ? '/switch-test restore shows it' : saved), stranded: false }
}

/** Shows a handoff that couldn't be kept or copied in the conversation (a notice, not sent to the model), your own
 *  request first, as the terminal draws only a notice's first 2,000 characters. */
function showHandoff($: EngineInterface, text: string): void {
  const mine = text.lastIndexOf('My next request:')
  if (mine > 0) $.ui.log(text.slice(mine))
  $.ui.log(text)
}

/** A handoff pasted as a new conversation's first message, its request edited or not: marked taken, so no other box
 *  gets it. */
async function takePasted($: EngineInterface, text: string): Promise<void> {
  try {
    const now = await $.clock.now()
    const id = await $.session.id()
    const dir = await handoffFolder($)
    const sent = flat(text)
    const e = (await keptList($, now)).find(x => inFolder(x, dir) && !x.h.taken && x.h.from !== id
      && sent.startsWith(fixedPart(x.h.text, x.h.requests)))
    if (e) await markTaken($, e, id, now)
  } catch {
    // a nicety
  }
}

/** A prompt went out here: a copy of it the card held back in this conversation's handoff is no longer unsent, so a
 *  later switch doesn't carry it. */
async function forgetHeld($: EngineInterface, text: string): Promise<void> {
  try {
    const t = text.trim()
    const key = keyOf(await handoffFolder($), await $.session.id())
    const h = await $.store.get(key)
    if (isKept(h) && h.held?.includes(t)) await $.store.set(key, { ...h, held: h.held.filter(x => x !== t) })
  } catch {
    // at worst the next switch's check of the transcript catches it
  }
}

/** Nothing sent in this conversation yet: no turn, or rows that are all a slash command's own (the /clear that began
 *  it, say), which turns() counts too. A reply, a compaction summary or a skill's run means something was sent. */
async function nothingSent($: EngineInterface): Promise<boolean> {
  const turns = await $.session.turns()
  return turns === 0 || (turns <= 3 && (await $.session.messages())
    .every(m => m.role === 'user' && !m.toolResults?.length && /^\s*<(command-|local-command)/.test(m.text)))
}

let submitted = false                        // a prompt went out in this conversation: never fill a box under it
let clearedFrom: string | undefined          // the conversation this window's last /clear ended, until a new session

/** A new conversation (nothing sent yet) in a folder a handoff waits for: the handoff goes in its box, once, for you to
 *  check and send. Tried in the terminal when the session starts (a few times, as the box may not be mounted yet),
 *  taking the folder's newest within 30 minutes; and after a /clear in any window (`cleared`: the conversation it
 *  ended, '' when unknown), taking the handoff that conversation made in its last break however old it is. When that
 *  one is out of date (Claude replied there after it: to you, or to a background task's report) or another box took
 *  it, a toast says so instead; only a conversation that made none takes the folder's newest within 30 minutes. Never
 *  in Cowork, never over a draft you started, and never once something was sent. A new Code-tab session's box is the
 *  app's own until its first message, so there you paste the handoff, or send /switch-test restore first. */
async function offerKept($: EngineInterface, tries = 3, cleared?: string): Promise<void> {
  try {
    // After a /clear turns() counts the /clear's own row, so there `submitted` alone says whether you sent anything.
    if (yielded || submitted || (await apart($)) || (cleared === undefined && !(await nothingSent($)))) return
    const now = await $.clock.now()
    const dir = await handoffFolder($)
    const id = await $.session.id()
    const all = (await keptList($, now)).filter(e => inFolder(e, dir))
    const here = all.filter(e => !e.h.taken && e.h.from !== id)
    const made = cleared ? all.find(e => e.h.from === cleared) : undefined          // one per conversation and folder
    const own = made && !made.h.taken && made.h.at > (await num($, 'last:' + cleared)) ? made : undefined
    const pick = own ?? (made ? undefined : choose(here.filter(e => now - e.h.at <= PICKUP_MS)))
    if ((await $.prompt.read()).text.trim() || submitted) return
    if (!pick) {
      if (made) {
        $.ui.toast(made.h.taken ? "The handoff you made here before the /clear isn't in the box: another conversation's box already took it. /switch-test restore shows it."
          : "The handoff you made here before the /clear isn't in the box: Claude replied in that conversation after you made it (to you, or to a background task's report), and the handoff doesn't carry that reply. /switch-test restore puts it in the box anyway.",
        { timeoutMs: 20000 })
      }
      return
    }
    if (!(await $.prompt.fill({ text: pick.h.text })).isFilled) {            // the box may not be mounted yet
      if (tries > 1) $.clock.after(tries === 3 ? 300 : 1500, () => { void offerKept($, tries - 1, cleared) })
      return
    }
    await markTaken($, pick, id, now)
    const what = own ? `The handoff you made here before the /clear${pick.h.test ? ' (a /switch-test)' : ''}`
      : `The handoff${pick.h.test ? ' from a /switch-test' : ''} kept for this folder ${ago(now - pick.h.at)} ago`
    const more = here.filter(e => e !== pick && !e.h.test).length
    const others = more ? ` ${more === 1 ? 'Another is' : `${more} more are`} kept for this folder: /switch-test restore all lists ${more === 1 ? 'it' : 'them'}.` : ''
    $.ui.toast(`${what} is in the box: check it, then press Enter.${others}`, { timeoutMs: 15000 })
  } catch {
    // a nicety: never let it break anything
  }
}

/** Work out the after-break offer for the band (no prompt yet). force: pretend the cache expired (/switch-test band). */
async function computeOffer($: EngineInterface, force: boolean): Promise<void> {
  try {
    if (!force && (await read($, offer))?.demo) return                     // leave the demo band up
    const id = await $.session.id()
    const now = await $.clock.now()
    const last = await num($, 'last:' + id)
    const idleMs = force ? P.ttlMs + 60_000 : now - last
    const quiet = now < (await num($, 'quiet:' + id)) || (await num($, 'dismissed:' + id)) === last
    const cwd = await $.session.cwd()
    if (!force && (!last || idleMs <= P.ttlMs || quiet || !enabled)) {
      await update($, offer, () => null)
      return
    }
    offeredFor = last
    const ctx = (await $.session.usage()).context.tokens ?? 0
    const precomputed = !force && (await num($, 'pre:' + id)) > last
    const facts = factsFrom(await $.session.messages())
    const flags = qualityFlags(facts, 'x'.repeat(120), await uncommitted($, cwd))
    const card = afterBreak(ctx, idleMs, flags, P, { precomputed, model: await modelOf($) })
    if (!card.show) {
      await update($, offer, () => null)
      return
    }
    const { line1, line2 } = bandLines(card, ctx, idleMs, flags, P, force ? 'TEST · ' : '')
    await update($, offer, () => ({ last, risk: card.risk, line1, line2, question: card.question, options: card.options,
      numbers: card.numbers, test: force }))
  } catch {
    // the band is a nicety: never let it break anything
  }
}

/** /switch-test demo: the band a real 640K-token conversation would get after a 2.3 h break with 2 task-list items
 *  open, on Opus 5.5, from the default numbers and an example weekly-limit rate, with nothing from this conversation.
 *  It stays up while Claude works, so it can be screenshotted, until one of its buttons, /switch-test off, or 30 min. */
async function showDemo($: EngineInterface): Promise<void> {
  const ctx = 640_000
  const idleMs = 2.3 * 3_600_000
  const flags = [openPlanFlag(2)]
  const example: Params = { ...DEFAULTS, usdPerPt: EXAMPLE_RATE }
  const card = afterBreak(ctx, idleMs, flags, example)
  await update($, offer, () => ({ last: 0, risk: card.risk, demo: true, question: card.question, options: card.options,
    ...bandLines(card, ctx, idleMs, flags, example) }))
  $.clock.after(30 * 60_000, () => { void hideDemo($) })
}

async function hideDemo($: EngineInterface): Promise<void> {
  await update($, offer, o => (o?.demo ? null : o))
}

/** A turn ended or a prompt went out: a real offer is done with; the demo stays. */
async function clearOffer($: EngineInterface): Promise<void> {
  await update($, offer, o => (o?.demo ? o : null))
}

async function switchNow($: EngineInterface, via: string, promptText?: string): Promise<void> {
  const o = await read($, offer)
  if (o?.demo) {
    await update($, offer, () => null)
    return
  }
  const id = await $.session.id()
  const now = await $.clock.now()
  const cwd = await $.session.cwd()
  const ctx = (await $.session.usage()).context.tokens ?? 0
  const facts = factsFrom(await $.session.messages())
  const draft = promptText ?? (await $.prompt.read()).text
  const text = resumePrompt(facts, { cwd, contextK: Math.round(ctx / 1000) }, draft.trim() || PLACEHOLDER)
  if (o?.test || !(await ownWaiting($, id))) {                   // a second switch in one break is logged once
    await record($, { session: id, kind: o?.test ? 'test' : 'after-break', via, contextTokens: ctx, numbers: o?.numbers,
      risk: o?.risk, choice: 'switch', handoffFirstLine: text.split('\n')[0] })
  }
  const saved = await saveHandoff($, `${now}-${id.slice(0, 8)}`, text)
  await update($, offer, () => null)
  const { note, stranded } = await keepForNext($, text, { saved, test: o?.test, carries: draft.trim() ? 'your draft' : undefined, request: draft })
  if (stranded) {
    showHandoff($, text)
    $.ui.toast(`This conversation stays as it is. ${note} It is shown in the conversation, to copy into a new one.`, { timeoutMs: 20000 })
    return
  }
  $.ui.toast(note, { timeoutMs: 20000 })
}

async function keepGoing($: EngineInterface, via: string): Promise<void> {
  const o = await read($, offer)
  if (o?.demo) {
    await update($, offer, () => null)
    return
  }
  const id = await $.session.id()
  if (o && !o.test) {
    await $.store.set('dismissed:' + id, o.last)
    await record($, { session: id, kind: 'after-break', via, numbers: o.numbers, risk: o.risk, choice: 'stay' })
  }
  await update($, offer, () => null)
}

async function details($: EngineInterface): Promise<void> {
  const o = await read($, offer)
  if (!o) return
  if (o.demo) {
    await update($, offer, () => null)
    return
  }
  // No time limit here: nothing is held while you read it. But a turn may end meanwhile, and with it the break.
  const pick = await ask($, (o.test ? 'TEST (your cache is not really expired): ' : '') + o.question, o.options)
  const still = await read($, offer)
  if (pick !== CLOSED && (!still || still.last !== o.last || !!still.test !== !!o.test)) {
    $.ui.toast('That card is out of date: the break it was for is over, so nothing changed.', { timeoutMs: 8000 })
    return
  }
  if (pick === 'New conversation') await switchNow($, 'band-details')
  else if (pick === 'Keep going here anyway') await keepGoing($, 'band-details')
  else if (pick !== CLOSED) $.ui.toast('That answer is neither choice, so the band stays up.', { timeoutMs: 8000 })   // typed under "Other"
}

async function decide($: EngineInterface, promptText: string) {
  const id = await $.session.id()
  const now = await $.clock.now()
  const last = await num($, 'last:' + id)
  if (!last || now - last <= P.ttlMs) return undefined                   // cards only after a break
  if (now < (await num($, 'quiet:' + id)) || (await num($, 'dismissed:' + id)) === last) return undefined
  const cwd = await $.session.cwd()
  const usage = await $.session.usage()
  const ctx = usage.context.tokens ?? 0
  const idleMs = now - last
  const precomputed = (await num($, 'pre:' + id)) > last
  const facts = factsFrom(await $.session.messages())
  const where: Where = { cwd, contextK: Math.round(ctx / 1000) }
  const stem = STEM.exec(promptText)?.[1]
  if (stem) {
    const path = await findPacket($, stem, cwd)
    let header: string | undefined
    let ageS = 0
    if (path) {
      header = (await $.fs.read(path)).split('\n').slice(0, 40).join('\n')
      ageS = (now - (await $.fs.stat(path)).mtimeMs) / 1000
    }
    const packet = classifyPacket(stem, header, facts.stems, ageS)
    const card = afterBreak(ctx, idleMs, [], P, { packet, precomputed, model: await modelOf($) })
    where.packetPath = path
    where.readFirst = header ? readFirst(header) : []
    return { id, now, ctx, idleMs, stem, card, text: packetPrompt(facts, where, promptText) }
  }
  if (!enabled) return undefined
  const flags = qualityFlags(facts, promptText, await uncommitted($, cwd))
  const card = afterBreak(ctx, idleMs, flags, P, { precomputed, model: await modelOf($) })
  return { id, now, ctx, idleMs, stem, card, text: resumePrompt(facts, where, promptText) }
}

/** The desktop app's own messages to continue after a compaction, a sleep, a usage limit, a quit or a restart on a
 *  remote host: "... while you were working ... Please continue from where you left off." (Claude 4.23118). Some
 *  reach the engine with no origin, so it takes them for typed. */
const HOST_CONTINUE = /\bwhile you were working\b[\s\S]*\bcontinue from where you left off\.\s*$/i

/** Sent by you in the app on this computer, as far as the engine can tell. It gives a prompt with a human origin the
 *  origin `composer`: Enter in the terminal, and any message the desktop app or Cowork relays without declaring an
 *  origin, which the engine presumes human; `bridge` when it came over the engine's own Remote Control from a phone or
 *  the web. A message the app sends by itself and declares automatic (a CI event, a continue after a quit or a
 *  limit) arrives as `sdk`, as the engine drops that label; one it sends without a label is caught by its wording
 *  (HOST_CONTINUE). Those, phone and web messages, notifications, schedules, peers and plugins are never held, as
 *  nobody may be at the computer to answer. A message relayed from a phone through Cowork's Dispatch, or one in a
 *  cloud session, still looks typed. */
function typedHere(kind: string, text: string): boolean {
  return kind === 'composer' && !HOST_CONTINUE.test(text)
}

/** The card: the label you chose or the text you typed under "Other"; CLOSED when you closed it or it couldn't be
 *  shown (nobody to ask). With `limit` (only at prompt submit, where your prompt waits), LATE when it got no answer
 *  in time; the card can't be taken off the screen, so an answer given to it later goes to `limit.onLate`. CLOSED and
 *  LATE are no choice: the prompt is sent as usual, with no quiet period and nothing logged. */
async function ask($: EngineInterface, question: string, options: [string, string],
  limit?: { ms: number; onLate: (pick: string) => void }): Promise<string> {
  let timer: { cancel: () => void } | undefined
  try {
    const answer = $.ui.ask(question, { options, header: 'Switch?' })
    if (!limit) return await answer
    let expired = false
    answer.then(p => { if (expired) limit.onLate(p) }, () => undefined)
    const late = new Promise<string>(resolve => { timer = $.clock.after(limit.ms, () => { expired = true; resolve(LATE) }) })
    return await Promise.race([answer, late])
  } catch {
    return CLOSED
  } finally {
    timer?.cancel()
  }
}

export const register: Register = (on) => {
  // One copy per session: the Code tab loads both the folder CLAUDE_CODE_PLUGIN_DIRS names and the copy in your Claude
  // account's plugins (the one Cowork needs), whichever loads first; the account copy steps back for the folder.
  on('session.start', async ($, e, next) => {
    submitted = false
    clearedFrom = undefined
    yielded = await folderCopyLoaded($)
    if (yielded) {
      $.ui.status(undefined)
      return next(e)
    }
    await loadSettings($)
    await $.command.register({ name: 'switch-test', description: 'Try the switch card ("band": the band; "demo": example numbers for screenshots; "off": hide it; "restore": the handoff kept for a new conversation; "restore all": a list of those kept for this folder; "restore 2": the second of them)' })
    const r = await next(e)
    void refreshStatus($)
    void computeOffer($, false)
    void tidy($)                                      // deletes handoffs 24 h old, and notes of long-idle conversations
    if (e.surface === 'terminal') void offerKept($)   // a new Code-tab session's box is the app's until its first message
    return r
  })

  // A /clear ends this conversation and starts a new one in the same window, under a new id, with no session.start:
  // the new one's box gets the handoff the old one made (classic.SessionStart, below). Nothing is sent in the new one
  // yet, so `submitted` is reset here, before the SessionStart hooks beneath run: a first message you send while they
  // do then keeps the box from being filled under it.
  on('session.end', async ($, e, next) => {
    if (!yielded && e.reason === 'clear') {
      clearedFrom = e.sessionId
      submitted = false
    }
    return next(e)
  })

  // /switch-test: the card with this conversation's numbers, pretending the cache expired. Nothing is sent.
  // /switch-test band: the same offer in the band above the prompt.
  on('command.run', { command: 'switch-test' }, async ($, e, next) => {
    if (yielded) return next(e)
    if (e.origin.kind === 'bridge') {     // the handoff it makes is for a new conversation at the computer
      return { text: 'switch-test: run it at the computer: from your phone or the web, the card and the handoff it makes are for a new conversation there.' }
    }
    const arg = String(e.args ?? '').trim()
    const undrawn = "but this window doesn't draw what plugins show (Cowork, or a cloud session), so it doesn't appear here. The card at your prompt still works: /switch-test shows it."
    if (arg === 'demo') {
      await showDemo($)
      if (await apart($)) return { text: `switch-test: the demo band is set, ${undrawn}` }
      return { text: 'switch-test: the demo band is showing above the prompt (example numbers only). Any of its buttons, or /switch-test off, hides it.' }
    }
    if (arg === 'off') {
      await update($, offer, () => null)
      return { text: 'switch-test: band hidden.' }
    }
    // A command's text is part of the conversation: Claude reads it with the next message sent here. So restore shows
    // one handoff, and restore all only lists them, briefly; restore <number> then shows that one.
    const nth = /^restore ([1-9]\d?)$/.exec(arg)?.[1]
    if (arg === 'restore' || arg === 'restore all' || nth) {   // the handoffs kept for a new conversation in this folder
      const now = await $.clock.now()
      const dir = await handoffFolder($)
      const id = await $.session.id()
      const all = await keptList($, now)
      const here = all.filter(x => inFolder(x, dir))
      if (!here.length) {
        const other = all[0]
        return { text: `switch-test: no handoff is kept for ${dir}.${other ? ` The newest kept is for ${other.h.cwd}: send /switch-test restore in a conversation there.` : ''} Each is kept for 24 hours.` }
      }
      const whose = (x: Entry) => `kept ${ago(now - x.h.at)} ago${x.h.from === id ? ' from this conversation' : ''}${x.h.test ? ', from a test' : ''}` +
        (x.h.taken === id ? ", already put in this conversation's box" : x.h.taken ? ", already put in a new conversation's box" : '')
      if (arg === 'restore all') {       // a list; nothing goes in the box or is marked
        const ask = (x: Entry) => {
          const line = (x.h.requests?.at(-1) ?? PLACEHOLDER).split('\n')[0]?.trim() ?? ''
          return line.length > 60 ? `${line.slice(0, 59)}…` : line
        }
        const each = here.map((x, i) => `${i + 1}. Kept ${whose(x).slice('kept '.length)}, asking: "${ask(x)}"`)
        return { text: `switch-test: ${here.length === 1 ? 'one handoff is' : `${here.length} handoffs are`} kept for ${dir}, newest first:\n${each.join('\n')}\nSend /switch-test restore <number> to see one, and to put it in the box of a conversation with nothing sent yet.` }
      }
      // This conversation's own; else the one it took (a /clear, a pickup or an earlier restore put it here); else the
      // one the conversation this window cleared made; else the newest a box hasn't taken; else the newest.
      const pick = nth ? here[Number(nth) - 1]
        : here.find(x => x.h.from === id) ?? here.find(x => x.h.taken === id) ?? (clearedFrom ? here.find(x => x.h.from === clearedFrom) : undefined)
          ?? choose(here.filter(x => !x.h.taken)) ?? here[0]
      if (!pick) return { text: `switch-test: there is no handoff ${nth} for ${dir}: /switch-test restore all lists them.` }
      const more = here.length - 1
      const what = `switch-test: the handoff ${whose(pick)}.${more ? ` ${more} more ${more === 1 ? 'is' : 'are'} kept for this folder: /switch-test restore all lists ${more === 1 ? 'it' : 'them'}.` : ''}`
      if (pick.h.from === id || !(await nothingSent($))) {   // never into the box of the conversation it came from, or a busy one
        return { text: `${what} Copy it into a new conversation or a new Cowork task:\n\n${pick.h.text}` }
      }
      // A new conversation: into the box, once the command has finished, as a pickup does. One another box took stays
      // marked as that box's, so its own restore still gives it back.
      $.clock.after(50, () => {
        void $.prompt.fill({ text: pick.h.text }).then(async f => {
          if (!f.isFilled) return
          // Nothing was sent here, so a handoff this box took before is in no box now: free it for another.
          for (const x of here) if (x !== pick && x.h.taken === id) await untake($, x, id)
          if (!pick.h.taken || pick.h.taken === id) await markTaken($, pick, id, now)
        }).catch(() => undefined)
      })
      return { text: `${what} It goes in the box; if it doesn't appear there, copy it from here:\n\n${pick.h.text}` }
    }
    if (arg === 'band') {
      await computeOffer($, true)
      if (!(await read($, offer))) return { text: 'switch-test: no band here: this conversation is too small for a switch to pay.' }
      return { text: (await apart($)) ? `switch-test: the band is set, ${undrawn}` : 'switch-test: the band is showing above the prompt.' }
    }
    const id = await $.session.id()
    const ctx = (await $.session.usage()).context.tokens ?? 0
    const cwd = await $.session.cwd()
    const facts = factsFrom(await $.session.messages())
    const flags = qualityFlags(facts, 'test '.repeat(25), await uncommitted($, cwd))
    const card = afterBreak(ctx, P.ttlMs + 60_000, flags, P, { model: await modelOf($) })
    if (!card.show) {
      return { text: `switch-test: no card here. ${card.note || `This conversation is ${Math.round(ctx / 1000)}K tokens, below the ${Math.round((P.baseTokens + P.rereadWorst + P.handoffTokens) / 1000)}K needed.`}` }
    }
    const pick = await ask($, 'TEST (your cache is not really expired): ' + card.question, card.options)   // no time limit: nothing is held
    if (pick === CLOSED) {
      return { text: 'switch-test: the card was closed, or could not be shown here. Nothing changed; a real card would have sent your message as usual.' }
    }
    if (!card.options.includes(pick)) return { text: 'switch-test: that answer is neither choice. Nothing changed.' }
    const switched = pick === 'New conversation'
    await record($, { session: id, kind: 'test', contextTokens: ctx, numbers: card.numbers, risk: card.risk, flags: card.flags,
      choice: switched ? 'switch' : 'stay' })
    if (!switched) return { text: `switch-test: you chose "${pick}". Nothing changed.` }
    const now = await $.clock.now()
    const text = resumePrompt(facts, { cwd, contextK: Math.round(ctx / 1000) }, PLACEHOLDER)
    const saved = await saveHandoff($, `${now}-${id.slice(0, 8)}-test`, text)
    const { note, stranded } = await keepForNext($, text, { saved, test: true })
    return { text: stranded ? `switch-test: ${note} Here it is, to copy:\n\n${text}` : `switch-test: ${note}` }
  })

  on('classic.SessionStart', async ($, e, next) => {
    if (yielded) return next(e)
    const r = await next(e)
    const input = e as unknown as { source?: string; session_id?: string; seconds_since_last_response?: number }
    if (input.source === 'resume' && typeof input.seconds_since_last_response === 'number' && input.session_id) {
      const now = await $.clock.now()
      await $.store.set('last:' + input.session_id, now - input.seconds_since_last_response * 1000)
      await $.store.set('resumed:' + input.session_id, now)     // so no other session's tidy() takes that break for idle
      void refreshStatus($)
      void computeOffer($, false)
    }
    // /clear starts a new conversation in this window; session.end, just before, named the one it ended.
    if (input.source === 'clear') void offerKept($, 3, clearedFrom ?? '')
    return r
  }).catch(($, e, next) => next(e))

  // Claude Code prepares a summary ahead of time during a long break; then continuing is cheap, so no offer.
  on('session.compact', { trigger: 'precompute' }, async ($, e, next) => {
    if (yielded) return next(e)
    const r = await next(e)
    if (!e.agentId && r.skip === undefined) {
      await $.store.set('pre:' + (await $.session.id()), await $.clock.now())
      void refreshStatus($)
      await update($, offer, () => null)
    }
    return r
  }).catch(($, e, next) => next(e))

  on('turn.complete', async ($, e, next) => {
    if (yielded) return next(e)
    const r = await next(e)
    if (!e.agentId) {
      await $.store.set('last:' + (await $.session.id()), await $.clock.now())
      await clearOffer($)
      void refreshStatus($)
    }
    return r
  })

  on('session.measure', async ($, e, next) => {
    if (yielded) return next(e)
    const r = await next(e)
    void refreshStatus($)
    return r
  })

  // You start typing after a break: make sure the band has the offer (once per break).
  on('prompt.edit', async ($, e, next) => {
    if (yielded) return next(e)
    const r = await next(e)
    const id = await $.session.id()
    const last = await num($, 'last:' + id)
    if (last && offeredFor !== last && (await $.clock.now()) - last > P.ttlMs) void computeOffer($, false)
    return r
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (yielded) return next(e)
    const o = await read($, offer)
    if (!o || e.props.hasSurvey || (e.props.isWorking && !o.demo) || e.surface === 'mobile') return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const high = o.risk === 'high'
    return (
      <Box flexDirection="column">
        <Text>{o.line1}</Text>
        <Text dimColor>{o.line2}</Text>
        <Box>
          {high && <Button key="keep" label="Keep going here anyway" onPress={() => keepGoing($, 'band')} />}
          <Button key="switch" label="New conversation" onPress={() => switchNow($, 'band')} />
          {!high && <Button key="keep" label="Keep going here anyway" onPress={() => keepGoing($, 'band')} />}
          <Button key="details" label="Details" onPress={() => details($)} />
        </Box>
      </Box>
    )
  })

  on('prompt.submit', async ($, e, next) => {
    // Only prompts you sent at this computer, and not over a running turn, which re-writes the cache anyway. A slash
    // command (a skill, or a built-in that asks the model) goes through too: dropped for a new conversation, it would
    // reach the box as plain text and no longer run as a command. So does a prompt with a pasted image or file, which
    // the new conversation's box can't take, and one the engine marks as not seen typed.
    submitted = true                         // from now on no pickup fills this conversation's box
    // Every way the prompt goes out here: a copy of it the card held back is then no longer unsent.
    const send = async () => {
      const r = await next(e)
      if (!yielded && r.drop === undefined) void forgetHeld($, e.text)   // only once it went in: a hook beneath may refuse it
      return r
    }
    if (!yielded && /^\s*New conversation, continuing from /.test(e.text)) void takePasted($, e.text)
    if (yielded || e.turnId !== undefined || e.command !== undefined || (e.attachments?.length ?? 0) > 0 || e.isMeta
      || !typedHere(e.origin.kind, e.text)) {
      return send()
    }
    const plan = await decide($, e.text)
    await clearOffer($)
    if (!plan) return send()
    const { card } = plan
    if (!card.show) {
      if (card.note) $.ui.toast(card.note, { timeoutMs: 8000 })
      return send()
    }
    const pick = await ask($, card.question, card.options, { ms: ASK_MS, onLate: late => {
      if (!late) return                      // closed after the time limit
      const msg = `That card had run out: your message went out here after 2 minutes, so ${late === 'New conversation'
        ? 'nothing was kept for a new conversation' : 'your answer changed nothing'}.`
      $.ui.toast(msg, { timeoutMs: 20000 })
      $.ui.log(`switch-advisor: ${msg}`)
    } })
    if (pick === CLOSED) return send()       // closed, or nobody to ask: sent as usual, and the card asks after your next break
    if (pick === LATE) {
      const msg = 'The card got no answer within 2 minutes, so your message was sent here as usual.'
      $.ui.toast(msg, { timeoutMs: 20000 })
      $.ui.log(`switch-advisor: ${msg}`)
      return send()
    }
    if (!card.options.includes(pick)) {      // text typed under the dialog's "Other": neither choice, so no stay either
      $.ui.toast('That answer is neither choice, so your message was sent here as usual. The card asks again next time.', { timeoutMs: 10000 })
      return send()
    }
    const switched = pick === 'New conversation'
    if (!switched || !(await ownWaiting($, plan.id))) {          // a second switch in one break is logged once
      await record($, { session: plan.id, kind: plan.stem ? 'packet' : 'after-break', via: 'submit', stem: plan.stem,
        contextTokens: plan.ctx, idleMs: plan.idleMs, numbers: card.numbers, risk: card.risk, flags: card.flags,
        choice: switched ? 'switch' : 'stay', handoffFirstLine: plan.text.split('\n')[0] })
    }
    if (!switched) {
      await $.store.set('quiet:' + plan.id, plan.now + P.quietMs)
      return send()
    }
    const saved = await saveHandoff($, `${plan.now}-${plan.id.slice(0, 8)}`, plan.text)
    const { note, reason, stranded } = await keepForNext($, plan.text, { saved, carries: 'your message', request: e.text, held: true })
    if (stranded) {                          // never drop a prompt whose handoff went nowhere
      const msg = 'The handoff could be neither kept nor copied, so your message was sent here as usual.'
      $.ui.toast(msg, { timeoutMs: 20000 })
      $.ui.log(`switch-advisor: ${msg}`)
      return send()
    }
    $.ui.toast(note, { timeoutMs: 20000 })
    return { drop: reason }
  }).catch(($, e, next) => next(e))                             // fail open: a problem here never blocks a prompt
}
