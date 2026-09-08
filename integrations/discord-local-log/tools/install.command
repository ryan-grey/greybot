#!/bin/bash
# One-click install for discord-local-log. Double-click in Finder.
#
# Builds Vencord from source with the DiscordLocalLog user plugin linked
# in, points Discord.app at that build, and loads the ingest launch agent.
# Re-run any time (after a Discord update, after pulling Vencord).

set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$HOME/.local/discord-local-log/Vencord"
PY="$HOME/.local/discord-mcp/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
PLIST="com.ryangrey.discord-local-log.plist"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

step "Toolchain"
command -v node >/dev/null || { echo "node not found (brew install node)"; exit 1; }
command -v pnpm >/dev/null || npm i -g pnpm
node --version; pnpm --version

step "Vencord source at $BUILD"
if [ ! -d "$BUILD/.git" ]; then
    git clone --depth 1 https://github.com/Vendicated/Vencord.git "$BUILD"
fi
# Copy, not symlink: esbuild resolves Vencord's @utils/@webpack aliases
# relative to a file's real path, so a symlink outside src/ fails to build.
mkdir -p "$BUILD/src/userplugins"
rm -rf "$BUILD/src/userplugins/discordLocalLog"
cp -R "$REPO/plugin/discordLocalLog" "$BUILD/src/userplugins/discordLocalLog"
ls -la "$BUILD/src/userplugins/discordLocalLog/"

step "pnpm install + build (a few minutes the first time)"
cd "$BUILD"
pnpm install --frozen-lockfile
pnpm build
[ -f dist/patcher.js ] || { echo "build produced no dist/patcher.js"; exit 1; }

step "Quit Discord"
if pgrep -x Discord >/dev/null; then
    osascript -e 'tell application "Discord" to quit' || true
    for _ in $(seq 1 30); do pgrep -x Discord >/dev/null || break; sleep 1; done
    pgrep -x Discord >/dev/null && { echo "Discord is still running; quit it and re-run"; exit 1; }
fi

step "Patch Discord.app"
"$PY" "$REPO/tools/patch_discord.py" --build "$BUILD"

step "Ingest launch agent"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Application Support/discord-local-log"
cp "$REPO/launchd/$PLIST" "$HOME/Library/LaunchAgents/$PLIST"
launchctl bootout "gui/$(id -u)/com.ryangrey.discord-local-log" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$PLIST"
launchctl print "gui/$(id -u)/com.ryangrey.discord-local-log" | grep -E "state|program" | head -3

step "Relaunch Discord"
open -a Discord

cat <<EOF

Done. In Discord: Settings > Vencord > Plugins > enable "DiscordLocalLog".
Then open a DM or scroll a channel; within seconds
  $HOME/Library/Application Support/discord-local-log/events.jsonl
starts filling and the launch agent folds it into discord.db.

Press return to close this window.
EOF
read -r _

