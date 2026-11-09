import { expect, mock, test } from 'claude-code/testing'

const BAND = {
  plugin: 'switch-advisor',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isWorking: false, maxRows: 12, bodyColumns: 100, scroll: { offset: 0, bodyRows: 11 }, view: {} },
} as const

const OFFER = {
  last: 1, risk: 'medium', line1: 'Back after 2 h: sending here re-caches 640K tokens ($4.77, 0.4–1.2% of your weekly limit).',
  line2: 'A new conversation from a handoff saves about $3.84 now. Quality risk is medium: 2 task-list items are still open.',
  question: 'Start a new conversation?', options: ['New conversation', 'Keep going here anyway'],
  numbers: { resendHere: 4.77, newConv: 0.93, saveNow: 3.84, typical: [8.15, 14.87], floor: 3.28 },
}

test('after a break the band shows the offer by itself, on the desktop and in the terminal', async ($, on) => {
  on('state.get', () => ({ value: { value: OFFER, version: 1 } }))
  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface } as any)
    expect((await ui.find({ type: 'Text', text: /Back after 2 h/ }))?.text).toBe(OFFER.line1)
    expect((await ui.find({ type: 'Button', key: 'switch' } as any)) !== undefined).toBe(true)
    expect((await ui.find({ type: 'Button', key: 'keep' } as any)) !== undefined).toBe(true)
    await ui.unmount()
  }
})

test('the band stays away on a phone, while a turn runs, and when there is no offer', async ($, on) => {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  on('state.get', () => ({ value: { value: OFFER, version: 1 } }))
  const phone = await $.ui.mount({ ...BAND, surface: 'mobile' } as any)
  expect((await phone.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
  await phone.unmount()
  const busy = await $.ui.mount({ ...BAND, surface: 'desktop', props: { ...BAND.props, isWorking: true } } as any)
  expect((await busy.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
  await busy.unmount()
})

test('the demo band stays up while a turn runs, so it can be screenshotted', async ($, on) => {
  on('state.get', () => ({ value: { value: { ...OFFER, demo: true }, version: 1 } }))
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop', props: { ...BAND.props, isWorking: true } } as any)
  expect((await ui.find({ type: 'Text', text: /Back after 2 h/ }))?.text).toBe(OFFER.line1)
  await ui.unmount()
})

async function answerKeepGoing($: any, on: any, inCheckout: boolean): Promise<string[]> {
  const files: string[] = []
  on('fs.exists', () => ({ value: inCheckout }))
  on('fs.read', (_: unknown, e: { path: string }) => { files.push(`read ${e.path}`); return { value: '{}' } })
  on('fs.write', (_: unknown, e: { path: string; text: string }) => {
    files.push(`write ${e.path}`, `text ${e.text}`)
    return { value: null }
  })
  on('command.register', () => ({ value: { command: 'switch-test' } }))
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'test-session' }))
  on('clock.now', () => ({ value: 1_000_000 }))
  on('store.get', () => ({ value: null }))
  on('store.set', () => ({ value: null }))
  let state: { value: unknown; version: number } = { value: undefined, version: 0 }
  on('state.get', () => ({ value: state }))
  on('state.set', (_: unknown, e: { value: unknown }) => {
    state = { value: e.value, version: state.version + 1 }
    return { value: { isSet: true, version: state.version } }
  })
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: true })
  state = { value: OFFER, version: state.version + 1 }            // a real offer, as after a break
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' } as any)
  await ui.press({ key: 'keep' })
  await ui.unmount()
  return files
}

test('installed outside its repository (from the marketplace, say), answering the band reads and writes no files', async ($, on) => {
  expect(await answerKeepGoing($, on, false)).toEqual([])
})

test('run from a checkout of its repository, it logs the answer there, with the numbers the guardrail weighs', async ($, on) => {
  const files = await answerKeepGoing($, on, true)
  expect(files.some(f => f.startsWith('write ') && f.replace(/\\/g, '/').includes('/data/switch_decisions/'))).toBe(true)
  const logged = JSON.parse(files.find(f => f.startsWith('text '))!.slice('text '.length))
  expect(logged.choice).toBe('stay')
  expect(logged.numbers.resendHere).toBe(4.77)
})

/** The engine beneath the band, for its buttons: a desktop window, a store and a clock in memory, and the card's answer
 *  given when the test says. */
function bandWorld(on: any) {
  const w = { commands: [] as string[], toasts: [] as string[], answer: (_: string) => {} }
  const store: Record<string, unknown> = {}
  const clock = mock.clock(on, { now: 1_000_000 })
  on('store.get', (_: unknown, e: { key: string }) => ({ value: store[e.key] }))
  on('store.set', (_: unknown, e: { key: string; value: unknown }) => { store[e.key] = JSON.parse(JSON.stringify(e.value)); return { value: undefined } })
  on('store.delete', (_: unknown, e: { key: string }) => { delete store[e.key]; return { value: undefined } })
  on('store.keys', () => ({ value: Object.keys(store) }))
  on('fs.exists', () => ({ value: false }))
  on('command.register', () => ({ value: { command: 'switch-test' } }))
  on('command.run', (_: unknown, e: { command: string }) => { w.commands.push(e.command); return { text: '' } })
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 'test-session' }))
  on('session.cwd', () => ({ value: 'C:/work/project' }))
  on('session.root', () => ({ value: 'C:/work/project' }))
  on('session.turns', () => ({ value: 3 }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { tokens: 640_000, window: 1_000_000 }, rateLimits: [] } }))
  on('session.messages', () => ({ value: [{ role: 'user', text: 'Build the thing', toolUses: [] }, { role: 'assistant', text: 'Built half.', toolUses: [] }] }))
  on('prompt.read', () => ({ value: { text: 'and then the docs', cursor: 0 } }))
  on('ui.copy', () => ({ value: { isCopied: true } }))
  on('ui.toast', (_: unknown, e: { text: string }) => { w.toasts.push(e.text); return { value: undefined } })
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('turn.complete', () => ({ text: '' }))
  on('tool.call', { tool: 'AskUserQuestion' }, async (_: unknown, e: any) => {
    const label = await new Promise<string>(resolve => { w.answer = resolve })
    return { result: { questions: e.questions, answers: { [e.questions[0].question]: label } } }
  })
  let state: { value: unknown; version: number } = { value: undefined, version: 0 }
  on('state.get', () => ({ value: state }))
  on('state.set', (_: unknown, e: { value: unknown }) => {
    state = { value: e.value, version: state.version + 1 }
    return { value: { isSet: true, version: state.version } }
  })
  return { w, store, clock, offer: (over: object = {}) => { state = { value: { ...OFFER, ...over }, version: state.version + 1 } } }
}

test("the band's New conversation leaves this conversation as it is and keeps the handoff for a new one", async ($, on) => {
  const { w, store, clock, offer } = bandWorld(on)
  await $.session.start({ cwd: 'C:/work/project', surface: 'desktop', isInteractive: true })
  await clock.settle()                                           // the start's own look for a break is done
  offer()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' } as any)
  await ui.press({ key: 'switch' })
  await clock.advance(5_000)                                     // a /clear left on a timer would show up here
  await ui.unmount()
  expect(w.commands).toEqual([])                                 // no /clear
  const kept = store['handoff:c:/work/project|test-session'] as any
  expect(kept.from).toBe('test-session')
  expect(kept.text.endsWith('and then the docs')).toBe(true)
  expect(w.toasts.some(t => t.startsWith('This conversation stays as it is') && t.includes('paste the handoff, with your draft'))).toBe(true)
})

test('Details waits as long as you need: a choice made after 5 minutes still switches', async ($, on) => {
  const { w, store, clock, offer } = bandWorld(on)
  await $.session.start({ cwd: 'C:/work/project', surface: 'desktop', isInteractive: true })
  await clock.settle()                                           // the start's own look for a break is done
  offer()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' } as any)
  const pressed = ui.press({ key: 'details' })
  await clock.advance(5 * 60_000)
  w.answer('New conversation')
  await pressed
  await ui.unmount()
  expect(store['handoff:c:/work/project|test-session'] !== undefined).toBe(true)
})

test("Details answered after the break is over changes nothing, and says why: a turn's end takes the offer down", async ($, on) => {
  const { w, store, clock, offer } = bandWorld(on)
  await $.session.start({ cwd: 'C:/work/project', surface: 'desktop', isInteractive: true })
  await clock.settle()
  offer()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' } as any)
  const pressed = ui.press({ key: 'details' })
  await clock.advance(60_000)
  // a background task's report runs a turn while the card is open
  await $.turn.complete({ answer: 'Noted.', durationMs: 1000, isAborted: false, turnId: 't1', reason: 'answer' })
  w.answer('New conversation')
  await pressed
  await ui.unmount()
  expect(store['handoff:c:/work/project|test-session']).toBe(undefined)
  expect(w.toasts.some(t => t.startsWith('That card is out of date'))).toBe(true)
})

test('Details answered after its break gave way to a newer one changes nothing either', async ($, on) => {
  const { w, store, clock, offer } = bandWorld(on)
  await $.session.start({ cwd: 'C:/work/project', surface: 'desktop', isInteractive: true })
  await clock.settle()
  offer()
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' } as any)
  const pressed = ui.press({ key: 'details' })
  await clock.advance(1_000)                                     // the card is up
  offer({ last: 2 })                                             // a later break's offer
  w.answer('New conversation')
  await pressed
  await ui.unmount()
  expect(store['handoff:c:/work/project|test-session']).toBe(undefined)
  expect(w.toasts.some(t => t.startsWith('That card is out of date'))).toBe(true)
})

test('with no offer the band leaves the slot to the engine', async ($, on) => {
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' } as any)
  expect((await ui.find({ type: 'Text', text: /engine/ }))?.text).toBe('engine')
  await ui.unmount()
})
