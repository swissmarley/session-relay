// Tests for the meter mod (hooks/meter.mjs). Run from the plugin directory:
//   claude plugin test
import { expect, test } from 'claude-code/testing'
import { meterRecord, safeId } from '../hooks/meter.mjs'

const ONE_M = { tokens: 420000, window: 1000000, percent: 42 }

type Write = { path: string; text: string }

// Stubs shared by most tests: the usage figures, the environment and a recorded fs.write.
function stubs(on, opts: { context?: object; env?: Record<string, string>; sessionId?: string } = {}) {
  const writes: Write[] = []
  const env = opts.env ?? { HOME: '/home/dev' }
  on('session.usage', () => ({ value: { context: opts.context ?? ONE_M, rateLimits: [] } }))
  on('session.id', () => ({ value: opts.sessionId ?? 'sess-from-api' }))
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('fs.write', ($, e) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  return writes
}

test('classic.Stop writes the exact reading before the Stop settings hooks run', async ($, on) => {
  const writes = stubs(on)
  let writesWhenSettingsHooksRan = -1
  on('classic.Stop', () => {
    writesWhenSettingsHooksRan = writes.length
    return {}
  })

  await $.classic.Stop({ session_id: 'sess-1', hook_event_name: 'Stop', stop_hook_active: false })

  expect(writesWhenSettingsHooksRan).toBe(1)
  expect(writes[0].path).toBe('/home/dev/.claude/session-relay/meter/sess-1.json')
  const record = JSON.parse(writes[0].text)
  expect(record).toMatchObject({
    session_id: 'sess-1', used_pct: 42, window_size: 1000000, input_total: 420000,
    source: 'mod', approx: false,
  })
  expect(record.ts).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
})

test('classic.UserPromptSubmit writes before the prompt hooks run', async ($, on) => {
  const writes = stubs(on)
  let seen = -1
  on('classic.UserPromptSubmit', () => {
    seen = writes.length
    return {}
  })

  await $.classic.UserPromptSubmit({ session_id: 'sess-2', hook_event_name: 'UserPromptSubmit', prompt: 'go on' })

  expect(seen).toBe(1)
  expect(writes[0].path).toBe('/home/dev/.claude/session-relay/meter/sess-2.json')
})

test('turn.complete writes for the main conversation only', async ($, on) => {
  const writes = stubs(on, { sessionId: 'sess-main' })
  on('turn.complete', () => ({ text: '' }))

  await $.turn.complete({ turnId: 't1', answer: 'done', durationMs: 5, isAborted: false, usage: null })
  await $.turn.complete({ turnId: 't2', answer: 'sub', durationMs: 5, isAborted: false, usage: null, agentId: 'agent-1' })

  expect(writes.length).toBe(1)
  expect(writes[0].path).toBe('/home/dev/.claude/session-relay/meter/sess-main.json')
})

test('a window without a percentage yet is written as null, never guessed', async ($, on) => {
  const writes = stubs(on, { context: { window: 1000000 } })
  on('classic.Stop', () => ({}))

  await $.classic.Stop({ session_id: 'sess-3', hook_event_name: 'Stop' })

  expect(JSON.parse(writes[0].text)).toMatchObject({ used_pct: null, window_size: 1000000, input_total: null })
})

test('USERPROFILE is used when HOME is not set', async ($, on) => {
  // A trailing separator is dropped. (The kit resolves non-POSIX paths against the
  // working directory, so a POSIX path stands in for C:\Users\dev here.)
  const writes = stubs(on, { env: { USERPROFILE: '/users/dev/' } })
  on('classic.Stop', () => ({}))

  await $.classic.Stop({ session_id: 'sess-4', hook_event_name: 'Stop' })

  expect(writes[0].path).toBe('/users/dev/.claude/session-relay/meter/sess-4.json')
})

test('a failing write never blocks the Stop hooks', async ($, on) => {
  on('session.usage', () => ({ value: { context: ONE_M, rateLimits: [] } }))
  on('env.get', () => ({ value: '/home/dev' }))
  on('fs.write', () => ({ deny: 'read-only file system' }))
  let settingsHooksRan = false
  on('classic.Stop', () => {
    settingsHooksRan = true
    return {}
  })

  await $.classic.Stop({ session_id: 'sess-5', hook_event_name: 'Stop' })

  expect(settingsHooksRan).toBe(true)
})

test('file names follow relay.py safe_id()', () => {
  expect(safeId('../../etc/passwd')).toBe('.._.._etc_passwd')
  expect(safeId('')).toBe('unknown')
  expect(safeId('a'.repeat(200)).length).toBe(128)
})

test('meterRecord keeps only finite numbers', () => {
  const r = meterRecord('s', { window: 0, percent: Number.NaN, tokens: 5 }, new Date('2026-10-08T12:00:00.123Z'))
  expect(r).toEqual({
    session_id: 's', used_pct: null, window_size: null, input_total: 5,
    ts: '2026-10-08T12:00:00Z', source: 'mod', approx: false,
  })
})
