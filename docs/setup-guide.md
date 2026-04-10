# Setup Guide

## Prerequisites

- Claude Code installed (`claude` CLI available)
- Python 3.10+
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` enabled
- Optional: `code-review-graph` for code bridge features

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/agent-brain.git
cd agent-brain
chmod +x setup.sh
./setup.sh
```

The setup script:
1. Creates Python venv at `~/.agent-brain/.venv/`
2. Installs `mcp` and `networkx`
3. Copies `server.py` to `~/.agent-brain/`
4. Creates `config.json` from template
5. Registers `agent-brain` MCP server globally
6. Copies agent templates to `~/.claude/agents/`

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

### 3. Restart Claude Code

```bash
# Close and reopen Claude Code, or:
claude mcp list  # verify agent-brain shows as connected
```

### 4. Verify

Ask Claude Code:
```
Call brain_stats()
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
