# Agent-Brain Amnesia Fix — Design Brief

> **Purpose:** Hand-off context for a separate work session to implement the fix. This document is DESIGN + GROUNDING ONLY — no code was changed in the session that produced it. Read this, then brainstorm→spec→plan→build the fix in the implementing window.

> **Author context:** Written by Claude (Opus 4.8) after personally hitting the failure this fixes — in a OneOnOneArena session, I jumped to an expensive fan-out research workflow to re-derive a pending roadmap that the brain already held, because (a) I didn't query the brain first, and (b) when I *did* query, `query_decisions` returned the wrong (recency-ordered) results. The brain's whole purpose — "have all context ready so you don't re-research" — failed at both the behavioral and structural level.

---

## 1. The Problem (two distinct failures)

### Failure A — Behavioral: AI skips the brain, re-researches
The agent reaches for `Grep`/`Workflow`/fan-out research before consulting the brain, even when the brain holds the answer. Token waste + latency. The existing `enforce_brain_protocol.py` hook only gates **Edit/Write** (forcing `log_decision`); nothing nudges a **brain query before research**.

### Failure B — Structural: post-compaction amnesia
This is the user's primary pain. The user runs on a 1M context but must `/compact` when full. **Post-compaction, the pending work / decisions discussed earlier get hallucinated or lost.** Root causes:
1. **No SessionStart hook and no PreCompact/PostCompact hook.** The only hook is `brain/hooks/enforce_brain_protocol.py` (PreToolUse on Edit|Write). Nothing re-injects the brain's pending state at the moments context is lost (session start, post-compact). Memory files survive only because the *harness* auto-injects `MEMORY.md` each session — the brain's own decision graph does NOT get surfaced unless the AI explicitly queries it, which it skips.
2. **`query_decisions` ranks by RECENCY, not RELEVANCE** (see §2). So even a correct query returns the wrong rows.

---

## 2. Grounding — how the brain actually works today (verified, not assumed)

**Repo:** `/Users/sandeepdhami/Documents/GitHub/agent-brain`
**MCP server:** `brain/server.py` (FastMCP; `@mcp.tool()` decorators)
**Existing hook:** `brain/hooks/enforce_brain_protocol.py` (PreToolUse, Edit|Write only)

### Storage model (verified at `server.py:39, 215, 310, 375`)
- `BRAIN_DIR` = `~/.agent-brain` (overridable via `AGENT_BRAIN_DIR` env).
- `decisions.json` (`GRAPH_FILE`) = a **NetworkX graph** persisted via `nx.node_link_data` — a periodic full SNAPSHOT.
- `decisions.journal` = append-only op log between snapshots.
- Decisions are **graph nodes** with `type="decision"` and fields: `agent`, `repo`, `area`, `action`, `reasoning`, `outcome` (`pending|accepted|rejected|failed|revised`), `timestamp`, plus edges (`touches` → code symbols, `feedback_on` ← feedback nodes).
- A decision marker file `~/.agent-brain/.last_decision_marker` (`DECISION_MARKER_FILE`, written at `server.py:167` by `log_decision`) is what the enforce hook reads for staleness.
- **SQLite (`sqlite3`) is used ONLY for the code-review-graph** (`.code-review-graph/graph.db`), NOT for decisions. Don't confuse the two.

### The `query_decisions` bug (verified at `server.py:1283`)
```python
def query_decisions(area="", agent="", repo="", outcome="", limit=10):
    # ... iterates G.nodes, filters by EXACT match on area/agent/repo/outcome ...
    results.append(f"[{node_id}] ...")
    return f"{len(results)} decision(s):\n\n" + "\n".join(results[-limit:])
```
- **No keyword/text/semantic ranking.** It only does exact-equality filters on the 4 structured fields. There is **no free-text search** over `action`/`reasoning`.
- `results[-limit:]` = **last N in graph-insertion order = recency.** When the structured filters don't narrow (e.g. a natural-language query with no matching `area`), it returns "all decisions, last 10" — which is why my "storage/AppStorage/pending" query returned the last 10 video-player decisions.
- Contrast: `query_san` (`server.py`, SAN search) DOES keyword-search file contents — so the keyword-search pattern already exists in the codebase to borrow from.

### What exists to build on
- `pre_check(agent, area, action_description)` (`server.py:718`) — already returns past failures + warnings for an area. Good injection point but **pull-only** and area-filtered.
- `get_decision(decision_id)` (`server.py:~1320`) — full detail incl. feedback + code symbols.
- `enforce_brain_protocol.py` — a clean, copyable hook template: reads stdin JSON, checks a marker file, exits 0 (allow) / 2 (block with stderr message), fails-open on any error, respects `BRAIN_SKIP_ENFORCE=1` and `config.json` skip globs.

---

## 3. The Fix — agreed direction (BOTH push + pull)

User decision: **do both** — push hooks for guaranteed re-injection AND smarter pull tools. Research-gate hard-vs-soft = decide during the implementing window's design phase (see §5).

### 3a. PUSH — proactive re-injection hooks (fixes Failure B, the amnesia)
Add hooks that inject a **"pending work + key decisions" digest** into the agent's context at the exact moments context is born or lost:

- **SessionStart hook** — on every new session, emit a compact digest of: open/`pending` decisions, the most-relevant recent decisions per active repo, and any explicitly-flagged "roadmap" decisions. This is what makes the brain's purpose actually work — context ready without the AI asking.
- **PreCompact hook** — fire right before a compaction. Two viable jobs (pick in design): (i) write/refresh a durable "pending digest" file so it survives the compaction window, and/or (ii) emit the digest into context so the summarizer preserves it. Claude Code exposes a `PreCompact` hook event — confirm the exact event name + payload in the harness docs during implementation.
- **(Optional) SessionStart `source` discrimination** — Claude Code's SessionStart hook receives a `source` field (`startup` | `resume` | `compact`). The `compact` source is the post-compaction re-entry — that's the highest-value injection point for Failure B. Use it to inject a fuller digest specifically post-compact.

**Digest content (the "pending roadmap" the AI keeps losing):** all `outcome=pending` decisions + decisions tagged as roadmap/blocker, newest-relevant first, each as `[id] area | action(truncated) -> outcome`. Keep it tight (token budget — this is injected every session). Make the digest a NEW brain tool too (see 3b) so it's both pushable (hook) and pullable (AI).

### 3b. PULL — smarter tools (fixes Failure A's "even when I query, wrong results")
1. **Fix `query_decisions` ranking** — add free-text relevance. Minimum: keyword/token match over `action`+`reasoning`+`area` (borrow the `query_san` keyword approach), score by match count + recency tiebreak, return top-N by SCORE not insertion order. Keep the existing structured filters as optional narrowing. Consider a `sort=relevance|recency` param (default relevance when a text query is present).
2. **New tool `get_pending_roadmap(repo="")`** (or `get_open_work`) — returns the same digest the push hooks use: `pending` + roadmap-tagged decisions, ranked, repo-scoped. One call gives the AI "what's left to do" without guessing query terms. (There may already be a `check_open_decisions`-style helper near `server.py:1477` — reconcile/extend rather than duplicate.)
3. **Roadmap tagging** — decisions need a way to be marked as durable roadmap/pending-work (vs transient implementation decisions). Options: a convention on `area` (e.g. `*/roadmap`), a new `tags` field, or reuse `outcome=pending` + a `kind` field. Decide in design; whatever's least invasive to the existing graph schema.

### 3c. RESEARCH-GATE — Failure A's behavioral miss (DECIDE IN DESIGN)
The behavioral fix (stop the AI jumping to Workflow/fan-out research before consulting the brain). Two candidates, weigh in the implementing window:
- **Hard gate (PreToolUse hook):** block `Workflow` (and/or large research fan-outs) unless a brain query (`pre_check`/`query_decisions`/`get_pending_roadmap`) happened recently this session — same marker-file + staleness pattern as `enforce_brain_protocol.py` (write a `~/.agent-brain/.last_query_marker` on any read tool; the gate checks it). **Risk:** false positives blocking legitimate research where the brain genuinely has nothing; needs a clean bypass (`BRAIN_SKIP_ENFORCE=1` already exists) and a "brain was consulted and had nothing" escape.
- **Soft reminder:** strengthen CLAUDE.md + the SessionStart digest with "query the brain before researching"; no hard block. Lower friction, relies on compliance (which already failed once — hence the user's skepticism).
- **Recommendation to evaluate:** hard gate ONLY on `Workflow`/fan-out-research tools (not all reads), with the existing bypass — narrow blast radius, directly targets the expensive mistake.

---

## 4. Why this specifically fixes what broke

| Failure | Fix | Mechanism |
|---|---|---|
| Post-compact amnesia (B) | SessionStart(`source=compact`) + PreCompact hooks inject the pending digest | Brain pushes context at the exact loss moments; no AI memory required |
| Query returns wrong rows (B/A) | Relevance-ranked `query_decisions` + `get_pending_roadmap` | Free-text scoring instead of recency; one-call roadmap |
| AI re-researches before asking (A) | Research-gate (hard/soft TBD) + SessionStart digest | The answer is already in context (push) and/or the gate forces a brain check before Workflow |

---

## 5. Implementation notes / constraints for the building window

- **Hooks must fail-open.** Copy the `enforce_brain_protocol.py` discipline: any exception/parse-error → `sys.exit(0)`, never break a session. Respect `BRAIN_SKIP_ENFORCE=1`.
- **Confirm Claude Code hook events** before wiring: `SessionStart` (with `source` ∈ startup|resume|compact) and `PreCompact` are the relevant events. Verify exact payload shape + how to emit context (stdout vs a specific JSON field) against the current Claude Code hooks docs — the harness, not the brain, defines these.
- **Token budget the digest.** It's injected every session/compact — cap it (e.g. top ~15 pending/roadmap items, action truncated ~150 chars, like the existing `query_decisions` formatting at `server.py:1305`).
- **Don't touch the code-review-graph SQLite path** — decisions live in `decisions.json` (NetworkX), separate subsystem.
- **Reconcile, don't duplicate:** there's likely an existing open-decisions helper around `server.py:1477` (`"No open decisions..."`) — extend it for the roadmap tool.
- **settings.json wiring:** the new hooks get registered in the user's Claude Code `settings.json` (SessionStart / PreCompact arrays), same as how `enforce_brain_protocol.py` is wired into PreToolUse. Document the exact snippet in the spec.
- **Migration:** existing decisions have no `tags`/`kind`/roadmap marker. The roadmap tool must work on the current graph (e.g. fall back to `outcome=pending`) so it's useful immediately, with tagging as an additive enhancement.

---

## 6. Concrete first test case (validates the fix end-to-end)

The exact decision that should have been surfaced (and wasn't): **`dec_20260620_110850_abede5`** (repo `OneOnOneArena`, area `kmp-foundation/roadmap`) — the KMP foundation-blocker roadmap (shared models → SharedDB → utils → AppStorage → feed data layer; ports list; carry-overs). 

**Acceptance test:** after the fix, in a fresh/post-compact OneOnOneArena session, the SessionStart digest (push) OR a single `get_pending_roadmap("OneOnOneArena")` call (pull) must surface `dec_20260620_110850_abede5` and the V1c-deferred carry-over decisions (`dec_20260619_192343_3ef703`, `dec_20260619_193726_97f83b`) WITHOUT any code research. A relevance query like `query_decisions` with text "foundation roadmap pending storage" must rank that decision in the top results (today it returns recent video-player decisions instead).

---

## 7. Out of scope (explicitly)
- No changes to the code-review-graph / SAN subsystem.
- No changes to OneOnOneArena (this is an agent-brain fix).
- The OneOnOneArena KMP foundation work is PAUSED, roadmap safely logged at `dec_20260620_110850_abede5`; resume after the brain fix.
