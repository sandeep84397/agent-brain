# Adapters — one brain, many runtimes

Agent Brain is an **MCP server**, so any MCP-compatible agent runtime can use
it. The decision memory, SAN code briefs, and all tools are the same regardless
of which agent drives them — **your accumulated knowledge is portable across
tools, owned by you, not trapped inside one vendor's editor.**

What differs between runtimes is only the **enforcement mechanism**:

| | Tools (pre_check, log_decision, get_san, …) | Standing protocol | Hard enforcement |
|---|---|---|---|
| **Claude Code** | ✅ native MCP | ✅ MCP `instructions` + CLAUDE.md | ✅ **5 hooks** — decision-gate, amnesia re-inject on compaction, Read/Bash→SAN routing |
| **Codex** | ✅ native MCP (stdio) | ✅ MCP `instructions` field | ⚠️ **advisory** — Codex has no hook lifecycle, so the protocol is followed as guidance, not blocked |
| **Any MCP host** (Cursor, Cline, …) | ✅ if it speaks MCP | ✅ if it reads `instructions` | depends on the host's hook support |

The knowledge (decisions, outcomes, SAN) is identical everywhere. Only *how
strictly the protocol is enforced* changes — and that's a property of the host,
not the brain.

## Claude Code (fully enforced)

```bash
./setup.sh
```
Registers the MCP server, all five hooks, and the CLAUDE.md tool-ladder. This
is the strictest experience: edits are blocked without a logged decision, the
roadmap is re-injected after compaction, raw code reads are routed to SAN.

MCP-only (no hooks):
```bash
claude mcp add --transport stdio --scope user agent-brain -- \
  ~/.agent-brain/.venv/bin/python ~/.agent-brain/server.py
```

## Codex (MCP-native, advisory protocol)

Codex runs MCP servers natively over stdio and reads the server's `instructions`
field as standing guidance. Print the exact config to paste:

```bash
python3 brain/server.py adapter codex
```

It emits a `[mcp_servers.agent-brain]` block for `~/.codex/config.toml`:

```toml
[mcp_servers.agent-brain]
command = "/path/to/.venv/bin/python"
args = ["/path/to/server.py"]
```

Then `codex mcp list` should show `agent-brain`. Every tool works
(`pre_check`, `log_decision`, `get_san`, `get_roadmap`, …), and Codex reads the
brain's protocol from the MCP `instructions` field.

**What you get on Codex:** the full decision memory, SAN reads, cross-session
recall, and the roadmap — the actual knowledge. **What you don't get:** hard
hook-enforcement. On Codex the "always pre_check first / read via get_san / log
before editing" protocol is *guidance the agent should follow*, because Codex
has no PreToolUse/SessionStart hooks to enforce it. In practice a capable model
follows a clearly-stated NON-NEGOTIABLE protocol well; it's just not blocked.

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
