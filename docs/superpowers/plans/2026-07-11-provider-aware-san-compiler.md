# Provider-Aware SAN Compiler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Agent Brain generate and publish SAN through the active Claude or Codex host, using fixed configurable low-cost models, without cross-provider calls or target-project pollution.

**Architecture:** Keep generation inside managed host adapters. Agent Brain owns one provider-neutral compiler contract, strict configuration, read-only refresh planning, structural validation, digest-bound atomic publication, and diagnostics. The MCP server never launches an LLM or provider CLI.

**Tech Stack:** Python 3.10+, FastMCP, `unittest`, Bash, JSON, TOML, Claude Code custom agents, Codex custom agents and skills.

## Global Constraints

- Implement only in `/Users/sandeepdhami/Documents/GitHub/agent-brain`.
- Do not edit, stage, commit, or regenerate anything in `/Users/sandeepdhami/Documents/GitHub/Jobfill`.
- Preserve pre-existing dirty work in both repositories.
- Follow Agent Brain protocol before each implementation task: `get_roadmap`, `pre_check`, `log_decision`; call `log_outcome` after verification/review.
- Use TDD: add the focused failing test, run it and confirm the expected failure, implement the minimum behavior, rerun focused tests, then commit.
- Defaults are `claude-sonnet-4-6` and `gpt-5.4-mini` with Codex `medium` reasoning.
- Never auto-select another model, provider, or reasoning tier. `allow_expensive_fallback` remains `false`.
- Never store provider credentials, source content, or SAN content in configuration, decisions, outcomes, metrics, or diagnostics.
- The server may validate configured provider/model metadata but must never authenticate or invoke a provider.
- `recompile_san(repo, dry_run=True)` and `plan_san_refresh(repo)` must make zero filesystem or metrics changes.
- A failed publication must retain the prior SAN, hashes, index, and publication metrics exactly.
- Provider adapter installation is managed and atomic. Existing unmarked files at managed paths are conflicts and must remain byte-identical.
- Normal tests use fixtures only; no provider authentication or live model call.
- Approved design is the source of truth: `docs/superpowers/specs/2026-07-11-provider-aware-san-compiler-design.md`.

---

## Task 1: Add strict compiler configuration

**Files:**

- Create: `brain/compiler_config.py`
- Create: `tests/test_compiler_config.py`
- Modify: `brain/config.example.json`

- [ ] **Step 1: Add failing configuration tests**

Create `tests/test_compiler_config.py` with these cases:

```python
import json
import tempfile
import unittest
from pathlib import Path

from brain.compiler_config import (
    CompilerConfigError,
    load_san_compiler_config,
    parse_san_compiler_config,
)


class CompilerConfigTests(unittest.TestCase):
    def test_missing_section_uses_release_defaults(self):
        cfg = parse_san_compiler_config({})
        self.assertEqual(cfg.claude.model, "claude-sonnet-4-6")
        self.assertEqual(cfg.codex.model, "gpt-5.4-mini")
        self.assertEqual(cfg.codex.reasoning_effort, "medium")
        self.assertFalse(cfg.allow_expensive_fallback)

    def test_valid_provider_overrides_are_preserved(self):
        cfg = parse_san_compiler_config({
            "san_compiler": {
                "claude": {"model": "claude-custom"},
                "codex": {"model": "gpt-custom", "reasoning_effort": "low"},
                "allow_expensive_fallback": False,
            }
        })
        self.assertEqual(cfg.claude.model, "claude-custom")
        self.assertEqual(cfg.codex.model, "gpt-custom")
        self.assertEqual(cfg.codex.reasoning_effort, "low")

    def test_supported_codex_efforts_are_accepted(self):
        for effort in ("none", "low", "medium", "high", "xhigh"):
            with self.subTest(effort=effort):
                cfg = parse_san_compiler_config({
                    "san_compiler": {"codex": {"reasoning_effort": effort}}
                })
                self.assertEqual(cfg.codex.reasoning_effort, effort)

    def test_invalid_fields_have_exact_paths(self):
        bad = (
            ({"san_compiler": {"claude": {"model": " "}}},
             "san_compiler.claude.model: expected non-empty string"),
            ({"san_compiler": {"codex": {"model": 7}}},
             "san_compiler.codex.model: expected non-empty string"),
            ({"san_compiler": {"codex": {"reasoning_effort": "ultra"}}},
             "san_compiler.codex.reasoning_effort: expected one of"),
            ({"san_compiler": {"allow_expensive_fallback": True}},
             "san_compiler.allow_expensive_fallback: must be false"),
        )
        for payload, message in bad:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(CompilerConfigError, message):
                    parse_san_compiler_config(payload)

    def test_file_loader_rejects_invalid_json_and_non_object_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{")
            with self.assertRaisesRegex(CompilerConfigError, "config.json: invalid JSON"):
                load_san_compiler_config(path)
            path.write_text(json.dumps([]))
            with self.assertRaisesRegex(CompilerConfigError, "config.json: expected JSON object"):
                load_san_compiler_config(path)
```

- [ ] **Step 2: Run the test and confirm RED**

Run:

```bash
python3 -m unittest tests.test_compiler_config -v
```

Expected: import failure because `brain/compiler_config.py` does not exist.

- [ ] **Step 3: Implement the typed parser**

Create these public interfaces in `brain/compiler_config.py`:

```text
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

CodexReasoningEffort = Literal["none", "low", "medium", "high", "xhigh"]

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_CODEX_MODEL = "gpt-5.4-mini"
DEFAULT_CODEX_REASONING_EFFORT: CodexReasoningEffort = "medium"
SUPPORTED_CODEX_REASONING_EFFORTS = frozenset(
    {"none", "low", "medium", "high", "xhigh"}
)

@dataclass(frozen=True)
class ClaudeCompilerConfig:
    model: str

@dataclass(frozen=True)
class CodexCompilerConfig:
    model: str
    reasoning_effort: CodexReasoningEffort

@dataclass(frozen=True)
class SanCompilerConfig:
    claude: ClaudeCompilerConfig
    codex: CodexCompilerConfig
    allow_expensive_fallback: bool

class CompilerConfigError(ValueError):
    def __init__(self, field: str, detail: str):
        self.field = field
        super().__init__(f"{field}: {detail}")

parse_san_compiler_config(root: Mapping[str, object]) -> SanCompilerConfig
load_san_compiler_config(path: str | Path) -> SanCompilerConfig
```

Parsing rules:

- Missing file or missing `san_compiler` section uses release defaults.
- Every present container must be a JSON object.
- Model values must be non-empty strings after trimming.
- Codex effort must be one of the five declared values.
- `allow_expensive_fallback` may be absent or exactly `false`; reject `true` and non-booleans.
- File-loader errors must identify `config.json` without exposing unrelated file contents.

Add the approved `san_compiler` block to `brain/config.example.json`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
python3 -m unittest tests.test_compiler_config -v
python3 -m json.tool brain/config.example.json >/dev/null
```

Expected: all tests pass; example JSON validates.

- [ ] **Step 5: Commit**

```bash
git add brain/compiler_config.py brain/config.example.json tests/test_compiler_config.py
git commit -m "feat: add strict SAN compiler configuration"
```

---

## Task 2: Create the canonical contract and provider adapters

**Files:**

- Create: `san/compiler-contract.md`
- Create: `san/adapters/claude/brain-compiler.md`
- Create: `san/adapters/codex/brain-compiler.toml`
- Create: `san/adapters/codex/brain-compiler/SKILL.md`
- Create: `tests/fixtures/san/compiler_sample.py`
- Create: `tests/fixtures/san/claude/compiler_sample.py.san`
- Create: `tests/fixtures/san/codex/compiler_sample.py.san`
- Create: `tests/fixtures/san/compiler_sample.inventory.json`
- Create: `tests/test_compiler_adapters.py`
- Delete: `san/brain-compiler.md`
- Modify: `san/README.md`
- Modify: `brain/pyproject.toml`

- [ ] **Step 1: Add failing static-conformance tests**

Create `tests/test_compiler_adapters.py`. It must:

- parse the Codex adapter template after replacing model, effort, skill-path, and contract-path placeholders;
- assert `name`, `description`, `developer_instructions`, `model`, `model_reasoning_effort`, and `skills.config` exist;
- assert Claude content has no `gpt-`, `codex exec`, or `model_reasoning_effort`;
- assert Codex content has no `claude-sonnet`, `ToolSearch`, or Claude CLI instruction;
- assert both adapters direct the compiler to `plan_san_refresh` and `publish_san`;
- assert both fixture SANs contain the inventory's exact names/signatures, dependency facts, public surfaces, and required error facts;
- assert both fixtures have the same block inventory.

Representative Codex assertion:

```python
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

rendered = template.replace("{{CODEX_MODEL}}", "gpt-5.4-mini") \
    .replace("{{CODEX_REASONING_EFFORT}}", "medium") \
    .replace("{{CODEX_SKILL_PATH}}", "/tmp/brain-compiler/SKILL.md") \
    .replace("{{CONTRACT_PATH}}", "/tmp/compiler-contract.md")
parsed = tomllib.loads(rendered)
self.assertEqual(parsed["model"], "gpt-5.4-mini")
self.assertEqual(parsed["model_reasoning_effort"], "medium")
self.assertEqual(
    parsed["skills"]["config"][0]["path"],
    "/tmp/brain-compiler/SKILL.md",
)
```

Add `tomli>=2.0.1; python_version < '3.11'` to `brain/pyproject.toml` so the test and any diagnostic parser remain compatible with the repository's declared Python 3.10 minimum.

- [ ] **Step 2: Run the test and confirm RED**

```bash
python3 -m unittest tests.test_compiler_adapters -v
```

Expected: missing canonical/adaptor/fixture files.

- [ ] **Step 3: Extract the provider-neutral contract**

Move SAN semantics out of the old Claude template into `san/compiler-contract.md`. Required sections:

1. supported source extensions and skipped directories;
2. canonical append output path (`src/A.py` -> `.san/src/A.py.san`);
3. SAN v2 block/header and `src: start-end` grammar;
4. canonical field ordering and source-order preservation;
5. exact identifiers/signatures and public-surface inventory;
6. dependencies, state, errors, constraints, threading, patterns, and risks;
7. compiler-side semantic parity checks;
8. `get_roadmap` / `pre_check` / decision / outcome boundaries;
9. read-only planning and per-file `publish_san` protocol;
10. retryable vs terminal failures;
11. privacy: never log source or SAN contents.

The contract must contain no provider model, CLI, frontmatter, or provider-specific tool bootstrap.

- [ ] **Step 4: Add thin host adapters**

`san/adapters/claude/brain-compiler.md`:

```markdown
---
name: brain-compiler
description: Generate or refresh SAN through the current Claude Code host.
model: {{CLAUDE_MODEL}}
---
<!-- agent-brain-managed:san-compiler provider=claude artifact=agent version=1 -->
```

Its body must read the installed `{{CONTRACT_PATH}}`, use inherited Agent Brain MCP tools, stop if its configured model is unavailable, and prohibit Codex/OpenAI/provider CLI invocation. Do not add a restrictive `tools:` field.

`san/adapters/codex/brain-compiler.toml` must render this shape:

```toml
# agent-brain-managed:san-compiler provider=codex artifact=agent version=1
name = "brain-compiler"
description = "Generate or refresh SAN through the current Codex host."
model = "{{CODEX_MODEL}}"
model_reasoning_effort = "{{CODEX_REASONING_EFFORT}}"
developer_instructions = """
Use the installed brain-compiler skill and canonical contract.
Never invoke Claude, another provider, or codex exec.
"""

[[skills.config]]
path = "{{CODEX_SKILL_PATH}}"
enabled = true
```

`san/adapters/codex/brain-compiler/SKILL.md` must contain a valid skill header, read `{{CONTRACT_PATH}}` completely, use the Agent Brain MCP tools, and prohibit Claude/provider subprocesses.

Delete the old `san/brain-compiler.md`; update `san/README.md` to identify the contract and two adapters.

- [ ] **Step 5: Add provider fixture outputs**

Use one small Python source containing:

- one public class;
- constructor and public method signatures;
- one imported dependency;
- one state field;
- one explicit raised exception.

Record the expected inventory in JSON. Claude and Codex fixture SANs may differ in whitespace only; both must preserve the same canonical facts. No live generation.

- [ ] **Step 6: Verify GREEN and provider isolation**

```bash
python3 -m unittest tests.test_compiler_adapters -v
rg -n "claude-sonnet|ToolSearch|claude[[:space:]]" san/adapters/codex && exit 1 || true
rg -n "gpt-|codex exec|model_reasoning_effort" san/adapters/claude && exit 1 || true
```

Expected: tests pass; both isolation searches return no matches.

- [ ] **Step 7: Commit**

```bash
git add san brain/pyproject.toml tests/fixtures/san tests/test_compiler_adapters.py
git commit -m "feat: add provider-neutral SAN compiler contract"
```

---

## Task 3: Add atomic managed-adapter installation

**Files:**

- Create: `brain/compiler_setup.py`
- Create: `tests/test_compiler_setup.py`

- [ ] **Step 1: Add failing managed-file tests**

Test these behaviors in `tests/test_compiler_setup.py`:

- `ManagedArtifactTests.test_missing_artifact_is_created_atomically`
- `ManagedArtifactTests.test_current_artifact_is_byte_and_mtime_idempotent`
- `ManagedArtifactTests.test_stale_managed_artifact_is_updated`
- `ManagedArtifactTests.test_unmarked_conflict_is_preserved`
- `ManagedArtifactTests.test_replace_failure_preserves_previous_bytes_and_cleans_temp`
- `ProviderInstallTests.test_claude_install_creates_only_claude_agent`
- `ProviderInstallTests.test_codex_install_creates_only_codex_agent_and_skill`
- `ProviderInstallTests.test_rendered_adapters_use_effective_overrides`
- `ProviderInstallTests.test_invalid_config_preserves_last_valid_artifacts`
- `ProviderInstallTests.test_unrelated_agents_and_skills_are_unchanged`

The idempotency test must compare bytes and `st_mtime_ns`. The conflict and injected `os.replace` failure tests must compare exact prior bytes and assert no `*.tmp-*` remains.

- [ ] **Step 2: Run the test and confirm RED**

```bash
python3 -m unittest tests.test_compiler_setup -v
```

Expected: import failure because `brain/compiler_setup.py` does not exist.

- [ ] **Step 3: Implement managed artifact primitives**

Create these interfaces:

```text
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ManagedState = Literal["missing", "current", "stale", "conflict"]
Provider = Literal["claude", "codex"]
ADAPTER_VERSION = 1
MANAGED_MARKER = "agent-brain-managed:san-compiler"

@dataclass(frozen=True)
class ManagedArtifactStatus:
    path: Path
    state: ManagedState
    expected_version: int
    installed_version: int | None

@dataclass(frozen=True)
class InstallResult:
    path: Path
    previous_state: ManagedState
    changed: bool

class ManagedArtifactConflict(RuntimeError):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"{path}: unmanaged SAN compiler artifact; preserved")

inspect_managed_artifact(path: str | Path, expected_content: str) -> ManagedArtifactStatus
install_managed_artifact(path: str | Path, rendered_content: str) -> InstallResult
render_claude_adapter(config: SanCompilerConfig, contract_path: Path, template: str) -> str
render_codex_agent(config: SanCompilerConfig, skill_path: Path, contract_path: Path, template: str) -> str
render_codex_skill(contract_path: Path, template: str) -> str
install_claude_adapter(*, claude_home: str | Path, config: SanCompilerConfig, assets_root: str | Path) -> InstallResult
install_codex_adapters(*, codex_home: str | Path, config: SanCompilerConfig, assets_root: str | Path) -> tuple[InstallResult, InstallResult]
```

Atomic write requirements:

- create parents only after config and rendering validate;
- use a same-directory `tempfile.NamedTemporaryFile(delete=False)`;
- flush and `os.fsync` before `os.replace`;
- clean the temporary file in `finally`;
- do not rewrite current bytes;
- treat only files containing `MANAGED_MARKER` as owned.

Use package/standalone import compatibility because setup will copy the module into `~/.agent-brain`:

```python
try:
    from .compiler_config import SanCompilerConfig, load_san_compiler_config
except ImportError:
    from compiler_config import SanCompilerConfig, load_san_compiler_config
```

- [ ] **Step 4: Verify GREEN**

```bash
python3 -m unittest tests.test_compiler_config tests.test_compiler_setup -v
```

- [ ] **Step 5: Commit**

```bash
git add brain/compiler_setup.py tests/test_compiler_setup.py
git commit -m "feat: add managed SAN compiler installer"
```

---

## Task 4: Wire the Claude setup path

**Files:**

- Modify: `brain/compiler_setup.py`
- Modify: `setup.sh`
- Modify: `tests/test_compiler_setup.py`

- [ ] **Step 1: Add failing CLI and Claude-only tests**

Add tests for:

- `compiler_setup.main()` with the complete `install-claude`, config, home, and assets arguments writes `~/.claude/agents/brain-compiler.md`;
- configured Claude override is rendered;
- invalid config exits before changing the last valid adapter;
- Claude-only installation creates no Codex artifact;
- an unmarked Claude file is preserved and produces a clear non-zero conflict.

- [ ] **Step 2: Confirm RED**

```bash
python3 -m unittest tests.test_compiler_setup.ProviderInstallTests -v
```

Expected: CLI entry point or `install-claude` command missing.

- [ ] **Step 3: Add the setup CLI**

Add:

```text
compiler_setup.py install-claude \
  --config <config.json> \
  --claude-home <~/.claude> \
  --assets-root <installed-or-source-root>
```

Load and validate config before rendering or writing anything. Print one concise `created`, `updated`, or `current` line. On conflict, print the preserved path and return non-zero.

- [ ] **Step 4: Copy compiler runtime/assets during Agent Brain setup**

In the existing Agent Brain copy stage, add:

```bash
cp "$SCRIPT_DIR/brain/compiler_config.py" "$BRAIN_DIR/compiler_config.py"
cp "$SCRIPT_DIR/brain/compiler_setup.py" "$BRAIN_DIR/compiler_setup.py"
mkdir -p "$BRAIN_DIR/san"
cp "$SCRIPT_DIR/san/compiler-contract.md" "$BRAIN_DIR/san/compiler-contract.md"
mkdir -p "$BRAIN_DIR/san/adapters/claude"
mkdir -p "$BRAIN_DIR/san/adapters/codex/brain-compiler"
cp "$SCRIPT_DIR/san/adapters/claude/brain-compiler.md" \
  "$BRAIN_DIR/san/adapters/claude/brain-compiler.md"
cp "$SCRIPT_DIR/san/adapters/codex/brain-compiler.toml" \
  "$BRAIN_DIR/san/adapters/codex/brain-compiler.toml"
cp "$SCRIPT_DIR/san/adapters/codex/brain-compiler/SKILL.md" \
  "$BRAIN_DIR/san/adapters/codex/brain-compiler/SKILL.md"
```

After config creation, and only when `INSTALL_CLAUDE=1`, run:

```bash
"$PYBIN" "$BRAIN_DIR/compiler_setup.py" install-claude \
  --config "$BRAIN_DIR/config.json" \
  --claude-home "$HOME/.claude" \
  --assets-root "$BRAIN_DIR"
```

This managed compiler step must remain separate from the interactive generic role-template skip/overwrite/manual prompt.

- [ ] **Step 5: Verify**

```bash
python3 -m unittest tests.test_compiler_config tests.test_compiler_setup -v
bash -n setup.sh
```

- [ ] **Step 6: Commit**

```bash
git add brain/compiler_setup.py setup.sh tests/test_compiler_setup.py
git commit -m "feat: install managed Claude SAN compiler"
```

---

## Task 5: Wire the Codex setup path

**Files:**

- Modify: `brain/codex_setup.py`
- Modify: `setup.sh`
- Modify: `tests/test_codex_setup.py`

- [ ] **Step 1: Add failing Codex installation tests**

Extend `tests/test_codex_setup.py` with:

- `test_install_user_installs_managed_codex_agent_and_skill`
- `test_install_user_uses_effective_model_and_effort`
- `test_install_user_is_byte_idempotent`
- `test_install_user_preserves_unmarked_compiler_conflict`
- `test_install_user_preserves_unrelated_config_hooks_agents_and_skills`

Keep all four existing Codex config/hook/project tests unchanged and green.

- [ ] **Step 2: Confirm RED**

```bash
python3 -m unittest tests.test_codex_setup -v
```

Expected: `install_user` lacks compiler configuration/assets parameters or does not create adapters.

- [ ] **Step 3: Extend `install_user`**

Use this signature:

```python
def install_user(
    codex_home: str | Path,
    pybin: str,
    server_py: str,
    hooks_dir: str,
    brain_config: str | Path,
    assets_root: str | Path,
) -> None:
    config = load_san_compiler_config(brain_config)
    # Only after validation: preserve existing MCP/hook behavior.
    ensure_codex_config(Path(codex_home) / "config.toml", pybin, server_py)
    ensure_codex_hooks(Path(codex_home) / "hooks.json", pybin, hooks_dir)
    install_codex_adapters(
        codex_home=codex_home,
        config=config,
        assets_root=assets_root,
    )
```

Add `--brain-config` and `--assets-root` to the `install-user` CLI. Use the same package/standalone import fallback as `compiler_setup.py`.

- [ ] **Step 4: Pass installed paths from `setup.sh`**

Add:

```bash
--brain-config "$BRAIN_DIR/config.json" \
--assets-root "$BRAIN_DIR"
```

Codex-only setup must not call `install-claude`; Claude-only setup must not call `codex_setup.py install-user`.

- [ ] **Step 5: Verify**

```bash
python3 -m unittest tests.test_codex_setup tests.test_compiler_setup -v
bash -n setup.sh
```

- [ ] **Step 6: Commit**

```bash
git add brain/codex_setup.py setup.sh tests/test_codex_setup.py
git commit -m "feat: install managed Codex SAN compiler"
```

---

## Task 6: Add structural validation and strict atomic SAN primitives

**Files:**

- Create: `brain/san_publish.py`
- Create: `tests/test_san_publish.py`
- Modify: `brain/server.py`
- Modify: `setup.sh`

- [ ] **Step 1: Add failing validator tests**

Create `SanCandidateValidationTests` covering:

- `test_accepts_valid_multi_block_candidate`
- `test_rejects_empty_candidate`
- `test_rejects_invalid_header_and_text_outside_blocks`
- `test_requires_src_as_first_block_line`
- `test_rejects_zero_reversed_and_past_eof_ranges`
- `test_rejects_stray_nested_and_unclosed_braces`
- `test_rejects_duplicate_qualified_name_kind`
- `test_rejects_placeholder_markers`
- `test_enforces_byte_and_block_limits`

The valid-result test must assert no source/SAN content is returned:

```python
result = validate_san_candidate(
    "pkg.Auth @svc {\n  src: 1-3\n  purpose: auth\n}\n",
    source_line_count=3,
)
self.assertTrue(result["valid"])
self.assertEqual(result["block_count"], 1)
self.assertNotIn("purpose: auth", json.dumps(result))
```

- [ ] **Step 2: Add failing atomic/index tests**

Create `SanAtomicPrimitiveTests` covering:

- exact atomic replacement and cleanup;
- injected `os.replace` failure preserves destination;
- temporary names are process-unique;
- `_rebuild_san_index(san_dir, repo_path=repo, strict=True)` propagates scan/write failure;
- non-strict index behavior remains best-effort for existing callers.

- [ ] **Step 3: Confirm RED**

```bash
python3 -m unittest tests.test_san_publish.SanCandidateValidationTests -v
python3 -m unittest tests.test_san_publish.SanAtomicPrimitiveTests -v
```

Expected: missing `brain.san_publish` and strict index API.

- [ ] **Step 4: Implement the line-state validator**

In `brain/san_publish.py` define:

```text
SAN_HEADER_RE = re.compile(r"^(\S+)\s+@(\w+)\s*\{$")
SAN_SRC_RE = re.compile(r"^  src: ([1-9]\d*)-([1-9]\d*)$")
SAN_PLACEHOLDER_RE = re.compile(
    r"(?im)^\s*(?:TODO|TBD|PLACEHOLDER|STUB)(?:\s*[:\-].*)?$|"
    r"\{\{[^{}\n]+\}\}|<\s*(?:placeholder|stub|fill[-_ ]?me)\s*>"
)
SAN_MAX_CANDIDATE_BYTES = 1_048_576
SAN_MAX_BLOCKS = 2_000

validate_san_candidate(san_content: str, source_line_count: int) -> dict[str, object]
```

Return only `valid`, structured `errors`, `block_count`, byte count, and block metadata (`qualified_name`, `kind`, `src_start`, `src_end`). Use these exact error codes:

```text
empty_candidate candidate_too_large placeholder_marker text_outside_block
invalid_header nested_block stray_closing_brace unclosed_block missing_src
invalid_src_range duplicate_block too_many_blocks
```

Headers and closing braces are column-zero. The first content line in every block is exactly `  src: start-end`. Reject duplicate `(qualified_name, kind)`.
The placeholder rule must target unfinished compiler/template output only; it must not reject a legitimate fact such as `risk: source contains TODO` or a language-level ellipsis.

- [ ] **Step 5: Implement reusable atomic file operations**

Add:

```text
atomic_write_bytes(path: Path, data: bytes) -> None
snapshot_file(path: Path) -> bytes | None
restore_file(path: Path, snapshot: bytes | None) -> None
def canonical_san_path(san_dir: Path, source_rel: str) -> Path:
    return san_dir / f"{source_rel}.san"
```

Temporary name:

```python
path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
```

Flush, `fsync`, `os.replace`, and cleanup. Preserve `None` as “file absent before transaction.”

- [ ] **Step 6: Split strict index build/write in `server.py`**

Refactor without changing index schema:

```text
_build_san_index(san_dir: Path, repo_path: Path | None = None) -> dict[str, dict]

def _rebuild_san_index(
    san_dir: Path,
    repo_path: Path | None = None,
    *,
    strict: bool = False,
) -> dict[str, dict]
```

`strict=True` propagates read/write errors. `strict=False` preserves existing best-effort behavior. Replace fixed `.tmp` writes in `_save_san_hashes` and index persistence with `atomic_write_bytes`.

Add package/standalone imports for `san_publish.py`. Update `setup.sh` to copy `brain/san_publish.py` into `$BRAIN_DIR`.

- [ ] **Step 7: Verify**

```bash
python3 -m unittest tests.test_san_publish tests.test_san_skip_dirs -v
bash -n setup.sh
```

- [ ] **Step 8: Commit**

```bash
git add brain/san_publish.py brain/server.py setup.sh tests/test_san_publish.py
git commit -m "feat: validate SAN candidates structurally"
```

---

## Task 7: Make freshness planning genuinely read-only

**Files:**

- Create: `tests/test_san_freshness.py`
- Modify: `brain/server.py`

- [ ] **Step 1: Add failing immutability and classification tests**

Use a `snapshot_tree` helper that records every relative path, bytes, and `st_mtime_ns`. Cover:

- `test_dry_run_does_not_create_san_directory`
- `test_dry_run_preserves_orphan_hash_index_metrics_and_mtimes`
- `test_plan_reports_all_states_distinctly_with_digests`
- `test_hash_match_is_fresh_without_touching_san`
- `test_hash_mismatch_is_stale_even_when_san_mtime_is_newer`
- `test_invalid_san_is_reported_malformed`
- `test_non_dry_run_still_removes_orphans_and_rebuilds_metadata`

Patch `_resolve_repo_path` to a temporary repo. Snapshot `METRICS_FILE` separately. The first two tests must compare exact before/after snapshots.

- [ ] **Step 2: Confirm RED and existing mutation**

```bash
python3 -m unittest tests.test_san_freshness -v
```

Expected: dry-run tests fail because current `check_san_freshness` invokes mutating `_ensure_san_fresh`.

- [ ] **Step 3: Implement a pure scanner**

Add:

```text
def _scan_san_freshness(repo: str) -> dict[str, object]:
    """Filesystem reads only. Never mkdir/write/touch/unlink/log."""

@mcp.tool()
def plan_san_refresh(repo: str) -> dict[str, object]:
    return _scan_san_freshness(repo)

_format_san_freshness(plan: dict[str, object]) -> str

def check_san_freshness(repo: str) -> str:
    return _format_san_freshness(_scan_san_freshness(repo))
```

Return this stable schema:

```python
{
    "status": "ok",
    "repo": "demo",
    "counts": {
        "missing": 1, "stale": 1, "fresh": 1,
        "orphaned": 1, "unsupported": 1, "malformed": 1,
    },
    "missing": [{"source_path": "src/Missing.py", "source_sha256": "2d711642b726b04401627ca9fbac32f5da7e5c8530fb1903cc4db02258717921"}],
    "stale": [{"source_path": "src/Stale.py", "source_sha256": "2d711642b726b04401627ca9fbac32f5da7e5c8530fb1903cc4db02258717921"}],
    "fresh": [{"source_path": "src/Fresh.py", "source_sha256": "2d711642b726b04401627ca9fbac32f5da7e5c8530fb1903cc4db02258717921"}],
    "orphaned": [{"san_path": ".san/src/Gone.py.san"}],
    "unsupported": [{"source_path": "src/View.vue", "reason": "unsupported_extension"}],
    "malformed": [{"source_path": "src/Broken.py", "san_path": ".san/src/Broken.py.san", "errors": []}],
}
```

Classification rules:

- supported source without SAN -> missing;
- structurally invalid existing SAN -> malformed before fresh/stale;
- stored source digest equal -> fresh;
- stored source digest different -> stale, regardless of mtime;
- no stored digest -> compatibility mtime comparison, with no backfill/touch;
- SAN without source -> orphaned;
- source represented by SAN/hash metadata but unsupported by `SOURCE_EXTS` -> unsupported;
- do not label every non-code repository file unsupported;
- invalid repo -> structured `repo_not_found` error.

Do not call `_ensure_san_fresh`, `_refresh_san`, `_save_san_hashes`, `_record_san_gen`, or `_rebuild_san_index` from this path.

- [ ] **Step 4: Preserve the mutating path explicitly**

Keep:

```python
def recompile_san(repo: str, dry_run: bool = False) -> str:
    if dry_run:
        return check_san_freshness(repo)
    # Existing housekeeping continues here.
```

- [ ] **Step 5: Verify**

```bash
python3 -m unittest tests.test_san_freshness tests.test_san_skip_dirs -v
```

- [ ] **Step 6: Commit**

```bash
git add brain/server.py tests/test_san_freshness.py
git commit -m "fix: make SAN freshness dry-run read-only"
```

---

## Task 8: Add digest-bound transactional `publish_san`

**Files:**

- Modify: `brain/server.py`
- Modify: `tests/test_san_publish.py`

- [ ] **Step 1: Add failing path/digest/publication tests**

Create `PublishSanTests` covering:

- `test_rejects_absolute_traversal_and_symlink_escape`
- `test_rejects_unsupported_and_skipped_source`
- `test_rejects_invalid_or_mismatched_provider_model_effort`
- `test_rejects_changed_source_digest_and_preserves_state`
- `test_rejects_invalid_candidate_and_preserves_state`
- `test_publishes_to_canonical_append_path`
- `test_updates_hash_index_and_metric_after_replace`
- `test_write_failure_preserves_all_prior_state`
- `test_hash_failure_rolls_back_all_prior_state`
- `test_index_failure_rolls_back_all_prior_state`
- `test_metric_failure_rolls_back_all_prior_state`
- `test_first_publication_failure_removes_created_empty_directories`
- `test_removes_process_unique_temp_files_after_failure`
- `test_rechecks_digest_immediately_before_replace`
- `test_serializes_concurrent_publications`
- `test_result_and_metric_never_contain_source_or_san_content`

Every failure-stage test must snapshot SAN bytes, `.san_hashes.json`, `_index.json`, `METRICS_FILE`, and relevant directories before the call.

- [ ] **Step 2: Confirm RED**

```bash
python3 -m unittest tests.test_san_publish.PublishSanTests -v
```

Expected: `publish_san` missing.

- [ ] **Step 3: Add strict metric append and publication lock**

Refactor metrics without changing best-effort callers:

```text
_append_metric_strict(event: dict) -> None
_log_metric(event: dict) -> None  # calls strict append and preserves current best-effort exception handling
SAN_PUBLISH_LOCK = threading.RLock()
```

Use one repo-wide lock because hash and index files are shared across sources. Source-digest compare-and-swap plus process-unique temporary files remain the cross-process guard.

- [ ] **Step 4: Implement source/path/provider validation**

The internal publication function must:

- resolve only configured repositories;
- reject absolute paths, `..`, symlink escapes, directories, unsupported suffixes, and skipped directories;
- require a lowercase 64-character SHA-256 digest;
- re-read current `san_compiler` config using `parse_san_compiler_config(_load_config())`;
- accept only `provider in {"claude", "codex"}`;
- require model and Codex effort to exactly equal effective configuration;
- require absent/empty reasoning effort for Claude;
- reject before writing if source digest or candidate validation fails.

The server validates declared metadata only; it still does not select or invoke a model.

- [ ] **Step 5: Implement the MCP contract**

```text
@mcp.tool()
def publish_san(
    repo: str,
    source_path: str,
    expected_source_sha256: str,
    san_content: str,
    provider: str,
    model: str,
    reasoning_effort: str | None = None,
) -> dict[str, object]
```

Success schema:

```python
{
    "status": "published",
    "repo": repo,
    "source_path": source_path,
    "san_path": f".san/{source_path}.san",
    "source_sha256": expected_source_sha256,
    "provider": provider,
    "model": model,
    "reasoning_effort": reasoning_effort,
    "validation": {"valid": True, "block_count": 1, "bytes": 132, "errors": []},
}
```

Failure codes:

```text
repo_not_found invalid_source_path source_not_found unsupported_extension
skipped_source invalid_digest source_changed compiler_config_invalid
provider_mismatch model_mismatch reasoning_effort_mismatch invalid_candidate
publication_failed rollback_failed
```

Include `retryable` and validation summaries, never content.

- [ ] **Step 6: Implement the transaction and rollback**

Inside `SAN_PUBLISH_LOCK`:

1. validate repo/path/provider/config/digest/candidate;
2. derive canonical append destination; never use legacy `_source_to_san_path` for writes;
3. snapshot destination, hash file, index file, metrics file, and missing parent directories;
4. re-read/re-hash source immediately before replacement;
5. atomically replace SAN;
6. atomically persist copied hashes with the expected digest;
7. rebuild index with `strict=True`;
8. append strict `san_publish` metric with provider/model/effort and token counts;
9. clear `_SAN_FRESH_CHECKED[repo]` only after commit.

On any post-replacement failure, restore SAN/hash/index/metrics snapshots and remove only newly created empty directories. If rollback fails, return `rollback_failed`; never claim success.

Metric shape:

```python
{
    "kind": "san_publish",
    "repo": repo,
    "file": source_path,
    "provider": provider,
    "model": model,
    "reasoning_effort": reasoning_effort,
    "input_tokens": source_tokens,
    "output_tokens": san_tokens,
    "gen_cost": source_tokens + san_tokens,
}
```

- [ ] **Step 7: Verify focused and SAN regression suites**

```bash
python3 -m unittest tests.test_san_publish.PublishSanTests -v
python3 -m unittest \
  tests.test_san_publish \
  tests.test_san_freshness \
  tests.test_san_skip_dirs -v
```

- [ ] **Step 8: Commit**

```bash
git add brain/server.py tests/test_san_publish.py
git commit -m "feat: publish SAN atomically with source digest binding"
```

---

## Task 9: Add adapter diagnostics and full `.san/` ignore guidance

**Files:**

- Modify: `brain/compiler_setup.py`
- Modify: `brain/server.py`
- Modify: `setup.sh`
- Create: `tests/test_compiler_diagnostics.py`

- [ ] **Step 1: Add failing diagnostic tests**

Cover:

- `test_reports_effective_models_versions_and_currentness`
- `test_reports_missing_claude_adapter_with_exact_setup_command`
- `test_reports_unmarked_codex_conflict`
- `test_reports_stale_managed_artifact`
- `test_skips_undetected_host_and_never_invokes_provider`
- `test_invalid_compiler_config_fails_without_mutation`
- `test_missing_canonical_contract_fails`
- `test_project_diagnose_reports_missing_dot_san_ignore`

Patch home/Codex paths and host detection. Tests must not patch or invoke a provider CLI.

- [ ] **Step 2: Confirm RED**

```bash
python3 -m unittest tests.test_compiler_diagnostics -v
```

Expected: adapter diagnostic APIs/checks missing.

- [ ] **Step 3: Add artifact diagnostic records**

In `compiler_setup.py` add:

```text
@dataclass(frozen=True)
class CompilerArtifactDiagnostic:
    provider: Provider
    artifact: Literal["agent", "skill"]
    path: Path
    state: ManagedState
    model: str
    reasoning_effort: CodexReasoningEffort | None
    expected_version: int
    detail: str

def diagnose_compiler_artifacts(
    *, home, codex_home, config, assets_root,
    claude_detected: bool, codex_detected: bool,
) -> tuple[CompilerArtifactDiagnostic, ...]
```

Render expected bytes with the same functions used by installation. Report `current`, `stale`, `missing`, or `conflict` without writing.

- [ ] **Step 4: Extend `server.py diagnose`**

Use source-package/installed-standalone import fallback. Detection only:

```python
claude_detected = (home / ".claude").exists() or shutil.which("claude") is not None
codex_detected = codex_home.exists() or shutil.which("codex") is not None
```

Report:

```text
current; version=1; model=claude-sonnet-4-6
current; version=1; model=gpt-5.4-mini; reasoning_effort=medium
missing; run ./setup.sh --claude
missing; run ./setup.sh --codex
conflict: unmanaged file preserved at <path>
stale managed artifact; rerun the provider setup
canonical compiler contract missing at <path>
```

Do not test another provider and do not call a model.

- [ ] **Step 5: Enforce full SAN ignore guidance**

In `setup.sh --link-project`, replace granular SAN metadata entries with:

```text
.mcp.json
.san/
```

In project diagnostics, require/advise `.san/` as a whole. Accept an existing equivalent anchored form `/.san/`; do not edit a project during diagnosis.

- [ ] **Step 6: Verify**

```bash
python3 -m unittest tests.test_compiler_diagnostics tests.test_compiler_setup -v
bash -n setup.sh
```

- [ ] **Step 7: Commit**

```bash
git add brain/compiler_setup.py brain/server.py setup.sh tests/test_compiler_diagnostics.py
git commit -m "feat: diagnose SAN compiler adapters"
```

---

## Task 10: Update documentation and run release-level verification

**Files:**

- Modify: `README.md`
- Create: `docs/adapters.md`
- Modify: `brain/server.py` user-facing SAN messages where they reference the removed template
- Modify: tests only if documentation/message assertions require it

- [ ] **Step 1: Add documentation acceptance checks**

Before editing, run and retain output:

```bash
rg -n "san/brain-compiler\.md|Only the brain-compiler.*Claude|Use Sonnet" README.md san brain/server.py
```

Expected: old Claude-only instructions remain and must be removed.

- [ ] **Step 2: Document the provider-aware workflow**

Update README SAN sections and create `docs/adapters.md` with:

- architecture boundary: host generates, server plans/validates/publishes;
- `./setup.sh --claude`, `--codex`, and `--all` behavior;
- exact managed paths;
- config example and defaults;
- explicit override/re-run setup behavior;
- no automatic model/effort/provider fallback;
- `plan_san_refresh` -> active `brain-compiler` -> `publish_san` flow;
- dry-run immutability;
- digest mismatch retry behavior;
- managed-file conflict behavior;
- diagnostics commands;
- local ignored `.san/` policy;
- optional live smoke is host-specific and never required by CI.

Update server comments/messages to reference `san/compiler-contract.md` or “the active host's installed brain-compiler adapter,” not the removed Claude template.

- [ ] **Step 3: Run static and focused verification**

```bash
rg -n "san/brain-compiler\.md|Only the brain-compiler.*Claude|Use Sonnet" README.md san brain/server.py && exit 1 || true
python3 -m unittest \
  tests.test_compiler_config \
  tests.test_compiler_adapters \
  tests.test_compiler_setup \
  tests.test_compiler_diagnostics \
  tests.test_codex_setup \
  tests.test_san_publish \
  tests.test_san_freshness \
  tests.test_san_skip_dirs -v
bash -n setup.sh
```

- [ ] **Step 4: Run required Agent Brain validation**

Use the repository interpreter first; if dependencies are missing, repeat with `~/.agent-brain/.venv/bin/python`:

```bash
python3 brain/server.py validate-san
python3 brain/server.py validate
```

Expected: both report zero failures.

- [ ] **Step 5: Run isolated installer smoke tests**

In a temporary HOME/CODEX_HOME/config, invoke the compiler setup CLI and `codex_setup.py install-user`. Verify:

- Claude-only creates only the Claude agent;
- Codex-only creates only the Codex agent/skill;
- `--all` equivalent creates all three;
- second run changes no bytes or mtimes;
- generated TOML parses with `tomllib`;
- no real provider CLI runs.

Do not use Jobfill as the temporary target.

- [ ] **Step 6: Audit repository boundaries**

```bash
git status --short
git diff --check
git diff --name-only origin/main...HEAD
git -C /Users/sandeepdhami/Documents/GitHub/Jobfill status --short
```

Confirm every implementation diff is under AgentBrain. Report Jobfill's pre-existing dirty entries without modifying them.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md docs/adapters.md brain/server.py
git commit -m "docs: explain provider-aware SAN compilation"
```

- [ ] **Step 8: Request code review and address only verified findings**

Use `superpowers:requesting-code-review`. Review against all 14 acceptance criteria in the approved spec. If code changes result, rerun the focused module plus full validation and commit the fixes separately.

- [ ] **Step 9: Refresh installed Agent Brain adapters**

After all tests pass, run the provider setup through `./setup.sh --all`. Preserve any unmarked conflict exactly and report its path. Do not regenerate Jobfill SAN in this task. Claude Code and Codex need a new session/restart to load refreshed managed agents/skills.

---

## Final Acceptance Checklist

- [ ] Claude adapter uses only configured Claude model; default `claude-sonnet-4-6`.
- [ ] Codex adapter uses only configured Codex model/effort; defaults `gpt-5.4-mini` / `medium`.
- [ ] No automatic fallback in code, configuration, prompts, or errors.
- [ ] MCP server never invokes an LLM/provider CLI.
- [ ] Canonical contract is provider-neutral and installed.
- [ ] Both adapters pass static conformance fixtures without credentials.
- [ ] Setup is provider-isolated, managed, atomic, idempotent, and conflict-safe.
- [ ] `plan_san_refresh` returns source digests and distinct freshness states.
- [ ] `recompile_san(dry_run=True)` is byte/mtime/metrics immutable.
- [ ] `publish_san` rejects traversal, unsupported/skipped paths, config drift, invalid content, and stale digests.
- [ ] Publication atomically updates SAN/hash/index/metrics or restores all prior state.
- [ ] Diagnostics show host-specific adapter/currentness/model/effort and never probe another provider.
- [ ] Project guidance ignores all `.san/` output.
- [ ] AgentBrain focused tests, `validate-san`, and `validate` pass.
- [ ] Jobfill remains untouched; later SAN regeneration is a separate explicit operation.
