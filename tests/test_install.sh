#!/bin/sh
# The installer must respect $HOME and leak no hard-coded path.
set -eu
FAKE_HOME="$(mktemp -d)"
trap 'rm -rf "$FAKE_HOME"' EXIT

HOME="$FAKE_HOME" DROP_URL="http://127.0.0.1:9" sh ./install.sh >"$FAKE_HOME/log" 2>&1 \
    || { echo "FAIL: installer exited non-zero"; cat "$FAKE_HOME/log"; exit 1; }

test -x "$FAKE_HOME/.local/bin/drop" || { echo "FAIL: CLI not installed"; exit 1; }
test -f "$FAKE_HOME/.claude/skills/claude-drop/SKILL.md" || {
    echo "FAIL: skill not installed"; exit 1; }
grep -q "cannot reach" "$FAKE_HOME/log" || {
    echo "FAIL: unreachable service did not warn"; cat "$FAKE_HOME/log"; exit 1; }
grep -qx 'url=http://127.0.0.1:9' "$FAKE_HOME/.config/claude-drop/config" || {
    echo "FAIL: service URL not recorded"; exit 1; }
if env -u DROP_URL HOME="$FAKE_HOME" sh ./install.sh >/dev/null 2>&1; then
    echo "FAIL: installer ran without DROP_URL"; exit 1
fi
if grep -rqE '/home/[a-z]' "$FAKE_HOME/.local/bin/drop" \
        "$FAKE_HOME/.claude/skills/claude-drop/SKILL.md"; then
    echo "FAIL: a hard-coded home directory leaked into the install"
    exit 1
fi
echo "PASS"
