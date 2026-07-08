# Adapters — one brain, many runtimes

Agent Brain is an **MCP server**, so any MCP-compatible agent runtime can use
it. The decision memory, SAN code briefs, and all tools are the same regardless
of which agent drives them — **your accumulated knowledge is portable across
tools, owned by you, not trapped inside one vendor's editor.**

What differs between runtimes is only the **install surface** and hook trust UX:

| | Tools (pre_check, log_decision, get_san, …) | Standing protocol | Hard enforcement |
|---|---|---|---|
| **Claude Code** | ✅ native MCP | ✅ MCP `instructions` + CLAUDE.md | ✅ **5 hooks** — decision-gate, amnesia re-inject on compaction, Read/Bash→SAN routing |
| **Codex** | ✅ native MCP (stdio) | ✅ MCP `instructions` + AGENTS.md | ✅ **5 hooks** — decision-gate, roadmap injection, Read/Bash→SAN routing |
| **Any MCP host** (Cursor, Cline, …) | ✅ if it speaks MCP | ✅ if it reads `instructions` | depends on the host's hook support |

The knowledge (decisions, outcomes, SAN) is identical everywhere. Hooks make the
protocol mechanical where the host supports them; MCP `instructions` remain the
portable fallback.

## Claude Code (fully enforced)

```bash
./setup.sh
```
When Claude Code is installed, this registers the MCP server, all five hooks,
and the CLAUDE.md tool-ladder. Edits are blocked without a logged decision, the
roadmap is re-injected after compaction, and raw code reads are routed to SAN.

MCP-only (no hooks):
```bash
claude mcp add --transport stdio --scope user agent-brain -- \
  ~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py
```

## Codex (MCP-native + hooks)

Recommended:

```bash
./setup.sh
```

When Codex is installed, this installs the brain server and writes:

- `~/.codex/config.toml` — adds `[mcp_servers.agent-brain]`
- `~/.codex/hooks.json` — adds the decision gate, roadmap injection, brain-before-research nudge, and SAN read-routing hooks

Restart Codex, then run `/mcp` to confirm `agent-brain` is enabled and `/hooks`
to review and trust the new hooks. Codex requires hook trust before
non-managed command hooks run.

For a specific repository, add Codex-visible project guidance:

```bash
./setup.sh --link-project=/absolute/path/to/your/project
```

This appends an idempotent Agent Brain block to `<project>/AGENTS.md`. Restart
Codex in that project so the instructions reload.

Codex runs MCP servers natively over stdio and reads the server's `instructions`
field as standing guidance. To print the MCP-only config manually:

```bash
python3 brain/server.py adapter codex
```

It emits a `[mcp_servers.agent-brain]` block for `~/.codex/config.toml`:

```toml
[mcp_servers.agent-brain]
command = "/path/to/.venv/bin/python"
args = ["/path/to/server.py"]
```

Then `/mcp` or `codex mcp list` should show `agent-brain`. Every tool works
(`pre_check`, `log_decision`, `get_san`, `get_roadmap`, …), and Codex reads the
brain's protocol from the MCP `instructions` field.

## Switching between runtimes (the point)

Because the brain is a separate MCP server with its own store, you can switch
which agent you use — Claude today, Codex tomorrow — **without losing your
accumulated decisions or SAN.** Point both runtimes at the same server; the
memory is shared. That portability is the whole idea: the brain outlives any one
runtime, so switching agents (for price, capability, or preference) doesn't reset
your project's hard-won knowledge.

## Quick reference

```bash
python3 brain/server.py adapter          # overview of what's portable vs runtime-specific
python3 brain/server.py adapter codex    # config.toml block for Codex
python3 brain/server.py adapter claude   # claude mcp add command
```
