// Switching: the card at prompt submit, what New conversation keeps and tells you, how a new conversation picks the
// handoff up, /switch-test restore, expiry, and the card's time limit. Each hook here stands for the engine.
import { expect, mock, test } from 'claude-code/testing'

import { EARLIER } from './handoff'

const HOUR = 3_600_000
const MIN = 60_000
const T0 = 1_800_000_000_000
const CWD = 'C:/work/project'
const KEY = (from: string, dir = 'c:/work/project') => `handoff:${dir}|${from}`
const COWORK = 'C:/Users/me/AppData/Local/Claude-Data/claude_x/local-agent-mode-sessions/o/u/local_t/host-cwd'
const PROMPT = 'Please carry on with the remaining steps of the plan in notes.md and run the tests once they are all in place.'
const PROMPT_B = 'Also update the changelog in CHANGES.md with what the remaining steps changed, once they are all done.'
const msg = (role: 'user' | 'assistant', text: string) => ({ role, text, toolUses: [] })
const CHAT = [msg('user', 'Build the thing'), msg('assistant', 'Built half of it.')]
/** The row a /clear leaves at the start of the new conversation: a user row, which turns() counts. */
const CLEAR_ROW = '<command-name>/clear</command-name>\n<command-message>clear</command-message>\n<command-args></command-args>'
const BAND = {
  plugin: 'switch-advisor', component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 12, bodyColumns: 100, scroll: { offset: 0, bodyRows: 11 }, view: {} },
} as const

/** What the engine beneath answers; a test may change any of it as it goes. */
type Opts = {
  id: string; cwd: string; root: string; turns: number; surfaces: string[]; box: string; copy: boolean; fill: boolean
  storeFull: boolean; answer: string; checkout: boolean; env: Record<string, string>; messages: object[]
  refuse: boolean       // the hooks beneath refuse the prompt (a settings hook's block)
}

/** The engine beneath the plugin, recording what the plugin asked of it. */
function world(on: any, over: Partial<Opts> = {}, entries: Record<string, unknown> = {}) {
  const o: Opts = { id: 'old-session', cwd: CWD, root: over.cwd ?? CWD, turns: 5, surfaces: ['desktop'], box: '', copy: true,
    fill: true, storeFull: false, answer: 'New conversation', checkout: false, env: {}, messages: CHAT, refuse: false, ...over }
  const w = { commands: [] as string[], toasts: [] as string[], logs: [] as string[], fills: [] as string[], copies: 0,
    asked: [] as string[], sent: [] as string[], writes: [] as string[], answerLater: (_: string) => {},
    gate: Promise.resolve() as Promise<void> }                  // the SessionStart hooks beneath wait for it
  const store: Record<string, unknown> = { ...entries }
  const clock = mock.clock(on, { now: T0 })
  mock.env(on, o.env)
  on('store.get', (_: unknown, e: { key: string }) => ({ value: store[e.key] }))
  on('store.set', (_: unknown, e: { key: string; value: unknown }) => {
    if (o.storeFull) return { deny: 'the store is full' }
    store[e.key] = JSON.parse(JSON.stringify(e.value))
    return { value: undefined }
  })
  on('store.delete', (_: unknown, e: { key: string }) => { delete store[e.key]; return { value: undefined } })
  on('store.keys', () => ({ value: Object.keys(store) }))
  on('session.id', () => ({ value: o.id }))
  on('session.cwd', () => ({ value: o.cwd }))
  on('session.root', () => ({ value: o.root }))
  on('session.turns', () => ({ value: o.turns }))
  on('session.surfaces', () => ({ value: o.surfaces }))
  on('session.model', () => ({ value: 'claude-opus-5-5' }))
  on('session.usage', () => ({ value: { startedAt: T0 - 10 * HOUR, context: { tokens: 640_000, window: 1_000_000 }, rateLimits: [] } }))
  on('session.messages', () => ({ value: o.messages }))
  on('fs.exists', () => ({ value: o.checkout }))
  on('fs.stat', () => ({ value: { kind: 'file', size: 2, mtimeMs: T0 - MIN, isLink: false } }))
  on('fs.read', (_: unknown, e: { path: string }) => ({ value: /plugin\.json$/.test(e.path) ? '{"name":"switch-advisor"}' : '{}' }))
  on('fs.write', (_: unknown, e: { path: string }) => { w.writes.push(e.path.replace(/\\/g, '/')); return { value: undefined } })
  on('process.run', () => ({ value: { exitCode: 128, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }))
  on('command.register', () => ({ value: { command: 'switch-test' } }))
  on('command.run', (_: unknown, e: { command: string }) => { w.commands.push(e.command); return { text: '' } })
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('session.end', (_: unknown, e: { sessionId: string }) => ({ sessionId: e.sessionId }))
  on('turn.complete', () => ({ text: '' }))
  on('classic.SessionStart', async () => { await w.gate; return {} })
  on('ui.toast', (_: unknown, e: { text: string }) => { w.toasts.push(e.text); return { value: undefined } })
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', (_: unknown, e: { text: string }) => { w.logs.push(e.text); return { value: undefined } })
  on('ui.copy', () => { w.copies += 1; return { value: o.copy ? { isCopied: true } : { isCopied: false, reason: 'no-surface' } } })
  on('prompt.read', () => ({ value: { text: o.box, cursor: 0 } }))
  on('prompt.fill', (_: unknown, e: { text: string }) => {
    if (!o.fill) return { isFilled: false, refusal: 'no_composer' }
    w.fills.push(e.text)
    return { isFilled: true }
  })
  on('prompt.submit', (_: unknown, e: { text: string }) => {
    if (o.refuse) return { drop: 'blocked by a hook' }
    w.sent.push(e.text)
    return { text: e.text }
  })
  on('tool.call', { tool: 'AskUserQuestion' }, async (_: unknown, e: any) => {
    const q = e.questions[0].question
    w.asked.push(q)
    let answer = o.answer
    if (answer === 'later') answer = await new Promise<string>(resolve => { w.answerLater = resolve })
    if (answer === 'deny') return { deny: 'dismissed' }
    return { result: { questions: e.questions, answers: { [q]: answer } } }
  })
  let state: { value: unknown; version: number } = { value: undefined, version: 0 }
  on('state.get', () => ({ value: state }))
  on('state.set', (_: unknown, e: { value: unknown }) => {
    state = { value: e.value, version: state.version + 1 }
    return { value: { isSet: true, version: state.version } }
  })
  return { o, w, store, clock, band: () => state.value as any }
}

const start = ($: any, o: Opts) => $.session.start({ cwd: o.cwd, surface: (o.surfaces[0] ?? null) as any, isInteractive: true })
const submit = ($: any, text = PROMPT, kind = 'composer') => $.prompt.submit({ text, wait: false, origin: { kind } })
const command = ($: any, args: string) =>
  $.command.run({ command: 'switch-test', args, origin: { kind: 'composer' }, presentation: { isFullscreen: false } })
const press = async ($: any, key: string) => {
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' } as any)
  await ui.press({ key })
  await ui.unmount()
}
/** A /clear in this window, as the engine runs one: it ends the conversation, then starts a new one under `next`, whose
 *  own /clear row turns() already counts. */
async function clear($: any, o: Opts, next: string) {
  await $.session.end({ reason: 'clear', sessionId: o.id, resume: { id: o.id } })
  o.id = next
  o.turns = 1
  o.messages = [msg('user', CLEAR_ROW)]
  await $.classic.SessionStart({ source: 'clear' })
}
/** Claude replies in this conversation: its break is over. */
const reply = ($: any) => $.turn.complete({ answer: 'Done.', durationMs: 1000, isAborted: false, turnId: 't1', reason: 'answer' })
const AFTER_BREAK = { 'last:old-session': T0 - 2 * HOUR }
const kept = (from: string, over: object = {}) =>
  ({ [KEY(from)]: { text: `HANDOFF FROM ${from}`, cwd: CWD, at: T0 - 10 * MIN, from, ...over } })
const decisions = (w: { writes: string[] }) => w.writes.filter(p => p.includes('/data/switch_decisions/'))

// ---- switching ---------------------------------------------------------------------------------------------------

test('back after a break, New conversation keeps the handoff, with your message, and never clears this conversation', async ($, on) => {
  const { o, w, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  const r: any = await submit($)
  await clock.advance(5_000)                                     // a /clear left on a timer would show up here
  expect(w.asked.length).toBe(1)
  expect(r.drop.includes('not sent here')).toBe(true)
  expect(w.sent).toEqual([])                                     // the prompt didn't go to this conversation
  expect(w.commands).toEqual([])                                 // no /clear
  const h = store[KEY('old-session')] as any
  expect(h.cwd).toBe(CWD)
  expect(h.text.startsWith('New conversation, continuing from')).toBe(true)
  expect(h.text.endsWith(PROMPT)).toBe(true)
  expect(w.copies).toBe(1)
})

test('in the desktop app the note says to paste the handoff into a new session, or /clear here', async ($, on) => {
  const { o, w } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($)
  const note = w.toasts.find(t => t.startsWith('This conversation stays as it is'))!
  expect(note.includes(`start a new session in ${CWD} and paste the handoff, with your message: it's on your clipboard`)).toBe(true)
  expect(note.includes("Or type /clear here, and it's in this window's box. /switch-test restore shows it for 24 hours.")).toBe(true)
  expect(note.includes('in its box')).toBe(false)               // a new Code-tab session's box can't be filled
})

test('in the terminal the note says a new conversation in the folder finds it in its box', async ($, on) => {
  const { o, w } = world(on, { surfaces: ['terminal'] }, AFTER_BREAK)
  await start($, o)
  await submit($)
  expect(w.toasts.some(t => t.includes(`Start a new one in ${CWD} within 30 minutes, or type /clear here, and the handoff, with your message, is in its box`))).toBe(true)
})

test('Keep going here anyway sends the message and stays quiet in this conversation for 12 hours', async ($, on) => {
  const { o, w, store } = world(on, { answer: 'Keep going here anyway' }, AFTER_BREAK)
  await start($, o)
  await submit($)
  expect(w.sent).toEqual([PROMPT])
  expect(store['quiet:old-session']).toBe(T0 + 12 * HOUR)
  expect(Object.keys(store).some(k => k.startsWith('handoff:'))).toBe(false)
})

test('a card that is closed, or can not be shown, sends the message as usual, with no quiet period and nothing logged', async ($, on) => {
  const { o, w, store } = world(on, { answer: 'deny', checkout: true }, AFTER_BREAK)
  await start($, o)
  await submit($)
  expect(w.asked.length).toBe(1)
  expect(w.sent).toEqual([PROMPT])
  expect(store['quiet:old-session']).toBe(undefined)
  expect(decisions(w)).toEqual([])
})

test('a card nobody answers within 2 minutes sends the message, says so in the transcript, and logs nothing', async ($, on) => {
  const { o, w, store, clock } = world(on, { answer: 'later', checkout: true }, AFTER_BREAK)
  await start($, o)
  const pending = submit($)
  await clock.advance(119_000)
  expect(w.sent).toEqual([])                                     // still waiting for an answer
  await clock.advance(2_000)
  await pending
  expect(w.sent).toEqual([PROMPT])
  expect(store['quiet:old-session']).toBe(undefined)
  expect(decisions(w)).toEqual([])
  expect(w.logs.some(l => l.includes('no answer within 2 minutes'))).toBe(true)
  w.answerLater('deny')
})

test('New conversation picked on a card that had already run out gets a true answer, and keeps nothing', async ($, on) => {
  const { o, w, store, clock } = world(on, { answer: 'later' }, AFTER_BREAK)
  await start($, o)
  const pending = submit($)
  await clock.advance(121_000)
  await pending
  const before = w.logs.length
  w.answerLater('New conversation')
  await clock.settle()
  expect(w.toasts.some(t => t.startsWith('That card had run out') && t.includes('nothing was kept'))).toBe(true)
  expect(w.logs.slice(before).some(l => l.includes('That card had run out'))).toBe(true)   // in the transcript too
  expect(Object.keys(store).some(k => k.startsWith('handoff:'))).toBe(false)
})

test('Keep going picked on a card that had already run out says it changed nothing, and sets no quiet period', async ($, on) => {
  const { o, w, store, clock } = world(on, { answer: 'later' }, AFTER_BREAK)
  await start($, o)
  const pending = submit($)
  await clock.advance(121_000)
  await pending
  w.answerLater('Keep going here anyway')
  await clock.settle()
  expect(w.toasts.some(t => t.startsWith('That card had run out') && t.includes('your answer changed nothing'))).toBe(true)
  expect(store['quiet:old-session']).toBe(undefined)
})

test('after Keep going on the card, the next prompt in the break goes out with no card', async ($, on) => {
  const { o, w } = world(on, { answer: 'Keep going here anyway' }, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  await submit($, PROMPT_B)
  expect(w.asked.length).toBe(1)
  expect(w.sent).toEqual([PROMPT, PROMPT_B])
})

test('text typed under Other at the card sends the message, with no quiet period and nothing logged', async ($, on) => {
  const { o, w, store } = world(on, { answer: 'what does it cost?', checkout: true }, AFTER_BREAK)
  await start($, o)
  await submit($)
  expect(w.sent).toEqual([PROMPT])
  expect(store['quiet:old-session']).toBe(undefined)
  expect(decisions(w)).toEqual([])
})

test('a second switch in the same break adds the new request to the first handoff, and is logged once', async ($, on) => {
  const { o, w, store } = world(on, { checkout: true }, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  await submit($, PROMPT_B)
  expect(w.asked.length).toBe(2)
  const h = store[KEY('old-session')] as any
  expect(h.text.includes(PROMPT)).toBe(true)
  expect(h.text.endsWith(PROMPT_B)).toBe(true)
  expect(decisions(w).length).toBe(1)
})

test('a second request is added even when its words are already in the handoff, and the same one only once', async ($, on) => {
  const { o, store } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  await submit($, 'continue')                                    // the handoff's own "Before you continue" holds it
  await submit($, 'continue')
  const h = store[KEY('old-session')] as any
  expect(h.text.endsWith(`${PROMPT}\n\ncontinue`)).toBe(true)
  expect(h.requests).toEqual([PROMPT, 'continue'])
  expect(h.held).toEqual([PROMPT, 'continue'])
})

test("after the band's switch with nothing typed, the next request takes the placeholder's place", async ($, on) => {
  const { o, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await clock.settle()
  await press($, 'switch')
  expect((store[KEY('old-session')] as any).text.endsWith('(write your next request here)')).toBe(true)
  await submit($, PROMPT)
  const h = store[KEY('old-session')] as any
  expect(h.text.includes('(write your next request here)')).toBe(false)
  expect(h.text.endsWith(`My next request:\n\n${PROMPT}`)).toBe(true)
})

test("the band's second switch in a break adds your draft to the card's handoff, and is logged once", async ($, on) => {
  const { o, w, store, clock } = world(on, { checkout: true }, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  o.box = PROMPT_B
  await start($, o)                                              // the engine starts again for this conversation
  await clock.settle()
  await press($, 'switch')
  const h = store[KEY('old-session')] as any
  expect(h.text.includes(PROMPT)).toBe(true)
  expect(h.text.endsWith(PROMPT_B)).toBe(true)
  expect(h.held).toEqual([PROMPT])                               // the draft is still in this conversation's box
  expect(decisions(w).length).toBe(1)
})

for (const surfaces of [['desktop'], ['terminal']]) {
  test(`a switch in a later break makes a new handoff, carrying a message the card held back before (${surfaces[0]})`, async ($, on) => {
    const { o, w, store, clock } = world(on, { surfaces }, AFTER_BREAK)
    await start($, o)
    await submit($, PROMPT)                                      // held back, and no new conversation took it
    await clock.advance(MIN)
    await reply($)                                               // you went on here instead
    await clock.advance(2 * HOUR)
    await submit($, PROMPT_B)
    const h = store[KEY('old-session')] as any
    expect(h.text.endsWith(`My next request:\n\n${PROMPT_B}`)).toBe(true)
    expect(h.text.includes(`${EARLIER}\n\n${PROMPT}\n\nMy next request:`)).toBe(true)
    expect(h.held).toEqual([PROMPT_B, PROMPT])
    expect(w.toasts.at(-1)?.includes('It also carries the message you held back here after an earlier break')).toBe(true)
  })
}

test('a held-back message you then send here is dropped from the handoff, so no later switch carries it, compaction or not', async ($, on) => {
  const { o, w, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)                                        // held back
  o.answer = 'Keep going here anyway'
  await submit($, PROMPT)                                        // the card asks again, and this time it goes out here
  await clock.settle()
  expect(w.sent).toEqual([PROMPT])
  expect((store[KEY('old-session')] as any).held).toEqual([])
  await reply($)
  await clock.advance(13 * HOUR)                                 // past the 12-hour quiet period
  o.messages = [msg('user', 'This session is being continued from a previous conversation. Summary:\nBuilt it.'), msg('assistant', 'Done.')]
  o.answer = 'New conversation'
  await submit($, PROMPT_B)
  expect((store[KEY('old-session')] as any).text.includes('Also held back')).toBe(false)
})

const SUMMARY_ONLY = [msg('user', 'This session is being continued from a previous conversation. Summary:\nBuilt it.'), msg('assistant', 'Done.')]

test('only the held message you send here is dropped: one still unsent is carried, after a compaction too', async ($, on) => {
  const PROMPT_C = 'And then tidy the imports in every file that the remaining steps of the plan in notes.md touch.'
  const { o, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  await submit($, PROMPT_C)                                      // a second switch in the break: both held
  expect((store[KEY('old-session')] as any).held).toEqual([PROMPT, PROMPT_C])
  o.answer = 'Keep going here anyway'
  await submit($, PROMPT)                                        // the first goes out here after all
  await clock.settle()
  expect((store[KEY('old-session')] as any).held).toEqual([PROMPT_C])
  await reply($)
  await clock.advance(13 * HOUR)
  o.messages = SUMMARY_ONLY
  o.answer = 'New conversation'
  await submit($, PROMPT_B)
  const h = store[KEY('old-session')] as any
  expect(h.held).toEqual([PROMPT_B, PROMPT_C])
  expect(h.text.includes(`${EARLIER}\n\n${PROMPT_C}\n\nMy next request:`)).toBe(true)
})

test('a held message you send again with no card, the cache warm, is dropped too, so a compaction never makes it look unsent', async ($, on) => {
  const { o, w, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)                                        // held back
  await clock.advance(MIN)
  await reply($)                                                 // Claude replied: the cache is warm
  await submit($, PROMPT)                                        // no card now: it goes out here
  await clock.settle()
  expect(w.sent).toEqual([PROMPT])
  await reply($)
  await clock.advance(2 * HOUR)
  o.messages = SUMMARY_ONLY
  await submit($, PROMPT_B)
  const h = store[KEY('old-session')] as any
  expect(h.text.includes('Also held back')).toBe(false)
  expect(h.held).toEqual([PROMPT_B])
  expect(w.toasts.at(-1)?.includes('It also carries')).toBe(false)
})

test('a held message the hooks beneath refuse stays held, as it never went in', async ($, on) => {
  const { o, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)                                        // held back
  o.refuse = true
  o.answer = 'Keep going here anyway'
  await submit($, PROMPT)                                        // sent here after all, but a settings hook blocks it
  await clock.settle()
  expect((store[KEY('old-session')] as any).held).toEqual([PROMPT])
})

test("a packet's handoff in a later break gets the earlier block before your prompt, closed by its own line, and a second switch after it", async ($, on) => {
  const PKT = 'Please follow PROMPT_web.md.'
  const { o, store, clock } = world(on, { checkout: true }, { ...AFTER_BREAK, 'packet:c:/work/project:web': 'C:/work/project/PROMPT_web.md' })
  await start($, o)
  await submit($, PROMPT)                                        // held back
  await clock.advance(MIN)
  await reply($)
  await clock.advance(2 * HOUR)
  await submit($, PKT)                                           // the packet card: New conversation
  let h = store[KEY('old-session')] as any
  expect(h.text.includes('Packet: `C:/work/project/PROMPT_web.md`. Read it first.')).toBe(true)
  expect(h.text.endsWith(`${EARLIER}\n\n${PROMPT}\n\nMy next request:\n\n${PKT}`)).toBe(true)
  await submit($, 'Please also run the web tests.')              // a second switch in the same break
  h = store[KEY('old-session')] as any
  expect(h.text.endsWith(`My next request:\n\n${PKT}\n\nPlease also run the web tests.`)).toBe(true)
})

test('when the store refuses a first switch, the note claims no version kept before', async ($, on) => {
  const { o, w } = world(on, { surfaces: ['terminal'], storeFull: true }, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  const note = w.toasts.at(-1) ?? ''
  expect(note.includes("couldn't be kept for a new conversation, but it's on your clipboard: paste it into one.")).toBe(true)
  expect(note.includes('The version kept before')).toBe(false)
})

test('a later switch with the same message you held back before carries it once, as the request', async ($, on) => {
  const { o, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  await clock.advance(MIN)
  await reply($)
  await clock.advance(2 * HOUR)
  await submit($, PROMPT)
  const h = store[KEY('old-session')] as any
  expect(h.text.includes('Also held back')).toBe(false)
  expect(h.text.endsWith(`My next request:\n\n${PROMPT}`)).toBe(true)
})

test('a switch after a new conversation took the first handoff makes a fresh one, with only the new request', async ($, on) => {
  const { o, store } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  ;(store[KEY('old-session')] as any).taken = 'n1'
  await submit($, PROMPT_B)
  const h = store[KEY('old-session')] as any
  expect(h.taken).toBe(undefined)
  expect(h.text.endsWith(`My next request:\n\n${PROMPT_B}`)).toBe(true)
  expect(h.text.includes(PROMPT)).toBe(false)
  expect(h.requests).toEqual([PROMPT_B])
})

test('when the store refuses a second switch, the note says a new box gets the version kept before', async ($, on) => {
  const { o, w } = world(on, { surfaces: ['terminal'] }, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  o.storeFull = true
  await submit($, PROMPT_B)
  expect(w.toasts.at(-1)?.includes("The version kept before, without this message, is what a new conversation's box or /switch-test restore gets, so paste this one over it.")).toBe(true)
})

test('a held-back message you then sent here yourself is not carried into the next handoff', async ($, on) => {
  const { o, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT)
  await clock.advance(MIN)
  o.messages = [...CHAT, msg('user', PROMPT), msg('assistant', 'Done.')]
  await reply($)
  await clock.advance(2 * HOUR)
  await submit($, PROMPT_B)
  const h = store[KEY('old-session')] as any
  expect(h.text.includes('Also held back')).toBe(false)
  expect(h.held).toEqual([PROMPT_B])
})

test('a handoff from an earlier break is replaced, not added to', async ($, on) => {
  const old = { text: 'OLD HANDOFF\n\nMy next request:\n\nold request', cwd: CWD, at: T0 - 3 * HOUR, from: 'old-session', requests: ['old request'] }
  const { o, store } = world(on, {}, { ...AFTER_BREAK, [KEY('old-session')]: old })
  await start($, o)
  await submit($, PROMPT)
  expect((store[KEY('old-session')] as any).text.includes('OLD HANDOFF')).toBe(false)
})

test('two conversations in one folder keep a handoff each; new conversations take the newer one first', async ($, on) => {
  const { o, w, store, clock } = world(on, { surfaces: ['terminal'] }, { 'last:a': T0 - 2 * HOUR, 'last:b': T0 - 2 * HOUR })
  o.id = 'a'
  await start($, o)
  await submit($, PROMPT)
  await clock.advance(MIN)
  o.id = 'b'
  await submit($, PROMPT_B)
  expect((store[KEY('a')] as any).text.endsWith(PROMPT)).toBe(true)
  expect((store[KEY('b')] as any).text.endsWith(PROMPT_B)).toBe(true)
  o.id = 'n1'
  o.turns = 0
  await start($, o)
  await clock.settle()
  expect(w.fills.at(-1)?.endsWith(PROMPT_B)).toBe(true)
  expect((store[KEY('b')] as any).taken).toBe('n1')
  expect(w.toasts.at(-1)?.includes('Another is kept for this folder')).toBe(true)
  o.id = 'n2'
  await start($, o)
  await clock.settle()
  expect(w.fills.at(-1)?.endsWith(PROMPT)).toBe(true)
  expect((store[KEY('a')] as any).taken).toBe('n2')
})

test("a new conversation takes another conversation's real handoff before a newer test one, and counts no test as another", async ($, on) => {
  const { o, w, clock } = world(on, { id: 'new-session', turns: 0, surfaces: ['terminal'] }, {
    ...kept('a', { text: 'REAL FROM a' }), ...kept('b', { text: 'TEST FROM b', at: T0 - 5 * MIN, test: true }),
  })
  await start($, o)
  await clock.settle()
  expect(w.fills).toEqual(['REAL FROM a'])
  expect(w.toasts.at(-1)).toBe('The handoff kept for this folder 10 min ago is in the box: check it, then press Enter.')
})

test('a folder whose name starts with this one\'s and has a "|" in it keeps its handoffs to itself', async ($, on) => {
  const { o, w, clock } = world(on, { id: 'new-session', cwd: '/x/proj', turns: 0, surfaces: ['terminal'] }, {
    'handoff:/x/proj|old|a': { text: 'OTHER FOLDER', cwd: '/x/proj|old', at: T0 - 5 * MIN, from: 'a' },
  })
  await start($, o)
  await clock.settle()
  expect(w.fills).toEqual([])
})

test('the handoff is kept for the folder the conversation started in, even after Claude ran cd into a subfolder', async ($, on) => {
  const { o, w, store, clock } = world(on, { cwd: `${CWD}/mod`, root: CWD, surfaces: ['terminal'] }, AFTER_BREAK)
  await start($, o)
  await submit($)
  expect(store[KEY('old-session')] !== undefined).toBe(true)
  o.id = 'new-session'
  o.cwd = CWD
  o.turns = 0
  await start($, o)
  await clock.settle()
  expect(w.fills.length).toBe(1)
})

test('when the store is full but the clipboard works, the held-back message points to the clipboard, not to restore', async ($, on) => {
  const { o } = world(on, { storeFull: true, surfaces: ['terminal'] }, AFTER_BREAK)
  await start($, o)
  const r: any = await submit($)
  expect(r.drop).toBe("switch-advisor: not sent here; it's in the handoff for a new conversation (it's on your clipboard).")
})

test('in the desktop app, with neither the store nor the clipboard, the held-back message points to the checkout copy', async ($, on) => {
  const { o } = world(on, { storeFull: true, copy: false, checkout: true }, AFTER_BREAK)
  await start($, o)
  const r: any = await submit($)
  expect(r.drop.startsWith("switch-advisor: not sent here; it's in the handoff for a new conversation (a copy is in ")).toBe(true)
  expect(r.drop.endsWith('/data/handoffs/).')).toBe(true)
})

test('with neither the store nor the clipboard, the message is sent here as usual, never lost', async ($, on) => {
  const { o, w } = world(on, { copy: false, storeFull: true }, AFTER_BREAK)
  await start($, o)
  const r: any = await submit($)
  expect(r.drop).toBe(undefined)
  expect(w.sent).toEqual([PROMPT])
  expect(w.toasts.some(t => t.includes('could be neither kept nor copied, so your message was sent here as usual'))).toBe(true)
})

for (const [name, over] of [
  ['in a local Cowork task', { cwd: COWORK }],
  ['where nothing draws (a cloud Cowork task)', { surfaces: [] as string[] }],
] as [string, Partial<Opts>][]) {
  test(`${name}, New conversation says to fetch the handoff with /switch-test restore, for a new task`, async ($, on) => {
    const { o, w, clock } = world(on, { copy: false, ...over }, AFTER_BREAK)
    await start($, o)
    const r: any = await submit($)
    await clock.advance(5_000)
    expect(r.drop.includes('send /switch-test restore here')).toBe(true)
    expect(r.drop.includes('new task')).toBe(true)
    expect(r.drop.includes('in its box')).toBe(false)
    expect(w.commands).toEqual([])
  })
}

// ---- picking it up ------------------------------------------------------------------------------------------------

test('a new terminal conversation in the same folder within 30 minutes finds the handoff in its box, once', async ($, on) => {
  const { o, w, store, clock } = world(on, { id: 'new-session', turns: 0, surfaces: ['terminal'] }, kept('old-session'))
  await start($, o)
  await clock.settle()
  expect(w.fills).toEqual(['HANDOFF FROM old-session'])
  expect((store[KEY('old-session')] as any).taken).toBe('new-session')   // marked, so no other box gets it
  expect(w.toasts.some(t => t.includes('is in the box'))).toBe(true)
  o.id = 'third-session'
  await start($, o)
  await clock.settle()
  expect(w.fills.length).toBe(1)
})

for (const [name, over, entries] of [
  ['the conversation it came from', { id: 'old-session' }, kept('old-session')],
  ['a conversation that already has messages', { turns: 2 }, kept('old-session')],
  ["another folder's conversation", { cwd: 'C:/work/other' }, kept('old-session')],
  ['a box holding your own draft', { box: 'my own draft' }, kept('old-session')],
  ['a new conversation after 30 minutes', {}, kept('old-session', { at: T0 - 31 * MIN })],
  ['a new conversation, once another took it', {}, kept('old-session', { taken: 'someone' })],
  ['a new Code-tab session (its box is the app\'s until its first message)', { surfaces: ['desktop'] }, kept('old-session')],
] as [string, Partial<Opts>, Record<string, unknown>][]) {
  test(`the handoff stays out of ${name}`, async ($, on) => {
    const { o, w, clock } = world(on, { id: 'new-session', turns: 0, surfaces: ['terminal'], ...over }, entries)
    await start($, o)
    await clock.advance(5_000)
    expect(w.fills).toEqual([])
  })
}

test('a prompt sent while the terminal box mounts stops the retry from filling under it', async ($, on) => {
  const { o, w, clock } = world(on, { id: 'new-session', turns: 0, surfaces: ['terminal'], fill: false }, kept('old-session'))
  await start($, o)
  await clock.settle()
  await submit($, 'run the linter')
  o.fill = true
  await clock.advance(2_000)
  expect(w.fills).toEqual([])
})

test("when the terminal's box isn't mounted yet, the fill is tried again", async ($, on) => {
  const { o, w, clock } = world(on, { id: 'new-session', turns: 0, surfaces: ['terminal'], fill: false }, kept('old-session'))
  await start($, o)
  await clock.settle()
  expect(w.fills).toEqual([])
  o.fill = true
  await clock.advance(400)
  expect(w.fills).toEqual(['HANDOFF FROM old-session'])
})

for (const surfaces of [['desktop'], ['terminal']]) {
  test(`/clear in the old conversation starts a new one that takes its handoff, though turns() counts the /clear (${surfaces[0]})`, async ($, on) => {
    const { o, w, store, clock } = world(on, { surfaces }, AFTER_BREAK)
    await start($, o)
    await submit($)
    await clear($, o, 'cleared-session')
    await clock.settle()
    expect(w.fills.length).toBe(1)
    expect(w.fills[0]?.endsWith(PROMPT)).toBe(true)
    expect((store[KEY('old-session')] as any).taken).toBe('cleared-session')
    expect(w.toasts.some(t => t.startsWith('The handoff you made here before the /clear is in the box'))).toBe(true)
  })
}

test("/clear takes the handoff this window made, not another conversation's newer one", async ($, on) => {
  const { o, w, store, clock } = world(on, { surfaces: ['terminal'] }, { 'last:a': T0 - 2 * HOUR, 'last:b': T0 - 2 * HOUR })
  o.id = 'a'
  await start($, o)
  await submit($, PROMPT)
  await clock.advance(MIN)
  o.id = 'b'
  await submit($, PROMPT_B)                                      // b's is the newer
  o.id = 'a'
  o.messages = CHAT
  o.turns = 5
  await clear($, o, 'a2')
  await clock.settle()
  expect(w.fills.at(-1)?.endsWith(PROMPT)).toBe(true)
  expect((store[KEY('a')] as any).taken).toBe('a2')
  expect((store[KEY('b')] as any).taken).toBe(undefined)
  expect(w.toasts.at(-1)?.includes('Another is kept for this folder: /switch-test restore all lists it.')).toBe(true)
  const back = await command($, 'restore')                       // you emptied the box, then asked for it back
  await clock.advance(100)
  expect(back.text?.includes("already put in this conversation's box")).toBe(true)
  expect(w.fills.at(-1)?.endsWith(PROMPT)).toBe(true)            // this window's own, not b's
  expect((store[KEY('b')] as any).taken).toBe(undefined)
  o.id = 'b'
  o.messages = CHAT
  o.turns = 5
  await clear($, o, 'b2')
  await clock.settle()
  expect(w.fills.at(-1)?.endsWith(PROMPT_B)).toBe(true)
  expect((store[KEY('b')] as any).taken).toBe('b2')
})

test('/clear hours after the switch still puts the handoff in the box, as the note says', async ($, on) => {
  const { o, w, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($)
  await clock.advance(3 * HOUR)
  await clear($, o, 'cleared-session')
  await clock.settle()
  expect(w.fills.length).toBe(1)
})

test('/clear after you went on in the old conversation leaves its old handoff out of the box', async ($, on) => {
  const { o, w, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($)
  await clock.advance(MIN)
  await reply($)                                                 // you carried on here, and Claude replied
  await clock.advance(5 * MIN)
  await clear($, o, 'cleared-session')
  await clock.settle()
  expect(w.fills).toEqual([])
  expect((store[KEY('old-session')] as any).taken).toBe(undefined)
  expect(w.toasts.at(-1)?.startsWith("The handoff you made here before the /clear isn't in the box: Claude replied in that conversation after you made it")).toBe(true)
})

test("/clear after Claude answered a background task's report says why the box is empty, and restore puts the handoff there", async ($, on) => {
  const { o, w, store, clock } = world(on, { surfaces: ['terminal'] },
    { ...AFTER_BREAK, ...kept('someone', { at: T0 + 30_000 }) })  // another conversation's, newer: restore still gives yours
  await start($, o)
  await submit($)                                                // held back, and kept
  await clock.advance(MIN)
  await submit($, '<task-notification>\n<status>completed</status>\n</task-notification>', 'task-notification')
  await reply($)                                                 // Claude answered the report, here
  await clear($, o, 'cleared-session')
  await clock.settle()
  expect(w.fills).toEqual([])
  expect(w.toasts.at(-1)?.includes("(to you, or to a background task's report), and the handoff doesn't carry that reply. /switch-test restore puts it in the box anyway.")).toBe(true)
  const r = await command($, 'restore')
  await clock.advance(100)
  expect(r.text?.includes('It goes in the box')).toBe(true)
  expect(w.fills.length).toBe(1)
  expect(w.fills[0]?.endsWith(PROMPT)).toBe(true)
  expect((store[KEY('old-session')] as any).taken).toBe('cleared-session')
})

test("/clear says so when another conversation's box already took this window's handoff", async ($, on) => {
  const { o, w, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($)
  ;(store[KEY('old-session')] as any).taken = 'n1'               // a new terminal conversation took it
  await clear($, o, 'cleared-session')
  await clock.settle()
  expect(w.fills).toEqual([])
  expect(w.toasts.at(-1)).toBe("The handoff you made here before the /clear isn't in the box: another conversation's box already took it. /switch-test restore shows it.")
})

test("/clear in a window whose own handoff is out of date never takes another conversation's instead", async ($, on) => {
  const { o, w, store, clock } = world(on, { surfaces: ['terminal'] }, { 'last:a': T0 - 2 * HOUR, 'last:b': T0 - 2 * HOUR })
  o.id = 'a'
  await start($, o)
  await submit($, PROMPT)
  await clock.advance(MIN)
  await reply($)                                                 // a went on, and Claude replied there
  o.id = 'b'
  await submit($, PROMPT_B)
  o.id = 'a'
  o.messages = CHAT
  o.turns = 5
  await clear($, o, 'a2')
  await clock.settle()
  expect(w.fills).toEqual([])
  expect((store[KEY('b')] as any).taken).toBe(undefined)
  o.id = 'b'
  o.messages = CHAT
  o.turns = 5
  await clear($, o, 'b2')
  await clock.settle()
  expect(w.fills.at(-1)?.endsWith(PROMPT_B)).toBe(true)
})

test('a first message sent while the SessionStart hooks beneath a /clear still run keeps the box from being filled under it', async ($, on) => {
  const { o, w, clock } = world(on, { surfaces: ['terminal'] }, AFTER_BREAK)
  await start($, o)
  await submit($)
  let open = () => {}
  w.gate = new Promise<void>(resolve => { open = resolve })
  await $.session.end({ reason: 'clear', sessionId: o.id, resume: { id: o.id } })
  o.id = 'cleared-session'
  o.turns = 1
  o.messages = [msg('user', CLEAR_ROW)]
  const starting = $.classic.SessionStart({ source: 'clear' })
  await submit($, 'a first message')
  open()
  await starting
  await clock.settle()
  expect(w.fills).toEqual([])
})

for (const [name, rows] of [
  ['begun with a skill', [msg('user', '<command-message>code-review</command-message>\n<command-name>/code-review</command-name>'), msg('assistant', 'Reviewed: two issues.')]],
  ['compacted in its last turn', [msg('user', 'This session is being continued from a previous conversation that ran out of context. Summary:\n1. Primary Request and Intent:\n   Build it.'), msg('assistant', 'Done.')]],
] as [string, object[]][]) {
  test(`a resumed terminal conversation with nothing typed in it, ${name}, is never taken for a new one`, async ($, on) => {
    const { o, w, store, clock } = world(on, { id: 'resumed', turns: 1, surfaces: ['terminal'], messages: rows }, kept('old-session'))
    await start($, o)
    await clock.settle()
    expect(w.fills).toEqual([])
    const r = await command($, 'restore')
    await clock.advance(100)
    expect(r.text?.includes('Copy it into a new conversation')).toBe(true)     // shown, not put in the box
    expect(w.fills).toEqual([])
    expect((store[KEY('old-session')] as any).taken).toBe(undefined)
  })
}

test("/clear in a window that kept no handoff takes the folder's newest within 30 minutes, and says whose it is", async ($, on) => {
  const { o, w, clock } = world(on, {}, kept('someone'))
  await start($, o)
  await clear($, o, 'cleared-session')
  await clock.settle()
  expect(w.fills).toEqual(['HANDOFF FROM someone'])
  expect(w.toasts.some(t => t.startsWith('The handoff kept for this folder 10 min ago is in the box'))).toBe(true)
})

test('/clear leaves a draft you already typed in the new box alone', async ($, on) => {
  const { o, w, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($)
  o.box = 'my own draft'
  await clear($, o, 'cleared-session')
  await clock.settle()
  expect(w.fills).toEqual([])
})

test('/switch-test restore right after a /clear puts the handoff in the box', async ($, on) => {
  const { o, w, store, clock } = world(on, {}, kept('someone', { at: T0 - 50 * MIN }))
  await start($, o)
  await clear($, o, 'cleared-session')                           // 50 minutes: too old for the /clear to take it
  await clock.settle()
  expect(w.fills).toEqual([])
  const r = await command($, 'restore')
  expect(r.text?.includes('It goes in the box')).toBe(true)
  await clock.advance(100)
  expect(w.fills).toEqual(['HANDOFF FROM someone'])
  expect((store[KEY('someone')] as any).taken).toBe('cleared-session')
})

test("a handoff pasted as a new conversation's first message is marked taken, so no other box gets it", async ($, on) => {
  const { o, store, clock } = world(on, { id: 'new-session', turns: 0 }, kept('old-session', { text: 'New conversation, continuing from the previous conversation...' }))
  await start($, o)
  await submit($, 'New conversation, continuing from the previous conversation...')
  await clock.settle()
  expect((store[KEY('old-session')] as any).taken).toBe('new-session')
})

test("the band's handoff pasted with your request written over its placeholder is marked taken too", async ($, on) => {
  const { o, w, store, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await clock.settle()
  await press($, 'switch')                                       // nothing typed: it ends with the placeholder
  const text = (store[KEY('old-session')] as any).text as string
  o.id = 'pasted-session'
  o.turns = 0
  await start($, o)
  const zeroWidth = String.fromCharCode(0x200b)                  // a composer may add one
  const edited = text.replace('(write your next request here)', 'Now the docs, please.').replace('Project:', `Project:${zeroWidth}`)
    .replace(/\n/g, '\r\n')
  await submit($, edited)
  await clock.settle()
  expect((store[KEY('old-session')] as any).taken).toBe('pasted-session')
  o.id = 'later-session'                                         // a new terminal conversation 15 minutes on gets nothing
  o.surfaces = ['terminal']
  await clock.advance(15 * MIN)
  await start($, o)
  await clock.settle()
  expect(w.fills).toEqual([])
})

test("a packet's handoff pasted with its prompt reworded is marked taken", async ($, on) => {
  const text = 'New conversation, continuing from the previous conversation (640K tokens).\n\nProject: `C:/work/project`\n' +
    'Packet: `C:/work/project/PROMPT_web.md`. Read it first.\n\nPlease follow PROMPT_web.md.'
  const { o, store, clock } = world(on, { id: 'new-session', turns: 0 }, kept('old-session', { text, requests: ['Please follow PROMPT_web.md.'] }))
  await start($, o)
  await submit($, text.replace('Please follow PROMPT_web.md.', 'Please follow PROMPT_web.md, but leave step 3 for later.'))
  await clock.settle()
  expect((store[KEY('old-session')] as any).taken).toBe('new-session')
})

test("a packet's handoff with two requests, pasted with the first one reworded, is marked taken", async ($, on) => {
  const head = 'New conversation, continuing from the previous conversation (640K tokens).\n\nProject: `C:/work/project`\n' +
    'Packet: `C:/work/project/PROMPT_web.md`. Read it first.'
  const text = `${head}\n\nPlease follow PROMPT_web.md.\n\nPlease also run the web tests.`
  const { o, store, clock } = world(on, { id: 'new-session', turns: 0 },
    kept('old-session', { text, requests: ['Please follow PROMPT_web.md.', 'Please also run the web tests.'] }))
  await start($, o)
  await submit($, `${head}\n\nPlease follow PROMPT_web.md, but leave step 3.\n\nPlease also run the web tests.`)
  await clock.settle()
  expect((store[KEY('old-session')] as any).taken).toBe('new-session')
})

test("a pasted handoff whose earlier-break block you left out is marked taken", async ($, on) => {
  const text = `New conversation, continuing from the previous conversation (640K tokens).\n\n> Built half of it.\n\n${EARLIER}\n\nold request\n\nMy next request:\n\nnew request`
  const { o, store, clock } = world(on, { id: 'new-session', turns: 0 }, kept('old-session', { text }))
  await start($, o)
  await submit($, text.replace(`${EARLIER}\n\nold request\n\n`, ''))
  await clock.settle()
  expect((store[KEY('old-session')] as any).taken).toBe('new-session')
})

test("a paste marks only an untaken handoff that another conversation in this folder made", async ($, on) => {
  const T = 'New conversation, continuing from the previous conversation (640K tokens).\n\nMy next request:\n\nx'
  const { o, store, clock } = world(on, { id: 'old-session', turns: 0 }, {
    ...kept('old-session', { text: T }), ...kept('done', { text: T, taken: 'n9', takenAt: T0 - MIN }),
    [KEY('other', 'c:/work/other')]: { text: T, cwd: 'C:/work/other', at: T0 - 10 * MIN, from: 'other' },
  })
  await start($, o)
  await submit($, T)
  await clock.settle()
  expect((store[KEY('old-session')] as any).taken).toBe(undefined)                // its own
  expect((store[KEY('done')] as any).taken).toBe('n9')                            // already taken
  expect((store[KEY('other', 'c:/work/other')] as any).taken).toBe(undefined)    // another folder's
})

test('a paste that changes what the handoff quotes is not taken for it', async ($, on) => {
  const text = 'New conversation, continuing from the previous conversation (640K tokens).\n\n> Built half of it.\n\nMy next request:\n\nmore'
  const { o, store, clock } = world(on, { id: 'new-session', turns: 0 }, kept('old-session', { text }))
  await start($, o)
  await submit($, text.replace('Built half', 'Built all'))
  await clock.settle()
  expect((store[KEY('old-session')] as any).taken).toBe(undefined)
})

// ---- restore and expiry -------------------------------------------------------------------------------------------

test("/switch-test restore shows the handoff to copy, fills only a new conversation's box, and still shows it after", async ($, on) => {
  const { o, w, store, clock } = world(on, {}, kept('old-session', { at: T0 - 40 * MIN }))
  await start($, o)
  const here = await command($, 'restore')
  expect(here.text?.includes('kept 40 min ago from this conversation')).toBe(true)
  expect(here.text?.endsWith('HANDOFF FROM old-session')).toBe(true)
  await clock.advance(100)
  expect(w.fills).toEqual([])                                    // never into the conversation it came from
  o.id = 'new-session'
  o.turns = 0
  const there = await command($, 'restore')
  expect(there.text?.includes('It goes in the box')).toBe(true)
  await clock.advance(100)
  expect(w.fills).toEqual(['HANDOFF FROM old-session'])
  expect((store[KEY('old-session')] as any).taken).toBe('new-session')
  o.id = 'old-session'
  o.turns = 5
  const again = await command($, 'restore')
  expect(again.text?.includes("already put in a new conversation's box")).toBe(true)
})

test('restore gives a new conversation the newest handoff still untaken, not a newer one already taken', async ($, on) => {
  const { o, w, clock } = world(on, { id: 'new-session', turns: 0 }, {
    ...kept('a', { text: 'UNTAKEN FROM a', at: T0 - 20 * MIN }), ...kept('b', { text: 'TAKEN FROM b', at: T0 - 5 * MIN, taken: 'x', takenAt: T0 - MIN }),
  })
  await start($, o)
  const r = await command($, 'restore')
  await clock.advance(100)
  expect(r.text?.endsWith('UNTAKEN FROM a')).toBe(true)
  expect(w.fills).toEqual(['UNTAKEN FROM a'])
})

test('restore in a window whose box took a handoff gives that one back, not another one still untaken', async ($, on) => {
  const { o, w, clock } = world(on, { id: 'n1', turns: 0, surfaces: ['terminal'] }, {
    ...kept('a', { text: 'FROM a', at: T0 - 20 * MIN }), ...kept('b', { text: 'FROM b', at: T0 - 5 * MIN }),
  })
  await start($, o)
  await clock.settle()
  expect(w.fills).toEqual(['FROM b'])                            // the pickup took the newer one
  const r = await command($, 'restore')                          // you emptied the box, then asked for it back
  await clock.advance(100)
  expect(r.text?.includes("already put in this conversation's box")).toBe(true)
  expect(w.fills).toEqual(['FROM b', 'FROM b'])
})

test('/switch-test restore all lists the handoffs kept for the folder, briefly and newest first, and restore <n> gives one', async ($, on) => {
  const { o, w, store, clock } = world(on, { id: 'new-session', turns: 0 }, {
    ...kept('a', { text: 'FROM a', at: T0 - 20 * MIN, requests: [PROMPT] }),
    ...kept('b', { text: 'FROM b', at: T0 - 5 * MIN, taken: 'x', takenAt: T0 - MIN, requests: ['fix the login form'] }),
  })
  await start($, o)
  const r = await command($, 'restore all')
  await clock.advance(100)
  expect(r.text).toBe(`switch-test: 2 handoffs are kept for ${CWD}, newest first:\n` +
    `1. Kept 5 min ago, already put in a new conversation's box, asking: "fix the login form"\n` +
    `2. Kept 20 min ago, asking: "${PROMPT.slice(0, 59)}…"\n` +
    'Send /switch-test restore <number> to see one, and to put it in the box of a conversation with nothing sent yet.')
  expect(w.fills).toEqual([])                                    // a list only: nothing filled or marked,
  expect(r.text?.includes('FROM a')).toBe(false)                 // and no handoff's text, which Claude would read
  expect((store[KEY('a')] as any).taken).toBe(undefined)
  const second = await command($, 'restore 2')
  await clock.advance(100)
  expect(second.text?.endsWith('FROM a')).toBe(true)
  expect(w.fills).toEqual(['FROM a'])
  expect((store[KEY('a')] as any).taken).toBe('new-session')
  const none = await command($, 'restore 3')
  expect(none.text).toBe(`switch-test: there is no handoff 3 for ${CWD}: /switch-test restore all lists them.`)
  const one = await command($, 'restore')
  expect(one.text?.includes('1 more is kept for this folder: /switch-test restore all lists it.')).toBe(true)
})

test('restore <n> into a box that held another handoff frees that one, so restore and the next new conversation get the right ones', async ($, on) => {
  const { o, w, store, clock } = world(on, { id: 'n1', turns: 0, surfaces: ['terminal'] }, {
    ...kept('a', { text: 'FROM a', at: T0 - 20 * MIN }), ...kept('b', { text: 'FROM b', at: T0 - 5 * MIN }),
  })
  await start($, o)
  await clock.settle()
  expect(w.fills).toEqual(['FROM b'])                            // the pickup took the newer one
  await command($, 'restore 2')                                  // you emptied the box and asked for a's instead
  await clock.advance(100)
  expect(w.fills).toEqual(['FROM b', 'FROM a'])
  expect((store[KEY('a')] as any).taken).toBe('n1')
  expect((store[KEY('b')] as any).taken).toBe(undefined)         // b's is in no box now
  const back = await command($, 'restore')                       // and restore gives back what this box holds
  await clock.advance(100)
  expect(back.text?.endsWith('FROM a')).toBe(true)
  o.id = 'n2'                                                    // a new terminal conversation then gets b's
  await start($, o)
  await clock.settle()
  expect(w.fills.at(-1)).toBe('FROM b')
})

test("restore of a handoff another box took leaves it marked as that box's, so that box's own restore still gives it back", async ($, on) => {
  const { o, w, store, clock } = world(on, { surfaces: ['terminal'] }, AFTER_BREAK)
  await start($, o)
  await submit($)
  ;(store[KEY('old-session')] as any).taken = 'n1'               // a new terminal conversation took it
  await clear($, o, 'cleared-session')                           // the toast: another box already took it
  await clock.settle()
  const r = await command($, 'restore')
  await clock.advance(100)
  expect(r.text?.includes('It goes in the box')).toBe(true)
  expect(w.fills.length).toBe(1)
  expect((store[KEY('old-session')] as any).taken).toBe('n1')
})

test('restore names the folder of the newest handoff kept elsewhere', async ($, on) => {
  const { o } = world(on, { cwd: 'C:/work/other' }, kept('old-session'))
  await start($, o)
  const r = await command($, 'restore')
  expect(r.text?.includes('no handoff is kept for C:/work/other')).toBe(true)
  expect(r.text?.includes(`The newest kept is for ${CWD}`)).toBe(true)
})

test('a handoff is deleted at the first start after 24 hours, in a resumed conversation too', async ($, on) => {
  const { o, store, clock } = world(on, { turns: 5 }, kept('someone', { at: T0 - 25 * HOUR }))
  await start($, o)
  await clock.settle()
  expect(store[KEY('someone')]).toBe(undefined)
})

test("other conversations' notes are deleted after 30 idle days, and quiet periods once over; this one's stay", async ($, on) => {
  const DAY = 24 * HOUR
  const { o, store, clock } = world(on, {}, {
    'last:old-session': T0 - 40 * DAY, 'last:gone': T0 - 31 * DAY, 'last:recent': T0 - 29 * DAY, 'dismissed:gone': T0 - 31 * DAY,
    'pre:gone': T0 - 31 * DAY, 'quiet:over': T0 - HOUR, 'quiet:running': T0 + HOUR,
    'last:resumed': T0 - 40 * DAY, 'dismissed:resumed': T0 - 40 * DAY, 'resumed:resumed': T0 - 2 * DAY, 'resumed:ancient': T0 - 31 * DAY,
  })
  await start($, o)
  await clock.settle()
  expect(Object.keys(store).sort()).toEqual(['dismissed:resumed', 'last:old-session', 'last:recent', 'last:resumed', 'quiet:running', 'resumed:resumed'])
})

test('a start deletes at most 50 such notes, so a crowded store is tidied a little at a time', async ($, on) => {
  const old = Object.fromEntries(Array.from({ length: 60 }, (_, i) => [`last:s${i}`, T0 - 40 * 24 * HOUR]))
  const { o, store, clock } = world(on, {}, old)
  await start($, o)
  await clock.settle()
  expect(Object.keys(store).filter(k => k.startsWith('last:s')).length).toBe(10)
})

test('resuming a conversation notes its break, and that it was resumed', async ($, on) => {
  const { o, store, clock } = world(on, {})
  await start($, o)
  await $.classic.SessionStart({ source: 'resume', session_id: 'old-session', seconds_since_last_response: 40 * 24 * 3600 })
  await clock.settle()
  expect(store['last:old-session']).toBe(T0 - 40 * 24 * HOUR)
  expect(store['resumed:old-session']).toBe(T0)
})

// ---- /switch-test ------------------------------------------------------------------------------------------------

test("/switch-test keeps a test handoff, but never in place of this conversation's real one, however old", async ($, on) => {
  const { o, store } = world(on)
  await start($, o)
  const first = await command($, '')
  expect(first.text?.startsWith('switch-test: This conversation stays as it is')).toBe(true)
  expect((store[KEY('old-session')] as any).test).toBe(true)
  store[KEY('old-session')] = { text: 'REAL', cwd: CWD, at: T0 - 40 * MIN, from: 'old-session' }
  const second = await command($, '')
  expect(second.text?.includes('A real handoff from this conversation is kept')).toBe(true)
  expect((store[KEY('old-session')] as any).text).toBe('REAL')
})

test('/switch-test never replaces a real handoff from this conversation that a new conversation already took', async ($, on) => {
  const { o, store } = world(on, {}, kept('old-session', { text: 'REAL', taken: 'n1', takenAt: T0 - 5 * MIN }))
  await start($, o)
  const r = await command($, '')
  expect(r.text?.includes('A real handoff from this conversation is kept')).toBe(true)
  expect((store[KEY('old-session')] as any).text).toBe('REAL')
})

test('/switch-test waits as long as you need: New conversation picked after 3 minutes keeps the test handoff', async ($, on) => {
  const { o, w, store, clock } = world(on, { answer: 'later' })
  await start($, o)
  const pending = command($, '')
  await clock.advance(3 * MIN)
  w.answerLater('New conversation')
  const r: any = await pending
  expect(r.text?.startsWith('switch-test: This conversation stays as it is')).toBe(true)
  expect((store[KEY('old-session')] as any).test).toBe(true)
})

test('/switch-test demo and band say so where nothing a plugin draws appears', async ($, on) => {
  const { o } = world(on, { surfaces: [] })
  await start($, o)
  const demo = await command($, 'demo')
  expect(demo.text?.startsWith("switch-test: the demo band is set, but this window doesn't draw what plugins show")).toBe(true)
  const band = await command($, 'band')
  expect(band.text?.startsWith("switch-test: the band is set, but this window doesn't draw what plugins show")).toBe(true)
})

test('/switch-test reports a card that was closed, and changes nothing', async ($, on) => {
  const { o, store } = world(on, { answer: 'deny' })
  await start($, o)
  const r = await command($, '')
  expect(r.text?.includes('was closed, or could not be shown here')).toBe(true)
  expect(Object.keys(store).some(k => k.startsWith('handoff:'))).toBe(false)
})

test('/switch-test takes text typed under Other as no choice, and logs nothing', async ($, on) => {
  const { o, w } = world(on, { answer: 'what does this cost?', checkout: true })
  await start($, o)
  const r = await command($, '')
  expect(r.text).toBe('switch-test: that answer is neither choice. Nothing changed.')
  expect(decisions(w)).toEqual([])
})

// ---- the band ------------------------------------------------------------------------------------------------------

test('after a break the band gets its offer by itself, with no typing', async ($, on) => {
  const { o, clock, band } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await clock.settle()
  expect(band()?.line1?.startsWith('Back after 2 h: sending here re-caches 640K tokens')).toBe(true)
})

test('in a quiet period the band offers nothing', async ($, on) => {
  const { o, clock, band } = world(on, {}, { ...AFTER_BREAK, 'quiet:old-session': T0 + HOUR })
  await start($, o)
  await clock.settle()
  expect(band() ?? null).toBe(null)
})

test("after the band's Keep going here anyway, a prompt in the same break goes out with no card", async ($, on) => {
  const { o, w, clock } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await clock.settle()
  await press($, 'keep')
  await submit($, PROMPT)
  expect(w.asked).toEqual([])
  expect(w.sent).toEqual([PROMPT])
})

// ---- when it stays out -------------------------------------------------------------------------------------------

test('no card while the cache is still warm', async ($, on) => {
  const { o, w } = world(on, {}, { 'last:old-session': T0 - 20 * MIN })
  await start($, o)
  await submit($)
  expect(w.asked).toEqual([])
  expect(w.sent).toEqual([PROMPT])
})

test('no card for a message from the phone over Remote Control', async ($, on) => {
  const { o, w } = world(on, {}, AFTER_BREAK)
  await start($, o)
  await submit($, PROMPT, 'bridge')
  expect(w.asked).toEqual([])
  expect(w.sent).toEqual([PROMPT])
})

test("the account's copy steps back where CLAUDE_CODE_PLUGIN_DIRS loads a folder copy, so only one acts", async ($, on) => {
  const { o, w } = world(on, { env: { CLAUDE_CODE_PLUGIN_DIRS: 'C:/checkout/mod/switch-advisor' } }, AFTER_BREAK)
  await start($, o)
  await submit($)
  expect(w.asked).toEqual([])
  expect(w.sent).toEqual([PROMPT])
})
