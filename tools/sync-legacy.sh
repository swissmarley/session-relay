#!/bin/sh
# Copy the plugin's scripts over the project-copy install in .claude/relay.
#
# plugins/session-relay/scripts/ is the source of truth; .claude/relay/ keeps
# identical copies for install.sh and for users who copy that folder into a
# project. tests/test_plugin_layout.py fails when the two drift apart.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
src="$root/plugins/session-relay/scripts"
dst="$root/.claude/relay"
for f in relay.py launch.sh statusline.sh; do
  cp "$src/$f" "$dst/bin/$f"
done
cp "$src/handoff-template.md" "$dst/handoff-template.md"
echo "synced $src -> $dst (config.json is kept separately)"
cp "$root/plugins/session-relay/hooks/meter.mjs" "$root/tests/mod/hooks/meter.mjs"
echo "synced meter.mjs -> tests/mod/hooks (mod test harness)"
