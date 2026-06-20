# Agent Brain

Persistent decision memory for AI code agent teams. Agents learn from mistakes, coordinate across sessions, and never repeat the same error twice — and read your codebase at ~20% of the usual token cost (tokenizer-measured: 81% saved).

**Works with any [MCP](https://modelcontextprotocol.io) (Model Context Protocol)-compatible agent**: Claude Code, Cursor, Windsurf, Cline, Continue, etc. Agent templates (`.md` files) are Claude Code specific — the MCP server itself is universal.

## Contents

- [What This Does](#what-this-does) · [Features](#features)
- [Quick Start](#quick-start) — install in 2 minutes
- [How To Use It](#how-to-use-it) — the agent loop, a worked example, what you get
- [Architecture](#architecture) — what lives where, [performance & internals](#performance--internals)
- [MCP Tools (21)](#mcp-tools-21) — the agent-facing API
- [Agent Team](#agent-team) — bundled role templates
- [Model Routing](#model-routing-quality-per-cost) — right model per phase, two-strikes escalation, plan handoff
- [Brain Protocol](#brain-protocol) — the enforced decision loop
- [SAN Protocol](#san-protocol) — code compression: is it worth it, measuring savings (`token_savings`)
- [SAN Setup](#san-setup) — turning SAN on, model choice, other platforms
- [Adaptive Warnings](#adaptive-warnings) · [Office Dashboard](#office-dashboard-live-visualization) — live pixel-art team view
- [Verification](#verification) · [Requirements](#requirements) · [Configuration](#configuration) · [Customization](#customization)

## What This Does

AI coding agents start fresh every session: no memory of past decisions, no learning from rejections, no cross-agent knowledge sharing — and they burn tokens re-reading the same source files task after task. Agent Brain fixes both:

1. **Memory** — decisions, outcomes, and review feedback persist across sessions and agents:

```
Agent    → pre_check()    → "WARNING: similar approach was rejected last week"
Agent    → log_decision() → records what you decided and why
Agent    → does work      → PR created
Reviewer → log_outcome()  → "rejected: violates DIP (dependency inversion)"
Next time, any agent → pre_check() → sees that rejection → avoids the mistake
```

2. **Cheap code reading** — the optional [SAN protocol](#san-protocol) compresses source files to ~17-27% of their original tokens (81% saved, tokenizer-measured), and [`token_savings`](#measuring-your-savings-token_savings) shows you exactly how much it saved, per session, in numbers and %.

## Features

| Feature | What it does |
|---------|-------------|
| **Decision Memory** | Log decisions, outcomes, feedback. Persists across sessions. |
| **Pre-Check Warnings** | Before starting work, see past failures in the same area. |
| **Fuzzy Matching** | "Rate limiting on signup" finds "rate limiting on login" rejection. |
| **Code Bridge** | Link decisions to code symbols: "Show me all decisions that touched AuthService." (Richer with the optional code-review-graph MCP server; works standalone too.) |
| **Agent Scorecards** | Acceptance rate, trends, top rejection categories per agent. |
| **Adaptive Warnings** | Agents with high rejection rates get stricter pre-check warnings. |
| **Team Dashboard** | All agents at a glance — for project managers. |
| **SAN Protocol** | Compress code to ~20% of original tokens (81% saved, measured). Full codebase fits in context. |
| **Token Savings Tracker** | [`token_savings`](#measuring-your-savings-token_savings) reports tokens saved this session / today / all-time, with %. |
| **Enforcement Hook** | Code edits are blocked until the agent logs a decision — memory actually gets populated. |
| **Survives compaction** | SessionStart hook re-injects the pending roadmap after `/compact` so the agent resumes instead of re-researching. [Details](#surviving-compaction-the-amnesia-fix) |
| **Relevance search** | `query_decisions(query="…")` ranks by topic relevance, not just recency; `get_roadmap` returns open work in one call. |
| **SAN as default read path** | A soft hook nudges raw `Read` of SAN-covered code toward `get_san`; `get_san` takes absolute paths so there's no friction. Same quality, ~5-11x fewer tokens. [Details](#making-san-the-default-read-path) |
| **Records & pruning** | Browse every decision as dated markdown; prune old/resolved ones (dry-run first, archived not deleted). Keeps the brain lean without losing the lessons. [Details](#managing-what-the-brain-remembers) |

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

**Where things land:** `setup.sh` installs a copy of the server to `~/.agent-brain/` with its own venv — that copy is what Claude Code runs. The repo checkout keeps the source. CLI examples in this README use `python3 brain/server.py <cmd>` from the repo root; against the installed copy, the equivalent is `~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py <cmd>`. If you edit the repo copy, re-copy it to `~/.agent-brain/server.py` (or re-run `setup.sh`) and restart Claude Code.

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

> Subagents not seeing brain tools? See [the 4-layer model](#how-brain-mcp-reaches-claude-code-subagents-4-layer-model) under Verification.

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
| `log_decision` | Appends the decision to the journal + drops a marker file | Code edits are now unblocked for ~30 min |
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

You rarely need to, but from the repo root (where you cloned agent-brain):

```bash
python3 brain/server.py stats        # overall health: how many decisions/agents/repos
python3 brain/server.py office       # who's working on what right now
python3 brain/server.py savings      # tokens SAN saved (last session / today / all-time)
```

From any agent/MCP client you can also ask in plain language — *"show me the team dashboard"*, *"what decisions touched the payment service?"*, *"what's karan's scorecard?"*, *"how many tokens did SAN save this session?"* — and the agent picks the right tool (`team_dashboard`, `decisions_for`, `agent_scorecard`, `token_savings`).

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Your Machine (global)                          │
│                                                 │
│  ~/.agent-brain/                                │
│  ├── server.py        ← MCP server (17 tools)  │
│  ├── config.json      ← your repos + team      │
│  ├── decisions.json   ← memory snapshot         │
│  └── decisions.journal← append-only deltas      │
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

### Performance & internals

The brain is built to stay fast as the decision history grows into thousands of entries:

| Concern | How it's handled |
|---------|------------------|
| **Reading the graph** | `decisions.json` is parsed once and held in an in-memory cache keyed on file mtime+size — repeat tool calls in a session reuse it (~0.03ms vs ~140ms re-parse). The cache self-invalidates if another session writes the file. |
| **Writing a decision** | Writes are **O(delta), not O(graph)**. `decisions.json` is a periodic full snapshot; `decisions.journal` is an append-only log of mutations. Logging an outcome on a ~4MB brain appends ~800 bytes instead of rewriting 4MB. The journal auto-compacts back into the snapshot once it passes 256KB. |
| **SAN freshness** | The freshness sweep (stat every indexed file + scan the `.san/` tree) is debounced to once per 60s per repo, so bursts of `get_san`/`query_san` calls don't each pay for it. |
| **Bounded responses** | Every list/detail tool caps its output (row limits, per-field truncation) so one giant decision can't blow up a response. Stored text fields are capped at write time too. |
| **Multi-session safety** | Each Claude Code session runs its own server process sharing `~/.agent-brain/`. Writes use `os.replace` with pid-unique temp files to avoid cross-process rename collisions. |

> **Files in `~/.agent-brain/`:** `decisions.json` (snapshot) + `decisions.journal` (deltas) are the decision memory — both are needed; don't delete one without the other. `office-state.json` is live dashboard state (self-pruning), `san_savings.jsonl` is the token-savings log. All are per-machine and git-ignored.

## MCP Tools (21)

### Core (every agent uses these)
| Tool | Purpose |
|------|---------|
| `pre_check` | Past failures before starting work + plan pointers, escalation hints, model routing, SAN coverage (pass `repo=`) |
| `log_decision` | Record what you decided and why; optional `plan_file` links a written plan |
| `log_outcome` | Record accepted/rejected/failed after review |
| `log_feedback` | Reviewers log feedback on decisions |

### Query
| Tool | Purpose |
|------|---------|
| `query_decisions` | Filter by area/agent/repo/outcome **and** rank by free-text relevance (`query="..."`) — finds decisions about a topic without knowing the exact area |
| `get_decision` | Full detail + feedback for one decision |
| `get_roadmap` | What's left to do — pending + roadmap/blocker-tagged work, ranked. One call to resume context after a fresh session or compaction |

### Records & pruning (keep the brain lean)
| Tool | Purpose |
|------|---------|
| `export_records` | Write a human-readable audit trail to `~/.agent-brain/records/` — one markdown file per day, browsable via `INDEX.md` |
| `prune_decisions` | Forget old, resolved decisions: archives to `decisions.archive.jsonl` (recoverable) **and** removes from the live graph. **Dry-run by default**; keeps rejections + roadmap (the learning) |
| `resolve_stale_pending` | Mark long-abandoned `pending` decisions as `superseded` so they stop polluting `get_roadmap`. Dry-run by default |

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
| `query_san` | **Find code instead of grepping raw source** — keyword search over index + content, far fewer tokens |
| `get_san` | **Read code instead of raw `Read`** — same structure, ~5x fewer tokens (`detail="full"`) / ~11x (`detail="sig"`). `file_path` may be **relative or absolute** (repo auto-detected); `max_chars` caps output |
| `token_savings` | Tokens saved by SAN this session / today / all-time — number + % |

### Admin (CLI only — not exposed via MCP)
Run from the repo root. These live on the CLI (not MCP) to keep the agent-facing tool surface lean:
```bash
python3 brain/server.py validate        # full brain self-tests (117 checks)
python3 brain/server.py validate-san    # SAN subsystem self-tests
python3 brain/server.py san-index <repo> # rebuild _index.json from .san/
python3 brain/server.py stats           # overall brain health
python3 brain/server.py office [repo]    # current office state (debug)
python3 brain/server.py savings         # SAN token savings (last session / today / all-time)
python3 brain/server.py roadmap [repo]   # open-work digest (same as get_roadmap)
python3 brain/server.py records [repo]   # export the dated records dir
python3 brain/server.py prune [repo] [--before-days=N] [--apply]  # forget old decisions (dry-run default)
python3 brain/server.py resolve-stale [repo] [--apply]            # mark abandoned pending as superseded
```

### Managing what the brain remembers

The brain only grows — it learns, it never forgets on its own. That's good for the *valuable* memory (what failed and why) but old, resolved decisions eventually add noise to `pre_check` and inflate `get_roadmap`. Two safe ways to keep it lean:

**1. Browse the records.** Every decision is exportable as dated, human-readable markdown:
```bash
python3 brain/server.py records          # writes ~/.agent-brain/records/YYYY-MM-DD.md + INDEX.md
open ~/.agent-brain/records/INDEX.md     # browse by date
```
Each entry shows the date, repo, area, agent, action, and outcome. The records are **regenerated from the graph** — deleting a day file just re-renders on the next export, so the graph stays the source of truth. To *actually* forget something, prune it.

**2. Prune (or let the AI do it).** `prune_decisions` is **dry-run by default** — it shows what *would* go without touching anything:
```bash
python3 brain/server.py prune --before-days=90          # preview: old, resolved decisions
python3 brain/server.py prune --before-days=90 --apply  # archive + remove for real
```
What it does on `--apply`:
- **Archives** each pruned decision to `~/.agent-brain/decisions.archive.jsonl` (recoverable — nothing is hard-deleted).
- **Removes** it from the live graph, so `pre_check`/`get_roadmap` stop surfacing it.
- **Keeps the learning**: rejections/failures and `roadmap`/`blocker`-tagged decisions are never pruned.
- **Re-exports** the records dir to reflect the change.

You can also just ask an agent: *"prune decisions older than 3 months, dry run first"* — it calls `prune_decisions(before_days=90, dry_run=True)`, shows you the list, and only applies after you confirm.

> **Is unbounded growth a problem?** Disk is trivial (~28 MB/year) and reads stay sub-millisecond (mtime-cached). The real cost is *relevance noise* — a 2-year-old rejected approach surfacing as a "similar failure". Prune resolved/old decisions periodically; keep the rejections, which are the cheapest lesson you'll ever get.

## Agent Team

The repo includes 6 agent templates. Each has the Brain Protocol baked in:

| Role | File | Responsibility | Pinned model |
|------|------|---------------|--------------|
| Project Manager | `project-manager.md` | Coordination, tracking, blockers | Haiku (cheap coordination) |
| Product Owner | `product-owner.md` | PRDs, acceptance criteria | Sonnet |
| Principal Engineer | `principal-engineer.md` | Architecture, SOLID, reviews | Opus (review is high-leverage) |
| Backend Engineer | `backend-engineer.md` | API, services, data layer | Sonnet |
| Frontend Engineer | `frontend-engineer.md` | UI, app logic, integration | Sonnet |
| QA Engineer | `qa-engineer.md` | Test plans, validation, quality gates | Sonnet |

Model pins live in each template's `model:` frontmatter — change them to fit your budget. See [Model Routing](#model-routing-quality-per-cost) for the full strategy.

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

## Model Routing (quality per cost)

Spend the expensive model where mistakes are costly to undo; spend the cheap ones where mistakes are cheap to fix. The brain supports this in three layers:

### 1. Per-role model pins

Each agent template pins a model in frontmatter (`model: claude-sonnet-4-6`). Defaults follow the phase-cost logic:

| Phase | Work | Model | Why |
|-------|------|-------|-----|
| Plan / architecture | System design, module boundaries, implementation plan | Fable / Opus | A wrong architecture costs days of rework; one good plan makes every later step cheaper |
| Scaffolding / boilerplate | Project setup, DI wiring, data classes, mappers | Sonnet / Haiku | Pattern-matching, not reasoning — executing against the plan, not deciding |
| Core / complex logic | Encryption flows, state machines, tricky concurrency | Opus, escalate on failure | Start mid-tier; escalate only when the data says so (see below) |
| Review | Architecture + code review of cheap-model output | Opus / Fable | Read-heavy, write-light — high leverage per output token |
| Tests / docs / polish | Unit tests against spec, KDoc, README | Sonnet / Haiku | Cheap-model territory |

### 2. `model_routing` config

Declare your routing once in `~/.agent-brain/config.json`:

```json
"model_routing": {
  "plan": "fable",
  "implement": "sonnet",
  "review": "opus",
  "boilerplate": "haiku",
  "escalate": "fable"
}
```

Every `pre_check` response then ends with one line —
`MODEL ROUTING: plan=fable | implement=sonnet | review=opus | boilerplate=haiku | escalate=fable` —
so whatever agent is orchestrating spawns subagents on the right tier without you re-explaining the strategy each session. Omit the key and the line disappears.

### 3. Two-strikes escalation (data-driven)

Repeated failed attempts on a cheap model can cost more than one clean shot on a strong one — but you don't know which problems are "strong-model problems" until the cheap model stumbles. The brain already logs every rejection, so it applies the two-strikes rule automatically:

> When the **same agent** has **≥2 rejected/failed decisions** in the **same area**, `pre_check` returns:
> `ESCALATION HINT: 'arjun' has 2 rejected/failed decisions in 'auth'. Two-strikes rule: do NOT retry on the same model tier — re-spawn this task on fable.`

The escalation target comes from `model_routing.escalate` (generic wording if unset). This is per-agent — another agent entering the same area is not escalated by someone else's failures.

### 4. Plan files as handoff artifacts

Pay for deep thinking once, reuse it across many cheap executions. The planner writes the plan to a file and logs it:

```python
log_decision(agent="marcus", repo="my-app", area="payments",
             action="Designed payment module architecture",
             reasoning="...", plan_file="docs/plans/payments-plan.md")
```

Every later `pre_check` in that area surfaces it:

```
PLAN AVAILABLE: docs/plans/payments-plan.md (by marcus, 2026-06-12).
Read it before re-deriving the approach — execute against it, don't re-plan.
```

The pointer stays active while the decision is `pending` or `accepted`; a rejected plan stops being advertised.

### Cost mechanics that matter as much as model choice

- **Context discipline beats model choice.** A Sonnet call with clean context beats an Opus call drowning in irrelevant files. SAN reads (~20% of raw cost) + `pre_check` (past failures only, not full history) are the brain's context discipline.
- **The orchestrator burns its own tokens.** Spawning a Sonnet subagent from an Opus session still pays Opus rates for coordination. Cheapest pattern: cheap main session as orchestrator, escalate *via subagents* — not an expensive main session delegating down.
- **Fewer turns > cheaper tokens.** For a genuinely complex task, a strong model finishing in fewer turns can land near mid-tier pricing. Don't be dogmatic — the two-strikes hint exists precisely to catch this case from real outcome data.

Rough split to aim for: ~70% of tokens on Sonnet-tier, ~25% on Opus-tier, ~5% on Fable-tier — that 5% (architecture + final review) determines whether the output is actually good.

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

### Surviving compaction (the amnesia fix)

A long session eventually hits `/compact`, and the summarizer can drop the pending work and roadmap you discussed earlier — the agent then re-researches things the brain already knows. Two hooks close that gap by **pushing** the brain's open-work digest back into context at the exact moments memory is born or lost:

| Hook | Event | What it does |
|------|-------|--------------|
| `inject_brain_context.py` | `SessionStart` (`startup`/`resume`/**`compact`**) | Injects the ranked open-work digest (pending + roadmap-tagged decisions) via `additionalContext`. **`source=compact` is the post-compaction re-entry** — it injects a fuller digest so the roadmap survives the summary. |
| `remind_brain_before_research.py` | `PreToolUse` matcher `Workflow` | Soft, non-blocking nudge to check `get_roadmap`/`query_decisions` before a fan-out research workflow. Doesn't block — the push digest already put the answer in context. |

This is the same digest `get_roadmap` returns, so push (hook) and pull (tool) never diverge. The digest is token-budgeted (top ~15 items, action truncated) — ~1k tokens once per compaction.

**Tagging durable work:** put `roadmap` or `blocker` in a decision's `area` (e.g. `area="kmp-foundation/roadmap"`) and it floats to the top of the digest above transient pending edits. Untagged `pending` decisions still appear, newest first — so the fix works on an existing brain with no migration.

**Optional hard research gate:** the reminder is soft by default. To *block* `Workflow` until a brain read happened this session, set `"research_gate": "hard"` in `~/.agent-brain/config.json`. A `pre_check`/`query_decisions`/`get_roadmap` call satisfies the gate (it writes `~/.agent-brain/.last_query_marker`); `BRAIN_SKIP_ENFORCE=1` bypasses it. Soft is recommended — a hard gate can false-positive when the brain genuinely has nothing on the topic.

**Install** (setup.sh wires all three hooks idempotently, backing up `settings.json.bak`):
```json
// ~/.claude/settings.json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "startup|resume|compact",
        "hooks": [{ "type": "command",
          "command": "~/.agent-brain/.venv/bin/python /path/to/agent-brain/brain/hooks/inject_brain_context.py",
          "timeout": 15000 }] }
    ],
    "PreToolUse": [
      { "matcher": "Workflow",
        "hooks": [{ "type": "command",
          "command": "~/.agent-brain/.venv/bin/python /path/to/agent-brain/brain/hooks/remind_brain_before_research.py",
          "timeout": 10000 }] }
    ]
  }
}
```

### Making SAN the default read path

SAN is far cheaper for reading code (see [Is SAN worth it?](#is-san-worth-it-measured-numbers)) — but agents kept defaulting to raw `Read` because nothing routed them to it. The same discoverability gap as the amnesia problem. Four ergonomic changes fix it (none touch the SAN format or quality):

| Change | Effect |
|--------|--------|
| **`get_san` accepts absolute paths** | Pass the exact path you got from a grep/glob hit — no repo-name + relative-path lookup. The repo is auto-detected (`repo` arg becomes optional). This was the decisive friction tax. |
| **`route_read_to_san.py` (PreToolUse `Read`)** | Soft, non-blocking: a raw `Read` of a code file that has a **fresh** `.san` gets a one-line nudge to use `get_san` instead — then the Read proceeds. One nudge per session (deduped). Stays silent for non-code, stale/missing `.san`, or files outside any configured repo. **Never blocks** — raw `Read` is correct for files you're about to edit. |
| **`pre_check(repo=...)` surfaces coverage** | When given a repo, `pre_check` appends `SAN AVAILABLE: repo 'X' has N files compiled. Read code with get_san…` so the read path is in view at "check before work" time. |
| **Standing directive** | The [project context template](agents/PROJECT_CONTEXT_TEMPLATE.md) and the SessionStart digest both carry: *"To READ/EXPLORE existing code, use `get_san` BEFORE raw `Read`."* Co-located with the code-review-graph "graph-first" rule that agents already honor. |

**When raw `Read` stays correct** (the hook stays silent for all of these): files you're about to **edit** (need exact bytes), non-code files, files with no or stale `.san`, and anything outside a configured repo. SAN is for *exploring*; raw Read is for *editing*.

Wire-up: `setup.sh` registers the `Read` hook alongside the others. Existing projects need the standing-directive bullet re-copied into their `CLAUDE.md` (or hand-added) since the template only seeds new projects.

## SAN Protocol

Structured Associative Notation compresses code to ~17-27% of its original tokens (81% saved blended, [tokenizer-measured](#is-san-worth-it-measured-numbers)) while preserving all facts. See [`san/README.md`](san/README.md) for the full spec.

```
# Before: 80 lines, ~1,200 tokens
class AuthServiceImpl(...) : AuthService { ... }

# After: ~220 tokens
AuthServiceImpl @svc {
  impl: AuthService iface
  deps: UserRepository + TokenProvider + RateLimiter
  fn:login(email, pwd) → AuthResult [validate → verify → issue_jwt]
  fn:register(RegisterRequest) → AuthResult [validate → create → issue_jwt]
  layer: application/service
  patterns: DIP-clean
}
```

### The escalation ladder

Agents never load the whole compressed repo — they climb four levels, paying more only when needed:

| Level | What | Avg cost/file | When |
|-------|------|--------------:|------|
| 1. Catalog | `_index.json` — every file + its blocks | ~0 (one lookup) | "Where does X live?" |
| 2. Signatures | `get_san(detail="sig")` — public API surface only | ~110 tokens | "What exists here?" |
| 3. SAN brief | `get_san` — full compressed facts | ~220 tokens | "How does this work?" |
| 4. Raw source | Read the real lines via `src:` anchors | ~1,170 tokens | "I'm about to edit this" |

The signatures tier is also the most staleness-resistant: public surfaces churn far slower than function bodies.

### Is SAN worth it? (measured numbers)

Measured with **real tokenizers** (tiktoken `o200k_base` and `cl100k_base` — both agree within 0.1%) across 3 production repos: 954 source/SAN file pairs, Kotlin/Java/TS/JS, ~1.12M source tokens. Compression varies by code style — boilerplate-heavy Android code compresses to ~17%, dense backend logic to ~27%; **18.9% blended (81% saved)**:

| Scenario | Raw source | Via SAN | Saved |
|----------|----------:|--------:|------:|
| Agent reads 1 file (avg) | ~1,170 tokens | ~220 tokens | **~950 (81%)** |
| One task (agent explores ~10 files) | ~11,700 tokens | ~2,200 tokens | **~9.5k per task** |
| Whole codebase in context (954 files) | ~1.12M tokens — *doesn't fit* | ~211k tokens — *fits in one window* | **~905k (81%)** |

| Repo (style) | Files | Raw tokens | SAN tokens | Ratio |
|--------------|------:|----------:|-----------:|------:|
| Android app (Kotlin, boilerplate-heavy) | 651 | 853k | 142k | 16.6% |
| Backend (Kotlin, dense logic) | 299 | 247k | 67k | 27.0% |
| Web (TS/JS) | 4 | 15k | 2.4k | 15.7% |

**Do SAN's unicode operators (`→ ⇒ ×`) waste tokens?** Not on modern tokenizers — measured: `→` = 1 token on both, and a typical SAN line costs exactly the same in unicode and ASCII form (19 vs 19 tokens). One caveat: standalone `⇒` is 1 token on o200k but 3 on the older cl100k — if you target older models, prefer the ASCII equivalents (`->`, `=>`, `xN`), which the spec allows everywhere.

Savings recur on **every read by every agent**; generation cost is one-time per file (plus regeneration when the file changes):

| Cost side | Amount |
|-----------|--------|
| Generate 1 file (Sonnet) | ~1 read of the source (~1,170 input tokens) + ~220 output tokens |
| Break-even (token count) | After **~1-2 reads** of that file via `get_san` instead of raw |
| Break-even (dollars) | **~2-3 reads** if reader = generator price (output tokens cost ~5× input); faster when generation runs on cheap Sonnet and reads are saved on expensive models |

**Use SAN when:**
- Agents repeatedly explore the same codebase (every task re-reads files)
- The repo is too big to fit in context raw — SAN makes whole-repo reasoning possible
- Multiple agents work the same repo (generation cost amortizes across the team)

**Skip SAN when:**
- The repo is small enough to fit in context anyway (< ~50 files)
- Files churn rapidly — stale SANs need regeneration, eroding the one-time-cost advantage
- One-off scripts / repos agents rarely revisit (won't reach break-even)

> Numbers above are tokenizer-measured (tiktoken). The live `token_savings` tracker uses tiktoken too when it's installed in the brain venv (`pip install tiktoken`) — exact counts, same methodology as the table. Without tiktoken it falls back to a ~4 chars/token estimate (measured ~1.4 points optimistic: 17.5% vs 18.9% ratio). Measure your own repos:
> ```bash
> pip install tiktoken
> python3 -c "
> import tiktoken; from pathlib import Path
> enc = tiktoken.get_encoding('o200k_base')
> raw = sum(len(enc.encode(f.read_text(errors='replace'))) for f in Path('.').rglob('*.kt'))
> san = sum(len(enc.encode(f.read_text(errors='replace'))) for f in Path('.san').rglob('*.san'))
> print(f'raw={raw:,} san={san:,} ratio={san/raw:.1%}')"
> ```

### Measuring your savings (`token_savings`)

You don't have to estimate — the brain **measures it live**. Every `get_san` call records what the raw source read *would* have cost vs the SAN tokens actually served. Ask any agent:

```
"how many tokens did SAN save this session?"   → agent calls token_savings()
```

```
=== SAN TOKEN SAVINGS ===

This session:
  SAN reads: 14
  Raw source cost avoided: 16,380 tokens
  SAN tokens served: 3,080 tokens
  SAVED: 13,300 tokens (81%)

Today (2026-06-11):  ...
All time:            ...
```

Or from the shell (reports the last recorded session instead of a live one):

```bash
python3 brain/server.py savings
```

How it counts — deliberately conservative, so the number is trustworthy:
- Only `get_san` reads count (a read that replaced opening the raw file)
- `query_san` searches and decision-memory benefits are **not** included
- Reads where SAN wouldn't have saved anything are skipped
- ~4 chars/token estimate; events persist in `~/.agent-brain/san_savings.jsonl`

Use it to decide whether SAN is paying off: if "All time" savings stay near zero after a week, your agents aren't reading via SAN — check coverage with `recompile_san(dry_run=True)`.

## SAN Setup

SAN (Structured Associative Notation) compresses source code to ~17-27% of its original tokens for LLM context. This is **optional** — the decision memory works without it.

**Supported languages:** the brain-compiler is language-agnostic, and the server's housekeeping (freshness, indexing, orphan cleanup) recognizes these extensions: `.kt .java .py .ts .tsx .js .jsx .swift .go .rs .rb .c .cpp .h .cs .php .scala .m .mm`. Files in other languages can still be compiled by hand, but won't be tracked by the freshness sweep until their extension is added to `SOURCE_EXTS` in `brain/server.py`.

1. **Create `.san/` in your repo:**
   ```bash
   mkdir -p your-repo/.san
   ```

2. **Generate SAN files** using the brain-compiler agent (see `san/brain-compiler.md`):
   ```
   # In Claude Code, spawn the brain-compiler agent:
   # "Convert src/services/AuthService.kt to SAN"
   ```
   The compiler writes `your-repo/.san/src/services/AuthService.kt.san` (the source path with `.san` appended — mirrors the source tree).

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
| `get_san("repo", "src/path/File.kt")` | Get SAN content for a source file. `file_path` may be **relative or absolute** (repo auto-detected); `detail="sig"` for the API surface only |
| `python3 brain/server.py san-index <repo>` | (CLI) Rebuild `_index.json` from all `.san` files |
| `python3 brain/server.py validate-san` | (CLI) 24 self-tests: hashing, orphan cleanup, staleness, index building. Isolated temp dir. |

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

Spend the savings where it matters: your engineering agents *consuming* SAN can run on bigger models, since SAN cuts their input cost to ~20% of raw anyway. Only escalate the compiler to a bigger model if you find SAN files missing relationships on gnarly, highly-dynamic code.

### Generating SAN from other platforms (ChatGPT, Cursor, etc.)

The **MCP server is platform-agnostic** — any MCP client can call `query_san`/`get_san`/`recompile_san`. Only the brain-compiler *agent template* is Claude Code specific. SAN files themselves are plain text, so any capable LLM can generate them:

1. Give the model the SAN spec ([`san/README.md`](san/README.md)) + the source file
2. Save its output to `<repo>/.san/<source-path>.san` — mirror the source tree and **append** `.san` to the full filename (e.g. `src/Auth.kt` → `.san/src/Auth.kt.san`)
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

## Adaptive Warnings

Agents with high rejection rates get progressively stricter warnings:

| Rejection Rate | Warning Level | Behavior |
|---------------|--------------|----------|
| < 30% | NORMAL | Standard pre_check |
| 30-49% | ELEVATED | "Pay close attention to past failures" |
| ≥ 50% | STRICT | Shows top rejection patterns, demands extra scrutiny |

Agents with fewer than 3 logged decisions always get NORMAL — no judgment on a tiny sample.

**Old rejections age out gracefully.** Warnings older than ~3 months are tagged
`[6mo old — verify the reason still applies]` — the codebase may have moved on
since the rejection, so the agent is told to re-check the reasoning instead of
treating it as current. The reason is always shown so the agent judges applicability;
nothing is silently filtered or silently blocked.

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

After setup, run the full validation from the repo root:

```bash
python3 brain/server.py validate
# Expected: "Agent Brain Validation: 117 passed, 0 failed ✓ ALL TESTS PASSED"
```

This tests every subsystem in isolation using a temp directory:

| Section | Checks | What's validated |
|---------|--------|-----------------|
| Graph Persistence | 4 | Save/load, atomic writes, empty state |
| Decision Memory | 16 | log_decision, log_outcome, log_feedback, error handling |
| Pre-check & Warnings | 7 | Exact matches, similar rejections, adaptive warning levels |
| Similarity Matching | 7 | Tokenizer (camelCase split), Jaccard + domain boost, false positives |
| Pattern Clustering | 1 | DIP-related rejections cluster together |
| Scorecards & Dashboard | 11 | Acceptance rates, trends, team_dashboard rendering |
| Query & Retrieval | 6 | Filters, missing ID handling, file-based search |
| Code Bridge | 4 | Symbol linking, callers, impact radius |
| Office State | 11 | Heartbeat, role resolution, messages, auto-heartbeat |
| Config & Edge Cases | 3 | Missing/corrupt config and graph files |
| SAN System | 1 | Delegates to the 24-check `validate-san` suite (hashing, orphans, staleness, indexing) |
| Integration Workflow | 10 | Full end-to-end: pre_check → decide → reject → feedback → re-check |

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

### Model routing (optional)

```json
"model_routing": {
  "plan": "fable",
  "implement": "sonnet",
  "review": "opus",
  "boilerplate": "haiku",
  "escalate": "fable"
}
```

Shown as one line in every `pre_check`; `escalate` names the tier in the two-strikes escalation hint. See [Model Routing](#model-routing-quality-per-cost).

## Customization

### Adding more agents
Copy any template, rename, change the `{{PLACEHOLDER}}` values.

### Adding domain terms
Edit `_DOMAIN_TERMS` in `server.py` to boost similarity matching for your domain.

### Custom warning thresholds
Edit `_adaptive_warning_level()` in `server.py`.

## License

MIT
