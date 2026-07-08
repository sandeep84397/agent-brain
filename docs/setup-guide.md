# Setup Guide

## Prerequisites

- Claude Code (`claude` CLI) or Codex installed
- Python 3.10+
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` enabled
- Optional: `code-review-graph` for code bridge features

## Installation

Recommended:

```bash
git clone https://github.com/YOUR_USERNAME/agent-brain.git
cd agent-brain
chmod +x setup.sh
./setup.sh
```

`./setup.sh` installs for every supported runtime it detects: Claude Code,
Codex, or both. To force one runtime:

```bash
./setup.sh --claude
./setup.sh --codex
./setup.sh --all
```

The setup script:
1. Creates Python venv at `~/.agent-brain/.venv/`
2. Installs `mcp` and `networkx`
3. Copies `server.py` to `~/.agent-brain/`
4. Creates `config.json` from template
5. Registers `agent-brain` MCP server globally for the selected runtime
6. Installs lifecycle hooks for the selected runtime
7. Copies Claude agent templates to `~/.claude/agents/` when installing Claude Code

For Codex, restart Codex after setup, run `/mcp` to confirm `agent-brain` is
enabled, then run `/hooks` and trust the Agent Brain hooks when prompted.

## Upgrade Existing Installs

Existing Agent Brain users can upgrade in place:

```bash
git pull
./setup.sh
```

This preserves `~/.agent-brain/config.json`, decisions, records, metrics, and
existing Claude agent files unless you choose to overwrite templates. It
refreshes the installed server/hooks, keeps Claude Code configured, and adds
Codex config when Codex is detected.

For each existing project repo:

```bash
./setup.sh --link-project=/absolute/path/to/your/project
```

Restart Claude Code and/or Codex after upgrading. In Codex, run `/mcp` and
`/hooks`; trust the Agent Brain hooks when prompted.

## Configuration

### 1. Edit config.json

```bash
$EDITOR ~/.agent-brain/config.json
```

```json
{
  "repos": {
    "my-backend": "/Users/you/projects/my-backend",
    "my-frontend": "/Users/you/projects/my-frontend"
  },
  "team": [
    {"name": "marcus", "role": "principal-engineer"},
    {"name": "maya", "role": "product-owner"},
    {"name": "arjun", "role": "backend-engineer"},
    {"name": "priya", "role": "frontend-engineer"},
    {"name": "rahul", "role": "qa-engineer"}
  ]
}
```

### 2. Customize agent templates

Edit each file in `~/.claude/agents/`:
- Replace `{{PM_NAME}}` → your PM agent's name
- Replace `{{PE_NAME}}` → your PE agent's name
- Replace `{{BE_NAME}}` → your BE agent's name
- etc.

### 3. Restart your runtime

```bash
# Close and reopen Claude Code, or:
claude mcp list  # verify agent-brain shows as connected
```

For Codex, restart the app or CLI session, then use `/mcp` and `/hooks`.

### 4. Verify

Run from the brain directory:
```
python3 brain/server.py stats
```

Expected: "Brain Stats: Nodes: 0 | Edges: 0 | Decisions: 0"

## Scaling the Team

### Adding agents of the same role

Copy and rename:
```bash
cp ~/.claude/agents/backend-engineer.md ~/.claude/agents/backend-engineer-2.md
```

Edit the copy:
- Change `name:` in frontmatter
- Change agent name in Brain Protocol
- Add "coordinate with peer" instruction

### Adding new roles

Create a new `.md` file with:
1. Frontmatter (name, description, model, tools)
2. Brain Protocol section (copy from any template)
3. Role-specific workflow
4. Authority rules

## Uninstall

```bash
claude mcp remove agent-brain
rm -rf ~/.agent-brain/
rm ~/.claude/agents/{project-manager,product-owner,principal-engineer,backend-engineer,frontend-engineer,qa-engineer}.md
```

Setup also adds an `agent-brain` block to `~/.claude/CLAUDE.md` (the SAN
tool-ladder) and hooks to `~/.claude/settings.json`. To remove them, delete the
block between `<!-- agent-brain:san-ladder -->` and `<!-- /agent-brain:san-ladder -->`
in CLAUDE.md, and drop the `route_*`/`enforce_*`/`inject_*`/`remind_*` hook
entries from `settings.json`.

For Codex, setup writes an `agent-brain` MCP block to `~/.codex/config.toml`
and hooks to `~/.codex/hooks.json`. Remove those entries and restart Codex to
uninstall the Codex integration.

## Troubleshooting

### MCP server not connecting
```bash
# Check server starts cleanly
~/.agent-brain/.venv/bin/python -c "from server import mcp; print('OK')"

# Check registration
claude mcp list
```

### Python version issues
Requires Python 3.10+. Check: `python3 --version`

### Config not loading
Verify `~/.agent-brain/config.json` is valid JSON:
```bash
python3 -c "import json; json.load(open('$HOME/.agent-brain/config.json')); print('Valid')"
```
