# Provider-Aware SAN Compiler Design

**Date:** 2026-07-11  
**Status:** Approved design  
**Repository:** `/Users/sandeepdhami/Documents/GitHub/agent-brain`  
**Decision:** `dec_20260711_094940_7cc052`

## 1. Problem

Agent Brain's MCP server, decision memory, hooks, and SAN read path support both
Claude Code and Codex. SAN generation does not. The bundled
`san/brain-compiler.md` is a Claude Code agent template pinned to
`claude-sonnet-4-6`, while Codex has no equivalent compiler agent or skill.
The setup script also does not install the bundled compiler template.

This creates the wrong provider dependency: a Codex session may try to invoke
Claude to refresh SAN, then fail on unrelated Claude authentication. The target
project also risks receiving Agent Brain implementation/configuration files
that belong in the Agent Brain repository.

The server's existing `recompile_san` tool is housekeeping only. It refreshes
hashes and indexes but does not generate SAN content. Its advertised
`dry_run=True` path must also become genuinely non-mutating before it is used as
the compiler's planning step.

## 2. Goals

1. Generate SAN through the AI host the user is actively using.
2. Use a fixed, inexpensive, provider-specific compiler model.
3. Keep model defaults configurable and deliberately versioned.
4. Never call Claude from Codex or Codex/OpenAI from Claude.
5. Never silently fall back to a more expensive model or reasoning tier.
6. Keep the Agent Brain server provider-neutral; it must not launch an LLM.
7. Validate and publish generated SAN atomically against the exact source
   digest used for generation.
8. Install Claude and Codex compiler adapters idempotently without overwriting
   unrelated user configuration.
9. Keep all implementation changes in the Agent Brain repository. Target
   repositories receive only their local, ignored `.san/` output after an
   explicit refresh request.

## 3. Non-goals

- Automatically query model catalogs or pricing on every SAN refresh.
- Automatically escalate to a larger model or higher reasoning effort.
- Add OpenAI or Anthropic API keys to Agent Brain configuration.
- Run provider CLIs as subprocesses from the MCP server.
- Change the SAN v2 notation or remove compatibility with existing SAN files.
- Commit `.san/` output to target repositories.
- Modify Jobfill source, configuration, or tracked files as part of this work.
- Require live provider calls in normal CI.
- Repair unrelated Agent Brain roadmap or dashboard work.

## 4. Chosen Architecture

### 4.1 Canonical provider-neutral compiler contract

Agent Brain owns one canonical compiler contract under `san/`. It defines:

- accepted source paths and skip directories;
- SAN v2 syntax and canonical field ordering;
- exact identifier/signature preservation;
- required dependency, state, error, constraint, threading, pattern, and risk
  facts;
- source-order preservation;
- output path rules;
- objective validation gates;
- Agent Brain decision/outcome logging boundaries;
- privacy rules for source and SAN content.

Provider adapters must consume this contract. Provider-specific prompts may
adapt tool names and frontmatter, but may not redefine SAN semantics.

### 4.2 Claude adapter

Claude setup installs a managed custom agent at:

```text
~/.claude/agents/brain-compiler.md
```

Default model:

```text
claude-sonnet-4-6
```

The agent operates in the active Claude Code session. It does not invoke Codex
or an OpenAI API. The existing Claude-specific compiler template is refactored
into this adapter rather than treated as the canonical contract.

### 4.3 Codex adapter

Codex setup installs:

```text
~/.codex/agents/brain-compiler.toml
~/.codex/skills/brain-compiler/SKILL.md
```

The custom agent pins the compiler model and loads the compiler skill. Default:

```toml
model = "gpt-5.4-mini"
model_reasoning_effort = "medium"
```

`gpt-5.4-mini` is the fixed low-cost Codex default for this Agent Brain release.
`medium` is the initial reasoning default because SAN must preserve exhaustive
semantic facts, while structural validation cannot prove every relationship was
captured. `low` remains an explicit user override and may become the future
default only after representative quality evaluations show no material loss.

Codex custom agents support their own model and reasoning settings, so this
path remains inside the active Codex host without launching `codex exec`.

References:

- <https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents>
- <https://developers.openai.com/api/docs/guides/reasoning#reasoning-effort>
- <https://learn.chatgpt.com/docs/pricing#what-are-the-usage-limits-for-my-plan>

### 4.4 Agent Brain server boundary

The MCP server remains provider-neutral. It performs only deterministic work:

- plan missing/stale SAN work;
- validate candidate SAN content;
- verify source digests;
- write validated SAN atomically;
- update hashes and indexes;
- report freshness and failures;
- record aggregate compiler provider/model metadata without source or SAN
  contents.

The server never selects, authenticates, or invokes a model.

## 5. Configuration

Effective user configuration lives in `~/.agent-brain/config.json`. Defaults are
documented in `brain/config.example.json`:

```json
{
  "san_compiler": {
    "claude": {
      "model": "claude-sonnet-4-6"
    },
    "codex": {
      "model": "gpt-5.4-mini",
      "reasoning_effort": "medium"
    },
    "allow_expensive_fallback": false
  }
}
```

Rules:

1. Missing `san_compiler` configuration uses the release defaults.
2. User configuration may override either model and Codex reasoning effort.
3. `allow_expensive_fallback` defaults to and remains `false` for generated
   adapters. No current workflow enables automatic fallback.
4. Model changes take effect after rerunning the relevant setup command so the
   managed host adapter is rendered from the effective configuration.
5. Agent Brain releases deliberately update defaults when model availability,
   cost, or quality changes. Runtime price/catalog discovery is not used.
6. Invalid configuration fails setup with a field-specific error and does not
   replace the last valid installed adapter.

## 6. Generation and Publication Flow

1. The user requests SAN generation or refresh from Claude or Codex.
2. The active host selects its installed `brain-compiler` custom agent.
3. The compiler calls Agent Brain `get_roadmap`, then `pre_check`, then logs one
   SAN decision for the bounded batch.
4. A read-only freshness plan returns missing/stale source paths and their
   current SHA-256 digests.
5. The compiler reads only those source files and generates candidate SAN using
   its host-specific fixed model.
6. For each source, the compiler calls a new provider-neutral `publish_san`
   tool with:
   - repository;
   - source-relative path;
   - expected source digest;
   - candidate SAN content;
   - provider identifier;
   - model identifier;
   - reasoning effort when applicable.
7. `publish_san` validates the path, source digest, and SAN structure.
8. A valid candidate is written to a same-directory temporary file and
   atomically replaces the destination SAN.
9. Only after replacement succeeds does Agent Brain update source hashes,
   generation metrics, and the SAN index.
10. The compiler logs an accepted, revised, or failed outcome containing file
    names and validation summaries, never source/SAN content.
11. The user receives generated, skipped, stale-during-generation, invalid, and
    failed counts plus retryable file paths.

## 7. `publish_san` Contract

### 7.1 Inputs

```text
repo
source_path
expected_source_sha256
san_content
provider
model
reasoning_effort?
```

`source_path` must resolve to a configured repository source file and must not
escape the repository root. The SAN destination is always derived by Agent
Brain using the canonical append rule:

```text
src/Auth.kt -> .san/src/Auth.kt.san
```

The caller cannot choose an arbitrary output path.

### 7.2 Objective validation

Before publication, Agent Brain verifies:

- source file still exists and its SHA-256 equals the expected digest;
- extension is supported and source path is not under a skipped directory;
- candidate is non-empty and contains no placeholder/stub marker;
- every top-level block header matches the SAN v2 index regex;
- every block has a valid `src: start-end` range inside the source file;
- block braces are balanced;
- duplicate qualified-name/kind blocks are rejected;
- candidate size and block count remain within bounded safety limits.

Semantic checks that require understanding arbitrary source languages remain
mandatory compiler-side quality gates: function/class/interface count parity,
verbatim signatures and identifiers, complete public surfaces and dependencies,
and `@errors` coverage. Provider conformance fixtures and evaluations verify
these gates. `publish_san` reports structural validation only and never claims
that it proved universal semantic completeness.

### 7.3 Atomicity and concurrency

- Publication rejects candidates when the source digest changed during
  generation.
- The final observable state retains the previous valid SAN and metadata on
  validation, write, hash, or index failure. A post-replacement bookkeeping
  failure must roll back both SAN bytes and metadata before returning.
- Temporary files use process-unique names and are cleaned after failure.
- Hash/index state is updated only for the successfully published candidate.
- Concurrent publication for the same source is serialized or compare-and-swap
  guarded by the expected source digest.

## 8. Freshness Planning and True Dry Run

`recompile_san(repo, dry_run=True)` must become observably read-only:

- no `.san/` directory creation;
- no orphan deletion;
- no mtime updates;
- no hash backfill;
- no metrics writes;
- no index rebuild;
- no configuration mutation.

It reports current missing, stale, fresh, orphaned, unsupported, and malformed
states only. Mutating housekeeping remains under `dry_run=False`.

The compiler may use a dedicated structured planning helper internally, but the
public dry-run behavior must match its documented no-change contract.

## 9. Failure Semantics

| Failure | Required behavior |
|---|---|
| Configured model unavailable | Stop before generation; name provider/model; no fallback |
| Host compiler adapter missing | Diagnose exact install command; no cross-provider call |
| Source changed during generation | Reject candidate; report retryable stale source |
| Candidate structurally invalid | Keep previous SAN; return validation errors |
| Atomic write fails | Keep previous SAN and previous hash/index state |
| Index/hash update fails after write | Restore previous SAN and previous metadata; report publication failure |
| Partial batch failure | Retain successful publications; retry only failed files |
| Unsupported source extension | Report unsupported; do not silently ignore |
| Invalid compiler configuration | Preserve last valid managed adapter |

No error path may silently switch to a different model, provider, or reasoning
effort.

## 10. Setup and Adapter Ownership

### 10.1 Commands

- `./setup.sh --claude` installs/refreshes the Claude compiler agent.
- `./setup.sh --codex` installs/refreshes the Codex compiler agent and skill.
- `./setup.sh --all` installs/refreshes both.

### 10.2 Managed files

Every installed compiler artifact contains an Agent Brain ownership/version
marker. Setup behavior:

1. Missing target: create it.
2. Matching managed target: update atomically when rendered content changes.
3. Matching managed target already current: make no change.
4. Existing unmarked target at the same path: preserve it and fail with a clear
   conflict message; never overwrite silently.
5. Unrelated agents, skills, hooks, and config entries: preserve them.

Claude-only setup must not install Codex artifacts. Codex-only setup must not
install Claude artifacts.

### 10.3 Diagnostics

`diagnose` reports for each detected host:

- compiler adapter installed/missing/conflicting;
- configured model and reasoning effort;
- managed artifact version/currentness;
- canonical contract availability;
- Agent Brain MCP availability;
- target project `.san/` ignore coverage when diagnosing a project.

Diagnostics never test another provider and never require a live model call.

## 11. Privacy and Repository Boundaries

- The active host model sees only source files selected for SAN generation.
- Claude credentials are never read from Codex workflows; OpenAI/Codex
  credentials are never read from Claude workflows.
- Provider credentials are not stored in Agent Brain configuration.
- Source contents and SAN contents are excluded from decisions, outcomes,
  metrics, and diagnostics.
- Provider/model/effort and aggregate token metrics may be recorded.
- Agent Brain implementation files live only in the Agent Brain repository and
  its managed user-install locations.
- Target repositories receive only `.san/` output, hashes, and index files.
- Agent Brain never stages or commits target-project SAN output.
- Jobfill remains untouched while this Agent Brain feature is implemented.

## 12. Testing Strategy

### 12.1 Configuration tests

- release defaults load when `san_compiler` is absent;
- valid Claude/Codex model overrides are preserved;
- Codex effort accepts supported configured values;
- invalid types/empty models fail with field-specific errors;
- fallback remains disabled;
- setup renders the effective configuration, not stale defaults.

### 12.2 Installer tests

- Claude-only, Codex-only, and combined installation;
- compiler artifacts installed at canonical host paths;
- repeated installation is byte-idempotent;
- managed stale artifacts update;
- unmarked conflicting artifacts are preserved;
- unrelated agents, skills, hooks, and config remain unchanged;
- Claude artifacts exclude Codex model/tool assumptions;
- Codex artifacts exclude Claude model/`ToolSearch` assumptions;
- current Codex config/hook regression suite remains green.

### 12.3 Publication tests

- canonical path derivation and traversal rejection;
- expected source digest success and mismatch rejection;
- valid candidate atomic publication;
- invalid header, range, braces, duplicate blocks, and stub rejection;
- existing SAN preservation for every failure stage;
- process-unique temporary file cleanup;
- concurrent stale publisher rejection;
- hash/index update only after accepted publication;
- provider/model metadata recorded without content.

### 12.4 Freshness tests

- real `recompile_san(dry_run=True)` filesystem immutability;
- no `.san/` creation during dry run;
- no orphan/hash/index/metric mutation during dry run;
- mutating housekeeping remains correct under `dry_run=False`;
- missing, stale, orphaned, unsupported, and malformed states are reported
  distinctly.

### 12.5 Adapter conformance

Static fixture candidates from Claude and Codex must pass the same publication
contract and represent the same source inventory. Fixtures explicitly compare
function/class/interface counts, verbatim signatures and identifiers, public
surfaces, dependencies, and required `@errors` facts. Normal CI does not call
live providers.

Optional live smoke tests may be run explicitly on hosts where that provider is
already authenticated. A Claude smoke never invokes Codex; a Codex smoke never
invokes Claude. Live smoke failure must not block unrelated host installation.

### 12.6 Required validation commands

```bash
python3 -m unittest tests.test_codex_setup -v
python3 brain/server.py validate-san
python3 brain/server.py validate
```

Additional focused test modules introduced by implementation must run before the
full validation commands.

## 13. Rollout

1. Add canonical compiler contract and configuration parser.
2. Add deterministic candidate validator and atomic `publish_san` tool.
3. Make SAN freshness dry-run truly non-mutating.
4. Add Claude managed adapter installation.
5. Add Codex managed custom-agent and skill installation.
6. Extend diagnostics and documentation.
7. Run static adapter conformance, focused tests, full Agent Brain validation,
   and optional per-host live smoke.
8. Rerun `setup.sh --all` to refresh installed Agent Brain artifacts.
9. Restart Claude Code and Codex so managed agents/skills reload.
10. Refresh Jobfill SAN in a later explicit operation using the active Codex
    compiler. Keep Jobfill `.san/` ignored and untracked.

Existing SAN files remain valid. No bulk regeneration is performed as part of
installing this feature.

## 14. Acceptance Criteria

1. A Claude SAN request uses the managed Claude compiler agent and configured
   Claude model only.
2. A Codex SAN request uses the managed Codex compiler custom agent/skill and
   configured Codex model/effort only.
3. Defaults are `claude-sonnet-4-6` and `gpt-5.4-mini` with Codex `medium`
   effort.
4. Users can deliberately override model and Codex effort in Agent Brain
   configuration.
5. No automatic model, effort, or provider fallback exists.
6. The Agent Brain server never launches an LLM or provider CLI.
7. Invalid or stale candidates never replace the previous SAN.
8. Accepted candidates are source-digest-bound, structurally validated,
   atomically written, hashed, and indexed; semantic quality is enforced by the
   compiler contract and provider conformance fixtures.
9. `recompile_san(dry_run=True)` changes no filesystem or metrics state.
10. Setup is idempotent and preserves unrelated/unmanaged host files.
11. Diagnostics expose installed adapter and effective model configuration.
12. CI verifies both adapters without requiring provider credentials.
13. Implementation changes are confined to Agent Brain; Jobfill source and
    tracked configuration remain unchanged.
14. Later Jobfill SAN regeneration uses Codex rather than Claude when invoked
    from Codex.

## 15. Anticipated Implementation Surface

The implementation plan may refine names while preserving this design. Expected
surface:

```text
san/compiler-contract.md
san/adapters/claude/brain-compiler.md
san/adapters/codex/brain-compiler.toml
san/adapters/codex/brain-compiler/SKILL.md
brain/compiler_config.py
brain/codex_setup.py
brain/server.py
brain/config.example.json
setup.sh
README.md
docs/adapters.md
tests/test_compiler_config.py
tests/test_codex_setup.py
tests/test_san_publish.py
tests/test_san_freshness.py
```

No file under `/Users/sandeepdhami/Documents/GitHub/Jobfill` is part of this
implementation surface.
