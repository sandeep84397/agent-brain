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

### Linking a project (so subagents can use brain)

`./setup.sh` registers brain at the user level. That's enough for the **main Claude Code session**, but **subagents spawned inside a project** read MCP config from project-scoped files. Run:

```bash
./setup.sh --link-project=/absolute/path/to/your/project
```

This is **idempotent** and writes/merges:

- `<project>/.mcp.json` — adds the `agent-brain` server entry alongside any existing entries
- `<project>/.claude/settings.local.json` — sets `enableAllProjectMcpServers: true` and adds `agent-brain` to `enabledMcpjsonServers`
- `<project>/.gitignore` — appends `.mcp.json`, `.san/.san_hashes.json`, `.san/_cache/`

After running it, restart Claude Code in the project (`/exit` then `claude`), then verify:

```bash
~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py diagnose --project=/absolute/path/to/your/project
```

### How brain MCP reaches Claude Code subagents (4-layer model)

Brain tools work in BOTH the main Claude Code session AND spawned subagents only when all four layers are correctly configured:

| Layer | File | What it does | Set by |
|---|---|---|---|
| 1 | `~/.claude.json` *or* `~/.claude/settings.json` `mcpServers` | Registers `agent-brain` server for the main session | `setup.sh` (initial install) |
| 2 | `~/.claude/settings.local.json` `enabledMcpjsonServers` | User-level allowlist (only relevant if you use allowlist mode) | `setup.sh` (auto-detects allowlist; appends `agent-brain` if needed) |
| 3 | `<project>/.mcp.json` | Project-scoped server registration — **subagents read this** | `setup.sh --link-project=<path>` |
| 4 | `<project>/.claude/settings.local.json` `enableAllProjectMcpServers: true` + `enabledMcpjsonServers: ["agent-brain"]` | Project-level activation | `setup.sh --link-project=<path>` |

**Plus** the agent frontmatter rule (see [Already have custom agents?](#already-have-custom-agents) below): **omit the `tools:` field** so MCP tools are inherited. Setting `tools: [Read, Write, ...]` makes it an allowlist that silently strips every `mcp__*` tool, and `mcp__agent-brain__*` is not a valid wildcard.

After any config change, restart Claude Code (`/exit` then `claude`) — MCP and agent definitions are loaded at session start. Then run `server.py diagnose --project=<path>` to confirm all four layers are wired up.

## How To Use It

Once set up, **you don't call brain tools yourself** — your agents do, automatically, as part of their normal work. Your job is just to give agents tasks and (optionally) review the memory that builds up.

### The loop every agent runs

For any non-trivial task, an agent follows this cycle (enforced by the hook — see [Enforcement Hook](#enforcement-hook)):

```
1. pre_check(agent, area, action)     ← "has anyone tried this before? did it fail?"
2. log_decision(agent, repo, area,    ← records the plan; unlocks code edits
                action, reasoning)
3. … writes the code …
4. log_outcome(decision_id, outcome,  ← records accepted / rejected / failed + why
               outcome_by, reason)
```

You just say *"add rate limiting to the signup endpoint"*. The agent does the rest.

### Worked example — across two sessions

**Monday — a decision gets rejected:**

```
You:   "Add rate limiting to /login"
Agent: pre_check(agent="karan", area="auth", action="rate limit login")
       → "No past failures in 'auth'. Proceed."
Agent: log_decision(... action="in-memory counter per IP", reasoning="simplest")
       → dec_20260609_..._a1b2c3
Agent: …writes code, opens PR…
PE:    log_outcome(dec_..._a1b2c3, outcome="rejected", outcome_by="marcus",
                   reason="in-memory won't survive multi-instance deploy; use Redis")
```

**Friday — a different agent, a related task, a different machine/session:**

```
You:   "Add rate limiting to the signup endpoint"
Agent: pre_check(agent="dev", area="auth", action="rate limit signup")
       → "SIMILAR REJECTIONS (1, 78% match):
          [2026-06-09] karan tried: in-memory counter per IP
          REJECTED by marcus: in-memory won't survive multi-instance deploy; use Redis"
Agent: …goes straight to a Redis-backed limiter, skips the mistake…
```

No human re-explained the Redis constraint. The brain carried it forward.

### How it behaves

| When an agent calls… | What happens | What you see |
|----------------------|--------------|--------------|
| `pre_check` | Searches past decisions in the same area + fuzzy-matches similar actions across all areas | Agent mentions relevant past rejections before coding |
| `log_decision` | Writes the decision to `decisions.json` + drops a marker file | Code edits are now unblocked for ~30 min |
| Edit/Write **without** a recent `log_decision` | PreToolUse hook blocks the edit (exit 2) | Agent is forced to log a decision first, then retries |
| `log_outcome` (rejected) | Records the rejection; raises that agent's rejection rate | Future `pre_check`s surface it; repeat offenders get stricter warnings |
| Any tool with a `repo`/status | Updates the live office dashboard | Agent appears working/reviewing/blocked at `localhost:3333` |

### What you get out of it (outcomes)

| Outcome | How it helps |
|---------|--------------|
| **Mistakes aren't repeated** | A rejection logged once warns every agent, every future session — even on a different machine. |
| **No re-explaining context** | Constraints ("use Redis", "don't bypass the auth middleware") live in the brain, not in your head. |
| **Cross-agent learning** | What backend-engineer learns, frontend-engineer and QA see. Knowledge is team-wide, not per-agent. |
| **Accountability & trends** | Scorecards show each agent's acceptance rate and recurring failure patterns — `agent_scorecard("karan", detail=True)`. |
| **Auditable history** | "Why did we build it this way?" → `decisions_for("AuthService.login")` returns every decision that touched it, with reasoning and outcome. |
| **Enforced discipline** | The hook means the memory actually gets populated — agents can't silently skip logging and edit code anyway. |

### Inspecting the memory yourself

You rarely need to, but from the brain directory:

```bash
python3 brain/server.py stats        # overall health: how many decisions/agents/repos
python3 brain/server.py office       # who's working on what right now
```

From any agent/MCP client you can also ask in plain language — *"show me the team dashboard"*, *"what decisions touched the payment service?"*, *"what's karan's scorecard?"* — and the agent picks the right tool (`team_dashboard`, `decisions_for`, `agent_scorecard`).

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Your Machine (global)                          │
│                                                 │
│  ~/.agent-brain/                                │
│  ├── server.py        ← MCP server (16 tools)  │
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

## MCP Tools (16)

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

### Code Bridge
| Tool | Purpose |
|------|---------|
| `decisions_for` | Decisions touching a code symbol or file (auto-detected) |
| `code_impact` | Blast radius: code symbols + callers |

### Patterns
| Tool | Purpose |
|------|---------|
| `get_patterns` | Cluster recurring rejections; pass `action` to find similar past failures |

### Scorecards
| Tool | Purpose |
|------|---------|
| `agent_scorecard` | Stats for one/all agents; `detail=True` for trends + advice |
| `team_dashboard` | All agents at a glance (`limit` caps rows) |

### Office Dashboard
| Tool | Purpose |
|------|---------|
| `heartbeat` | Report agent status (working/idle/discussing/blocked) for live dashboard |
| `detect_stalls` | Find agents with open decisions but no activity for N minutes (default 5) |

### SAN (Structured Associative Notation)
| Tool | Purpose |
|------|---------|
| `recompile_san` | Refresh SAN metadata: rebuild index, clean orphans, update hashes. `dry_run=True` for a freshness report only. Does NOT generate content. |
| `query_san` | Search SAN files by keyword (index + content) |
| `get_san` | Get SAN-compressed content for a source file (`max_chars` caps output) |

### Admin (CLI only — not exposed via MCP)
Run from the brain directory to keep the agent-facing tool surface lean:
```bash
python3 brain/server.py validate        # full brain self-tests (79 checks)
python3 brain/server.py validate-san    # SAN subsystem self-tests
python3 brain/server.py san-index <repo> # rebuild _index.json from .san/
python3 brain/server.py stats           # overall brain health
python3 brain/server.py office [repo]    # current office state (debug)
```

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

**Critical: do NOT set the frontmatter `tools:` field.** Claude Code subagents inherit ALL tools from the parent session — including every `mcp__agent-brain__*` tool — *only when `tools:` is omitted*. Setting it (even with `ToolSearch` included) turns it into a literal allowlist that silently strips MCP tools, because `mcp__*` is not a valid wildcard. Reference: [Claude Code subagents — Available tools](https://code.claude.com/docs/en/sub-agents#available-tools).

```yaml
---
name: my-agent
description: ...
model: claude-sonnet-4-6
# No `tools:` — inherits everything from the parent session, including MCP.
# To restrict tools, use `disallowedTools:` instead.
---
```

> **What if I really must restrict tools?** Add `ToolSearch` to your `tools:` allowlist
> and bootstrap brain tools at the top of every task with
> `ToolSearch(query="agent-brain", max_results=25)`. This is a fallback for the rare
> case where you genuinely need a tool denylist; for normal use, omit `tools:` entirely.

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

### Enforcement Hook

Text in `.md` files is advisory — agents can skip it. The enforcement hook makes it **mandatory**: any Edit/Write to code files is blocked if no `log_decision` was called in the last 30 minutes.

**How it works:**
1. `log_decision()` writes a marker file (`~/.agent-brain/.last_decision_marker`)
2. A PreToolUse hook fires before every Edit/Write
3. If marker is missing or stale (>30min), the hook blocks with exit code 2
4. Claude sees the block reason and calls `log_decision` before retrying

**Skips** (no block): `.md`, `.json`, `.yaml`, `.toml`, config files, `.claude/`, `.git/`, `.san/`, `node_modules/`, `build/`

**Custom skip patterns** — extend the built-in skip list with fnmatch globs in `~/.agent-brain/config.json`:

```json
{
  "hook_skip_paths": [
    "**/docs/**",
    "**/.github/**",
    "**/CHANGELOG*",
    "**/migrations/**"
  ]
}
```

Patterns are matched against the absolute file path. The hook fails open: an invalid `hook_skip_paths` value is ignored silently rather than blocking your session.

**Install** (setup.sh does this automatically):
```json
// ~/.claude/settings.json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/agent-brain/brain/hooks/enforce_brain_protocol.py",
            "timeout": 5000
          }
        ]
      }
    ]
  }
}
```

> **Fail-open**: If the marker file is corrupt or the hook script errors, it allows the edit (exit 0). The hook never crashes your workflow — it only blocks when it's confident no decision was logged.

> **Bypass for direct edits**: The hook fires on all Edit/Write — agents and user alike. To skip enforcement when you're editing directly, set `BRAIN_SKIP_ENFORCE=1` in your shell before launching Claude Code, or add it to your settings.json env block:
> ```json
> { "env": { "BRAIN_SKIP_ENFORCE": "1" } }
> ```
> Agents spawned via the team system won't inherit this, so enforcement stays active for them.

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

After setup, run the full validation from the brain directory:

```bash
python3 brain/server.py validate
# Expected: "Agent Brain Validation: 79 passed, 0 failed ✓ ALL TESTS PASSED"
```

This tests every subsystem in isolation using a temp directory:

| Section | Tests | What's validated |
|---------|-------|-----------------|
| Graph Persistence | 4 | Save/load, atomic writes, empty state |
| Decision Memory | 10 | log_decision, log_outcome, log_feedback, error handling |
| Pre-check & Warnings | 7 | Exact matches, similar rejections, adaptive warning levels |
| Similarity Matching | 6 | Tokenizer, Jaccard + domain boost, false positive rejection |
| Pattern Clustering | 1 | DIP-related rejections cluster together |
| Scorecards & Dashboard | 9 | Acceptance rates, trends, team_dashboard rendering |
| Query & Retrieval | 6 | Filters, missing ID handling, file-based search |
| Code Bridge | 4 | Symbol linking, callers, impact radius |
| Office State | 11 | Heartbeat, role resolution, messages, auto-heartbeat |
| Config & Edge Cases | 3 | Missing/corrupt config and graph files |
| SAN System | 23 | Hashing, orphan cleanup, staleness, index building |
| Integration Workflow | 9 | Full end-to-end: pre_check → decide → reject → feedback → re-check |

You can also run just the SAN subsystem: `python3 brain/server.py validate-san`

Or verify basic connectivity with `python3 brain/server.py stats`:

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
| brain tools not found | Restart Claude Code. Check `claude mcp list` shows `agent-brain`. |
| MCP connection error | Check venv: `~/.agent-brain/.venv/bin/python -c "import mcp, networkx"` |
| No tools registered | Verify: `~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py` shouldn't error |
| `config.json` not found | Server works without it (empty brain). Create one if you want repo integration. |
| `AGENT_BRAIN_DIR` not set | Defaults to `~/.agent-brain/`. Set the env var only if you want a custom location. |
| Anything looks off | Run `~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py diagnose` for a full health report (no Claude session needed). |

### Diagnose CLI

```bash
~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py diagnose [--project=/path/to/project]
```

Runs a standalone health check from the shell — no Claude session required.

**Always verifies:**

1. MCP tools are registered in the server
2. `config.json` is valid JSON (or absent — empty brain is OK)
3. `~/.agent-brain/` is writable (decision marker round-trip)
4. `decisions.json` is readable if present
5. `agent-brain` is registered as an MCP server in `~/.claude.json` and/or `~/.claude/settings.json` (layer 1)
6. Every `~/.claude/agents/*.md` is **subagent-MCP-safe**: omits the `tools:` frontmatter field (preferred — inherits MCP) **or** lists `ToolSearch` in `tools:` (fallback bootstrap)
7. Per-repo team resolution: which agents the brain considers in-team for each configured repo

**With `--project=<path>`, also verifies:**

8. `<project>/.mcp.json` exists and registers `agent-brain` (layer 3)
9. `<project>/.claude/settings.local.json` enables project MCP and allowlists `agent-brain` (layer 4)
10. `<project>/.gitignore` covers brain artifacts (informational)

Exit code is `0` when all checks pass, `1` otherwise — safe to call from a CI pre-flight or a dotfiles bootstrap.

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
   ```bash
   python3 brain/server.py san-index my-backend   # admin CLI; recompile_san also rebuilds it
   ```

4. **Query SAN:**
   ```
   query_san("my-backend", "Auth")      # search by keyword
   get_san("my-backend", "src/services/AuthService.kt")  # get specific file
   recompile_san("my-backend", dry_run=True)    # find stale files
   ```

### SAN Commands

| Command | What it does |
|---------|-------------|
| `recompile_san("repo", dry_run=True)` | Report which SANs are stale, missing, or orphaned vs source (no changes) |
| `recompile_san("repo")` | Refresh metadata: rebuild index, clean orphans, update hashes. Does NOT generate SAN content. |
| `query_san("repo", "keyword")` | Search SAN index + file contents by keyword |
| `get_san("repo", "src/path/File.kt")` | Get SAN-compressed content for a source file (`max_chars` caps output) |
| `python3 brain/server.py san-index <repo>` | (CLI) Rebuild `_index.json` from all `.san` files |
| `python3 brain/server.py validate-san` | (CLI) 23 self-tests: hashing, orphan cleanup, staleness, index building. Isolated temp dir. |

### How SAN Generation Works

SAN files are **only generated by the brain-compiler agent** (LLM-powered). The server itself does NOT generate SAN content — it only manages metadata, detects staleness, and cleans up orphans. Asking the agent-brain MCP server to "generate SAN" does nothing; spawn the brain-compiler agent instead.

**Workflow:**
1. Brain-compiler generates rich SAN files (dependencies, patterns, execution flow)
2. Server tracks source hashes to detect when SANs become stale
3. `recompile_san(dry_run=True)` / `query_san` / `get_san` report stale SANs
4. You re-run brain-compiler on stale files to regenerate

### Which model to use for generation

**Use Sonnet.** SAN conversion is mechanical (read source → emit facts in SAN notation) — it doesn't need a frontier model, and you'll be converting hundreds of files. The bundled [`san/brain-compiler.md`](san/brain-compiler.md) agent already pins this:

```yaml
model: claude-sonnet-4-6   # cheap, fast, accurate enough for mechanical conversion
```

Spend the savings where it matters: your engineering agents *consuming* SAN can run on bigger models, since SAN cuts their input cost ~85-90% anyway. Only escalate the compiler to a bigger model if you find SAN files missing relationships on gnarly, highly-dynamic code.

### Generating SAN from other platforms (ChatGPT, Cursor, etc.)

The **MCP server is platform-agnostic** — any MCP client can call `query_san`/`get_san`/`recompile_san`. Only the brain-compiler *agent template* is Claude Code specific. SAN files themselves are plain text, so any capable LLM can generate them:

1. Give the model the SAN spec ([`san/README.md`](san/README.md)) + the source file
2. Save its output to `<repo>/.san/<source-path>.san` (mirror the source tree, swap extension to `.san`)
3. Rebuild the index: `python3 brain/server.py san-index <repo>` (or call `recompile_san("<repo>")` from any MCP client)

The server's hash-based staleness tracking works identically regardless of which model wrote the file. Cheap-tier models on other platforms (e.g. GPT-4o-mini class) generally handle the conversion; verify a few files against the spec before bulk-converting.

### Content Hashing

SAN staleness detection uses **sha256 content hashing** to avoid false positives:

- Source file hashes are stored in `.san/.san_hashes.json`
- When checking freshness, if the source content hash matches the stored hash, the file is skipped (even if mtime changed)
- This catches false positives from `git checkout`, `git stash pop`, `touch`, or editor save-without-change
- Hashes are updated when `recompile_san` runs

### Orphan Cleanup

When a source file is deleted, its SAN file becomes an orphan. Orphans are detected and cleaned up automatically:

- Every source tracked in `.san_hashes.json` is checked for existence
- If the source is gone, the corresponding `.san` file and hash entry are removed
- `.san` files with no matching source (even if not in hash tracker) are also cleaned up
- Stats report `orphans_removed` so you can see what was cleaned up

> **Important: SAN refresh is NOT automatic.** Staleness checks run when you call `query_san`, `get_san`, or `recompile_san(dry_run=True)` — they report stale SANs but do NOT regenerate them. To force a full metadata refresh (e.g., after a large merge or branch switch), call `recompile_san("repo")`. To regenerate stale SAN content, run the brain-compiler agent on the reported files.

> **Commit `.san/` to git.** SAN files are prebuilt knowledge — they help any developer (or agent) working on the project. Don't `.gitignore` them. Add `.san/.san_hashes.json` to `.gitignore` — it's a local cache.

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

### Per-repo team scoping

A flat `team` list applies to every repo — fine when one team owns everything. When you run multiple repos with different staffing, scope members per repo so heartbeats from `arjun` on `my-backend` don't pollute the `my-frontend` office state.

**Two ways to scope:**

1. **Per-entry `repos` filter** (simplest — extends the flat list):
   ```json
   {
     "team": [
       {"name": "marcus", "role": "principal-engineer"},
       {"name": "arjun",  "role": "backend-engineer",  "repos": ["my-backend"]},
       {"name": "priya",  "role": "frontend-engineer", "repos": ["my-frontend"]}
     ]
   }
   ```
   - `marcus` has no `repos` → global, applies to every repo.
   - `arjun` only resolves on `my-backend`.
   - `priya` only resolves on `my-frontend`.

2. **`teams_per_repo` override** (full replacement for one repo):
   ```json
   {
     "team": [ /* default global team */ ],
     "teams_per_repo": {
       "experimental-repo": [
         {"name": "marcus", "role": "principal-engineer"},
         {"name": "neha",   "role": "product-owner"}
       ]
     }
   }
   ```
   When `teams_per_repo[repo]` is present, the flat `team` list is ignored for that repo.

**Backwards compatible**: configs without `teams_per_repo` and no `repos` field on entries behave exactly like before.

**How it's used:** brain tools that take a `repo` arg (`heartbeat`, `log_decision`, etc.) feed it through `_get_team_for_repo()` for role resolution and dashboard filtering. The `office` CLI command (`python3 brain/server.py office my-backend`) shows only that repo's agents.

## Customization

### Adding more agents
Copy any template, rename, change the `{{PLACEHOLDER}}` values.

### Adding domain terms
Edit `_DOMAIN_TERMS` in `server.py` to boost similarity matching for your domain.

### Custom warning thresholds
Edit `_adaptive_warning_level()` in `server.py`.

## License

MIT
