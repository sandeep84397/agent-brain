#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Agent Brain — Setup Script
# Installs the agent-brain MCP server and configures Claude Code agent team.
# ============================================================================

BRAIN_DIR="${AGENT_BRAIN_DIR:-$HOME/.agent-brain}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "============================================"
echo "  Agent Brain — Setup"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Python venv + dependencies
# ---------------------------------------------------------------------------
echo "[1/5] Setting up Python environment..."

if [ ! -d "$BRAIN_DIR/.venv" ]; then
    python3 -m venv "$BRAIN_DIR/.venv"
    echo "  Created venv at $BRAIN_DIR/.venv"
else
    echo "  Venv already exists at $BRAIN_DIR/.venv"
fi

"$BRAIN_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$BRAIN_DIR/.venv/bin/pip" install --quiet mcp networkx
echo "  Dependencies installed."

# ---------------------------------------------------------------------------
# Step 2: Copy server
# ---------------------------------------------------------------------------
echo ""
echo "[2/5] Installing brain server..."

cp "$SCRIPT_DIR/brain/server.py" "$BRAIN_DIR/server.py"
echo "  Copied server.py to $BRAIN_DIR/"

# ---------------------------------------------------------------------------
# Step 3: Config
# ---------------------------------------------------------------------------
echo ""
echo "[3/5] Configuring..."

if [ ! -f "$BRAIN_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/brain/config.example.json" "$BRAIN_DIR/config.json"
    echo "  Created config.json from template."
    echo "  IMPORTANT: Edit $BRAIN_DIR/config.json with your repo paths and team."
else
    echo "  config.json already exists. Skipping."
fi

# ---------------------------------------------------------------------------
# Step 4: Register MCP server with Claude Code
# ---------------------------------------------------------------------------
echo ""
echo "[4/5] Registering MCP server..."

if command -v claude &>/dev/null; then
    # Remove existing registration if present (idempotent)
    claude mcp remove agent-brain 2>/dev/null || true
    claude mcp add --transport stdio --scope user agent-brain -- \
        "$BRAIN_DIR/.venv/bin/python" "$BRAIN_DIR/server.py"
    echo "  Registered agent-brain MCP (global scope)."
else
    echo "  WARNING: 'claude' CLI not found. Register manually:"
    echo "  claude mcp add --transport stdio --scope user agent-brain -- \\"
    echo "      $BRAIN_DIR/.venv/bin/python $BRAIN_DIR/server.py"
fi

# ---------------------------------------------------------------------------
# Step 5: Install agent templates
# ---------------------------------------------------------------------------
echo ""
echo "[5/5] Installing agent templates..."

AGENTS_DIR="$HOME/.claude/agents"
mkdir -p "$AGENTS_DIR"

INSTALLED=0
for template in "$SCRIPT_DIR"/agents/*.md; do
    [ -f "$template" ] || continue
    filename="$(basename "$template")"
    if [ ! -f "$AGENTS_DIR/$filename" ]; then
        cp "$template" "$AGENTS_DIR/$filename"
        INSTALLED=$((INSTALLED + 1))
    fi
done

if [ "$INSTALLED" -gt 0 ]; then
    echo "  Installed $INSTALLED agent template(s) to $AGENTS_DIR/"
    echo "  IMPORTANT: Customize agent names, paths, and stack in each .md file."
else
    echo "  Agent templates already exist. Skipping. (Delete to reinstall.)"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo "  Setup complete!"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Edit $BRAIN_DIR/config.json with your repo paths"
echo "  2. Customize agent files in $AGENTS_DIR/"
echo "  3. Restart Claude Code"
echo "  4. Test: ask any agent to call brain_stats()"
echo ""
echo "Docs: $SCRIPT_DIR/docs/"
echo ""
