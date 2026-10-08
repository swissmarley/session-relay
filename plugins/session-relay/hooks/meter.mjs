// Session Relay context meter: a small mod (a hooks module, see hooks.json).
//
// Plugins cannot set statusLine, and the transcript fallback cannot tell a 1M-token
// context window from a 200k one. Claude Code knows both exactly, so this mod copies
// $.session.usage().context to ~/.claude/session-relay/meter/<session_id>.json, which
// relay.py reads before the status line state and the transcript estimate.
//
// When it writes:
//   * after every turn of the main conversation (turn.complete);
//   * right before the Stop and UserPromptSubmit settings hooks run: a classic.<Event>
//     hook wraps the settings hooks of that event, so relay.py's `stop` and `prompt`
//     always see a reading taken a moment earlier.
//
// Needs Claude Code v2.1.287 or later in the terminal (v2.1.286 in the Desktop app),
// where mods are on by default. Older versions skip the module; the relay then falls
// back to the status line state and the transcript. A meter problem never blocks the
// session: every hook swallows its own errors and passes the event on.

const METER_DIR = '/.claude/session-relay/meter/'

// Same rule as relay.py's safe_id(): the file name relay.py looks for.
export function safeId(value) {
  return String(value || 'unknown').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 128)
}

// One reading in the shape relay.py's meter files use. A figure Claude Code does not
// have yet (a fresh or just-compacted session) is written as null, never guessed.
export function meterRecord(sessionId, context, now = new Date()) {
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  const window = num(context?.window)
  return {
    session_id: sessionId,
    used_pct: num(context?.percent),
    window_size: window !== null && window > 0 ? window : null,
    input_total: num(context?.tokens),
    ts: now.toISOString().replace(/\.\d+Z$/, 'Z'),
    source: 'mod',
    approx: false,
  }
}

// The home folder as relay.py resolves it: HOME, else USERPROFILE (Windows).
async function homeDir($) {
  const home = await $.env.get('HOME')
  if (home) return home
  return (await $.env.get('USERPROFILE')) || null
}

export async function writeMeter($, sessionId) {
  try {
    const id = sessionId || (await $.session.id())
    const home = await homeDir($)
    if (!id || !home) return null
    const { context } = await $.session.usage()
    const path = home.replace(/[\\/]+$/, '') + METER_DIR + safeId(id) + '.json'
    await $.fs.write(path, JSON.stringify(meterRecord(id, context)) + '\n')
    return path
  } catch {
    return null // relay.py falls back to the status line state and the transcript
  }
}

export function register(on) {
  on('turn.complete', async ($, e, next) => {
    // A subagent's turn has its own window; the relay measures the main conversation.
    if (!e.agentId) await writeMeter($)
    return next(e)
  }).catch(() => undefined)

  on('classic.UserPromptSubmit', async ($, e, next) => {
    await writeMeter($, e.session_id)
    return next(e)
  }).catch(() => undefined)

  on('classic.Stop', async ($, e, next) => {
    await writeMeter($, e.session_id)
    return next(e)
  }).catch(() => undefined)
}
