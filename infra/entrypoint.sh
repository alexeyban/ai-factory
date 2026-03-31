#!/bin/bash
# Container entrypoint: sets up a writable ~/.claude/ with credentials from the
# read-only host mount and a permissive settings.json for automated agent use.
set -e

CLAUDE_DIR=/root/.claude
CLAUDE_HOST_DIR=/root/.claude-host

mkdir -p "$CLAUDE_DIR"

# Copy OAuth credentials from host mount (read-only) into writable container dir
if [ -f "$CLAUDE_HOST_DIR/.credentials.json" ]; then
    cp "$CLAUDE_HOST_DIR/.credentials.json" "$CLAUDE_DIR/.credentials.json"
elif [ -n "$ANTHROPIC_API_KEY" ]; then
    echo "[entrypoint] No host credentials found — relying on ANTHROPIC_API_KEY"
else
    echo "[entrypoint] WARNING: no ~/.claude/.credentials.json and no ANTHROPIC_API_KEY — claude CLI may fail auth"
fi

# Write container-appropriate settings: allow all tools needed by agents.
# --dangerously-skip-permissions handles interactive prompts; this provides
# the baseline permission list for non-interactive (-p) subprocess calls.
cat > "$CLAUDE_DIR/settings.json" << 'EOF'
{
  "permissions": {
    "allow": [
      "Bash(*)",
      "Read",
      "Write",
      "Edit",
      "Glob",
      "Grep",
      "WebFetch",
      "WebSearch"
    ],
    "deny": []
  }
}
EOF

echo "[entrypoint] ~/.claude/ ready (tools: Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch)"

exec "$@"
