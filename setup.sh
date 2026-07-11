#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Agent Brain — Setup Script
#
# Modes:
#   ./setup.sh                          # install for every detected runtime
#                                        # (Claude Code, Codex, or both)
#   ./setup.sh --claude                 # Claude Code install only
#   ./setup.sh --codex                  # Codex install (MCP + hooks)
#   ./setup.sh --all                    # Claude Code + Codex install
#   ./setup.sh --link-project=<path>    # link an existing project to brain
#                                        (Claude project MCP + Codex AGENTS.md;
#                                         install must already be done)
#   ./setup.sh --link-codex-project=<path>
#                                        # add AGENTS.md brain protocol guidance
# ============================================================================

BRAIN_DIR="${AGENT_BRAIN_DIR:-$HOME/.agent-brain}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
LINK_PROJECT=""
LINK_CODEX_PROJECT=""
INSTALL_CLAUDE="auto"
INSTALL_CODEX="auto"
for arg in "$@"; do
    case "$arg" in
        --link-project=*) LINK_PROJECT="${arg#*=}" ;;
        --link-project)   echo "ERROR: --link-project requires a value (use --link-project=/path)"; exit 2 ;;
        --link-codex-project=*) LINK_CODEX_PROJECT="${arg#*=}" ;;
        --link-codex-project)   echo "ERROR: --link-codex-project requires a value (use --link-codex-project=/path)"; exit 2 ;;
        --claude) INSTALL_CLAUDE=1; INSTALL_CODEX=0 ;;
        --codex) INSTALL_CLAUDE=0; INSTALL_CODEX=1 ;;
        --with-codex) INSTALL_CODEX=1 ;;
        --all) INSTALL_CLAUDE=1; INSTALL_CODEX=1 ;;
        --help|-h)
            echo "Usage:"
            echo "  ./setup.sh                          Install for every detected runtime"
            echo "  ./setup.sh --claude                 Install for Claude Code only"
            echo "  ./setup.sh --codex                  Install for Codex only"
            echo "  ./setup.sh --with-codex             Existing Claude install + Codex config"
            echo "  ./setup.sh --all                    Install for Claude Code and Codex"
            echo "  ./setup.sh --link-project=<path>    Link an existing project to a"
            echo "                                       previously-installed brain for all runtimes"
            echo "  ./setup.sh --link-codex-project=<path>"
            echo "                                       Add Codex AGENTS.md guidance to a project"
            exit 0 ;;
        *) echo "Unknown flag: $arg"; echo "Try ./setup.sh --help"; exit 2 ;;
    esac
done

codex_detected() {
    command -v codex &>/dev/null || [ -d "/Applications/Codex.app" ] || [ -n "${CODEX_HOME:-}" ]
}

if [ "$INSTALL_CLAUDE" = "auto" ]; then
    if command -v claude &>/dev/null; then
        INSTALL_CLAUDE=1
    else
        INSTALL_CLAUDE=0
    fi
fi

if [ "$INSTALL_CODEX" = "auto" ]; then
    if codex_detected; then
        INSTALL_CODEX=1
    else
        INSTALL_CODEX=0
    fi
fi

if [ "$INSTALL_CLAUDE" -eq 0 ] && [ "$INSTALL_CODEX" -eq 0 ]; then
    # Preserve the old fallback: still install the brain and print Claude's
    # manual registration command when no supported runtime is detected.
    INSTALL_CLAUDE=1
fi

# ---------------------------------------------------------------------------
# --link-project mode: do NOT run the install wizard.
# Brain must already be installed at $BRAIN_DIR.
# ---------------------------------------------------------------------------
if [ -n "$LINK_PROJECT" ]; then
    PROJECT_PATH="$LINK_PROJECT"
    if [ ! -d "$PROJECT_PATH" ]; then
        echo "ERROR: project path '$PROJECT_PATH' is not a directory."
        exit 1
    fi
    if [ ! -f "$BRAIN_DIR/server.py" ] || [ ! -x "$BRAIN_DIR/.venv/bin/python" ]; then
        echo "ERROR: agent-brain is not installed at $BRAIN_DIR."
        echo "Run ./setup.sh (no flags) first to install, then re-run with --link-project."
        exit 1
    fi

    PROJECT_PATH="$(cd "$PROJECT_PATH" && pwd)"
    PYBIN="$BRAIN_DIR/.venv/bin/python"
    SERVER_PY="$BRAIN_DIR/server.py"

    echo "============================================"
    echo "  Agent Brain — Link Project"
    echo "============================================"
    echo "  Project:  $PROJECT_PATH"
    echo "  Brain:    $BRAIN_DIR"
    echo ""

    # Use the brain venv's python for safe JSON merging — every action below is
    # idempotent: re-running --link-project on the same project must NOT
    # duplicate entries.
    "$PYBIN" - "$PROJECT_PATH" "$SERVER_PY" "$PYBIN" <<'PYEOF'
import json, sys
from pathlib import Path

project = Path(sys.argv[1])
server_py = sys.argv[2]
pybin = sys.argv[3]

def load_json(p: Path) -> dict:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        print(f"  WARN: {p} is not valid JSON, leaving untouched and bailing.")
        sys.exit(1)

def write_json(p: Path, data: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")

# 1. .mcp.json (project-scoped server registration; subagents read this)
mcp_path = project / ".mcp.json"
mcp = load_json(mcp_path)
mcp.setdefault("mcpServers", {})
brain_entry = {
    "command": pybin,
    "args": [server_py],
    "type": "stdio",
    "env": {},
}
existing = mcp["mcpServers"].get("agent-brain")
if existing == brain_entry:
    print(f"  ✓ {mcp_path} already up to date")
else:
    mcp["mcpServers"]["agent-brain"] = brain_entry
    write_json(mcp_path, mcp)
    print(f"  ✓ Wrote {mcp_path}")

# 2. .claude/settings.local.json
settings_path = project / ".claude" / "settings.local.json"
settings = load_json(settings_path)
changed = False
if settings.get("enableAllProjectMcpServers") is not True:
    settings["enableAllProjectMcpServers"] = True
    changed = True
allowlist = settings.get("enabledMcpjsonServers")
if allowlist is None:
    settings["enabledMcpjsonServers"] = ["agent-brain"]
    changed = True
elif isinstance(allowlist, list) and "agent-brain" not in allowlist:
    allowlist.append("agent-brain")
    changed = True
elif not isinstance(allowlist, list):
    print(f"  WARN: enabledMcpjsonServers in {settings_path} is not a list; "
          "leaving as-is. Convert it to an array containing 'agent-brain' manually.")
if changed:
    write_json(settings_path, settings)
    print(f"  ✓ Updated {settings_path}")
else:
    print(f"  ✓ {settings_path} already up to date")

# 3. .gitignore (append-if-missing for the brain artifacts)
gitignore_path = project / ".gitignore"
required_lines = [
    ".mcp.json",
    ".san/.san_hashes.json",
    ".san/_cache/",
]
existing_lines: list[str] = []
if gitignore_path.exists():
    existing_lines = gitignore_path.read_text().splitlines()
existing_set = {ln.strip() for ln in existing_lines}
to_append = [ln for ln in required_lines if ln not in existing_set]
if to_append:
    block = ["", "# agent-brain"] + to_append + [""]
    with gitignore_path.open("a") as f:
        # Add a leading newline if file didn't end in one
        if existing_lines and existing_lines[-1].strip():
            f.write("\n")
        f.write("\n".join(block) + "\n")
    print(f"  ✓ Appended {len(to_append)} line(s) to {gitignore_path}")
else:
    print(f"  ✓ .gitignore already covers brain artifacts (or no gitignore — skipping)")

# 4. Friendly note: confirm the project name is registered in brain config
brain_config = Path(sys.argv[2]).parent / "config.json"
known_repos: list[str] = []
if brain_config.exists():
    try:
        known_repos = list(json.loads(brain_config.read_text()).get("repos", {}).keys())
    except json.JSONDecodeError:
        pass
project_name = project.name
matching = [r for r in known_repos if r.lower() == project_name.lower()
            or project.as_posix().endswith("/" + r)]
if not matching:
    print()
    print(f"  ⚠ '{project_name}' is not registered in {brain_config}.")
    print( "    Add an entry like:")
    print( '      "repos": {')
    print(f'        "{project_name}": "{project.as_posix()}"')
    print( '      }')
    print( "    so brain decisions can be filed under this repo's name.")
PYEOF

    "$PYBIN" "$SCRIPT_DIR/brain/codex_setup.py" \
        link-project --project "$PROJECT_PATH"

    PROJECT_NAME="$(basename "$PROJECT_PATH")"
    echo ""
    echo "Next steps:"
    echo "  1. If '$PROJECT_NAME' is missing from $BRAIN_DIR/config.json, add it (see above)."
    echo "  2. Restart Claude Code and/or Codex in the project so config and AGENTS.md reload."
    echo "  3. In Codex, run /mcp and /hooks to confirm agent-brain is enabled and trusted."
    echo "  4. Verify:  $BRAIN_DIR/.venv/bin/python $BRAIN_DIR/server.py diagnose --project=$PROJECT_PATH"
    exit 0
fi

if [ -n "$LINK_CODEX_PROJECT" ]; then
    PROJECT_PATH="$LINK_CODEX_PROJECT"
    if [ ! -d "$PROJECT_PATH" ]; then
        echo "ERROR: project path '$PROJECT_PATH' is not a directory."
        exit 1
    fi
    if [ ! -f "$BRAIN_DIR/server.py" ] || [ ! -x "$BRAIN_DIR/.venv/bin/python" ]; then
        echo "ERROR: agent-brain is not installed at $BRAIN_DIR."
        echo "Run ./setup.sh --codex first, then re-run with --link-codex-project."
        exit 1
    fi

    PROJECT_PATH="$(cd "$PROJECT_PATH" && pwd)"
    echo "============================================"
    echo "  Agent Brain — Link Codex Project"
    echo "============================================"
    echo "  Project:  $PROJECT_PATH"
    echo ""
    "$BRAIN_DIR/.venv/bin/python" "$SCRIPT_DIR/brain/codex_setup.py" \
        link-project --project "$PROJECT_PATH"
    echo ""
    echo "Next steps:"
    echo "  1. Make sure '$PROJECT_PATH' is registered in $BRAIN_DIR/config.json."
    echo "  2. Restart Codex in the project so AGENTS.md is reloaded."
    echo "  3. In Codex, run /mcp and /hooks to confirm agent-brain is enabled and trusted."
    exit 0
fi

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
"$BRAIN_DIR/.venv/bin/pip" install --quiet mcp networkx tiktoken
echo "  Dependencies installed."

# ---------------------------------------------------------------------------
# Step 2: Copy server
# ---------------------------------------------------------------------------
echo ""
echo "[2/6] Installing brain server..."

mkdir -p "$BRAIN_DIR"
cp "$SCRIPT_DIR/brain/server.py" "$BRAIN_DIR/server.py"
cp "$SCRIPT_DIR/brain/san_publish.py" "$BRAIN_DIR/san_publish.py"
mkdir -p "$BRAIN_DIR/hooks"
cp "$SCRIPT_DIR"/brain/hooks/*.py "$BRAIN_DIR/hooks/"
echo "  Copied server.py and hooks to $BRAIN_DIR/"

# Managed SAN compiler runtime + provider adapter assets
cp "$SCRIPT_DIR/brain/compiler_config.py" "$BRAIN_DIR/compiler_config.py"
cp "$SCRIPT_DIR/brain/compiler_setup.py" "$BRAIN_DIR/compiler_setup.py"
mkdir -p "$BRAIN_DIR/san"
cp "$SCRIPT_DIR/san/compiler-contract.md" "$BRAIN_DIR/san/compiler-contract.md"
mkdir -p "$BRAIN_DIR/san/adapters/claude"
mkdir -p "$BRAIN_DIR/san/adapters/codex/brain-compiler"
cp "$SCRIPT_DIR/san/adapters/claude/brain-compiler.md" \
  "$BRAIN_DIR/san/adapters/claude/brain-compiler.md"
cp "$SCRIPT_DIR/san/adapters/codex/brain-compiler.toml" \
  "$BRAIN_DIR/san/adapters/codex/brain-compiler.toml"
cp "$SCRIPT_DIR/san/adapters/codex/brain-compiler/SKILL.md" \
  "$BRAIN_DIR/san/adapters/codex/brain-compiler/SKILL.md"
echo "  Copied SAN compiler runtime + adapter assets to $BRAIN_DIR/san/"

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

# Managed Claude SAN compiler adapter (separate from the interactive
# role-template prompt below; runs only when installing for Claude).
if [ "$INSTALL_CLAUDE" -eq 1 ]; then
    echo ""
    echo "  Installing managed Claude SAN compiler adapter..."
    if "$BRAIN_DIR/.venv/bin/python" "$BRAIN_DIR/compiler_setup.py" install-claude \
        --config "$BRAIN_DIR/config.json" \
        --claude-home "$HOME/.claude" \
        --assets-root "$BRAIN_DIR/san"; then
        :
    else
        echo "  WARNING: managed Claude SAN compiler adapter not installed" \
             "(config invalid or an unmanaged brain-compiler.md exists)."
    fi
fi

# ---------------------------------------------------------------------------
# Step 4: Register MCP server with Claude Code
# ---------------------------------------------------------------------------
echo ""
echo "[4/6] Registering MCP server..."

if [ "$INSTALL_CLAUDE" -eq 1 ] && command -v claude &>/dev/null; then
    # Remove existing registration if present (idempotent)
    claude mcp remove agent-brain 2>/dev/null || true
    claude mcp add --transport stdio --scope user agent-brain -- \
        "$BRAIN_DIR/.venv/bin/python" "$BRAIN_DIR/server.py"
    echo "  Registered agent-brain MCP with Claude Code (global scope)."
elif [ "$INSTALL_CLAUDE" -eq 1 ]; then
    echo "  WARNING: 'claude' CLI not found. Register manually:"
    echo "  claude mcp add --transport stdio --scope user agent-brain -- \\"
    echo "      $BRAIN_DIR/.venv/bin/python $BRAIN_DIR/server.py"
else
    echo "  Skipping Claude Code MCP registration (Claude Code not selected or not detected)."
fi

if [ "$INSTALL_CODEX" -eq 1 ]; then
    echo ""
    echo "[4a] Installing Codex MCP + hooks..."
    CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
    if "$BRAIN_DIR/.venv/bin/python" "$SCRIPT_DIR/brain/codex_setup.py" \
        install-user \
        --codex-home "$CODEX_HOME" \
        --pybin "$BRAIN_DIR/.venv/bin/python" \
        --server "$BRAIN_DIR/server.py" \
        --hooks-dir "$BRAIN_DIR/hooks" \
        --brain-config "$BRAIN_DIR/config.json" \
        --assets-root "$BRAIN_DIR/san"; then
        echo "  Restart Codex, then run /mcp and /hooks to review and trust hooks."
    else
        echo "  WARNING: Codex install incomplete" \
             "(config invalid or an unmanaged brain-compiler artifact exists)."
    fi
fi

# ---------------------------------------------------------------------------
# Step 4b: Install brain hooks (enforce + amnesia-fix injection + research nudge)
# ---------------------------------------------------------------------------
echo ""
echo "[4b] Installing brain hooks..."

SETTINGS_FILE="$HOME/.claude/settings.json"
HOOKS_DIR="$BRAIN_DIR/hooks"
PYBIN="$BRAIN_DIR/.venv/bin/python"

if [ "$INSTALL_CLAUDE" -eq 0 ]; then
    echo "  Skipping Claude Code hooks (Claude Code not selected or not detected)."
else
# Idempotent JSON merge — adds five hooks without clobbering existing ones:
#   PreToolUse  Edit|Write  -> enforce_brain_protocol.py   (gate code edits)
#   SessionStart startup|resume|compact -> inject_brain_context.py  (amnesia fix)
#   PreToolUse  Workflow    -> remind_brain_before_research.py (soft research nudge)
#   PreToolUse  Read        -> route_read_to_san.py        (Read->SAN nudge)
#   PreToolUse  Bash        -> route_bash_to_san.py        (cat/grep/sed->SAN nudge)
"$PYBIN" - "$SETTINGS_FILE" "$HOOKS_DIR" "$PYBIN" <<'PYEOF'
import json, sys
from pathlib import Path

settings_path = Path(sys.argv[1])
hooks_dir = sys.argv[2]
pybin = sys.argv[3]

settings = {}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        print(f"  WARN: {settings_path} is not valid JSON — skipping hook install.")
        sys.exit(0)

hooks = settings.setdefault("hooks", {})

def add(event, matcher, script, timeout):
    cmd = f"{pybin} {hooks_dir}/{script}"
    existing_groups = hooks.get(event, [])
    if any(h.get("command", "") == cmd
           for blk in existing_groups for h in blk.get("hooks", [])):
        print(f"  = {event}/{script} already installed")
        return
    kept = []
    removed = False
    for blk in existing_groups:
        old_hooks = blk.get("hooks", [])
        new_hooks = [h for h in old_hooks if script not in h.get("command", "")]
        if len(new_hooks) != len(old_hooks):
            removed = True
        if new_hooks:
            next_blk = dict(blk)
            next_blk["hooks"] = new_hooks
            kept.append(next_blk)
    hooks[event] = kept
    hooks.setdefault(event, []).append({
        "matcher": matcher,
        "hooks": [{"type": "command", "command": cmd, "timeout": timeout}],
    })
    prefix = "~" if removed else "+"
    print(f"  {prefix} {event}/{script}")

add("PreToolUse", "Edit|Write", "enforce_brain_protocol.py", 5000)
add("SessionStart", "startup|resume|compact", "inject_brain_context.py", 15000)
add("PreToolUse", "Workflow", "remind_brain_before_research.py", 10000)
add("PreToolUse", "Read", "route_read_to_san.py", 5000)
add("PreToolUse", "Bash", "route_bash_to_san.py", 5000)

if settings_path.exists():
    settings_path.with_suffix(".json.bak").write_text(settings_path.read_text())
settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, indent=2) + "\n")
print(f"  Wrote {settings_path} (backup at settings.json.bak)")
PYEOF
echo "  Set BRAIN_SKIP_ENFORCE=1 to bypass all gates for your own direct edits."
fi

# ---------------------------------------------------------------------------
# Step 4c: Install the SAN tool-ladder directive into the global CLAUDE.md
# ---------------------------------------------------------------------------
# So every install gets the "find->read->grep-literal->edit" habit without the
# user having to remember to hand-edit CLAUDE.md. Idempotent: a marked block is
# inserted once and skipped on re-run; the rest of CLAUDE.md is never touched.
echo ""
echo "[4c] Adding SAN tool-ladder to global CLAUDE.md..."

if [ "$INSTALL_CLAUDE" -eq 0 ]; then
    echo "  Skipping Claude Code CLAUDE.md update (Claude Code not selected or not detected)."
else
GLOBAL_CLAUDE="$HOME/.claude/CLAUDE.md"
"$PYBIN" - "$GLOBAL_CLAUDE" <<'PYEOF'
import sys
from pathlib import Path

path = Path(sys.argv[1])
MARKER = "<!-- agent-brain:san-ladder -->"
BLOCK = f"""{MARKER}
## Code Reading: SAN tool-ladder (agent-brain)

When a `.san` brief exists for a source file, use the right tool per step —
this is a standing rule, like graph-first exploration:

| Step | Goal | Use | NOT |
|------|------|-----|-----|
| Find | which file/symbol has X | `query_san` / `semantic_search_nodes` | `grep -r` raw source |
| Read/understand | what a file/class does | `get_san(file_path="<abs>")` (`detail="sig"`/`"full"`) | `cat`/`head`/`sed` the file |
| Exact literal | find a precise string/line | `grep`/`rg` on the file | `get_san` (SAN drops literals) |
| Edit | change exact bytes | raw `Read` then `Edit` | — |

`get_san` is ~5-11x fewer tokens than reading the raw file and carries the same
structure (signatures, deps, errors). Discovery `grep` across files and
build/run/git/test in the shell are always fine.
<!-- /agent-brain:san-ladder -->
"""

existing = path.read_text() if path.exists() else ""
if MARKER in existing:
    print(f"  ✓ SAN ladder already in {path}")
else:
    path.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if (not existing or existing.endswith("\n\n")) else \
          ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + sep + BLOCK)
    print(f"  ✓ Added SAN ladder to {path}")
PYEOF
fi

# ---------------------------------------------------------------------------
# Step 5: Install agent templates
# ---------------------------------------------------------------------------
echo ""
echo "[5/6] Installing agent templates..."

if [ "$INSTALL_CLAUDE" -eq 0 ]; then
    echo "  Skipping Claude Code agent templates (Claude Code not selected or not detected)."
else
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
fi

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
if [ "$INSTALL_CLAUDE" -eq 1 ] && command -v claude &>/dev/null; then
    if claude mcp list 2>/dev/null | grep -q "agent-brain"; then
        echo "  ✓ MCP registered with Claude Code"
    else
        echo "  ⚠ MCP not found in claude mcp list (may need restart)"
    fi
fi

if [ "$INSTALL_CODEX" -eq 1 ]; then
    CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
    if [ -f "$CODEX_HOME/config.toml" ] && grep -q "\[mcp_servers.agent-brain\]" "$CODEX_HOME/config.toml"; then
        echo "  ✓ MCP configured for Codex"
    else
        echo "  ✗ Codex MCP config missing!"
        ERRORS=$((ERRORS + 1))
    fi
    if [ -f "$CODEX_HOME/hooks.json" ] && grep -q "enforce_brain_protocol.py" "$CODEX_HOME/hooks.json"; then
        echo "  ✓ Codex hooks configured"
    else
        echo "  ✗ Codex hooks missing!"
        ERRORS=$((ERRORS + 1))
    fi
fi

# Run the standalone diagnose CLI (works post-install too)
echo ""
echo "  Running diagnose..."
if "$BRAIN_DIR/.venv/bin/python" "$BRAIN_DIR/server.py" diagnose; then
    true
else
    ERRORS=$((ERRORS + 1))
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
if [ "$INSTALL_CLAUDE" -eq 1 ]; then
    echo "  2. Customize agent names in $HOME/.claude/agents/*.md (if skipped)"
    echo "  3. Restart Claude Code"
elif [ "$INSTALL_CODEX" -eq 1 ]; then
    echo "  2. Restart Codex"
    echo "  3. Run /mcp and /hooks; trust the agent-brain hooks when prompted"
fi
if [ "$INSTALL_CLAUDE" -eq 1 ] && [ "$INSTALL_CODEX" -eq 1 ]; then
    echo "  4. Optional per project: ./setup.sh --link-project=/absolute/path/to/project"
elif [ "$INSTALL_CODEX" -eq 1 ]; then
    echo "  4. Optional per project: ./setup.sh --link-codex-project=/absolute/path/to/project"
else
    echo "  4. Optional per project: ./setup.sh --link-project=/absolute/path/to/project"
fi
echo "  5. Test: $BRAIN_DIR/.venv/bin/python $BRAIN_DIR/server.py stats"
echo "  6. Re-run health check anytime:"
echo "       $BRAIN_DIR/.venv/bin/python $BRAIN_DIR/server.py diagnose"
echo ""
echo "Expected 'server.py stats' output:"
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
echo "  3. Run 'python3 brain/server.py san-index <repo>' to build the index"
echo "  4. Use query_san/get_san/recompile_san(dry_run=True) to work with compressed code"
echo "  See: san/README.md for the full SAN protocol spec"
echo ""
echo "Docs: $SCRIPT_DIR/docs/"
echo ""
