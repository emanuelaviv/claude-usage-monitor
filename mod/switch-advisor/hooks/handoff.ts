// Builds the first prompt of a new conversation from the old one's messages, and the facts the quality flags read.
// Pure: no `$` calls, no model. Mirrors monitor/handoff.py; the plugin feeds it `$.session.messages()`.
import type { SessionMessage } from 'claude-code'

export type PlanItem = { content: string; status: string }
export type Facts = {
  firstPrompt: string
  summary: string           // the earlier part, as the latest compaction summary describes it (its preamble left out)
  lastText: string
  lastRecap: string         // the last end-of-turn recap (wrap_up), if any
  lastAsk: string           // what it last asked you to decide (wrap_up ask), if any
  plan: PlanItem[]          // the task list after the last turn (wrap_up completes it)
  edits: string[]           // files changed, oldest first, unique
  created: string[]         // files written whole (reports among them)
  lastCheck?: { cmd: string; ok: boolean; recent: boolean }   // recent: within the last two turns
  stems: string[]           // PROMPT_*.md stems you named, in order
  prompts: number
  asksYou: boolean          // its last reply asks you something
  chatOnlyMessages: number  // >= 2 when you sent several messages since anything was written to a file
  editsSincePacket: number  // files changed since the prompt that opened the current packet
  backgroundRunning: number // workflows / background agents started in the last turn with no report yet
  backgroundFinished: number
  backgroundShell: number   // background shell commands started in the last turn (they never report)
  workflowOutputs: string[] // output files of workflows that reported in the last turn
}

const STEM = /PROMPT_([A-Za-z0-9_.\-]+?)\.md/gi
const EDIT_TOOLS = new Set(['Edit', 'MultiEdit', 'Write', 'NotebookEdit'])
const SHELL_TOOLS = new Set(['Bash', 'PowerShell'])
const DOCS = /\.(md|txt|html|docx|xlsx|pptx|pdf)$/i
const NAMES_FILE = /[\w./\\-]+\.(md|txt|json|ya?ml|py|kt|kts|ts|tsx|js|sql|gradle|csv|html|docx|xlsx|pptx|pdf)\b/i
const CHECK = /\b(pytest|unittest|gradlew|jest|vitest|flutter test|deno test|cargo test|go test|npm (run )?(test|build|lint)|pnpm (test|build)|yarn (test|build)|tsc\b|assemble\w*|lint\w*|python -m monitor\.\w+)/i
const TAGGED = /^\s*(<task-notification|<scheduled-task|<command-|<local-command|<system-reminder|\[Request interrupted)/
/** The user row a compaction leaves: Claude Code's summary of everything before it, not something you typed. */
const SUMMARY = /^\s*This session is being continued from a previous conversation\b/i

const isNotification = (m: SessionMessage) => m.role === 'user' && /^\s*<task-notification/.test(m.text)
const isSummary = (m: SessionMessage) => m.role === 'user' && SUMMARY.test(m.text)
const isHuman = (m: SessionMessage) =>
  m.role === 'user' && m.text.trim() !== '' && !(m.toolResults && m.toolResults.length) && !TAGGED.test(m.text) && !SUMMARY.test(m.text)

/** The text of each message you typed, oldest first: no tool result, notification, command row or summary. */
export const humanTexts = (messages: readonly SessionMessage[]): string[] => messages.filter(isHuman).map(m => m.text)

/** A numbered section heading of a compaction summary, at the start of a line: "8. Current Work:", or in bold on a
 *  line of its own, "8. **Current Work:**", "**8. Current Work:**" or "8. **Current Work** (what was under way):". A
 *  numbered item indented under a section is not one, nor a numbered line that only opens in bold ("1. **Update the
 *  docs** in README.md"). */
const SECTION = /^(?:\*\*)?\d+\.[ \t]+(?:(?:\*\*)?(?<bold>[A-Z][^\n*]{2,60}?):?\*\*:?(?:[ \t]*\([^\n)]*\))?:?[ \t]*$|(?<plain>[A-Z][^\n:*]{2,60}):[ \t]*)/gm
/** The sections a handoff quotes, each trimmed to its length: what was asked, what is left, where it stopped. */
const WANTED: [RegExp, number][] = [[/primary request/i, 500], [/pending tasks/i, 350], [/current work/i, 650], [/next step/i, 350]]

/** A compaction summary less the engine's own text: its preamble, the reminder and tags it adds, and the closing
 *  paragraphs that point to the old transcript and tell Claude to carry on. */
export function summaryBody(text: string): string {
  let t = text.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, '')
    .replace(/<tools-used-before-summary>[\s\S]*?<\/tools-used-before-summary>/g, '')
  const at = t.search(/^\s*Summary:/m)
  t = at >= 0 ? t.slice(at).replace(/^\s*Summary:\s*/, '')
    : t.replace(SUMMARY, '').replace(/^[^.]*\.\s*(The summary below covers[^.]*\.)?/i, '').replace(/^\s*The conversation is summarized below:\s*/i, '')
  t = t.replace(/\n+\s*If you need specific details from before compaction[\s\S]*$/i, '')
    .replace(/\n+\s*Continue the conversation from where it left off[\s\S]*$/i, '')
    .replace(/\n+\s*The messages after this summary[\s\S]*$/i, '')
  return t.replace(/<\/?summary>/g, '').trim()
}

function sections(body: string): { title: string; content: string }[] {
  const heads = [...body.matchAll(SECTION)]
  return heads.map((h, i) => {
    const content = body.slice((h.index ?? 0) + h[0].length, heads[i + 1]?.index ?? body.length).trim()
    // "**8. Current Work:** text", read as a plain heading, leaves its closing bold at the start of its text
    const inline = h.groups?.plain !== undefined && h[0].startsWith('**')
    return { title: h.groups?.bold ?? h.groups?.plain ?? '', content: inline ? content.replace(/^\*\*[ \t]*/, '') : content }
  })
}

/** What a handoff quotes of a summary: its request, pending tasks, current work and next step, found by their
 *  numbered headings, each trimmed; its first 700 characters when it has none of those headings. */
export function summaryDigest(body: string): string {
  const all = sections(body)
  const parts: string[] = []
  for (const [want, limit] of WANTED) {
    const s = all.find(x => want.test(x.title))
    if (s?.content) parts.push(`${s.title}: ${trim(s.content, limit)}`)
  }
  return parts.length ? parts.join('\n\n') : trim(body, 700)
}

/** The packets your own messages named, as the summary lists them; the whole summary when it has no such list. */
function summaryStems(body: string): string[] {
  const mine = sections(body).find(x => /all user messages/i.test(x.title))
  return [...(mine ? mine.content : body).matchAll(STEM)].map(x => x[1] ?? '').filter(Boolean)
}

function applyPlan(plan: PlanItem[], input: Record<string, unknown>): PlanItem[] {
  const items = input.items
  if (Array.isArray(items)) {
    return items.map(i => ({ content: String((i as PlanItem).content ?? ''), status: String((i as PlanItem).status ?? 'pending') }))
  }
  const next = plan.map(i => ({ ...i }))
  if (input.advance) {
    const k = next.findIndex(i => i.status === 'in_progress')
    if (k >= 0) {
      next[k].status = 'completed'
      const n = next.findIndex((i, j) => j > k && i.status === 'pending')
      if (n >= 0) next[n].status = 'in_progress'
    }
  }
  for (const d of (Array.isArray(input.done) ? input.done : []) as number[]) {
    if (d > 0 && d <= next.length) next[d - 1].status = 'completed'
  }
  const s = Number(input.start)
  if (s > 0 && s <= next.length) next[s - 1].status = 'in_progress'
  return next
}

export function factsFrom(messages: readonly SessionMessage[]): Facts {
  const f: Facts = {
    firstPrompt: '', summary: '', lastText: '', lastRecap: '', lastAsk: '', plan: [], edits: [], created: [], stems: [], prompts: 0,
    asksYou: false, chatOnlyMessages: 0, editsSincePacket: 0, backgroundRunning: 0, backgroundFinished: 0,
    backgroundShell: 0, workflowOutputs: [],
  }
  let turn = 0
  let chat = { count: 0, anyWithoutFile: false }
  let packetEdits = new Set<string>()
  let launched = 0
  let lastToolWasAsk = false
  let lastTurnRecapAsk = false
  for (const m of messages) {
    if (isSummary(m)) {
      // Not a prompt: the earlier part of the conversation, compacted. The packets you named there were worked on here.
      const body = summaryBody(m.text)
      f.summary = summaryDigest(body)
      for (const s of summaryStems(body)) if (!f.stems.includes(s)) f.stems.push(s)
      continue
    }
    if (isHuman(m)) {
      turn += 1
      f.prompts += 1
      if (!f.firstPrompt) f.firstPrompt = m.text
      const named = [...m.text.matchAll(STEM)].map(x => x[1])
      f.stems.push(...named)
      if (named.length) {
        chat = { count: 0, anyWithoutFile: false }
        packetEdits = new Set()
      } else {
        chat.count += 1
        if (!NAMES_FILE.test(m.text)) chat.anyWithoutFile = true
      }
      launched = 0
      f.backgroundFinished = 0
      f.backgroundShell = 0
      f.workflowOutputs = []
      lastTurnRecapAsk = false
      continue
    }
    if (isNotification(m)) {
      f.backgroundFinished += 1
      launched = Math.max(0, launched - 1)
      const out = m.text.match(/<output-file>([^<]+)<\/output-file>/)
      if (out) f.workflowOutputs.push(out[1].trim())
      continue
    }
    if (m.role !== 'assistant') continue
    if (m.text.trim()) f.lastText = m.text
    for (const u of m.toolUses) {
      const input = (u.input ?? {}) as Record<string, unknown>
      lastToolWasAsk = u.tool === 'AskUserQuestion'
      if (EDIT_TOOLS.has(u.tool) && typeof input.file_path === 'string') {
        const fp = input.file_path
        f.edits = [...f.edits.filter(x => x !== fp), fp]
        packetEdits.add(fp)
        if (u.tool === 'Write' && !f.created.includes(fp)) f.created.push(fp)
        if (DOCS.test(fp)) chat = { count: 0, anyWithoutFile: false }
      } else if (SHELL_TOOLS.has(u.tool) && typeof input.command === 'string') {
        if (input.run_in_background === true) f.backgroundShell += 1
        else if (CHECK.test(input.command) && u.text !== undefined) {
          f.lastCheck = { cmd: input.command.trim().split('\n')[0].slice(0, 120), ok: !u.isError, recent: true }
          ;(f.lastCheck as any).turn = turn
        }
      } else if (u.tool === 'Workflow' || ((u.tool === 'Agent' || u.tool === 'Task') && input.run_in_background === true)) {
        launched += 1
      } else if (u.tool.endsWith('update_plan')) {
        f.plan = applyPlan(f.plan, input)
      } else if (u.tool.endsWith('wrap_up')) {
        f.plan = f.plan.map(i => ({ ...i, status: 'completed' }))     // wrapping up closes the task list
        f.lastRecap = String(input.recap ?? '')
        const ask = String(input.ask ?? '').trim()
        f.lastAsk = ask
        lastTurnRecapAsk = ask !== '' || input.blocked === true
      } else if (u.tool === 'TodoWrite' && Array.isArray(input.todos)) {
        f.plan = (input.todos as PlanItem[]).map(t => ({ content: String(t.content ?? ''), status: String(t.status ?? 'pending') }))
      }
    }
  }
  if (f.lastCheck) f.lastCheck.recent = turn - Number((f.lastCheck as any).turn ?? 0) <= 1
  const lastLine = f.lastText.trim().split('\n').filter(l => l.trim()).at(-1) ?? ''
  f.asksYou = /\?\s*(\*\*)?\s*$/.test(lastLine) || lastTurnRecapAsk || lastToolWasAsk
  f.chatOnlyMessages = chat.count >= 2 && chat.anyWithoutFile ? chat.count : 0
  f.editsSincePacket = packetEdits.size
  f.backgroundRunning = launched
  return f
}

export function trim(text: string, limit = 1500): string {
  const t = text.trim().replace(/\n{3,}/g, '\n\n')
  if (t.length <= limit) return t
  const cut = t.slice(0, limit)
  const nl = cut.lastIndexOf('\n')
  return (nl > limit * 0.6 ? cut.slice(0, nl) : cut).trimEnd() + '\n[…trimmed]'
}

const quote = (t: string) => t.split('\n').map(l => (l.trim() ? '> ' + l : '>')).join('\n')
const rel = (p: string, cwd: string) => {
  const a = p.replace(/\\/g, '/'), b = cwd.replace(/\\/g, '/').replace(/\/$/, '')
  return a.toLowerCase().startsWith(b.toLowerCase() + '/') ? a.slice(b.length + 1) : p
}

export type Where = { cwd: string; branch?: string; title?: string; contextK: number; packetPath?: string; readFirst?: string[] }

function head(w: Where): string[] {
  const from = w.title ? `“${w.title}”` : 'the previous conversation'
  const lines = [`New conversation, continuing from ${from} (${w.contextK}K tokens), moved here so that context isn't resent on every message.`, '']
  lines.push(`Project: \`${w.cwd}\`` + (w.branch && w.branch !== 'HEAD' ? ` · branch \`${w.branch}\`` : ''))
  if (w.packetPath) lines.push(`Packet: \`${w.packetPath}\`. Read it first.`)
  if (w.readFirst && w.readFirst.length) lines.push('Its header also says to read: ' + w.readFirst.map(x => `\`${x}\``).join(', ') + '.')
  return lines
}

/** A new packet: your own prompt carries the task; a short preamble says where things are. */
export function packetPrompt(f: Facts, w: Where, userText: string): string {
  const lines = head(w)
  const reports = f.created.filter(x => /\.(md|txt)$/i.test(x)).slice(-5)
  if (reports.length) lines.push('Reports the previous conversation wrote, in case they matter: ' + reports.map(x => `\`${rel(x, w.cwd)}\``).join(', ') + '.')
  lines.push('', userText.trim())
  return lines.join('\n')
}

/** A return after a break: where the old conversation stopped, then your new prompt. */
export function resumePrompt(f: Facts, w: Where, userText: string): string {
  const lines = head(w)
  if (f.summary) lines.push('', 'The earlier part, as the summary Claude Code made when it compacted the conversation describes it (trimmed):', '', quote(f.summary))
  else if (f.firstPrompt) lines.push('', 'The original request (its first message, trimmed):', '', quote(trim(f.firstPrompt, 700)))
  lines.push('', 'Where the previous conversation stopped (its last message, trimmed):', '', quote(trim(f.lastText)))
  if (f.lastRecap && !f.lastText.includes(f.lastRecap.slice(0, 40))) lines.push('', `Its last recap: ${f.lastRecap.trim()}`)
  if (f.lastAsk) lines.push(`It had asked me: ${f.lastAsk}`)
  if (f.plan.length) {
    const mark: Record<string, string> = { completed: 'done', in_progress: 'in progress', pending: 'to do' }
    lines.push('', 'Its task list at that point:', ...f.plan.map(i => `- [${mark[i.status] ?? i.status}] ${i.content}`))
  }
  if (f.edits.length) {
    const shown = f.edits.slice(-12).map(x => `\`${rel(x, w.cwd)}\``).join(', ')
    lines.push('', `Files it changed (most recent last): ${shown}${f.edits.length > 12 ? ` (+${f.edits.length - 12} more)` : ''}.`)
  }
  if (f.workflowOutputs.length) lines.push('Results of background work that reported just before the break: ' + f.workflowOutputs.map(x => `\`${x}\``).join(', ') + '.')
  if (f.lastCheck) lines.push(`Last build or test: \`${f.lastCheck.cmd}\`: ${f.lastCheck.ok ? 'passed' : 'FAILED'}.`)
  lines.push('', "Before you continue: re-read the packet and the changed files above, don't assume anything that isn't written here, and ask me if something is unclear.",
    '', 'My next request:', '', userText.trim())
  return lines.join('\n')
}

/** Files a packet header lists under a "read first" heading (up to 12). */
export function readFirst(header: string): string[] {
  const out: string[] = []
  let on = false
  for (const ln of header.split('\n').slice(0, 80)) {
    if (/read (these )?first|before you start, read|read before/i.test(ln)) { on = true; continue }
    if (!on) continue
    if (!ln.trim() || ln.trimStart().startsWith('#')) { if (out.length) break; continue }
    for (const m of ln.match(/[\w./\\-]*[\w-]\.(?:md|txt|json|ya?ml|py|kt|kts|ts|tsx|js|sql|gradle|csv)\b/g) ?? []) {
      if (!out.includes(m) && out.length < 12) out.push(m)
    }
  }
  return out
}

/** Ends a handoff made with nothing typed, for you to write over. */
export const PLACEHOLDER = '(write your next request here)'
/** Heads the requests an earlier break held back, which a later handoff carries before your new one. */
export const EARLIER = "Also held back from this conversation after an earlier break, and not sent anywhere since (leave it out if it's done):"

/** A handoff with the requests an earlier break held back, which no new conversation took, put before your new one
 *  `req`. A packet's handoff has no "My next request:" line and ends with your prompt: the block then goes before
 *  that prompt, and the line is added after the block, so your prompt never reads as part of it. */
export function withEarlier(text: string, earlier: string[], req: string): string {
  const block = `${EARLIER}\n\n${earlier.join('\n\n')}`
  const at = text.lastIndexOf('\nMy next request:')
  if (at >= 0) return `${text.slice(0, at)}\n${block}\n${text.slice(at)}`
  if (req && text.endsWith('\n' + req)) return `${text.slice(0, -req.length)}${block}\n\nMy next request:\n\n${req}`
  return `${text}\n\n${block}`
}

/** Text compared with what was pasted: no zero-width characters, and any run of spaces or line ends as one space. */
export const flat = (s: string) => s.replace(/[\u200B-\u200D\u2060\uFEFF]/g, '').replace(/\s+/g, ' ').trim()

/** The part of a handoff you don't write: all of it before the requests at its end, which you may have edited (the
 *  band's placeholder is there to be written over, and an earlier break's block to be left out); for a packet's,
 *  which ends with your prompt, all of it before that prompt. */
export function fixedPart(text: string, requests: string[] = []): string {
  const cuts = [text.indexOf('\n' + EARLIER), text.indexOf('\nMy next request:')].filter(i => i >= 0)
  if (cuts.length) return flat(text.slice(0, Math.min(...cuts)))
  const tail = requests.join('\n\n')
  return flat(tail && text.endsWith(tail) ? text.slice(0, -tail.length) : text)
}
