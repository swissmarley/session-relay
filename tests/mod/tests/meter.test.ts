// Tests for the meter mod (plugins/session-relay/hooks/meter.mjs). This folder is a
// test harness, not shipped: hooks/meter.mjs here is a byte copy kept in sync by
// tools/sync-legacy.sh (the test kit refuses imports from outside the mod's folder).
//   claude plugin test tests/mod
import { expect, test } from 'claude-code/testing'
import { meterRecord, readingName, safeId } from '../hooks/meter.mjs'

const ONE_M = { tokens: 420000, window: 1000000, percent: 42 }
const NAME = /^\d{13}-[0-9a-f]{8}\.json$/

type Write = { path: string; text: string }

// Stubs shared by most tests: usage figures, model, environment and a recorded fs.write.
function stubs(on, opts: { context?: object; env?: Record<string, string>; sessionId?: string } = {}) {
  const writes: Write[] = []
  const env = opts.env ?? { HOME: '/home/dev' }
  on('session.usage', () => ({ value: { context: opts.context ?? ONE_M, rateLimits: [] } }))
  on('session.id', () => ({ value: opts.sessionId ?? 'sess-from-api' }))
  on('session.model', () => ({ value: 'claude-opus-5-5[1m]' }))
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('fs.write', ($, e) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  return writes
}

function inDir(path: string, dir: string) {
  expect(path.startsWith(dir)).toBe(true)
  expect(path.slice(dir.length)).toMatch(NAME)
}

test('classic.Stop writes a new reading file before the Stop settings hooks run', async ($, on) => {
  const writes = stubs(on)
  let writesWhenSettingsHooksRan = -1
  on('classic.Stop', () => {
    writesWhenSettingsHooksRan = writes.length
    return {}
  })

  await $.classic.Stop({ session_id: 'sess-1', hook_event_name: 'Stop', stop_hook_active: false })

  expect(writesWhenSettingsHooksRan).toBe(1)
  inDir(writes[0].path, '/home/dev/.claude/session-relay/meter/sess-1/')
  const record = JSON.parse(writes[0].text)
  expect(record).toMatchObject({
    session_id: 'sess-1', model: 'claude-opus-5-5[1m]', used_pct: 42, window_size: 1000000,
    input_total: 420000, source: 'mod', approx: false,
  })
  expect(record.ts).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
})

test('each write goes to its own file, never over the previous one', async ($, on) => {
  const writes = stubs(on)
  on('classic.Stop', () => ({}))
  await $.classic.Stop({ session_id: 'sess-1', hook_event_name: 'Stop' })
  await $.classic.Stop({ session_id: 'sess-1', hook_event_name: 'Stop' })
  expect(writes.length).toBe(2)
  expect(writes[0].path === writes[1].path).toBe(false)
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
  inDir(writes[0].path, '/home/dev/.claude/session-relay/meter/sess-2/')
})

test('turn.complete writes for the main conversation only', async ($, on) => {
  const writes = stubs(on, { sessionId: 'sess-main' })
  on('turn.complete', () => ({ text: '' }))

  await $.turn.complete({ turnId: 't1', answer: 'done', durationMs: 5, isAborted: false, usage: null })
  await $.turn.complete({ turnId: 't2', answer: 'sub', durationMs: 5, isAborted: false, usage: null, agentId: 'agent-1' })

  expect(writes.length).toBe(1)
  inDir(writes[0].path, '/home/dev/.claude/session-relay/meter/sess-main/')
})

test('a window without a percentage yet is written as null, never guessed', async ($, on) => {
  const writes = stubs(on, { context: { window: 1000000 } })
  on('classic.Stop', () => ({}))

  await $.classic.Stop({ session_id: 'sess-3', hook_event_name: 'Stop' })

  expect(JSON.parse(writes[0].text)).toMatchObject({ used_pct: null, window_size: 1000000, input_total: null })
})

test('CLAUDE_CONFIG_DIR wins, then HOME, then USERPROFILE', async ($, on) => {
  // A trailing separator is dropped. (The kit resolves non-POSIX paths against the
  // working directory, so POSIX paths stand in for Windows ones here.)
  const env: Record<string, string> = { CLAUDE_CONFIG_DIR: '/cfg/claude/', HOME: '/home/dev', USERPROFILE: '/users/dev' }
  const writes = stubs(on, { env })
  on('classic.Stop', () => ({}))

  await $.classic.Stop({ session_id: 's', hook_event_name: 'Stop' })
  delete env.CLAUDE_CONFIG_DIR
  await $.classic.Stop({ session_id: 's', hook_event_name: 'Stop' })
  delete env.HOME
  await $.classic.Stop({ session_id: 's', hook_event_name: 'Stop' })

  inDir(writes[0].path, '/cfg/claude/session-relay/meter/s/')
  inDir(writes[1].path, '/home/dev/.claude/session-relay/meter/s/')
  inDir(writes[2].path, '/users/dev/.claude/session-relay/meter/s/')
})

test('a failing write never blocks the Stop hooks', async ($, on) => {
  on('session.usage', () => ({ value: { context: ONE_M, rateLimits: [] } }))
  on('session.model', () => ({ value: 'm' }))
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

test('when the meter itself throws, the next handlers still run', async ($, on) => {
  // Every API call the meter makes rejects; the relay's own hooks must run regardless.
  on('env.get', () => ({ deny: 'no environment' }))
  on('session.usage', () => ({ deny: 'no usage' }))
  on('session.id', () => ({ deny: 'no id' }))
  let stopRan = false
  let promptRan = false
  let turnRan = false
  on('classic.Stop', () => {
    stopRan = true
    return {}
  })
  on('classic.UserPromptSubmit', () => {
    promptRan = true
    return {}
  })
  on('turn.complete', () => {
    turnRan = true
    return { text: '' }
  })

  await $.classic.Stop({ session_id: 'sess-6', hook_event_name: 'Stop' })
  await $.classic.UserPromptSubmit({ session_id: 'sess-6', hook_event_name: 'UserPromptSubmit', prompt: 'x' })
  await $.turn.complete({ turnId: 't', answer: '', durationMs: 1, isAborted: false, usage: null })

  expect([stopRan, promptRan, turnRan]).toEqual([true, true, true])
})

test('file names follow relay.py safe_id() and sort by time', () => {
  expect(safeId('../../etc/passwd')).toBe('.._.._etc_passwd')
  expect(safeId('')).toBe('unknown')
  expect(safeId('a'.repeat(200)).length).toBe(128)
  const a = readingName(new Date(1000))
  const b = readingName(new Date(2000000000000))
  expect(a).toMatch(NAME)
  expect(a < b).toBe(true)
})

test('meterRecord keeps only finite numbers', () => {
  const r = meterRecord('s', { window: 0, percent: Number.NaN, tokens: 5 }, new Date('2026-10-08T12:00:00.123Z'))
  expect(r).toEqual({
    session_id: 's', model: null, used_pct: null, window_size: null, input_total: 5,
    ts: '2026-10-08T12:00:00Z', source: 'mod', approx: false,
  })
})
