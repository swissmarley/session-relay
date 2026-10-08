// Session Relay context meter: a small mod (a hooks module, see hooks.json).
//
// Plugins cannot set statusLine, and the transcript fallback cannot tell a 1M-token
// context window from a 200k one. Claude Code knows both exactly, so this mod copies
// $.session.usage().context to
//   <config dir>/session-relay/meter/<session_id>/<epoch ms>-<random>.json
// (config dir: CLAUDE_CONFIG_DIR, else ~/.claude), which relay.py reads before the
// status line state and the transcript estimate.
//
// Each reading is a new file, never an overwrite. The mods API has no rename, and
// $.fs.write replaces a file's content in place, so a reader could meet a half-written
// file; with one file per reading it can always fall back to the previous, complete
// one. relay.py reads the newest file that parses and prunes the older ones.
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
// session: writeMeter() swallows its own errors and every hook then calls next(e). The
// hooks have no .catch handler on purpose: a hook that fails anyway is skipped and the
// next handler runs in its place (fail open), which is what the relay needs.

const METER_DIR = '/session-relay/meter/'

// Same rule as relay.py's safe_id(): the file name relay.py looks for.
export function safeId(value) {
  return String(value || 'unknown').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 128)
}

// One reading in the shape relay.py's meter files use. A figure Claude Code does not
// have yet (a fresh or just-compacted session) is written as null, never guessed.
export function meterRecord(sessionId, context, now = new Date(), model = null) {
  const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  const window = num(context?.window)
  return {
    session_id: sessionId,
    model: typeof model === 'string' && model ? model : null,
    used_pct: num(context?.percent),
    window_size: window !== null && window > 0 ? window : null,
    input_total: num(context?.tokens),
    ts: now.toISOString().replace(/\.\d+Z$/, 'Z'),
    source: 'mod',
    approx: false,
  }
}

// Claude Code's config folder as relay.py resolves it: CLAUDE_CONFIG_DIR, else
// <home>/.claude with home = HOME, else USERPROFILE (Windows).
async function configDir($) {
  const dir = await $.env.get('CLAUDE_CONFIG_DIR')
  if (dir) return dir.replace(/[\\/]+$/, '')
  const home = (await $.env.get('HOME')) || (await $.env.get('USERPROFILE'))
  return home ? home.replace(/[\\/]+$/, '') + '/.claude' : null
}

// A file name that sorts by time and never collides: 13-digit epoch ms, random suffix.
export function readingName(now = new Date()) {
  const bytes = new Uint8Array(4)
  crypto.getRandomValues(bytes)
  const rand = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return String(now.getTime()).padStart(13, '0') + '-' + rand + '.json'
}

export async function writeMeter($, sessionId) {
  try {
    const id = sessionId || (await $.session.id())
    const dir = await configDir($)
    if (!id || !dir) return null
    const { context } = await $.session.usage()
    let model = null
    try {
      model = await $.session.model()
    } catch {
      // the model name is informative only
    }
    const now = new Date()
    const path = dir + METER_DIR + safeId(id) + '/' + readingName(now)
    await $.fs.write(path, JSON.stringify(meterRecord(id, context, now, model)) + '\n')
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
  })

  on('classic.UserPromptSubmit', async ($, e, next) => {
    await writeMeter($, e.session_id)
    return next(e)
  })

  on('classic.Stop', async ($, e, next) => {
    await writeMeter($, e.session_id)
    return next(e)
  })
}
