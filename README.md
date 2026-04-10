# Agent Brain

Persistent decision memory for AI code agent teams. Agents learn from mistakes, coordinate across sessions, and never repeat the same error twice.

**Works with any MCP-compatible agent**: Claude Code, Cursor, Windsurf, Cline, Continue, etc. Agent templates (`.md` files) are Claude Code specific — the MCP server itself is universal.

## What This Does

Claude Code agents start fresh every session. No memory of past decisions, no learning from rejections, no cross-agent knowledge sharing. Agent Brain fixes this.

```
Agent → pre_check()    → "WARNING: similar approach was rejected last week"
Agent → log_decision() → records what you decided and why
Agent → does work      → PR created
PE    → log_outcome()  → "rejected: violates DIP"
Next time, any agent → pre_check() → sees that rejection → avoids the mistake
```

## Features

| Feature | What it does |
|---------|-------------|
| **Decision Memory** | Log decisions, outcomes, feedback. Persists across sessions. |
| **Pre-Check Warnings** | Before starting work, see past failures in the same area. |
| **Fuzzy Matching** | "Rate limiting on signup" finds "rate limiting on login" rejection. |
| **Code Bridge** | Link decisions to code-review-graph nodes. "Show me all decisions that touched AuthService." |
| **Agent Scorecards** | Acceptance rate, trends, top rejection categories per agent. |
| **Adaptive Warnings** | Agents with high rejection rates get stricter pre-check warnings. |
| **Team Dashboard** | All agents at a glance — for project managers. |
| **SAN Protocol** | Compress code to 15% of original tokens. Full codebase fits in context. |

## Quick Start

```bash
git clone https://github.com/sandeep84397/agent-brain.git
cd agent-brain
chmod +x setup.sh
./setup.sh
```

The setup wizard will:
- Create a Python venv and install dependencies
- Prompt for your repo paths (or use the template config)
- Register the MCP server globally with Claude Code
- Offer to customize agent names interactively
- Run verification checks

> **No `setup.sh`?** The server works standalone. Just `pip install mcp networkx` and register manually:
> ```bash
> claude mcp add --transport stdio --scope user agent-brain -- python3 /path/to/server.py
> ```
> The server gracefully handles a missing `config.json` — it starts with an empty brain.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Your Machine (global)                          │
│                                                 │
│  ~/.agent-brain/                                │
│  ├── server.py        ← MCP server (21 tools)  │
│  ├── config.json      ← your repos + team      │
│  └── decisions.json   ← persistent memory       │
│                                                 │
│  ~/.claude/agents/                              │
│  ├── project-manager.md                         │
│  ├── product-owner.md                           │
│  ├── principal-engineer.md                      │
│  ├── backend-engineer.md                        │
│  ├── frontend-engineer.md                       │
│  └── qa-engineer.md                             │
│                                                 │
│  project-repo/.san/   ← SAN-compressed code     │
│  ├── _index.json                                │
│  └── src/**/*.san                               │
│                                                 │
│  dashboard/           ← pixel art office UI     │
│  ├── server.py        (python, zero deps)       │
│  └── static/          (HTML5 Canvas + SSE)      │
└─────────────────────────────────────────────────┘
```

## MCP Tools (21)

### Core (every agent uses these)
| Tool | Purpose |
|------|---------|
| `pre_check` | Check past failures before starting work |
| `log_decision` | Record what you decided and why |
| `log_outcome` | Record accepted/rejected/failed after review |
| `log_feedback` | Reviewers log feedback on decisions |

### Query
| Tool | Purpose |
|------|---------|
| `query_decisions` | Filter decisions by area/agent/repo/outcome |
| `get_decision` | Full detail + feedback for one decision |
| `brain_stats` | Overall brain health |

### Code Bridge
| Tool | Purpose |
|------|---------|
| `decisions_for_code` | All decisions that touched a code symbol |
| `decisions_for_file` | All decisions that touched a file |
| `code_impact` | Blast radius: code symbols + callers |

### Patterns
| Tool | Purpose |
|------|---------|
| `similar_failures` | Fuzzy cross-area search for similar rejections |
| `get_patterns` | Cluster similar rejection reasons |

### Scorecards
| Tool | Purpose |
|------|---------|
| `get_agent_stats` | Quick stats for one or all agents |
| `agent_scorecard` | Detailed breakdown with trends and advice |
| `team_dashboard` | All agents at a glance |

### Office Dashboard
| Tool | Purpose |
|------|---------|
| `heartbeat` | Report agent status (working/idle/discussing/blocked) for live dashboard |
| `office_state` | Get current office state (debugging) |

### SAN (Structured Associative Notation)
| Tool | Purpose |
|------|---------|
| `check_san_freshness` | Check which SAN files are stale vs source |
| `query_san` | Search SAN files by keyword (index + content) |
| `get_san` | Get SAN-compressed content for a source file |
| `update_san_index` | Rebuild `_index.json` from .san/ directory |

## Agent Team

The repo includes 6 agent templates. Each has the Brain Protocol baked in:

| Role | File | Responsibility |
|------|------|---------------|
| Project Manager | `project-manager.md` | Coordination, tracking, blockers |
| Product Owner | `product-owner.md` | PRDs, acceptance criteria |
| Principal Engineer | `principal-engineer.md` | Architecture, SOLID, reviews |
| Backend Engineer | `backend-engineer.md` | API, services, data layer |
| Frontend Engineer | `frontend-engineer.md` | UI, app logic, integration |
| QA Engineer | `qa-engineer.md` | Test plans, validation, quality gates |

Scale by duplicating templates (e.g., `backend-engineer-2.md`).

### Placeholders

Each template has `{{ROLE_NAME}}` / `{{ROLE_NAME_LOWER}}` placeholders:

| File | Placeholders |
|------|-------------|
| `project-manager.md` | `{{PM_NAME}}`, `{{PM_NAME_LOWER}}` |
| `product-owner.md` | `{{PO_NAME}}`, `{{PO_NAME_LOWER}}` |
| `principal-engineer.md` | `{{PE_NAME}}`, `{{PE_NAME_LOWER}}` |
| `backend-engineer.md` | `{{BE_NAME}}`, `{{BE_NAME_LOWER}}` |
| `frontend-engineer.md` | `{{FE_NAME}}`, `{{FE_NAME_LOWER}}` |
| `qa-engineer.md` | `{{QA_NAME}}`, `{{QA_NAME_LOWER}}` |

`setup.sh` offers to replace these interactively. Or do it manually:
```bash
sed -i 's/{{BE_NAME}}/Arjun/g; s/{{BE_NAME_LOWER}}/arjun/g' ~/.claude/agents/backend-engineer.md
```

### Already have custom agents?

If you already have agent `.md` files, **don't overwrite them**. Instead, add the Brain Protocol block to each:

```markdown
# Brain Protocol
Before starting any task:
1. Call `pre_check(agent="<name>", area="<area>", action_description="<plan>")`
2. If warnings exist, adjust approach
3. Call `log_decision(agent="<name>", repo="<repo>", area="<area>", action="<plan>", reasoning="<why>", files_touched=["<paths>"])`
After feedback:
4. Call `log_outcome(decision_id="<id>", outcome="<result>", outcome_by="<who>", reason="<why>")`
NON-NEGOTIABLE.
```

For reviewers (PE, QA), also add:
```markdown
5. Call `log_feedback(agent="<name>", decision_id="<their-id>", feedback="<detail>", severity="blocker|warning|info")`
```

`setup.sh` shows this snippet if it detects existing agents (choose `[m]` for manual).

## Brain Protocol

Every agent must follow this before starting work:

```
1. pre_check(agent, area, action_description)
   → See past failures. Adjust approach if warnings.

2. log_decision(agent, repo, area, action, reasoning)
   → Record your plan before implementing.

3. [do the work]

4. log_outcome(decision_id, outcome, outcome_by, reason)
   → Record what happened after review.
```

This is enforced in every agent's `.md` file as NON-NEGOTIABLE.

## SAN Protocol

Structured Associative Notation compresses code by ~85% while preserving all facts. See [`san/README.md`](san/README.md) for the full spec.

```
# Before: 80 lines, ~1200 tokens
class AuthServiceImpl(...) : AuthService { ... }

# After: ~150 tokens
AuthServiceImpl @svc {
  impl: AuthService iface
  deps: UserRepository + TokenProvider + RateLimiter
  fn:login(email, pwd) → AuthResult [validate → verify → issue_jwt]
  fn:register(RegisterRequest) → AuthResult [validate → create → issue_jwt]
  layer: application/service
  patterns: DIP-clean
}
```

## Adaptive Warnings

Agents with high rejection rates get progressively stricter warnings:

| Rejection Rate | Warning Level | Behavior |
|---------------|--------------|----------|
| < 30% | NORMAL | Standard pre_check |
| 30-50% | ELEVATED | "Pay close attention to past failures" |
| > 50% | STRICT | Shows top rejection patterns, demands extra scrutiny |

## Office Dashboard (Live Visualization)

A pixel art virtual office that shows your agents working in real-time. Agents move between desks and the meeting table, show speech bubbles during discussions, and display status indicators.

```bash
python dashboard/server.py
# Opens http://localhost:3333 in your browser
```

**Features:**
- Pixel art office with desks, meeting table, whiteboard, coffee machine
- Agents animate: idle bob, working (typing), walking, discussing (gestures)
- Status dots: 🟢 working, 🟡 planning, 🟠 reviewing, 🔵 discussing, 🔴 blocked, ⚫ offline
- Speech bubbles with actual message content
- Chat log sidebar with all agent interactions
- Team status panel with live agent list

**How it works:**
1. Brain tools (`pre_check`, `log_decision`, etc.) auto-update agent status — **zero changes to your agents needed**
2. For richer state (idle, messages, discussing), agents can call `heartbeat()` explicitly
3. Dashboard reads `~/.agent-brain/office-state.json` via SSE (polls every 500ms)
4. Canvas renders pixel art at 60fps with smooth agent movement

**Auto-heartbeat** (free, no agent changes):
| Brain Tool | Dashboard Status |
|-----------|-----------------|
| `pre_check` | Agent shows as "planning" |
| `log_decision` | Agent shows as "working" |
| `log_outcome` | Reviewer shows as "reviewing" |
| `log_feedback` | Reviewer shows as "reviewing", linked to target agent |

**Explicit heartbeat** (richer state):
```
heartbeat(agent="arjun", status="discussing", talking_to="marcus", message="DIP violation in AuthService?")
```
→ Both agents walk to meeting table, speech bubbles appear, message shows in chat log.

> **Tip**: Add `heartbeat(agent="<name>", status="idle")` to agent templates for when they finish a task. Otherwise agents stay at their last status until the 2-minute timeout.

## Verification

After setup, restart Claude Code and ask any agent to call `brain_stats()`. Expected output:

```
Brain Stats:
  Nodes: 0 | Edges: 0
  Decisions: 0 | Feedback: 0 | Code refs: 0
  Areas: none
  Repos: none
  Agents: none
```

**Troubleshooting:**

| Problem | Fix |
|---------|-----|
| `brain_stats` not found | Restart Claude Code. Check `claude mcp list` shows `agent-brain`. |
| MCP connection error | Check venv: `~/.agent-brain/.venv/bin/python -c "import mcp, networkx"` |
| No tools registered | Verify: `~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py` shouldn't error |
| `config.json` not found | Server works without it (empty brain). Create one if you want repo integration. |
| `AGENT_BRAIN_DIR` not set | Defaults to `~/.agent-brain/`. Set the env var only if you want a custom location. |

## SAN Setup

SAN (Structured Associative Notation) compresses source code by ~85% for LLM context. This is **optional** — the decision memory works without it.

1. **Create `.san/` in your repo:**
   ```bash
   mkdir -p your-repo/.san
   ```

2. **Generate SAN files** using the brain-compiler agent (see `san/brain-compiler.md`):
   ```
   # In Claude Code, spawn the brain-compiler agent:
   # "Convert src/services/AuthService.kt to SAN"
   ```
   The compiler writes `your-repo/.san/src/services/AuthService.san`.

3. **Build the index:**
   ```
   # Call update_san_index("my-backend") via any agent
   ```

4. **Query SAN:**
   ```
   query_san("my-backend", "Auth")      # search by keyword
   get_san("my-backend", "src/services/AuthService.kt")  # get specific file
   check_san_freshness("my-backend")    # find stale files
   ```

> **Commit `.san/` to git.** SAN files are prebuilt knowledge — they help any developer (or agent) working on the project. Don't `.gitignore` them.

## Requirements

- Any MCP-compatible AI code agent (Claude Code, Cursor, Windsurf, Cline, etc.)
- Python 3.10+
- Optional: `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` for multi-agent orchestration
- Optional: [code-review-graph](https://github.com/nicobailey/code-review-graph) for code bridge features

## Configuration

Edit `~/.agent-brain/config.json`:

```json
{
  "repos": {
    "my-backend": "/absolute/path/to/backend",
    "my-frontend": "/absolute/path/to/frontend"
  },
  "team": [
    {"name": "marcus", "role": "principal-engineer"},
    {"name": "arjun", "role": "backend-engineer"}
  ]
}
```

## Customization

### Adding more agents
Copy any template, rename, change the `{{PLACEHOLDER}}` values.

### Adding domain terms
Edit `_DOMAIN_TERMS` in `server.py` to boost similarity matching for your domain.

### Custom warning thresholds
Edit `_adaptive_warning_level()` in `server.py`.

## License

MIT
