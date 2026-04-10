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

Then:
1. Edit `~/.agent-brain/config.json` with your repo paths
2. Customize agents in `~/.claude/agents/`
3. Restart Claude Code
4. Test: ask any agent to call `brain_stats()`

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Your Machine (global)                          │
│                                                 │
│  ~/.agent-brain/                                │
│  ├── server.py        ← MCP server (19 tools)  │
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
└─────────────────────────────────────────────────┘
```

## MCP Tools (19)

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

## Requirements

- Claude Code (with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`)
- Python 3.10+
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
