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
echo "[1/6] Setting up Python environment..."

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
echo "[2/6] Installing brain server..."

mkdir -p "$BRAIN_DIR"
cp "$SCRIPT_DIR/brain/server.py" "$BRAIN_DIR/server.py"
echo "  Copied server.py to $BRAIN_DIR/"

# ---------------------------------------------------------------------------
# Step 3: Config
# ---------------------------------------------------------------------------
echo ""
echo "[3/6] Configuring..."

if [ ! -f "$BRAIN_DIR/config.json" ]; then
    echo ""
    echo "  No config.json found. Let's set up your repos."
    echo ""

    # Collect repo paths interactively (or create from template)
    REPOS_JSON="{"
    REPO_COUNT=0
    while true; do
        read -rp "  Repo name (e.g. my-backend) [enter to skip]: " REPO_NAME
        [ -z "$REPO_NAME" ] && break
        read -rp "  Absolute path for '$REPO_NAME': " REPO_PATH
        if [ -n "$REPO_PATH" ]; then
            [ "$REPO_COUNT" -gt 0 ] && REPOS_JSON+=","
            REPOS_JSON+="\"$REPO_NAME\":\"$REPO_PATH\""
            REPO_COUNT=$((REPO_COUNT + 1))
        fi
    done
    REPOS_JSON+="}"

    if [ "$REPO_COUNT" -eq 0 ]; then
        cp "$SCRIPT_DIR/brain/config.example.json" "$BRAIN_DIR/config.json"
        echo "  Created config.json from template (no repos added)."
        echo "  IMPORTANT: Edit $BRAIN_DIR/config.json with your repo paths."
    else
        cat > "$BRAIN_DIR/config.json" <<CONFIGEOF
{
  "repos": $REPOS_JSON,
  "team": []
}
CONFIGEOF
        echo "  Created config.json with $REPO_COUNT repo(s)."
        echo "  TIP: Add team members later: edit $BRAIN_DIR/config.json"
    fi
else
    echo "  config.json already exists. Skipping."
fi

# ---------------------------------------------------------------------------
# Step 4: Register MCP server with Claude Code
# ---------------------------------------------------------------------------
echo ""
echo "[4/6] Registering MCP server..."

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
# Step 4b: Install enforcement hook
# ---------------------------------------------------------------------------
echo ""
echo "[4b] Installing brain protocol enforcement hook..."

SETTINGS_FILE="$HOME/.claude/settings.json"
HOOK_SCRIPT="$SCRIPT_DIR/brain/hooks/enforce_brain_protocol.py"

if [ -f "$HOOK_SCRIPT" ]; then
    if [ -f "$SETTINGS_FILE" ]; then
        # Check if hook already installed
        if grep -q "enforce_brain_protocol" "$SETTINGS_FILE" 2>/dev/null; then
            echo "  Enforcement hook already installed. Skipping."
        else
            echo "  NOTE: Add this PreToolUse hook to $SETTINGS_FILE to enforce brain protocol:"
            echo "  {\"hooks\": {\"PreToolUse\": [{\"matcher\": \"Edit|Write\", \"hooks\": [{\"type\": \"command\", \"command\": \"python3 $HOOK_SCRIPT\", \"timeout\": 5000}]}]}}"
            echo "  This blocks code edits unless log_decision was called first."
            echo "  Set BRAIN_SKIP_ENFORCE=1 in env to bypass for your own direct edits."
        fi
    else
        echo "  WARNING: $SETTINGS_FILE not found. Create it and add the hook manually."
    fi
else
    echo "  WARNING: Hook script not found at $HOOK_SCRIPT"
fi

# ---------------------------------------------------------------------------
# Step 5: Install agent templates
# ---------------------------------------------------------------------------
echo ""
echo "[5/6] Installing agent templates..."

AGENTS_DIR="$HOME/.claude/agents"
mkdir -p "$AGENTS_DIR"

# Check if user already has agent files
EXISTING_AGENTS=0
for template in "$SCRIPT_DIR"/agents/*.md; do
    [ -f "$template" ] || continue
    filename="$(basename "$template")"
    [ -f "$AGENTS_DIR/$filename" ] && EXISTING_AGENTS=$((EXISTING_AGENTS + 1))
done

if [ "$EXISTING_AGENTS" -gt 0 ]; then
    echo ""
    echo "  Found $EXISTING_AGENTS existing agent file(s) in $AGENTS_DIR/"
    echo "  Options:"
    echo "    [s] Skip — keep your existing agents (default)"
    echo "    [o] Overwrite — replace with templates"
    echo "    [m] Manual — print Brain Protocol snippet to paste into existing agents"
    echo ""
    read -rp "  Choice [s/o/m]: " AGENT_CHOICE
    AGENT_CHOICE="${AGENT_CHOICE:-s}"
else
    AGENT_CHOICE="install"
fi

case "$AGENT_CHOICE" in
    o|O)
        INSTALLED=0
        for template in "$SCRIPT_DIR"/agents/*.md; do
            [ -f "$template" ] || continue
            filename="$(basename "$template")"
            cp "$template" "$AGENTS_DIR/$filename"
            INSTALLED=$((INSTALLED + 1))
        done
        echo "  Overwrote $INSTALLED agent template(s)."
        ;;
    m|M)
        echo ""
        echo "  ┌──────────────────────────────────────────────────────────────┐"
        echo "  │ Add this Brain Protocol block to EACH of your agent .md     │"
        echo "  │ files (paste near the top, after identity section):          │"
        echo "  └──────────────────────────────────────────────────────────────┘"
        echo ""
        echo '  # Brain Protocol'
        echo '  Before starting any task:'
        echo '  1. Call `pre_check(agent="<your-agent-name>", area="<area>", action_description="<plan>")`'
        echo '  2. If warnings exist, adjust approach'
        echo '  3. Call `log_decision(agent="<your-agent-name>", repo="<repo>", area="<area>", action="<plan>", reasoning="<why>", files_touched=["<paths>"])`'
        echo '  After feedback:'
        echo '  4. Call `log_outcome(decision_id="<id>", outcome="<result>", outcome_by="<who>", reason="<why>")`'
        echo '  NON-NEGOTIABLE.'
        echo ""
        echo "  Replace <your-agent-name> with the agent's lowercase name."
        echo ""
        ;;
    install)
        # Fresh install — prompt for names
        echo ""
        echo "  Agent templates use placeholder names (e.g. {{BE_NAME}})."
        read -rp "  Want to customize agent names now? [y/N]: " CUSTOMIZE_NAMES
        CUSTOMIZE_NAMES="${CUSTOMIZE_NAMES:-n}"

        INSTALLED=0
        for template in "$SCRIPT_DIR"/agents/*.md; do
            [ -f "$template" ] || continue
            filename="$(basename "$template")"
            TARGET="$AGENTS_DIR/$filename"
            cp "$template" "$TARGET"
            INSTALLED=$((INSTALLED + 1))
        done

        if [[ "$CUSTOMIZE_NAMES" =~ ^[yY] ]]; then
            echo ""
            # Collect names per role
            declare -A ROLE_PLACEHOLDERS=(
                ["project-manager"]="PM_NAME"
                ["product-owner"]="PO_NAME"
                ["principal-engineer"]="PE_NAME"
                ["backend-engineer"]="BE_NAME"
                ["frontend-engineer"]="FE_NAME"
                ["qa-engineer"]="QA_NAME"
            )
            for role in project-manager product-owner principal-engineer backend-engineer frontend-engineer qa-engineer; do
                FILE="$AGENTS_DIR/$role.md"
                [ -f "$FILE" ] || continue
                PLACEHOLDER="${ROLE_PLACEHOLDERS[$role]}"
                read -rp "  Name for $role [enter to keep placeholder]: " AGENT_NAME
                if [ -n "$AGENT_NAME" ]; then
                    LOWER_NAME="$(echo "$AGENT_NAME" | tr '[:upper:]' '[:lower:]')"
                    sed -i '' "s/{{${PLACEHOLDER}}}/$AGENT_NAME/g" "$FILE" 2>/dev/null || \
                        sed -i "s/{{${PLACEHOLDER}}}/$AGENT_NAME/g" "$FILE"
                    sed -i '' "s/{{${PLACEHOLDER}_LOWER}}/$LOWER_NAME/g" "$FILE" 2>/dev/null || \
                        sed -i "s/{{${PLACEHOLDER}_LOWER}}/$LOWER_NAME/g" "$FILE"
                    echo "    $role → $AGENT_NAME"
                fi
            done
        else
            echo ""
            echo "  Installed $INSTALLED template(s) with placeholder names."
            echo "  Customize later: find {{PLACEHOLDER}} in $AGENTS_DIR/*.md"
            echo ""
            echo "  Placeholders per role:"
            echo "    project-manager.md    → {{PM_NAME}}, {{PM_NAME_LOWER}}"
            echo "    product-owner.md      → {{PO_NAME}}, {{PO_NAME_LOWER}}"
            echo "    principal-engineer.md → {{PE_NAME}}, {{PE_NAME_LOWER}}"
            echo "    backend-engineer.md   → {{BE_NAME}}, {{BE_NAME_LOWER}}"
            echo "    frontend-engineer.md  → {{FE_NAME}}, {{FE_NAME_LOWER}}"
            echo "    qa-engineer.md        → {{QA_NAME}}, {{QA_NAME_LOWER}}"
        fi
        ;;
    *)
        echo "  Skipped. Existing agents preserved."
        ;;
esac

# ---------------------------------------------------------------------------
# Step 6: Verify installation
# ---------------------------------------------------------------------------
echo ""
echo "[6/6] Verifying installation..."

ERRORS=0

# Check server.py exists
if [ -f "$BRAIN_DIR/server.py" ]; then
    echo "  ✓ server.py installed"
else
    echo "  ✗ server.py missing!"
    ERRORS=$((ERRORS + 1))
fi

# Check config.json exists
if [ -f "$BRAIN_DIR/config.json" ]; then
    echo "  ✓ config.json exists"
else
    echo "  ✗ config.json missing!"
    ERRORS=$((ERRORS + 1))
fi

# Check venv and imports
if "$BRAIN_DIR/.venv/bin/python" -c "from mcp.server.fastmcp import FastMCP; import networkx" 2>/dev/null; then
    echo "  ✓ Python deps (mcp, networkx) importable"
else
    echo "  ✗ Python deps import failed!"
    ERRORS=$((ERRORS + 1))
fi

# Check server loads without errors
if "$BRAIN_DIR/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$BRAIN_DIR')
import server as S
tools = list(S.mcp._tool_manager._tools.keys())
print(f'  ✓ Server loads: {len(tools)} MCP tools registered')
" 2>/dev/null; then
    true
else
    echo "  ✗ Server failed to load!"
    ERRORS=$((ERRORS + 1))
fi

# Check MCP registration
if command -v claude &>/dev/null; then
    if claude mcp list 2>/dev/null | grep -q "agent-brain"; then
        echo "  ✓ MCP registered with Claude Code"
    else
        echo "  ⚠ MCP not found in claude mcp list (may need restart)"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
if [ "$ERRORS" -eq 0 ]; then
    echo "============================================"
    echo "  ✓ Setup complete! All checks passed."
    echo "============================================"
else
    echo "============================================"
    echo "  ⚠ Setup complete with $ERRORS error(s)."
    echo "============================================"
fi

echo ""
echo "Next steps:"
echo "  1. Edit $BRAIN_DIR/config.json (add repos if not done above)"
echo "  2. Customize agent names in $AGENTS_DIR/*.md (if skipped)"
echo "  3. Restart Claude Code"
echo "  4. Test: ask any agent to call brain_stats()"
echo ""
echo "Expected brain_stats() output:"
echo '  Brain Stats:'
echo '    Nodes: 0 | Edges: 0'
echo '    Decisions: 0 | Feedback: 0 | Code refs: 0'
echo '    Areas: none'
echo '    Repos: none'
echo '    Agents: none'
echo ""
echo "SAN setup (optional):"
echo "  1. Create .san/ dir in your project repo root"
echo "  2. Use the brain-compiler agent to convert source files to SAN"
echo "  3. Run update_san_index(<repo>) to build the index"
echo "  4. Use query_san/get_san/check_san_freshness to work with compressed code"
echo "  See: san/README.md for the full SAN protocol spec"
echo ""
echo "Docs: $SCRIPT_DIR/docs/"
echo ""
