import { expect, test } from 'claude-code/testing'

import { EARLIER, factsFrom, fixedPart, resumePrompt, withEarlier } from './handoff'
import { DEFAULTS, EXAMPLE_RATE, afterBreak, bandLines, classifyPacket, openPlanFlag, qualityFlags, statusLine } from './rule'

const HOUR = 3_600_000
const msg = (role: 'user' | 'assistant', text: string, toolUses: any[] = []) => ({ role, text, toolUses } as any)

test('no card while the cache is warm', async () => {
  const card = afterBreak(600_000, 30 * 60_000, [], DEFAULTS)
  expect(card.show).toBe(false)
})

test('after a break, a large conversation gets the card, and even the worst case saves money', async () => {
  const card = afterBreak(600_000, 2 * HOUR, [], DEFAULTS)
  expect(card.show).toBe(true)
  expect(card.options[0]).toBe('New conversation')
  expect(Math.round(card.numbers.saveNow * 100)).toBe(352)
  expect(card.numbers.floor > 0.5).toBe(true)
  expect(card.question.includes('Quality risk: low')).toBe(true)
})

test('below the worst-case size the card stays away, so a switch never loses money', async () => {
  const card = afterBreak(220_000, 2 * HOUR, [], DEFAULTS)
  expect(card.show).toBe(false)
})

test('when Claude Code prepared its own summary during the break, no card', async () => {
  const card = afterBreak(600_000, 2 * HOUR, [], DEFAULTS, { precomputed: true })
  expect(card.show).toBe(false)
  expect(card.note.includes('summary')).toBe(true)
})

test('a high quality risk makes staying the default choice', async () => {
  const flags = [{ key: 'failed-check', level: 'high' as const, text: 'the last build or test failed' }]
  const card = afterBreak(600_000, 2 * HOUR, flags, DEFAULTS)
  expect(card.show).toBe(true)
  expect(card.options[0]).toBe('Keep going here anyway')
  expect(card.options[1]).toBe('New conversation')
  expect(card.question.includes('quality risk is high')).toBe(true)
})

test('a high-risk packet gets only a note', async () => {
  const packet = classifyPacket('step7_web_fixes', '# (the Claude Code session that built it)\n...', ['step7_web_build'], 60)
  expect(packet.level).toBe('high')
  const card = afterBreak(600_000, 2 * HOUR, [], DEFAULTS, { packet })
  expect(card.show).toBe(false)
  expect(card.note.includes('No switch suggested')).toBe(true)
})

test('packet risk classes', async () => {
  expect(classifyPacket('rehearsal_x', '# Rehearsal\nRead first: `a.md`', ['step5_rules'], 600).level).toBe('low')
  expect(classifyPacket('claude_step7_e1', '# For the session; if you can\'t reach it, a new Claude Code session', ['claude_step7_a'], 600).level).toBe('medium')
  expect(classifyPacket('step7_backend_build', '# x', ['step7_backend_build'], 600).level).toBe('high')
  expect(classifyPacket('step7_new', '# x', [], 30 * 3600).level).toBe('high')
  expect(classifyPacket('step7_new', undefined, [], 0).level).toBe('high')
})

test('wrapping up closes the task list and carries its question', async () => {
  const f = factsFrom([
    msg('user', 'Build the thing'),
    msg('assistant', 'Working', [{ tool_use_id: '1', tool: 'mcp__ccd_turn__update_plan', input: { items: [{ content: 'a', status: 'in_progress' }, { content: 'b', status: 'pending' }] } }]),
    msg('assistant', 'Done. Shall I go on?', [{ tool_use_id: '2', tool: 'mcp__ccd_turn__wrap_up', input: { recap: 'Built it.', ask: 'Go on?' } }]),
  ])
  expect(f.plan.every(i => i.status === 'completed')).toBe(true)
  expect(f.lastAsk).toBe('Go on?')
  expect(f.asksYou).toBe(true)
  const flags = qualityFlags(f, 'yes')
  expect(flags.some(x => x.key === 'open-plan')).toBe(false)
  expect(flags.some(x => x.key === 'awaiting-answer')).toBe(true)
})

test('a workflow still running is a high risk; one that reported is medium', async () => {
  const running = factsFrom([
    msg('user', 'Review it'),
    msg('assistant', 'Started a review.', [{ tool_use_id: '1', tool: 'Workflow', input: {} }]),
  ])
  expect(qualityFlags(running, 'please follow PROMPT_x.md').find(x => x.key === 'background-running')?.level).toBe('high')
  const done = factsFrom([
    msg('user', 'Review it'),
    msg('assistant', 'Started a review.', [{ tool_use_id: '1', tool: 'Workflow', input: {} }]),
    msg('user', '<task-notification><output-file>C:/tmp/out.txt</output-file></task-notification>'),
    msg('assistant', 'The review is back.'),
  ])
  expect(done.backgroundRunning).toBe(0)
  expect(qualityFlags(done, 'please follow PROMPT_x.md').find(x => x.key === 'background-finished')?.level).toBe('medium')
  expect(done.workflowOutputs[0]).toBe('C:/tmp/out.txt')
})

test('messages that exist only in the chat are a high risk', async () => {
  const f = factsFrom([
    msg('user', 'please follow PROMPT_a.md'),
    msg('assistant', 'Done'),
    msg('user', 'Actually make the header blue and keep the old footer'),
    msg('assistant', 'OK'),
    msg('user', 'And rename the second section to Results'),
    msg('assistant', 'Renamed.'),
  ])
  expect(f.chatOnlyMessages).toBe(2)
  expect(qualityFlags(f, 'please follow PROMPT_b.md').find(x => x.key === 'chat-only')?.level).toBe('high')
})

test('the handoff ends with your new request', async () => {
  const f = factsFrom([msg('user', 'Build the thing'), msg('assistant', 'Built half of it.')])
  const text = resumePrompt(f, { cwd: 'C:/p', contextK: 600 }, 'Now finish it')
  expect(text.endsWith('Now finish it')).toBe(true)
  expect(text.includes('Built half of it.')).toBe(true)
  expect(text.includes('The original request (its first message, trimmed):')).toBe(true)
})

/** A compaction summary row laid out as Claude Code 2.1.317 writes one. */
const summaryRow = (sections: string) => 'This session is being continued from a previous conversation that ran out of context. ' +
  'The summary below covers the earlier portion of the conversation.\n\n<system-reminder>\n<tools-used-before-summary>Bash, Edit' +
  '</tools-used-before-summary>\n</system-reminder>\nSummary:\n' + sections +
  '\n\nIf you need specific details from before compaction (like exact code snippets, error messages, or content you generated), ' +
  'read the full transcript at: C:\\Users\\me\\.claude\\projects\\p\\abc.jsonl\n\nContinue the conversation from where it left off ' +
  'without asking the user any further questions. Resume directly.'
const SUMMARY_ROW = summaryRow('1. Primary Request and Intent:\n   Build the web packet in PROMPT_step9_web.md and test it.\n\n' +
  '2. Key Technical Concepts:\n   - the web build\n\n3. Files and Code Sections:\n   - web/app.ts (wrote PROMPT_next_session.md for later)\n\n' +
  '6. All user messages:\n   - "please follow PROMPT_step9_web.md"\n\n7. Pending Tasks:\n   - run the web tests\n\n' +
  '8. Current Work:\n   Fixing the login form validation in web/login.ts.\n\n9. Optional Next Step:\n   Run npm test in web/.')

test('after a compaction, the handoff quotes what the summary says was asked, is pending, is under way and comes next', async () => {
  const f = factsFrom([msg('user', SUMMARY_ROW), msg('user', 'go on'), msg('assistant', 'Done with step 3.')])
  expect(f.summary.startsWith('Primary Request and Intent: Build the web packet')).toBe(true)
  expect(f.summary.includes('Pending Tasks: - run the web tests')).toBe(true)
  expect(f.summary.includes('Current Work: Fixing the login form validation')).toBe(true)
  expect(f.summary.includes('Optional Next Step: Run npm test in web/.')).toBe(true)
  expect(f.summary.includes('Key Technical Concepts')).toBe(false)
  expect(f.firstPrompt).toBe('go on')
  expect(f.prompts).toBe(1)
  const text = resumePrompt(f, { cwd: 'C:/p', contextK: 300 }, 'Next step')
  for (const engineText of ['This session is being continued', 'tools-used-before-summary', 'read the full transcript', 'Continue the conversation from where']) {
    expect(text.includes(engineText)).toBe(false)
  }
  expect(text.includes('The original request (its first message')).toBe(false)
})

test('the packets you named before a compaction count as already worked on; ones it only wrote do not', async () => {
  const f = factsFrom([msg('user', SUMMARY_ROW), msg('assistant', 'OK')])
  expect(f.stems).toEqual(['step9_web'])
  expect(classifyPacket('step9_web', '# x', f.stems, 60).level).toBe('high')
  expect(classifyPacket('next_session', '# x', f.stems, 60).level).toBe('low')
})

test('summary headings in bold, with the colon inside, after or not at all, are sections too', async () => {
  for (const [a, b] of [[':**', '**'], ['**:', '**:'], ['**', '** (what was under way)'], ['**', '** (what was under way):']]) {
    const f = factsFrom([msg('user', summaryRow(`1. **Primary Request and Intent${a}\n   Build the web packet.\n\n` +
      `2. **Key Technical Concepts${a}\n   - the web build\n\n7. **Pending Tasks${a}\n   - run the web tests\n\n` +
      `8. **Current Work${b}\n   Fixing the login form.\n\n9. **Optional Next Step${a}\n   Run npm test.`)), msg('assistant', 'OK')])
    expect(f.summary).toBe('Primary Request and Intent: Build the web packet.\n\nPending Tasks: - run the web tests\n\n' +
      'Current Work: Fixing the login form.\n\nOptional Next Step: Run npm test.')
  }
})

test('a numbered line that only opens in bold is no heading; "**1. Title:**" is one', async () => {
  const f = factsFrom([msg('user', summaryRow('1. Primary Request and Intent:\n   Build it.\n\n' +
    '7. Pending Tasks:\n1. **Update the docs** in README.md\n2. **Run the tests** after that\n\n' +
    '8. Current Work:\n   Editing README.md, which now reads:\n```\n1. **Install** the plugin\n```\n\n' +
    '9. Optional Next Step:\n   1. **Next step for the parser**: none agreed yet\n   Run npm test.')), msg('assistant', 'OK')])
  expect(f.summary.includes('Pending Tasks: 1. **Update the docs** in README.md\n2. **Run the tests** after that')).toBe(true)
  expect(f.summary.includes('Current Work: Editing README.md, which now reads:\n```\n1. **Install** the plugin\n```')).toBe(true)
  expect(f.summary.endsWith('Run npm test.')).toBe(true)
  const g = factsFrom([msg('user', summaryRow('**1. Primary Request and Intent:**\n   Build it.\n\n**8. Current Work:**\n   Testing.')), msg('assistant', 'OK')])
  expect(g.summary).toBe('Primary Request and Intent: Build it.\n\nCurrent Work: Testing.')
  const k = factsFrom([msg('user', summaryRow('7. Pending Tasks:\n**1. Next step for the parser**: none agreed yet\n\n9. Optional Next Step:\n   Run npm test.')), msg('assistant', 'OK')])
  expect(k.summary).toBe('Pending Tasks: **1. Next step for the parser**: none agreed yet\n\nOptional Next Step: Run npm test.')
  const inline = factsFrom([msg('user', summaryRow('**1. Primary Request and Intent:** Build it.\n\n**8. Current Work:** Testing.')), msg('assistant', 'OK')])
  expect(inline.summary).toBe('Primary Request and Intent: Build it.\n\nCurrent Work: Testing.')
  const own = factsFrom([msg('user', summaryRow('**8. Current Work:**\n**Note**: the tests run.')), msg('assistant', 'OK')])
  expect(own.summary).toBe('Current Work: **Note**: the tests run.')
})

test("an earlier break's block goes before your new request, in a packet's handoff too", async () => {
  const resume = 'HEAD\n\nBefore you continue: read.\n\nMy next request:\n\nnew'
  expect(withEarlier(resume, ['old'], 'new')).toBe(`HEAD\n\nBefore you continue: read.\n\n${EARLIER}\n\nold\n\nMy next request:\n\nnew`)
  const packet = 'HEAD\nPacket: `p`. Read it first.\n\nPlease follow PROMPT_web.md.'
  expect(withEarlier(packet, ['old'], 'Please follow PROMPT_web.md.'))
    .toBe(`HEAD\nPacket: \`p\`. Read it first.\n\n${EARLIER}\n\nold\n\nMy next request:\n\nPlease follow PROMPT_web.md.`)
})

test('what a pasted handoff must start with: all of it before the requests at its end', async () => {
  expect(fixedPart('A\nB\n\nMy next request:\n\nx')).toBe('A B')
  expect(fixedPart(`A\n\n${EARLIER}\n\nold\n\nMy next request:\n\nx`)).toBe('A')
  expect(fixedPart('A\n\nPlease follow PROMPT_web.md.', ['Please follow PROMPT_web.md.'])).toBe('A')
  expect(fixedPart('A\n\nsomething')).toBe('A something')
})

test('a numbered item indented under a section is not taken for a heading', async () => {
  const f = factsFrom([msg('user', summaryRow('1. Primary Request and Intent:\n   Build it.\n   2. Next steps agreed: none yet.\n\n' +
    '7. Pending Tasks:\n   1. Tests: run them.\n   2. Docs: update them.\n\n9. Optional Next Step:\n   Run npm test.')), msg('assistant', 'OK')])
  expect(f.summary.includes('Pending Tasks: 1. Tests: run them.\n   2. Docs: update them.')).toBe(true)
  expect(f.summary.endsWith('Optional Next Step: Run npm test.')).toBe(true)
})

test('a summary without the numbered sections is quoted from its start, with the engine text taken out', async () => {
  const f = factsFrom([msg('user', summaryRow('We built the parser and started on the tests. The CSV reader is next.')), msg('assistant', 'OK')])
  expect(f.summary).toBe('We built the parser and started on the tests. The CSV reader is next.')
  const noHeading = 'This session is being continued from a previous conversation that ran out of context. The conversation is summarized below:\n' +
    '<tools-used-before-summary>Bash</tools-used-before-summary>\nWe built the parser.'
  expect(factsFrom([msg('user', noHeading)]).summary).toBe('We built the parser.')
})

test('the demo band reads exactly like a real one with the same numbers', async () => {
  const flags = [openPlanFlag(2)]
  const example = { ...DEFAULTS, usdPerPt: EXAMPLE_RATE }
  const card = afterBreak(640_000, 2.3 * HOUR, flags, example)
  expect(card.show).toBe(true)
  const { line1, line2 } = bandLines(card, 640_000, 2.3 * HOUR, flags, example)
  expect(line1).toBe('Back after 2.3 h: sending here re-caches 640K tokens ($4.77, 0.4–1.2% of your weekly limit).')
  expect(line2).toBe('A new conversation from a handoff saves about $3.84 now, typically $8.15–$14.87 by your next break ' +
    "(worst case $3.28). Quality risk is medium: 2 task-list items are still open; the list travels, the details of a half-done step don't.")
  const high = [{ key: 'failed-check', level: 'high' as const, text: 'the last build or test failed' }, openPlanFlag(1)]
  const risky = bandLines(afterBreak(640_000, 2.3 * HOUR, high, DEFAULTS), 640_000, 2.3 * HOUR, high, DEFAULTS, 'TEST · ')
  expect(risky.line1.startsWith('TEST · Back after 2.3 h')).toBe(true)
  expect(risky.line2.endsWith('but the quality risk is high: the last build or test failed.')).toBe(true)
})

test('until the weekly-limit rate is calibrated for the account, only dollars are shown', async () => {
  const flags = [openPlanFlag(2)]
  const card = afterBreak(640_000, 2.3 * HOUR, flags, DEFAULTS)
  expect(bandLines(card, 640_000, 2.3 * HOUR, flags, DEFAULTS).line1).toBe('Back after 2.3 h: sending here re-caches 640K tokens ($4.77).')
  expect(card.question.includes('weekly limit')).toBe(false)
})

test("prices follow the session's model, however the app names it", async () => {
  const resend = (model?: string) => Math.round(afterBreak(640_000, 2.3 * HOUR, [], DEFAULTS, { model }).numbers.resendHere * 100)
  expect(resend(undefined)).toBe(477)
  expect(resend('Opus 5.5')).toBe(477)
  expect(resend('claude-sonnet-5-5[1m]')).toBe(239)
  expect(resend('Sonnet 5.5')).toBe(239)
  expect(resend('claude-haiku-5-5')).toBe(60)
})

test('status line', async () => {
  expect(statusLine(100_000, 0, DEFAULTS)).toBe(undefined)
  expect(statusLine(640_000, 20 * 60_000, DEFAULTS)?.includes('warm 40 more min')).toBe(true)
  expect(statusLine(640_000, 2 * HOUR, DEFAULTS)?.includes('re-caches')).toBe(true)
  expect(statusLine(640_000, 2 * HOUR, DEFAULTS, true)?.includes('summary ready')).toBe(true)
})
