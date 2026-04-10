# Architecture

## 8-Layer Stack

```
Layer 1: Agent Team         — 6+ agents with defined roles
Layer 2: Decision Brain     — persistent decision memory (MCP)
Layer 3: Code Graph Bridge  — links decisions to code nodes
Layer 4: Smart Patterns     — fuzzy similarity matching
Layer 5: Agent Scorecards   — adaptive warnings + trends
Layer 6: Caveman            — 75% token reduction in communication
Layer 7: SAN Project Brain  — 85% token reduction in knowledge
Layer 8: Specialist Skills  — VoltAgent/domain expertise delegation
```

## Data Flow

```
Agent starts task
    ↓
pre_check() ← queries decision graph
    ↓ warnings or clear
log_decision() → writes to decision graph
    ↓ linked to code-review-graph nodes
Agent does work
    ↓
Reviewer (PE/QA) reviews
    ↓
log_outcome() → accepted/rejected
log_feedback() → detailed feedback
    ↓
Next agent, same area → pre_check() sees rejection
    ↓
Avoids same mistake. Learning loop closed.
```

## Storage

```
~/.agent-brain/
├── server.py           # MCP server (Python, stdio transport)
├── config.json         # Repo paths + team config
├── decisions.json      # NetworkX DiGraph (node_link_data format)
└── .venv/              # Python venv (mcp + networkx)
```

### Decision Node Schema
```json
{
  "id": "dec_20260410_100000_abc123",
  "type": "decision",
  "agent": "arjun",
  "repo": "my-backend",
  "area": "auth",
  "action": "Implement rate limiting on login",
  "reasoning": "Prevent brute force",
  "files": ["src/routes/AuthRoutes.kt"],
  "code_symbols": ["src/routes/AuthRoutes.kt::login"],
  "timestamp": "2026-04-10T10:00:00",
  "outcome": "rejected",
  "outcome_by": "marcus",
  "outcome_reason": "Use sliding window, not token bucket"
}
```

### Feedback Node Schema
```json
{
  "id": "fb_20260410_100100_def456",
  "type": "feedback",
  "agent": "marcus",
  "feedback": "Violates DIP — concrete middleware instead of interface",
  "severity": "blocker",
  "timestamp": "2026-04-10T10:01:00"
}
```

### Edge Types
- `feedback_on`: feedback → decision
- `touches`: decision → code_ref

## Code Bridge

When `code-review-graph` is installed, decisions auto-link to code nodes:

```
decision ──touches──→ code_ref (qualified_name)
                         ↕
              code-review-graph SQLite
              (callers, imports, tests)
```

This enables:
- "What decisions touched AuthService?" → `decisions_for_code`
- "Who changed this file and what happened?" → `decisions_for_file`
- "What's the blast radius of this decision?" → `code_impact`

## Similarity Engine

Fuzzy matching uses Jaccard similarity + domain-term boosting:

```
Score = Jaccard(tokens_a, tokens_b) + DomainBoost

DomainBoost = (shared_domain_terms / union_size) * 0.3
```

Domain terms include: interface, dependency, injection, middleware, rate, cache, auth, token, layer, architecture, violation, etc.

This means "rate limiting on signup" matches "token bucket rate limiting on login" even though the exact words differ.

## Adaptive Warning System

```
Agent rejection rate > 50% → STRICT
  - pre_check shows top rejection patterns
  - Demands extra scrutiny

Agent rejection rate 30-50% → ELEVATED
  - pre_check highlights past failures

Agent rejection rate < 30% → NORMAL
  - Standard pre_check
```

Warning level is per-agent, computed from their full history.
