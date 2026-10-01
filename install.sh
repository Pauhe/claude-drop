#!/bin/sh
# Install the drop CLI and the Claude Code skill for the invoking user.
#
# Every path comes from $HOME: this runs on any VM, as whichever user runs
# Claude Code there. DROP_URL names your drop service:
#
#   curl -fsSL https://drop.example.ts.net/install.sh | DROP_URL=https://drop.example.ts.net sh
set -eu

SOURCE_URL="${DROP_URL:?set DROP_URL to your drop service, e.g. https://drop.example.ts.net}"
SOURCE_URL="${SOURCE_URL%/}"
BIN_DIR="$HOME/.local/bin"
SKILL_DIR="$HOME/.claude/skills/claude-drop"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "drop: needs Python 3.11 or newer; found $(python3 --version 2>&1)" >&2
    exit 1
}

mkdir -p "$BIN_DIR" "$SKILL_DIR"

if [ -f "./cli/drop" ] && [ -f "./skill/SKILL.md" ]; then
    cp ./cli/drop "$BIN_DIR/drop"
    cp ./skill/SKILL.md "$SKILL_DIR/SKILL.md"
else
    curl -fsSL "$SOURCE_URL/cli/drop" -o "$BIN_DIR/drop"
    curl -fsSL "$SOURCE_URL/skill/SKILL.md" -o "$SKILL_DIR/SKILL.md"
fi
chmod +x "$BIN_DIR/drop"

mkdir -p "$HOME/.config/claude-drop"
printf 'url=%s\n' "$SOURCE_URL" > "$HOME/.config/claude-drop/config"

echo "drop: installed $BIN_DIR/drop"
echo "drop: installed $SKILL_DIR/SKILL.md"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "drop: WARNING — $BIN_DIR is not on PATH, so Claude will not find"
       echo "     the command. Add it to your shell profile, then open a new shell." ;;
esac

if ! "$BIN_DIR/drop" list --limit 1 >/dev/null 2>&1; then
    echo "drop: WARNING — cannot reach $SOURCE_URL. Check tailnet connectivity."
fi

echo
echo "drop: a Claude Code session started before now will not see the skill."
echo "      Start a new session on this machine."
